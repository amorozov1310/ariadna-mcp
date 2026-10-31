"""
Метаданные конфигурации прямо из XML-выгрузки (Конфигуратор → «Выгрузить
конфигурацию в файлы»), без отчёта по конфигурации.

Результат — те же ConfigInfo и MetadataObject, что даёт report_parser для
отчёта Конфигуратора, в том же виде: русские виды объектов в full_name,
типы как в отчёте на языке конфигурации («СправочникСсылка.Склады»,
«Строка(25, Переменная)» или «CatalogRef.Users», «String(25, Variable)»).
Поэтому индексатор и поиск не различают, откуда пришли метаданные.

Берётся только то, что индексатор хранит: объекты верхнего уровня, их
реквизиты, измерения, ресурсы, табличные части, формы, значения
перечислений, флаги общих модулей, состав подсистем (с вложенными).
Команды, макеты, шаблоны URL и прочие дочерние элементы, как и в отчёте,
пропускаются.
"""

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .report_parser import (
    OBJECT_TYPE_MAP, AttributeInfo, CommonModuleInfo, ConfigInfo, EnumValueInfo,
    FormInfo, MetadataObject, SubsystemInfo,
)

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

# Перечислимые значения свойств: в русском отчёте они русские.
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
    """Язык представления: как в отчёте конфигурации с этим ScriptVariant."""

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


def is_russian_dump(xml_root: str | Path) -> bool:
    """Язык встроенного языка выгрузки (ScriptVariant в Configuration.xml);
    без Configuration.xml (выгрузка только модулей) — русский."""
    try:
        root = ET.parse(str(Path(xml_root) / 'Configuration.xml')).getroot()
    except (OSError, ET.ParseError):
        return True
    for obj in root:
        return _text(_child(obj, 'Properties'), 'ScriptVariant') != 'English'
    return True


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

    objects: list[MetadataObject] = []
    sub_word = 'Подсистемы' if lang.ru else 'Subsystems'

    def load(path: Path):
        try:
            return _object_element(path)
        except (OSError, ET.ParseError) as e:
            logger.warning('Метаданные: не прочитан %s (%s)', path, e)
            return None

    def add_subsystem(dir_path: Path, name: str, full_name: str):
        el = load(dir_path / f'{name}.xml')
        if el is None:
            return
        objects.append(_build_object(el, 'Subsystem', 'Подсистема', full_name, lang))
        for nested in _children(_child(el, 'ChildObjects'), 'Subsystem'):
            nested_name = (nested.text or '').strip()
            if nested_name:
                add_subsystem(dir_path / name / 'Subsystems', nested_name,
                              f'{full_name}.{sub_word}.{nested_name}')

    for child in _children_of(conf_el, 'ChildObjects'):
        kind_en = _local(child.tag)
        name = (child.text or '').strip()
        if kind_en not in _KINDS or not name:
            continue
        plural, kind_ru = _KINDS[kind_en]
        if kind_en == 'Subsystem':
            add_subsystem(root / plural, name, f'Подсистема.{name}')
            continue
        el = load(root / plural / f'{name}.xml')
        if el is not None:
            objects.append(_build_object(el, kind_en, kind_ru, f'{kind_ru}.{name}', lang))

    return config, objects
