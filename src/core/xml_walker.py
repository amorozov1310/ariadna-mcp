"""
XML Walker: traverses XML configuration dump to find .bsl module files.
Handles 1C's #U-encoded Unicode directory names.

Structure:
    ConfigDump/
    ├── CommonModules/#U041e#U0431#U0449.../Ext/Module.bsl
    ├── Catalogs/#U041d#U043e#U043c.../Ext/ObjectModule.bsl
    ├── Catalogs/#U041d#U043e#U043c.../Forms/#U0424.../Ext/Form/Module.bsl
    ├── Documents/#U041f.../Ext/ObjectModule.bsl
    ├── Ext/ManagedApplicationModule.bsl
    └── Ext/SessionModule.bsl
"""

import re
from dataclasses import dataclass
from pathlib import Path


# Mapping: XML directory name → 1C object kind
DIR_TO_KIND = {
    'Catalogs': 'Справочник',
    'Documents': 'Документ',
    'InformationRegisters': 'РегистрСведений',
    'AccumulationRegisters': 'РегистрНакопления',
    'AccountingRegisters': 'РегистрБухгалтерии',
    'CalculationRegisters': 'РегистрРасчета',
    'CommonModules': 'ОбщийМодуль',
    'DataProcessors': 'Обработка',
    'Reports': 'Отчет',
    'Enums': 'Перечисление',
    'ChartsOfCharacteristicTypes': 'ПланВидовХарактеристик',
    'ChartsOfAccounts': 'ПланСчетов',
    'BusinessProcesses': 'БизнесПроцесс',
    'Tasks': 'Задача',
    'ExchangePlans': 'ПланОбмена',
    'CommonForms': 'ОбщаяФорма',
    'CommonCommands': 'ОбщаяКоманда',
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


def decode_1c_dirname(name: str) -> str:
    """Decode 1C's #U-encoded directory name to Unicode.
    
    Example: #U041d#U043e#U043c → Ном
    """
    def replace_match(m):
        return chr(int(m.group(1), 16))
    return re.sub(r'#U([0-9A-Fa-f]{4})', replace_match, name)


class XMLWalker:
    """Walks XML config dump directory and yields .bsl files."""

    def walk(self, xml_root: str | Path) -> list[ModuleFile]:
        """Find all .bsl files in XML dump. Returns list of ModuleFile."""
        root = Path(xml_root)
        if not root.exists():
            return []

        modules = []

        # 1. Top-level modules: Ext/SessionModule.bsl, Ext/ManagedApplicationModule.bsl
        ext_dir = root / 'Ext'
        if ext_dir.exists():
            for bsl in ext_dir.glob('*.bsl'):
                module_type = FILE_TO_MODULE_TYPE.get(bsl.name, bsl.stem)
                modules.append(ModuleFile(
                    file_path=str(bsl),
                    relative_path=bsl.relative_to(root).as_posix(),
                    object_kind='Конфигурация',
                    object_name='Конфигурация',
                    module_type=module_type,
                    full_name=f'Конфигурация.{module_type}',
                ))

        # 2. Object modules: {Category}/{EncodedName}/Ext/{ModuleType}.bsl
        for dir_name, kind in DIR_TO_KIND.items():
            category_dir = root / dir_name
            if not category_dir.exists():
                continue

            for obj_dir in sorted(category_dir.iterdir()):
                if not obj_dir.is_dir():
                    continue

                obj_name = decode_1c_dirname(obj_dir.name)

                # Object-level modules: Ext/ObjectModule.bsl, Ext/ManagerModule.bsl
                obj_ext = obj_dir / 'Ext'
                if obj_ext.exists():
                    for bsl in obj_ext.glob('*.bsl'):
                        module_type = FILE_TO_MODULE_TYPE.get(bsl.name, bsl.stem)
                        full_name = f'{kind}.{obj_name}.{module_type}'
                        modules.append(ModuleFile(
                            file_path=str(bsl),
                            relative_path=bsl.relative_to(root).as_posix(),
                            object_kind=kind,
                            object_name=obj_name,
                            module_type=module_type,
                            full_name=full_name,
                        ))

                # Form modules: Forms/{EncodedFormName}/Ext/Form/Module.bsl
                forms_dir = obj_dir / 'Forms'
                if forms_dir.exists():
                    for form_dir in sorted(forms_dir.iterdir()):
                        if not form_dir.is_dir():
                            continue
                        form_name = decode_1c_dirname(form_dir.name)
                        form_bsl = form_dir / 'Ext' / 'Form' / 'Module.bsl'
                        if form_bsl.exists():
                            full_name = f'{kind}.{obj_name}.Форма.{form_name}'
                            modules.append(ModuleFile(
                                file_path=str(form_bsl),
                                relative_path=form_bsl.relative_to(root).as_posix(),
                                object_kind=kind,
                                object_name=obj_name,
                                module_type='МодульФормы',
                                form_name=form_name,
                                full_name=full_name,
                            ))

        return modules
