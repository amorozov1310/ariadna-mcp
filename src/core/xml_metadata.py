"""
Метаданные конфигурации из XML-выгрузки (Конфигуратор → «Выгрузить
конфигурацию в файлы»; формат EDT не поддерживается).

Результат — ConfigInfo и MetadataObject (metadata_model): русские виды
объектов в full_name, типы на языке конфигурации, как их показывает
Конфигуратор («СправочникСсылка.Склады»,
«Строка(25, Переменная)» или «CatalogRef.Users», «String(25, Variable)»).
Поэтому индексатор и поиск не различают, откуда пришли метаданные.

Берётся только то, что индексатор хранит: объекты верхнего уровня, их
реквизиты, измерения, ресурсы, табличные части, формы, значения
перечислений, флаги общих модулей, состав подсистем (с вложенными).
Команды, макеты, шаблоны URL и прочие дочерние элементы пропускаются.
"""

import html
import logging
import re
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .metadata_model import (
    OBJECT_TYPE_MAP, AttributeInfo, CommonModuleInfo, ConfigInfo, EnumValueInfo,
    FormInfo, MetadataObject, SubsystemInfo,
)
from .standard_attributes import OWNERS, RAW_PROPERTIES as STANDARD_PROPERTIES
from .xml_walker import IO_THREADS

logger = logging.getLogger('ariadna')

# Тег в Configuration.xml/ChildObjects (англ. ед. число) → (каталог выгрузки, рус. вид).
_KINDS: dict[str, tuple[str, str]] = {
    en_singular: (plural, ru_singular)
    for plural, (ru_singular, en_singular) in OBJECT_TYPE_MAP.items()
    if plural.isascii() and en_singular != 'Configuration'
}
# В Configuration.xml тег параметра функциональной опции — во множественном
# числе по первому слову, не так, как в таблице видов.
_KINDS['FunctionalOptionsParameter'] = _KINDS['FunctionalOptionParameter']

# Англ. вид → рус. вид для ссылок вида «Catalog.X» (состав подсистем), включая
# виды, которых индекс как объекты не хранит.
_KIND_RU = {en: ru for en, (_plural, ru) in _KINDS.items()} | {
    'FunctionalOptionsParameter': 'ПараметрФункциональныхОпций',
    'XDTOPackage': 'ПакетXDTO', 'StyleItem': 'ЭлементСтиля', 'IntegrationService': 'СервисИнтеграции',
    'Sequence': 'Последовательность', 'WSReference': 'WSСсылка', 'DocumentNumerator': 'НумераторДокументов',
    'Bot': 'Бот', 'WebSocketClient': 'WebSocketКлиент', 'PaletteColor': 'ЦветПалитры',
}
# Элемент состава подсистемы из другой конфигурации записан идентификатором
# (у заимствованной подсистемы расширения) — по нему объект не найти.
_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# Части имён типов cfg:<Вид><Суффикс>: англ. → рус.
_TYPE_KIND_RU = {
    'ChartOfCharacteristicTypes': 'ПланВидовХарактеристик',
    'ChartOfCalculationTypes': 'ПланВидовРасчета',
    'ChartOfAccounts': 'ПланСчетов',
    'ExchangePlan': 'ПланОбмена',
    'BusinessProcess': 'БизнесПроцесс',
    'InformationRegister': 'РегистрСведений',
    'AccumulationRegister': 'РегистрНакопления',
    'AccountingRegister': 'РегистрБухгалтерии',
    'CalculationRegister': 'РегистрРасчета',
    'DocumentJournal': 'ЖурналДокументов',
    'DataProcessor': 'Обработка',
    'Catalog': 'Справочник',
    'Document': 'Документ',
    'Enum': 'Перечисление',
    'Task': 'Задача',
    'Constant': 'Константа',
    'Report': 'Отчет',
    'Sequence': 'Последовательность',
    'Recalculation': 'Перерасчет',
}
_TYPE_SUFFIX_RU = {
    'Ref': 'Ссылка', 'Object': 'Объект', 'Manager': 'Менеджер', 'Selection': 'Выборка',
    'List': 'Список', 'RecordSet': 'НаборЗаписей', 'RecordManager': 'МенеджерЗаписи',
    'RecordKey': 'КлючЗаписи', 'ValueManager': 'МенеджерЗначения',
}
# Готовые имена, которые не складываются из вида и суффикса.
_TYPE_NAME_RU = {
    'DefinedType': 'ОпределяемыйТип',
    'Characteristic': 'Характеристика',
    'AnyRef': 'ЛюбаяСсылка',
    'AnyIBRef': 'ЛюбаяСсылка',
    'BusinessProcessRoutePointRef': 'ТочкаМаршрутаБизнесПроцессаСсылка',
    'ConstantsSet': 'НаборКонстант',
    'ReportBuilder': 'ПостроительОтчета',
    'ValueStorage': 'ХранилищеЗначения',
    'UUID': 'УникальныйИдентификатор',
    'FixedStructure': 'ФиксированнаяСтруктура',
    'FixedMap': 'ФиксированноеСоответствие',
    'FixedArray': 'ФиксированныйМассив',
    'ValueListType': 'СписокЗначений',
    'ValueTree': 'ДеревоЗначений',
    'ValueTable': 'ТаблицаЗначений',
    'StandardPeriod': 'СтандартныйПериод',
    'StandardBeginningDate': 'СтандартнаяДатаНачала',
    'SpreadsheetDocument': 'ТабличныйДокумент',
    'DataCompositionSettingsComposer': 'КомпоновщикНастроекКомпоновкиДанных',
    'Null': 'Null',
}
_TYPE_NAME_EN = {'ValueListType': 'ValueList', 'AnyIBRef': 'AnyRef'}

