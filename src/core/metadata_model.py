"""
Модель метаданных конфигурации: объекты, реквизиты, формы, общие модули,
подсистемы — то, что строит разбор XML-выгрузки (xml_metadata) и пишет
индексатор; таблица видов объектов (RU/EN) и приведение фильтра kind к
русскому виду (canonicalize_kind).
"""

from dataclasses import dataclass, field


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
    # Документ: регистры, по которым он делает движения («РегистрНакопления.X»);
    # только из XML-выгрузки (RegisterRecords), см. register_info.
    register_records: list[str] = field(default_factory=list)


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
