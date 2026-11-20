"""
XML Walker: traverses XML configuration dump to find .bsl module files.
Handles 1C's #U-encoded Unicode directory names.

Structure:
    ConfigDump/
    ├── CommonModules/#U041e#U0431#U0449.../Ext/Module.bsl
    ├── Catalogs/#U041d#U043e#U043c.../Ext/ObjectModule.bsl
    ├── Catalogs/#U041d#U043e#U043c.../Forms/#U0424.../Ext/Form/Module.bsl
    ├── CommonForms/#U0412.../Ext/Form/Module.bsl   (общая форма, без Forms/)
    ├── Documents/#U041f.../Ext/ObjectModule.bsl
    ├── Ext/ManagedApplicationModule.bsl
    └── Ext/SessionModule.bsl
"""

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


# Mapping: XML directory name → 1C object kind
DIR_TO_KIND = {
    'Catalogs': 'Справочник',
    'Constants': 'Константа',
    'Documents': 'Документ',
    'DocumentJournals': 'ЖурналДокументов',
    'InformationRegisters': 'РегистрСведений',
    'AccumulationRegisters': 'РегистрНакопления',
    'AccountingRegisters': 'РегистрБухгалтерии',
    'CalculationRegisters': 'РегистрРасчета',
    'CommonModules': 'ОбщийМодуль',
    'DataProcessors': 'Обработка',
    'Reports': 'Отчет',
    'Enums': 'Перечисление',
    'ChartsOfCharacteristicTypes': 'ПланВидовХарактеристик',
    'ChartsOfCalculationTypes': 'ПланВидовРасчета',
    'ChartsOfAccounts': 'ПланСчетов',
    'BusinessProcesses': 'БизнесПроцесс',
    'Tasks': 'Задача',
    'ExchangePlans': 'ПланОбмена',
    'CommonForms': 'ОбщаяФорма',
    'CommonCommands': 'ОбщаяКоманда',
    'SettingsStorages': 'ХранилищеНастроек',
    'FilterCriteria': 'КритерийОтбора',
}

# Mapping: .bsl filename → module type
FILE_TO_MODULE_TYPE = {
    'Module.bsl': 'Модуль',
    'ObjectModule.bsl': 'МодульОбъекта',
    'ManagerModule.bsl': 'МодульМенеджера',
    'CommandModule.bsl': 'МодульКоманды',
    'RecordSetModule.bsl': 'МодульНабораЗаписей',
    'ManagedApplicationModule.bsl': 'МодульУправляемогоПриложения',
    'SessionModule.bsl': 'МодульСеанса',
}


@dataclass
class ModuleFile:
    """Discovered .bsl file in XML dump."""
    file_path: str              # Absolute path to .bsl
    relative_path: str          # Relative to XML root
    object_kind: str            # Справочник, Документ, ОбщийМодуль...
    object_name: str            # Номенклатура, РасчетныйМодуль...
    module_type: str            # МодульОбъекта, МодульМенеджера, Модуль...
    form_name: str = ''         # For form modules: ФормаЭлемента, etc.
    full_name: str = ''         # Справочник.Номенклатура.Форма.ФормаЭлемента
    form_xml_path: str = ''     # For form modules: absolute path to Ext/Form.xml, if present


def decode_1c_dirname(name: str) -> str:
    """Decode 1C's #U-encoded directory name to Unicode.
    
    Example: #U041d#U043e#U043c → Ном
    """
    def replace_match(m):
        return chr(int(m.group(1), 16))
    return re.sub(r'#U([0-9A-Fa-f]{4})', replace_match, name)