# Перечислимые значения свойств — по-русски для конфигурации на русском.
_VALUE_RU = {
    'ShowError': 'ВыдаватьОшибку', 'DontCheck': 'НеПроверять',
    'ForItem': 'ДляЭлемента', 'ForFolder': 'ДляГруппы', 'ForFolderAndItem': 'ДляГруппыИЭлемента',
    'DontUse': 'НеИспользовать', 'DuringRequest': 'НаВремяВызова', 'DuringSession': 'НаВремяСеанса',
    'DontIndex': 'НеИндексировать', 'Index': 'Индексировать',
    'IndexWithAdditionalOrder': 'ИндексироватьСДопУпорядочиванием',
}


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _child(el, name: str):
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name: str):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _children_of(el, name: str):
    """Дочерние элементы контейнера name (пустой список, если его нет)."""
    container = _child(el, name)
    return list(container) if container is not None else []


def _text(el, name: str) -> str:
    c = _child(el, name)
    return (c.text or '').strip() if c is not None else ''


def _localized(el) -> str:
    """Многоязычная строка (Synonym): русский вариант, иначе первый."""
    if el is None:
        return ''
    items = []
    for item in _children(el, 'item'):
        items.append((_text(item, 'lang'), _text(item, 'content')))
    for lang, content in items:
        if lang == 'ru':
            return content
    return items[0][1] if items else ''


class _Lang:
    """Язык представления: как показывает Конфигуратор при этом ScriptVariant."""

    def __init__(self, russian: bool):
        self.ru = russian

    def value(self, v: str) -> str:
        return _VALUE_RU.get(v, v) if self.ru else v

    def bool_word(self, v: bool) -> str:
        return ('Истина' if v else 'Ложь') if self.ru else ('True' if v else 'False')

    def type_name(self, raw: str) -> str:
        """«cfg:CatalogRef.Склады» → «СправочникСсылка.Склады» / «CatalogRef.Склады»."""
        name = raw.split(':', 1)[1] if ':' in raw else raw
        head, dot, tail = name.partition('.')
        if not self.ru:
            return _TYPE_NAME_EN.get(head, head) + dot + tail
        ru = _TYPE_NAME_RU.get(head)
        if ru is None:
            for kind, kind_ru in _TYPE_KIND_RU.items():
                suffix = head[len(kind):]
                if head.startswith(kind) and suffix in _TYPE_SUFFIX_RU:
                    ru = kind_ru + _TYPE_SUFFIX_RU[suffix]
                    break
        return (ru or head) + dot + tail

    def render_type(self, type_el) -> str:
        """Описание типа реквизита: «Строка(25, Переменная), СправочникСсылка.X»."""
        if type_el is None:
            return ''
        sq = _child(type_el, 'StringQualifiers')
        nq = _child(type_el, 'NumberQualifiers')
        dq = _child(type_el, 'DateQualifiers')
        parts = []
        for t in type_el:
            tag = _local(t.tag)
            if tag not in ('Type', 'TypeSet'):
                continue
            raw = (t.text or '').strip()
            if not raw:
                continue
            parts.append(self._one_type(raw, sq, nq, dq))
        return ', '.join(parts)

    def _one_type(self, raw: str, sq, nq, dq) -> str:
        ru = self.ru
        if raw == 'xs:string':
            length = _text(sq, 'Length') or '0'
            fixed = _text(sq, 'AllowedLength') == 'Fixed'
            mode = ('Фиксированная' if fixed else 'Переменная') if ru else ('Fixed' if fixed else 'Variable')
            return f"{'Строка' if ru else 'String'}({length}, {mode})"
        if raw == 'xs:decimal':
            digits = _text(nq, 'Digits') or '0'
            fraction = _text(nq, 'FractionDigits') or '0'
            sign = ''
            if _text(nq, 'AllowedSign') == 'Nonnegative':
                sign = ', Неотрицательный' if ru else ', Nonnegative'
            return f"{'Число' if ru else 'Number'}({digits}, {fraction}{sign})"
        if raw == 'xs:dateTime':
            fractions = _text(dq, 'DateFractions') or 'DateTime'
            if ru:
                fractions = {'Date': 'Дата', 'DateTime': 'ДатаВремя', 'Time': 'Время'}.get(fractions, fractions)
            return f"{'Дата' if ru else 'Date'}({fractions})"
        if raw == 'xs:boolean':
            return 'Булево' if ru else 'Boolean'
        if raw == 'xs:base64Binary':
            return 'ДвоичныеДанные' if ru else 'BinaryData'
        return self.type_name(raw)


