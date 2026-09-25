"""
Parser for 1C Configurator report (Отчёт по конфигурации).
Reads UTF-16LE text file and extracts metadata into structured objects.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path


# ============================================
# DATA CLASSES
# ============================================

@dataclass
class ConfigInfo:
    name: str = ''
    synonym: str = ''
    version: str = ''
    comment: str = ''
    compatibility_mode: str = ''


@dataclass
class AttributeInfo:
    name: str = ''
    synonym: str = ''
    comment: str = ''
    type_desc: str = ''
    kind: str = 'Реквизит'  # Реквизит, Измерение, Ресурс
    is_indexed: bool = False
    check_fill: str = ''
    usage: str = ''          # ДляЭлемента, ДляГруппы


@dataclass
class FormInfo:
    name: str = ''
    synonym: str = ''


@dataclass
class EnumValueInfo:
    name: str = ''
    synonym: str = ''
    comment: str = ''


@dataclass
class SubsystemInfo:
    name: str = ''
    synonym: str = ''
    comment: str = ''
    content: list[str] = field(default_factory=list)


@dataclass
class CommonModuleInfo:
    name: str = ''
    synonym: str = ''
    comment: str = ''
    is_global: bool = False
    is_client: bool = False
    is_server: bool = False
    is_external: bool = False
    server_call: bool = False
    is_privileged: bool = False
    reuse: str = ''


@dataclass
class MetadataObject:
    """Represents any 1C configuration object."""
    name: str = ''
    full_name: str = ''            # Справочник.Номенклатура
    kind: str = ''                 # Справочник, Документ, etc.
    synonym: str = ''
    comment: str = ''
    # Child elements
    attributes: list[AttributeInfo] = field(default_factory=list)
    tabular_sections: dict[str, list[AttributeInfo]] = field(default_factory=dict)
    forms: list[FormInfo] = field(default_factory=list)
    enum_values: list[EnumValueInfo] = field(default_factory=list)
    # Common module properties
    module_info: CommonModuleInfo | None = None
    # Subsystem properties
    subsystem_info: SubsystemInfo | None = None
    # Defined type
    type_desc: str = ''
    # Raw properties
    properties: dict[str, str] = field(default_factory=dict)


# ============================================
# TYPE MAPPING
# ============================================

# Plural form in report → (singular for full_name, english kind)
# Supports both Russian (ScriptVariant=Russian) and English (ScriptVariant=English) report formats.
OBJECT_TYPE_MAP: dict[str, tuple[str, str]] = {
    # ── Russian plural forms ────────────────────────────────────────────────
    'Справочники':          ('Справочник',          'Catalog'),
    'Документы':            ('Документ',            'Document'),
    'РегистрыСведений':     ('РегистрСведений',     'InformationRegister'),
    'РегистрыНакопления':   ('РегистрНакопления',   'AccumulationRegister'),
    'РегистрыБухгалтерии':  ('РегистрБухгалтерии',  'AccountingRegister'),
    'РегистрыРасчета':      ('РегистрРасчета',      'CalculationRegister'),
    'Перечисления':         ('Перечисление',        'Enum'),
    'Отчеты':               ('Отчет',               'Report'),
    'Обработки':            ('Обработка',           'DataProcessor'),
    'ОбщиеМодули':          ('ОбщийМодуль',         'CommonModule'),
    'ОбщиеФормы':           ('ОбщаяФорма',          'CommonForm'),
    'Подсистемы':           ('Подсистема',          'Subsystem'),
    'ОпределяемыеТипы':     ('ОпределяемыйТип',    'DefinedType'),
    'ОбщиеКартинки':        ('ОбщаяКартинка',       'CommonPicture'),
    'ПараметрыСеанса':      ('ПараметрСеанса',      'SessionParameter'),
    'ОбщиеКоманды':         ('ОбщаяКоманда',        'CommonCommand'),
    'ОбщиеРеквизиты':       ('ОбщийРеквизит',       'CommonAttribute'),
    'Роли':                 ('Роль',                'Role'),
    'Стили':                ('Стиль',               'Style'),
    'ГруппыКоманд':         ('ГруппаКоманд',        'CommandGroup'),
    'Языки':                ('Язык',                'Language'),
    'Константы':            ('Константа',           'Constant'),
    'Критерии':             ('КритерийОтбора',      'FilterCriterion'),
    'ПланыВидовХарактеристик': ('ПланВидовХарактеристик', 'ChartOfCharacteristicTypes'),
    'ПланыСчетов':          ('ПланСчетов',          'ChartOfAccounts'),
    'ПланыВидовРасчета':    ('ПланВидовРасчета',    'ChartOfCalculationTypes'),
    'БизнесПроцессы':       ('БизнесПроцесс',      'BusinessProcess'),
    'Задачи':               ('Задача',              'Task'),
    'ЖурналыДокументов':    ('ЖурналДокументов',    'DocumentJournal'),
    'ВнешниеИсточникиДанных': ('ВнешнийИсточникДанных', 'ExternalDataSource'),
    'ФункциональныеОпции':  ('ФункциональнаяОпция', 'FunctionalOption'),
    'ПараметрыФункциональныхОпций': ('ПараметрФункциональнойОпции', 'FunctionalOptionParameter'),
    'ХранилищаНастроек':    ('ХранилищеНастроек',   'SettingsStorage'),
    'РегламентныеЗадания':  ('РегламентноеЗадание', 'ScheduledJob'),
    'ПодпискиНаСобытия':    ('ПодпискаНаСобытие',   'EventSubscription'),
    'Интерфейсы':           ('Интерфейс',           'Interface'),
    'ПланыОбмена':          ('ПланОбмена',          'ExchangePlan'),
    'HTTPСервисы':          ('HTTPСервис',          'HTTPService'),
    'WebСервисы':           ('WebСервис',           'WebService'),
    'ОбщиеМакеты':          ('ОбщийМакет',          'CommonTemplate'),
    'ОбщиеШаблоны':         ('ОбщийШаблон',         'CommonTemplate'),

    # ── English plural forms (ScriptVariant = English) ──────────────────────
    'Catalogs':                     ('Справочник',          'Catalog'),
    'Documents':                    ('Документ',            'Document'),
    'InformationRegisters':         ('РегистрСведений',     'InformationRegister'),
    'AccumulationRegisters':        ('РегистрНакопления',   'AccumulationRegister'),
    'AccountingRegisters':          ('РегистрБухгалтерии',  'AccountingRegister'),
    'CalculationRegisters':         ('РегистрРасчета',      'CalculationRegister'),
    'Enums':                        ('Перечисление',        'Enum'),
    'Reports':                      ('Отчет',               'Report'),
    'DataProcessors':               ('Обработка',           'DataProcessor'),
    'CommonModules':                ('ОбщийМодуль',         'CommonModule'),
    'CommonForms':                  ('ОбщаяФорма',          'CommonForm'),
    'Subsystems':                   ('Подсистема',          'Subsystem'),
    'DefinedTypes':                 ('ОпределяемыйТип',    'DefinedType'),
    'CommonPictures':               ('ОбщаяКартинка',       'CommonPicture'),
    'SessionParameters':            ('ПараметрСеанса',      'SessionParameter'),
    'CommonCommands':               ('ОбщаяКоманда',        'CommonCommand'),
    'CommonAttributes':             ('ОбщийРеквизит',       'CommonAttribute'),
    'Roles':                        ('Роль',                'Role'),
    'Styles':                       ('Стиль',               'Style'),
    'CommandGroups':                ('ГруппаКоманд',        'CommandGroup'),
    'Languages':                    ('Язык',                'Language'),
    'Constants':                    ('Константа',           'Constant'),
    'FilterCriteria':               ('КритерийОтбора',      'FilterCriterion'),
    'ChartsOfCharacteristicTypes':  ('ПланВидовХарактеристик', 'ChartOfCharacteristicTypes'),
    'ChartsOfAccounts':             ('ПланСчетов',          'ChartOfAccounts'),
    'ChartsOfCalculationTypes':     ('ПланВидовРасчета',    'ChartOfCalculationTypes'),
    'BusinessProcesses':            ('БизнесПроцесс',      'BusinessProcess'),
    'Tasks':                        ('Задача',              'Task'),
    'DocumentJournals':             ('ЖурналДокументов',    'DocumentJournal'),
    'ExternalDataSources':          ('ВнешнийИсточникДанных', 'ExternalDataSource'),
    'FunctionalOptions':            ('ФункциональнаяОпция', 'FunctionalOption'),
    'FunctionalOptionsParameters':  ('ПараметрФункциональнойОпции', 'FunctionalOptionParameter'),
    'SettingsStorages':             ('ХранилищеНастроек',   'SettingsStorage'),
    'ScheduledJobs':                ('РегламентноеЗадание', 'ScheduledJob'),
    'EventSubscriptions':           ('ПодпискаНаСобытие',   'EventSubscription'),
    'Interfaces':                   ('Интерфейс',           'Interface'),
    'ExchangePlans':                ('ПланОбмена',          'ExchangePlan'),
    'HTTPServices':                 ('HTTPСервис',          'HTTPService'),
    'WebServices':                  ('WebСервис',           'WebService'),
    'CommonTemplates':              ('ОбщийМакет',          'CommonTemplate'),
    'Configurations':               ('Конфигурация',        'Configuration'),
}

# EN singular kind name (case-folded) -> RU canonical singular kind, derived
# from OBJECT_TYPE_MAP. Internal canonicalization stays Russian (Этап 2,
# D8-D11): every tool accepting a ``kind`` filter runs it through
# canonicalize_kind() so callers can pass either "Catalog" or "Справочник".
KIND_ALIASES_EN_TO_RU: dict[str, str] = {
    en.casefold(): ru for ru, en in OBJECT_TYPE_MAP.values()
}

# RU canonical singular kind (case-folded) -> canonically-cased RU kind —
# lets canonicalize_kind() also accept a RU kind typed in the wrong case
# ("справочник" -> "Справочник").
_RU_KIND_CANONICAL: dict[str, str] = {
    ru.casefold(): ru for ru, _en in OBJECT_TYPE_MAP.values()
}


def canonicalize_kind(kind: str) -> str:
    """
    Normalize a user-supplied ``kind`` filter to the internal RU canonical
    form, accepting the RU name (any case), the EN name (any case), or
    anything else unchanged (so an unrecognized value still just fails to
    match, same as before — no new way to error out).
    """
    if not kind:
        return kind
    folded = kind.strip().casefold()
    return KIND_ALIASES_EN_TO_RU.get(folded) or _RU_KIND_CANONICAL.get(folded) or kind

# Child element identifiers in the dot-path
# Supports both Russian and English report variants.
CHILD_KIND_MAP: dict[str, str] = {
    # Russian
    'Реквизиты':            'Реквизит',
    'Измерения':            'Измерение',
    'Ресурсы':              'Ресурс',
    'ТабличныеЧасти':      'ТабличнаяЧасть',
    'Формы':                'Форма',
    'ЗначенияПеречисления': 'ЗначениеПеречисления',
    'Команды':              'Команда',
    'Макеты':               'Макет',
    # English
    'Attributes':           'Реквизит',
    'Dimensions':           'Измерение',
    'Resources':            'Ресурс',
    'TabularSections':      'ТабличнаяЧасть',
    'Forms':                'Форма',
    'EnumValues':           'ЗначениеПеречисления',
    'Commands':             'Команда',
    'Templates':            'Макет',
    'URLTemplates':         'URLШаблон',
}

# Markers that introduce tabular section blocks (contain nested attributes)
_TAB_SECTION_MARKERS = {'ТабличныеЧасти', 'TabularSections'}


# ============================================
# PARSER
# ============================================

class ReportParser:
    """
    Parses 1C configuration report line by line.
    
    The report uses tab-indentation for hierarchy:
    - "\\t\\t- Type.Name" defines an object
    - "\\t\\t\\tKey: \\"Value\\"" defines a property
    - "\\t\\t\\tТип:" followed by "\\t\\t\\t\\t\\"TypeDesc\\"" on next lines
    """

    def __init__(self):
        self.config = ConfigInfo()
        self.objects: list[MetadataObject] = []
        self._current_object: MetadataObject | None = None
        self._current_attribute: AttributeInfo | None = None
        self._current_enum_value: EnumValueInfo | None = None
        self._current_tabsection_name: str | None = None
        self._current_form: FormInfo | None = None
        self._current_subsystem: SubsystemInfo | None = None
        self._current_module_info: CommonModuleInfo | None = None
        self._collecting_type: bool = False
        self._collecting_array: str | None = None
        self._array_values: list[str] = []
        self._current_context: str = 'config'  # config | object | attribute | form | enum_value | tabsection_attr
        self._context_stack: list[str] = []

    def parse_file(self, filepath: str | Path) -> tuple[ConfigInfo, list[MetadataObject]]:
        """Parse report file, return config info and list of objects."""
        filepath = Path(filepath)

        # Try UTF-16LE first (standard 1C export), fallback to UTF-8
        for encoding in ('utf-16-le', 'utf-16', 'utf-8-sig', 'utf-8', 'cp1251'):
            try:
                with open(filepath, 'r', encoding=encoding) as f:
                    content = f.read()
                if content and not content.startswith('\x00'):
                    break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            raise ValueError(f"Cannot decode file {filepath}")

        lines = content.splitlines()
        self._parse_lines(lines)
        return self.config, self.objects

    def _parse_lines(self, lines: list[str]):
        """Process all lines."""
        for i, raw_line in enumerate(lines):
            line = raw_line.rstrip('\r\n')
            if not line.strip():
                continue

            # Count leading tabs
            indent = 0
            for ch in line:
                if ch == '\t':
                    indent += 1
                else:
                    break
            content = line[indent:]

            # Skip empty content
            if not content:
                continue

            # Are we collecting multi-line type or array values?
            if self._collecting_type:
                if content.startswith('"') and content.endswith('"'):
                    type_val = content[1:-1]
                    self._apply_type(type_val)
                    continue
                else:
                    self._collecting_type = False
                    # Fall through to normal processing

            if self._collecting_array is not None:
                if content.startswith('"') and content.endswith('"'):
                    self._array_values.append(content[1:-1])
                    continue
                else:
                    self._flush_array()
                    # Fall through

            # Object definition line: "- Справочники.ВредоносныеОбъекты"
            if content.startswith('- '):
                self._handle_object_line(content[2:], indent)
                continue

            # Property line: 'Имя: "Значение"'
            prop_match = re.match(r'^(.+?):\s*"(.*)"$', content)
            if prop_match:
                key = prop_match.group(1).strip()
                value = prop_match.group(2)
                self._handle_property(key, value, indent)
                continue

            # Section start (array or multi-line): "Состав:" / "Тип:"
            if content.endswith(':') and not content.startswith('"'):
                section_name = content[:-1].strip()
                self._handle_section_start(section_name, indent)
                continue

            # Bare keyword without colon/quotes (e.g. "СтандартныеРеквизиты", "Характеристики")
            # — just ignore these

    # ============================================
    # OBJECT DEFINITION HANDLER
    # ============================================

    def _handle_object_line(self, path: str, indent: int):
        """Handle '- Type.Name[.Child.ChildName...]' line."""
        parts = path.split('.')

        if len(parts) < 2:
            return

        plural_type = parts[0]

        # Is this a top-level object (like Справочники.Номенклатура)?
        if plural_type in OBJECT_TYPE_MAP:
            singular, eng_kind = OBJECT_TYPE_MAP[plural_type]

            # Check if this is a child element (Реквизиты, Формы, etc.)
            child_kind = self._detect_child_kind(parts)

            if child_kind is None:
                # Top-level object definition
                self._flush_current()
                obj_name = parts[-1]

                # Handle nested subsystems: Подсистемы.Parent.Подсистемы.Child
                if plural_type == 'Подсистемы' and len(parts) > 2:
                    obj_name = parts[-1]
                    full_name = f"{singular}.{'.'.join(parts[1:])}"
                else:
                    full_name = f"{singular}.{parts[1]}" if len(parts) == 2 else f"{singular}.{'.'.join(parts[1:])}"

                self._current_object = MetadataObject(
                    name=obj_name,
                    full_name=full_name,
                    kind=singular,
                )

                if singular == 'ОбщийМодуль':
                    self._current_object.module_info = CommonModuleInfo(name=obj_name)
                elif singular == 'Подсистема':
                    self._current_object.subsystem_info = SubsystemInfo(name=obj_name)

                self._current_context = 'object'
                self._current_attribute = None
                self._current_form = None
                self._current_enum_value = None
                self._current_tabsection_name = None

            elif child_kind == 'Реквизит' or child_kind == 'Измерение' or child_kind == 'Ресурс':
                self._handle_attribute_def(parts, child_kind)

            elif child_kind == 'ТабличнаяЧасть':
                self._handle_tabsection_def(parts)

            elif child_kind == 'ТабЧастьРеквизит':
                self._handle_tabsection_attr_def(parts)

            elif child_kind == 'Форма':
                self._handle_form_def(parts)

            elif child_kind == 'ЗначениеПеречисления':
                self._handle_enum_value_def(parts)
        else:
            # Possibly nested subsystem: Подсистемы.X.Подсистемы.Y
            # Already handled above — if we get here, it's something unknown
            pass

    def _detect_child_kind(self, parts: list[str]) -> str | None:
        """
        Detect what kind of child element this is based on path parts.
        Returns None for top-level objects.
        Handles both Russian and English report variants.

        Examples:
        ['Справочники', 'Номенклатура'] → None (top-level)
        ['Catalogs', 'Account', 'Attributes', 'Username'] → 'Реквизит'
        ['Catalogs', 'EventType', 'TabularSections', 'RegisterEvent'] → 'ТабличнаяЧасть'
        ['Catalogs', 'EventType', 'TabularSections', 'RegisterEvent', 'Attributes', 'Qty'] → 'ТабЧастьРеквизит'
        ['Catalogs', 'Account', 'Forms', 'ItemForm'] → 'Форма'
        ['Enums', 'AccountType', 'EnumValues', 'Personal'] → 'ЗначениеПеречисления'
        ['Subsystems', 'ProcessMgmt', 'Subsystems', 'Processes'] → None (nested subsystem = top-level)
        """
        if len(parts) <= 2:
            return None

        # Check for nested subsystem: Subsystems.X.Subsystems.Y  or  Подсистемы.X.Подсистемы.Y
        subsystem_plurals = {'Подсистемы', 'Subsystems'}
        if parts[0] in subsystem_plurals:
            for i in range(2, len(parts)):
                if parts[i] in subsystem_plurals:
                    continue  # nested subsystem level
                if parts[i] in CHILD_KIND_MAP:
                    return CHILD_KIND_MAP[parts[i]]
            return None  # All parts are subsystem nesting

        # Look for the DEEPEST child kind marker (scan from end)
        last_marker = None
        last_marker_idx = -1
        for i in range(2, len(parts)):
            marker = parts[i]
            if marker in CHILD_KIND_MAP:
                last_marker = marker
                last_marker_idx = i

        if last_marker is None:
            return None

        # Is this a tabular section attribute?
        # Pattern: [..., 'TabularSections'/'ТабличныеЧасти', TSName, 'Attributes'/'Реквизиты', AttrName]
        if last_marker in ('Реквизиты', 'Attributes'):
            for j in range(2, last_marker_idx):
                if parts[j] in _TAB_SECTION_MARKERS:
                    return 'ТабЧастьРеквизит'

        return CHILD_KIND_MAP[last_marker]

    # ============================================
    # CHILD ELEMENT HANDLERS
    # ============================================

    def _handle_attribute_def(self, parts: list[str], kind: str):
        """Handle attribute/dimension/resource definition."""
        attr_name = parts[-1]
        self._current_attribute = AttributeInfo(name=attr_name, kind=kind)
        self._current_context = 'attribute'
        self._current_form = None
        self._current_enum_value = None

    def _handle_tabsection_def(self, parts: list[str]):
        """Handle tabular section definition."""
        ts_name = parts[-1]
        self._current_tabsection_name = ts_name
        if self._current_object and ts_name not in self._current_object.tabular_sections:
            self._current_object.tabular_sections[ts_name] = []
        self._current_attribute = None
        self._current_context = 'tabsection'

    def _handle_tabsection_attr_def(self, parts: list[str]):
        """Handle attribute inside a tabular section."""
        attr_name = parts[-1]
        # Find tabular section name: ...TabularSections/ТабличныеЧасти.TSName.Attributes/Реквизиты.AttrName
        ts_idx = None
        for i, p in enumerate(parts):
            if p in _TAB_SECTION_MARKERS and i + 1 < len(parts):
                ts_idx = i + 1
                break
        if ts_idx:
            self._current_tabsection_name = parts[ts_idx]

        self._current_attribute = AttributeInfo(name=attr_name, kind='Реквизит')
        self._current_context = 'tabsection_attr'

    def _handle_form_def(self, parts: list[str]):
        """Handle form definition."""
        form_name = parts[-1]
        self._current_form = FormInfo(name=form_name)
        self._current_context = 'form'
        self._current_attribute = None

    def _handle_enum_value_def(self, parts: list[str]):
        """Handle enum value definition."""
        val_name = parts[-1]
        self._current_enum_value = EnumValueInfo(name=val_name)
        self._current_context = 'enum_value'
        self._current_attribute = None

    # ============================================
    # PROPERTY HANDLER
    # ============================================

    def _handle_property(self, key: str, value: str, indent: int):
        """Handle 'Key: "Value"' property line."""

        # Configuration-level properties (indent == 2, before first object)
        if self._current_object is None:
            self._handle_config_property(key, value)
            return

        # Skip context: inside СтандартныеРеквизиты child or tabsection header
        if self._current_context in ('skip', 'tabsection'):
            return

        # Dispatch based on context
        if self._current_context == 'attribute' and self._current_attribute:
            self._handle_attribute_property(key, value)
        elif self._current_context == 'tabsection_attr' and self._current_attribute:
            self._handle_attribute_property(key, value)
        elif self._current_context == 'form' and self._current_form:
            self._handle_form_property(key, value)
        elif self._current_context == 'enum_value' and self._current_enum_value:
            self._handle_enum_value_property(key, value)
        elif self._current_context in ('object', 'tabsection'):
            self._handle_object_property(key, value)

    def _handle_config_property(self, key: str, value: str):
        """Handle top-level configuration property (Russian and English)."""
        match key:
            case 'Имя' | 'Name':
                self.config.name = value
            case 'Синоним' | 'Synonym':
                self.config.synonym = value
            case 'Версия' | 'Version':
                self.config.version = value
            case 'Комментарий' | 'Comment':
                self.config.comment = value
            case 'РежимСовместимости' | 'CompatibilityMode':
                self.config.compatibility_mode = value

    def _handle_object_property(self, key: str, value: str):
        """Handle property of current object (Russian and English)."""
        obj = self._current_object
        if not obj:
            return

        # True/False are 'Истина'/'Ложь' in RU reports, 'True'/'False' in EN reports
        bool_true = value in ('Истина', 'True')

        match key:
            case 'Имя' | 'Name':
                obj.name = value
            case 'Синоним' | 'Synonym':
                obj.synonym = value
            case 'Комментарий' | 'Comment':
                obj.comment = value

        # Common module specific properties
        if obj.module_info:
            mi = obj.module_info
            match key:
                # Russian property names
                case 'Глобальный':
                    mi.is_global = bool_true
                case 'КлиентУправляемоеПриложение':
                    mi.is_client = bool_true
                case 'Сервер':
                    mi.is_server = bool_true
                case 'ВнешнееСоединение':
                    mi.is_external = bool_true
                case 'ВызовСервера':
                    mi.server_call = bool_true
                case 'Привилегированный':
                    mi.is_privileged = bool_true
                case 'ПовторноеИспользованиеВозвращаемыхЗначений':
                    mi.reuse = value
                # English property names
                case 'Global':
                    mi.is_global = bool_true
                case 'ClientManagedApplication':
                    mi.is_client = bool_true
                case 'Server':
                    mi.is_server = bool_true
                case 'ExternalConnection':
                    mi.is_external = bool_true
                case 'ServerCall':
                    mi.server_call = bool_true
                case 'Privileged':
                    mi.is_privileged = bool_true
                case 'ReturnValuesReuse':
                    mi.reuse = value

        # Store all properties
        obj.properties[key] = value

    def _handle_attribute_property(self, key: str, value: str):
        """Handle property of current attribute (Russian and English)."""
        attr = self._current_attribute
        if not attr:
            return
        match key:
            case 'Имя' | 'Name':
                attr.name = value
            case 'Синоним' | 'Synonym':
                attr.synonym = value
            case 'Комментарий' | 'Comment':
                attr.comment = value
            case 'Индексирование':
                attr.is_indexed = (value != 'НеИндексировать')
            case 'Indexing':
                attr.is_indexed = (value != 'DontIndex')
            case 'ПроверкаЗаполнения' | 'FillChecking':
                attr.check_fill = value
            case 'Использование' | 'Use':
                attr.usage = value

    def _handle_form_property(self, key: str, value: str):
        """Handle form property (Russian and English)."""
        if not self._current_form:
            return
        match key:
            case 'Имя' | 'Name':
                self._current_form.name = value
            case 'Синоним' | 'Synonym':
                self._current_form.synonym = value

    def _handle_enum_value_property(self, key: str, value: str):
        """Handle enum value property (Russian and English)."""
        if not self._current_enum_value:
            return
        match key:
            case 'Имя' | 'Name':
                self._current_enum_value.name = value
            case 'Синоним' | 'Synonym':
                self._current_enum_value.synonym = value
            case 'Комментарий' | 'Comment':
                self._current_enum_value.comment = value

    # ============================================
    # SECTION / ARRAY / TYPE HANDLERS
    # ============================================

    def _handle_section_start(self, section_name: str, indent: int):
        """Handle 'SectionName:' lines (Тип:/Type:, Состав:/Content:, etc.)."""
        if section_name in ('Тип', 'Type'):
            self._collecting_type = True
        elif section_name in (
            # Russian array sections
            'Состав', 'ВводПоСтроке', 'Владельцы',
            'ПоляБлокировкиДанных', 'ОсновныеРоли',
            'РолиОграниченияАвтономнойКонфигурации',
            'ДополнительныеСловариПолнотекстовогоПоиска',
            'НавигационныеСсылкиМобильногоПриложения',
            'ДопустимыеТипыВходящихЗапросовПоделиться',
            'СвязиПараметровВыбора',
            # English array sections
            'Content', 'InputByString', 'Owners',
            'DataLockFields', 'MainRoles',
            'StandaloneConfigurationRestrictionRoles',
            'AdditionalFullTextSearchDictionaries',
            'MobileApplicationURLs',
            'AllowedIncomingShareRequestTypes',
            'ChoiceParameterLinks',
            'BasedOn', 'DefaultRoles',
        ):
            self._collecting_array = section_name
            self._array_values = []
            self._collecting_array = section_name
            self._array_values = []

    def _apply_type(self, type_val: str):
        """Apply collected type value."""
        if self._current_context in ('attribute', 'tabsection_attr') and self._current_attribute:
            if self._current_attribute.type_desc:
                self._current_attribute.type_desc += ', ' + type_val
            else:
                self._current_attribute.type_desc = type_val
        elif self._current_context == 'object' and self._current_object:
            # ОпределяемыйТип.Тип:
            if self._current_object.type_desc:
                self._current_object.type_desc += ', ' + type_val
            else:
                self._current_object.type_desc = type_val

    def _flush_array(self):
        """Flush collected array values to current context."""
        if self._collecting_array in ('Состав', 'Content') and self._current_object:
            if self._current_object.subsystem_info:
                self._current_object.subsystem_info.content = self._array_values[:]
        self._collecting_array = None
        self._array_values = []

    # ============================================
    # FLUSH / FINALIZE
    # ============================================

    def _flush_current(self):
        """Flush current attribute/form/enum_value to current object, then flush object."""
        self._flush_child()
        if self._current_object:
            self.objects.append(self._current_object)
            self._current_object = None

    def _flush_child(self):
        """Flush current child element (attribute, form, enum_value) to parent object."""
        obj = self._current_object
        if not obj:
            return

        if self._current_attribute:
            if self._current_context == 'tabsection_attr' and self._current_tabsection_name:
                ts = self._current_tabsection_name
                if ts not in obj.tabular_sections:
                    obj.tabular_sections[ts] = []
                obj.tabular_sections[ts].append(self._current_attribute)
            elif self._current_context == 'attribute':
                obj.attributes.append(self._current_attribute)
            self._current_attribute = None

        if self._current_form:
            obj.forms.append(self._current_form)
            self._current_form = None

        if self._current_enum_value:
            obj.enum_values.append(self._current_enum_value)
            self._current_enum_value = None

    def _handle_object_line(self, path: str, indent: int):
        """Handle '- Type.Name[.Child.ChildName...]' line."""
        # Flush previous child before processing new line
        self._flush_child()

        parts = path.split('.')
        if len(parts) < 2:
            # Single name like "- Владелец" under СтандартныеРеквизиты
            # Switch to skip context so its properties don't overwrite parent object
            self._current_context = 'skip'
            return

        plural_type = parts[0]

        if plural_type in OBJECT_TYPE_MAP:
            singular, eng_kind = OBJECT_TYPE_MAP[plural_type]
            child_kind = self._detect_child_kind(parts)

            if child_kind is None:
                # New top-level object — flush previous
                if self._current_object:
                    self.objects.append(self._current_object)

                obj_name = parts[-1]
                full_name = f"{singular}.{'.'.join(parts[1:])}"

                self._current_object = MetadataObject(
                    name=obj_name,
                    full_name=full_name,
                    kind=singular,
                )

                if singular == 'ОбщийМодуль':
                    self._current_object.module_info = CommonModuleInfo(name=obj_name)
                elif singular == 'Подсистема':
                    self._current_object.subsystem_info = SubsystemInfo(name=obj_name)

                self._current_context = 'object'
                self._current_attribute = None
                self._current_form = None
                self._current_enum_value = None
                self._current_tabsection_name = None

            elif child_kind in ('Реквизит', 'Измерение', 'Ресурс'):
                self._handle_attribute_def(parts, child_kind)
            elif child_kind == 'ТабличнаяЧасть':
                self._handle_tabsection_def(parts)
            elif child_kind == 'ТабЧастьРеквизит':
                self._handle_tabsection_attr_def(parts)
            elif child_kind == 'Форма':
                self._handle_form_def(parts)
            elif child_kind == 'ЗначениеПеречисления':
                self._handle_enum_value_def(parts)
            else:
                # Unhandled child kind (Команда, Макет, URLШаблон, etc.) —
                # switch to skip context so its Name/Synonym don't overwrite the parent object
                self._current_context = 'skip' 

    def finalize(self):
        """Call after parsing all lines to flush remaining data."""
        self._flush_child()
        if self._collecting_array is not None:
            self._flush_array()
        if self._current_object:
            self.objects.append(self._current_object)
            self._current_object = None

    def _parse_lines(self, lines: list[str]):
        """Process all lines."""
        # Reset the duplicate method — use only this one
        self._current_object = None
        self._current_attribute = None
        self._current_form = None
        self._current_enum_value = None
        self._current_tabsection_name = None
        self._collecting_type = False
        self._collecting_array = None
        self._current_context = 'config'
        self.objects = []

        for i, raw_line in enumerate(lines):
            line = raw_line.rstrip('\r\n')
            if not line.strip():
                continue

            indent = 0
            for ch in line:
                if ch == '\t':
                    indent += 1
                else:
                    break
            content = line[indent:]

            if not content:
                continue

            # Collecting multi-line type?
            if self._collecting_type:
                if content.startswith('"') and content.endswith('"'):
                    self._apply_type(content[1:-1])
                    continue
                else:
                    self._collecting_type = False

            # Collecting array?
            if self._collecting_array is not None:
                if content.startswith('"') and content.endswith('"'):
                    self._array_values.append(content[1:-1])
                    continue
                else:
                    self._flush_array()

            # Object definition
            if content.startswith('- '):
                self._handle_object_line(content[2:], indent)
                continue

            # Property: Key: "Value"
            prop_match = re.match(r'^(.+?):\s*"(.*)"$', content)
            if prop_match:
                self._handle_property(prop_match.group(1).strip(), prop_match.group(2), indent)
                continue

            # Multi-line property start with value on same line
            # e.g.: ИспользуемаяФункциональностьМобильногоПриложения: "Функциональность:
            ml_match = re.match(r'^(.+?):\s*"(.+)$', content)
            if ml_match and not content.endswith('"'):
                # Multi-line value — skip, not critical for metadata
                continue

            # Section start: 'Keyword:'
            if content.endswith(':') and not content.startswith('"'):
                self._handle_section_start(content[:-1].strip(), indent)
                continue

        self.finalize()


# ============================================
# CONVENIENCE FUNCTION
# ============================================

def parse_report(filepath: str | Path) -> tuple[ConfigInfo, list[MetadataObject]]:
    """Parse a 1C configuration report file."""
    parser = ReportParser()
    return parser.parse_file(filepath)