def _local_tag(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


@dataclass
class FormAttribute:
    name: str
    title: str = ''
    type_desc: str = ''


def read_form_attribute_details(form_xml_path: str | Path,
                                russian: bool = True) -> list[FormAttribute] | None:
    """Реквизиты формы из её Ext/Form.xml: имя, заголовок, тип — для
    search_attributes и для разбора модуля формы (read_form_attributes).

    Берутся только прямые <Attribute> внутри <Attributes> корня <Form> —
    и внутри <BaseForm>: у заимствованной формы расширения там лежит копия
    исходной формы, чьи реквизиты модуль расширения тоже видит. Колонки
    реквизитов-таблиц (<Columns>/<Column>, <AdditionalColumns>) — не
    переменные модуля, а параметры и элементы формы доступны только через
    Параметры.X / Элементы.X, поэтому их здесь нет намеренно.

    Имена тегов в выгрузке всегда английские, язык выгрузки влияет только
    на значения (name="Объект" / name="Object"), так что разбор один;
    russian — на каком языке описывать тип («Число(10, 2)» / «Number(10, 2)»).
    None — файл не читается или повреждён.
    """
    from .xml_metadata import _Lang, _child, _localized
    try:
        root = ET.parse(str(form_xml_path)).getroot()
    except (OSError, ET.ParseError):
        return None
    lang = _Lang(russian)
    containers = [root] + [c for c in root if _local_tag(c.tag) == 'BaseForm']
    result: list[FormAttribute] = []
    seen: set[str] = set()
    for container in containers:
        for attrs in container:
            if _local_tag(attrs.tag) != 'Attributes':
                continue
            for attr in attrs:
                name = attr.get('name')
                if _local_tag(attr.tag) != 'Attribute' or not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                result.append(FormAttribute(name, _localized(_child(attr, 'Title')),
                                            lang.render_type(_child(attr, 'Type'))))
    return result


def read_form_attributes(form_xml_path: str | Path) -> set[str]:
    """Имена реквизитов формы (lower-case) — переменные модуля формы.
    Битый или отсутствующий файл — пустое множество: реквизиты лишь
    подавляют фантомные вызовы, их отсутствие ничего не ломает."""
    return {a.name.lower() for a in read_form_attribute_details(form_xml_path) or []}


# Сколько каталогов выгрузки читать одновременно. Обход упирается не в диск, а
# в задержку каждого обращения: через 9p (Docker Desktop на Windows) это
# миллисекунды, и на крупной конфигурации последовательный обход шёл минуты.
IO_THREADS = 16


def scan_dir(path: str | Path) -> dict[str, bool]:
    """Содержимое каталога: имя → это каталог. Одно обращение к ФС вместо
    exists/is_dir/glob на каждое имя; {} — каталога нет."""
    try:
        with os.scandir(path) as entries:
            return {e.name: e.is_dir() for e in entries}
    except OSError:
        return {}


class XMLWalker:
    """Walks XML config dump directory and yields .bsl files."""

    def walk(self, xml_root: str | Path) -> list[ModuleFile]:
        """Find all .bsl files in XML dump. Returns list of ModuleFile."""
        root = Path(xml_root)
        top = scan_dir(root)
        if not top:
            return []

        modules = []

        # 1. Top-level modules: Ext/SessionModule.bsl, Ext/ManagedApplicationModule.bsl
        if top.get('Ext'):
            for name, is_dir in sorted(scan_dir(root / 'Ext').items()):
                if is_dir or not name.endswith('.bsl'):
                    continue
                bsl = root / 'Ext' / name
                module_type = FILE_TO_MODULE_TYPE.get(name, bsl.stem)
                modules.append(ModuleFile(
                    file_path=str(bsl),
                    relative_path=bsl.relative_to(root).as_posix(),
                    object_kind='Конфигурация',
                    object_name='Конфигурация',
                    module_type=module_type,
                    full_name=f'Конфигурация.{module_type}',
                ))

        # 2. Object modules: {Category}/{EncodedName}/Ext/{ModuleType}.bsl —
        # объекты читаются параллельно, порядок результата — как у списка.
        objects = []
        for dir_name, kind in DIR_TO_KIND.items():
            if not top.get(dir_name):
                continue
            category_dir = root / dir_name
            objects += [(kind, category_dir / name)
                        for name, is_dir in sorted(scan_dir(category_dir).items()) if is_dir]
        with ThreadPoolExecutor(IO_THREADS) as pool:
            for found in pool.map(lambda o: self._walk_object(root, *o), objects):
                modules += found
        return modules

    @staticmethod
    def _walk_object(root: Path, kind: str, obj_dir: Path) -> list[ModuleFile]:
        obj_name = decode_1c_dirname(obj_dir.name)
        entries = scan_dir(obj_dir)
        modules = []

        # Object-level modules: Ext/ObjectModule.bsl, Ext/ManagerModule.bsl
        obj_ext = obj_dir / 'Ext'
        ext = scan_dir(obj_ext) if entries.get('Ext') else {}
        for name, is_dir in sorted(ext.items()):
            if is_dir or not name.endswith('.bsl'):
                continue
            bsl = obj_ext / name
            module_type = FILE_TO_MODULE_TYPE.get(name, bsl.stem)
            modules.append(ModuleFile(
                file_path=str(bsl),
                relative_path=bsl.relative_to(root).as_posix(),
                object_kind=kind,
                object_name=obj_name,
                module_type=module_type,
                full_name=f'{kind}.{obj_name}.{module_type}',
            ))

        # Common form module: CommonForms/{Name}/Ext/Form/Module.bsl —
        # the object itself is the form, there's no Forms/ subdir.
        # full_name matches the object name (ОбщаяФорма.X).
        if kind == 'ОбщаяФорма' and ext.get('Form'):
            form_bsl = obj_ext / 'Form' / 'Module.bsl'
            if form_bsl.exists():
                modules.append(ModuleFile(
                    file_path=str(form_bsl),
                    relative_path=form_bsl.relative_to(root).as_posix(),
                    object_kind=kind,
                    object_name=obj_name,
                    module_type='МодульФормы',
                    form_name=obj_name,
                    full_name=f'{kind}.{obj_name}',
                    form_xml_path=str(obj_ext / 'Form.xml') if ext.get('Form.xml') is False else '',
                ))

        # Form modules: Forms/{EncodedFormName}/Ext/Form/Module.bsl
        if entries.get('Forms'):
            forms_dir = obj_dir / 'Forms'
            for form_dir_name, is_dir in sorted(scan_dir(forms_dir).items()):
                if not is_dir:
                    continue
                form_dir = forms_dir / form_dir_name
                form_ext = scan_dir(form_dir / 'Ext')
                form_bsl = form_dir / 'Ext' / 'Form' / 'Module.bsl'
                if not form_ext.get('Form') or not form_bsl.exists():
                    continue
                form_name = decode_1c_dirname(form_dir_name)
                modules.append(ModuleFile(
                    file_path=str(form_bsl),
                    relative_path=form_bsl.relative_to(root).as_posix(),
                    object_kind=kind,
                    object_name=obj_name,
                    module_type='МодульФормы',
                    form_name=form_name,
                    full_name=f'{kind}.{obj_name}.Форма.{form_name}',
                    form_xml_path=str(form_dir / 'Ext' / 'Form.xml') if form_ext.get('Form.xml') is False else '',
                ))
        return modules