def _object_element(path: Path):
    """Корневой элемент объекта (<Catalog>, <Document>…) внутри <MetaDataObject>."""
    root = ET.parse(str(path)).getroot()
    for c in root:
        return c
    return None


def _attribute(el, kind: str, lang: _Lang) -> AttributeInfo:
    props = _child(el, 'Properties')
    indexing = _text(props, 'Indexing')
    return AttributeInfo(
        name=_text(props, 'Name'),
        synonym=_localized(_child(props, 'Synonym')),
        comment=_text(props, 'Comment'),
        type_desc=lang.render_type(_child(props, 'Type')),
        kind=kind,
        is_indexed=bool(indexing) and indexing != 'DontIndex',
        check_fill=lang.value(_text(props, 'FillChecking')),
        usage=lang.value(_text(props, 'Use')),
    )


_ATTRIBUTE_KINDS = {'Attribute': 'Реквизит', 'Dimension': 'Измерение', 'Resource': 'Ресурс'}
_MODULE_FLAGS = {
    'Global': 'is_global', 'ClientManagedApplication': 'is_client', 'Server': 'is_server',
    'ExternalConnection': 'is_external', 'ServerCall': 'server_call', 'Privileged': 'is_privileged',
}


_REGISTER_KINDS = ('AccumulationRegister', 'InformationRegister', 'AccountingRegister',
                   'CalculationRegister')
_REGISTER_PROPERTIES = ('RegisterType', 'InformationRegisterPeriodicity', 'WriteMode',
                        'ChartOfAccounts', 'Correspondence')


def _build_object(el, kind_en: str, kind_ru: str, full_name: str, lang: _Lang) -> MetadataObject:
    props = _child(el, 'Properties')
    obj = MetadataObject(
        name=_text(props, 'Name'),
        full_name=full_name,
        kind=kind_ru,
        synonym=_localized(_child(props, 'Synonym')),
        comment=_text(props, 'Comment'),
    )

    if kind_en == 'CommonModule':
        mi = CommonModuleInfo(name=obj.name)
        for prop, attr in _MODULE_FLAGS.items():
            setattr(mi, attr, _text(props, prop) == 'true')
        mi.reuse = lang.value(_text(props, 'ReturnValuesReuse'))
        obj.module_info = mi
    elif kind_en == 'Subsystem':
        content = []
        for item in _children(_child(props, 'Content'), 'Item'):
            ref = (item.text or '').strip()
            if not ref or _UUID.match(ref):
                continue
            head, dot, tail = ref.partition('.')
            content.append((_KIND_RU.get(head, head) if lang.ru else head) + dot + tail)
        obj.subsystem_info = SubsystemInfo(name=obj.name, content=content)
    elif kind_en in ('DefinedType', 'SessionParameter', 'Constant'):
        obj.type_desc = lang.render_type(_child(props, 'Type'))
    elif kind_en in _REGISTER_KINDS:
        # Как строить запрос к регистру (register_info): исходные значения,
        # к одному виду их приводит register_info.object_properties.
        for prop in _REGISTER_PROPERTIES:
            value = _text(props, prop)
            if value:
                obj.properties[prop] = value
    elif kind_en == 'Document':
        for item in _children(_child(props, 'RegisterRecords'), 'Item'):
            ref = (item.text or '').strip()
            if not ref or _UUID.match(ref):
                continue
            head, dot, tail = ref.partition('.')
            # Полное имя — с русским видом, как у объектов в индексе.
            obj.register_records.append(_KIND_RU.get(head, head) + dot + tail)

    # Стандартные реквизиты (standard_attributes): длина кода и
    # наименования, иерархия, владельцы, номер, проведение.
    for prop in STANDARD_PROPERTIES.get(kind_en, ()):
        value = _text(props, prop)
        if value:
            obj.properties[prop] = value
    if kind_en == 'Catalog':
        owners = []
        for item in _children(_child(props, 'Owners'), 'Item'):
            ref = (item.text or '').strip()
            if ref and not _UUID.match(ref):
                head, dot, tail = ref.partition('.')
                owners.append(_KIND_RU.get(head, head) + dot + tail)
        if owners:
            obj.properties[OWNERS] = ', '.join(owners)

    for child in _children_of(el, 'ChildObjects'):
        tag = _local(child.tag)
        if tag in _ATTRIBUTE_KINDS:
            obj.attributes.append(_attribute(child, _ATTRIBUTE_KINDS[tag], lang))
        elif tag == 'TabularSection':
            ts_props = _child(child, 'Properties')
            ts_name = _text(ts_props, 'Name')
            obj.tabular_sections[ts_name] = [
                _attribute(a, 'Реквизит', lang)
                for a in _children(_child(child, 'ChildObjects'), 'Attribute')]
        elif tag == 'Form':
            obj.forms.append(FormInfo(name=(child.text or '').strip()))
        elif tag == 'EnumValue':
            ev_props = _child(child, 'Properties')
            obj.enum_values.append(EnumValueInfo(
                name=_text(ev_props, 'Name'),
                synonym=_localized(_child(ev_props, 'Synonym')),
                comment=_text(ev_props, 'Comment')))
    return obj


def has_configuration(xml_root: str | Path) -> bool:
    """Есть ли в каталоге выгрузка с метаданными (Configuration.xml в корне)."""
    return (Path(xml_root) / 'Configuration.xml').is_file()


def read_script_settings(xml_root: str | Path) -> tuple[bool, str]:
    """Язык BSL и числовой режим совместимости из Configuration.xml."""
    try:
        props = _child(_object_element(Path(xml_root) / 'Configuration.xml'), 'Properties')
    except (OSError, ET.ParseError):
        return True, ''
    russian = _text(props, 'ScriptVariant') != 'English'
    mode = _text(props, 'CompatibilityMode')
    match = re.fullmatch(r'Version(\d+(?:_\d+)+)', mode)
    return russian, match[1].replace('_', '.') if match else ''


HELP_MAX_CHARS = 20000   # справка длиннее — обрезается: назначение объекта в начале


class _HelpText(HTMLParser):
    """HTML справки → текст: без разметки, стилей и скриптов; абзацы — переводом строки."""
    BLOCKS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'ul', 'ol'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'): self.skip += 1
        elif tag in self.BLOCKS: self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head') and self.skip: self.skip -= 1
        elif tag in self.BLOCKS: self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def help_to_text(markup: str) -> str:
    parser = _HelpText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:          # битая разметка — не повод терять справку
        return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', markup))).strip()[:HELP_MAX_CHARS]
    lines = (re.sub(r'[ \t\xa0﻿]+', ' ', ln).strip() for ln in ''.join(parser.parts).split('\n'))
    return '\n'.join(ln for ln in lines if ln)[:HELP_MAX_CHARS]


DCS_QUERY_MAX_CHARS = 60000   # запрос схемы длиннее — обрезается (у крупных отчётов ~50 тыс.)


def read_dcs_queries(obj_el, obj_dir: Path) -> list[tuple[str, str, str]]:
    """Запросы схем компоновки данных объекта: макеты вида DataCompositionSchema
    (Templates/<Имя>.xml → Templates/<Имя>/Ext/Template.xml), наборы данных-запросы,
    в том числе внутри объединений. → [(макет, набор, текст)]."""
    found = []
    for t in _children_of(obj_el, 'ChildObjects'):
        if _local(t.tag) != 'Template' or not (t.text or '').strip():
            continue
        name = t.text.strip()
        try:
            meta = _object_element(obj_dir / 'Templates' / f'{name}.xml')
        except (OSError, ET.ParseError):
            continue
        if _text(_child(meta, 'Properties'), 'TemplateType') != 'DataCompositionSchema':
            continue
        try:
            schema = ET.parse(obj_dir / 'Templates' / name / 'Ext' / 'Template.xml').getroot()
        except (OSError, ET.ParseError) as e:
            logger.warning('Схема компоновки: не прочитан макет %s %s (%s)', obj_dir.name, name, e)
            continue
        for ds in schema.iter():
            if _local(ds.tag) not in ('dataSet', 'item'):
                continue
            query = _child(ds, 'query')
            if query is None or not (query.text or '').strip():
                continue
            found.append((name, _text(ds, 'name'), query.text.strip()[:DCS_QUERY_MAX_CHARS]))
    return found


def read_help(obj_dir: Path) -> str:
    """Справка объекта из выгрузки: Ext/Help/ru.html (иначе первый .html каталога). Нет — пусто."""
    help_dir = obj_dir / 'Ext' / 'Help'
    try:
        pages = sorted(help_dir.glob('*.html'), key=lambda p: (p.stem != 'ru', p.name))
    except OSError:
        return ''
    for page in pages:
        try:
            return help_to_text(page.read_text(encoding='utf-8-sig', errors='replace'))
        except OSError as e:
            logger.warning('Справка: не прочитан %s (%s)', page, e)
    return ''


def parse_xml_dump(xml_root: str | Path) -> tuple[ConfigInfo, list[MetadataObject]]:
    """ConfigInfo и объекты метаданных из корня XML-выгрузки.

    Файл объекта, который не читается или повреждён, пропускается с
    предупреждением в логе — остальные метаданные всё равно индексируются.
    """
    root = Path(xml_root)
    conf_el = _object_element(root / 'Configuration.xml')
    props = _child(conf_el, 'Properties')
    lang = _Lang(_text(props, 'ScriptVariant') != 'English')

    compat = _text(props, 'CompatibilityMode')
    if lang.ru:
        compat = compat.replace('Version', 'Версия', 1) if compat.startswith('Version') else lang.value(compat)
    config = ConfigInfo(
        name=_text(props, 'Name'),
        synonym=_localized(_child(props, 'Synonym')),
        version=_text(props, 'Version'),
        comment=_text(props, 'Comment'),
        compatibility_mode=compat,
    )

    sub_word = 'Подсистемы' if lang.ru else 'Subsystems'

    def load(path: Path):
        try:
            return _object_element(path)
        except (OSError, ET.ParseError) as e:
            logger.warning('Метаданные: не прочитан %s (%s)', path, e)
            return None

    def subsystem(dir_path: Path, name: str, full_name: str) -> list[MetadataObject]:
        el = load(dir_path / f'{name}.xml')
        if el is None:
            return []
        found = [with_help(_build_object(el, 'Subsystem', 'Подсистема', full_name, lang), dir_path / name)]
        for nested in _children(_child(el, 'ChildObjects'), 'Subsystem'):
            nested_name = (nested.text or '').strip()
            if nested_name:
                found += subsystem(dir_path / name / 'Subsystems', nested_name,
                                   f'{full_name}.{sub_word}.{nested_name}')
        return found

    def with_help(obj: MetadataObject, obj_dir: Path) -> MetadataObject:
        obj.help_text = read_help(obj_dir)
        return obj

    def child_objects(child) -> list[MetadataObject]:
        kind_en = _local(child.tag)
        name = (child.text or '').strip()
        if kind_en not in _KINDS or not name:
            return []
        plural, kind_ru = _KINDS[kind_en]
        if kind_en == 'Subsystem':
            return subsystem(root / plural, name, f'Подсистема.{name}')
        el = load(root / plural / f'{name}.xml')
        if el is None:
            return []
        obj = with_help(_build_object(el, kind_en, kind_ru, f'{kind_ru}.{name}', lang), root / plural / name)
        if kind_en == 'Report':
            obj.dcs_queries = read_dcs_queries(el, root / plural / name)
        return [obj]

    # Файлы объектов читаются параллельно: через 9p (Docker Desktop на
    # Windows) чтение десятков тысяч файлов упирается в задержку каждого
    # обращения, а не в разбор. Порядок объектов — как в Configuration.xml.
    objects: list[MetadataObject] = []
    with ThreadPoolExecutor(IO_THREADS) as pool:
        for found in pool.map(child_objects, list(_children_of(conf_el, 'ChildObjects'))):
            objects += found

    return config, objects
