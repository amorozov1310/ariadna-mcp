"""
Стандартные реквизиты объекта для get_object_details: есть ли у справочника
Код и какой длины Наименование, иерархический ли он (Родитель, ЭтоГруппа),
кто Владелец; у документа — Номер и Проведен; у планов видов
характеристик, счетов, видов расчета, обмена, бизнес-процессов и задач —
их стандартные поля. Агент, который пишет запрос к базе, без этого угадывает
«Код» и «ЭтоГруппа» там, где их нет, и получает «Поле не найдено».

Свойства — из XML-выгрузки (CodeLength, DescriptionLength, Hierarchical,
HierarchyType, Owners, NumberLength, Posting…, см. RAW_PROPERTIES);
standard_rows приводит их к русским ключам и значениям для таблицы
object_properties, standard_attributes_line собирает строку карточки.
"""

from .metadata_model import MetadataObject

# Англ. вид (тег выгрузки) → свойства, которые разбор XML кладёт в
# MetadataObject.properties. Owners справочника — отдельно (список ссылок).
RAW_PROPERTIES: dict[str, tuple[str, ...]] = {
    'Catalog': ('CodeLength', 'CodeType', 'DescriptionLength', 'Hierarchical', 'HierarchyType'),
    'Document': ('NumberLength', 'NumberType', 'Posting'),
    'ChartOfCharacteristicTypes': ('CodeLength', 'DescriptionLength', 'Hierarchical'),
    'ChartOfAccounts': ('CodeLength', 'DescriptionLength', 'OrderLength', 'MaxExtDimensionCount',
                        'ExtDimensionTypes'),
    'ChartOfCalculationTypes': ('CodeLength', 'CodeType', 'DescriptionLength',
                                'DependenceOnCalculationTypes', 'ActionPeriodUse'),
    'ExchangePlan': ('CodeLength', 'DescriptionLength'),
    'BusinessProcess': ('NumberLength', 'NumberType'),
    'Task': ('NumberLength', 'NumberType', 'DescriptionLength'),
}
# Свойство справочника со списком владельцев (полные имена через запятую).
OWNERS = 'Owners'

CODE_LENGTH = 'ДлинаКода'
CODE_TYPE = 'ТипКода'
DESCRIPTION_LENGTH = 'ДлинаНаименования'
HIERARCHICAL = 'Иерархический'
HIERARCHY_TYPE = 'ВидИерархии'
OWNER = 'Владелец'
NUMBER_LENGTH = 'ДлинаНомера'
NUMBER_TYPE = 'ТипНомера'
POSTING = 'Проведение'
ORDER_LENGTH = 'ДлинаПорядка'
EXT_DIMENSION_COUNT = 'КоличествоСубконто'
EXT_DIMENSION_TYPES = 'ВидыСубконто'
DEPENDENCE = 'ЗависимостьОтВидовРасчета'
ACTION_PERIOD = 'ПериодДействия'

_KEYS = {
    'CodeLength': CODE_LENGTH, 'CodeType': CODE_TYPE, 'DescriptionLength': DESCRIPTION_LENGTH,
    'Hierarchical': HIERARCHICAL, 'HierarchyType': HIERARCHY_TYPE,
    'NumberLength': NUMBER_LENGTH, 'NumberType': NUMBER_TYPE, 'Posting': POSTING,
    'OrderLength': ORDER_LENGTH, 'MaxExtDimensionCount': EXT_DIMENSION_COUNT,
    'ExtDimensionTypes': EXT_DIMENSION_TYPES,
    'DependenceOnCalculationTypes': DEPENDENCE, 'ActionPeriodUse': ACTION_PERIOD,
}
_VALUES = {
    'true': 'Да', 'false': 'Нет', 'String': 'Строка', 'Number': 'Число',
    'HierarchyFoldersAndItems': 'ГруппыИЭлементы', 'HierarchyOfItems': 'Элементы',
    'Allow': 'Разрешить', 'Deny': 'Запретить',
    'DontUse': 'НеИспользовать', 'OnActionPeriod': 'ПоПериодуДействия',
    'OnRegistrationPeriod': 'ПоПериодуРегистрации',
}
_REF_PREFIXES = {'ChartOfCharacteristicTypes.': 'ПланВидовХарактеристик.'}

# Русский вид → англ. (только виды со стандартными реквизитами).
_KIND_EN = {
    'Справочник': 'Catalog', 'Документ': 'Document',
    'ПланВидовХарактеристик': 'ChartOfCharacteristicTypes', 'ПланСчетов': 'ChartOfAccounts',
    'ПланВидовРасчета': 'ChartOfCalculationTypes', 'ПланОбмена': 'ExchangePlan',
    'БизнесПроцесс': 'BusinessProcess', 'Задача': 'Task',
}
# Ключи индекса, по которым видно, что сведения о стандартных реквизитах есть
# (индекс, построенный до их появления, их не содержит — строки не будет).
_MARKERS = {CODE_LENGTH, DESCRIPTION_LENGTH, NUMBER_LENGTH, POSTING}


def _value(raw: str) -> str:
    value = raw.strip()
    for prefix, ru in _REF_PREFIXES.items():
        if value.startswith(prefix):
            return ru + value[len(prefix):]
    return _VALUES.get(value, value)


def standard_rows(obj: MetadataObject) -> list[tuple[str, str]]:
    """(ключ, значение) стандартных свойств объекта для object_properties;
    владельцы справочника — по строке на каждого."""
    kind_en = _KIND_EN.get(obj.kind)
    if not kind_en:
        return []
    rows = []
    for raw_key in RAW_PROPERTIES[kind_en]:
        value = obj.properties.get(raw_key, '')
        if value and value.strip():
            rows.append((_KEYS[raw_key], _value(value)))
    if kind_en == 'Catalog':
        rows += [(OWNER, o.strip()) for o in obj.properties.get(OWNERS, '').split(',') if o.strip()]
    return rows


def _int(one: dict[str, str], key: str) -> int:
    try:
        return int(one.get(key, '0') or 0)
    except ValueError:
        return 0


def standard_attributes_line(kind: str, props: dict[str, list[str]]) -> str:
    """«Стандартные реквизиты: Ссылка, ПометкаУдаления, …; кода нет; …» —
    пусто, если вид без стандартных реквизитов или в индексе нет сведений."""
    kind_en = _KIND_EN.get(kind)
    if not kind_en or not _MARKERS & props.keys():
        return ''
    one = {k: v[0] for k, v in props.items() if v}
    fields = ['Ссылка', 'ПометкаУдаления']
    notes: list[str] = []
    absent: list[str] = []

    def code(default_type: str = 'Строка'):
        length = _int(one, CODE_LENGTH)
        if length:
            fields.append(f"Код ({one.get(CODE_TYPE, default_type)} {length})")
        else:
            notes.append('кода нет')
            absent.append('Код')

    def description():
        length = _int(one, DESCRIPTION_LENGTH)
        if length:
            fields.append(f'Наименование (Строка {length})')
        else:
            notes.append('наименования нет')
            absent.append('Наименование')

    def number():
        length = _int(one, NUMBER_LENGTH)
        if length:
            fields.insert(fields.index('Дата') + 1,
                          f"Номер ({one.get(NUMBER_TYPE, 'Строка')} {length})")
        else:
            notes.append('номера нет')
            absent.append('Номер')

    predefined = ['Предопределенный', 'ИмяПредопределенныхДанных']
    if kind_en == 'Catalog':
        code()
        description()
        if one.get(HIERARCHICAL) == 'Да':
            if one.get(HIERARCHY_TYPE) == 'Элементы':
                fields.append('Родитель')
                notes.append('иерархия элементов')
                absent.append('ЭтоГруппа')
            else:
                fields += ['Родитель', 'ЭтоГруппа']
                notes.append('иерархия групп и элементов')
        else:
            notes.append('не иерархический')
            absent += ['Родитель', 'ЭтоГруппа']
        owners = props.get(OWNER) or []
        if owners:
            fields.append(f"Владелец ({' или '.join(owners)})")
        else:
            notes.append('без владельца')
            absent.append('Владелец')
        fields += predefined
    elif kind_en == 'ChartOfCharacteristicTypes':
        code()
        description()
        fields.append('ТипЗначения')
        if one.get(HIERARCHICAL) == 'Да':
            fields += ['Родитель', 'ЭтоГруппа']
            notes.append('иерархия групп')
        else:
            notes.append('не иерархический')
            absent += ['Родитель', 'ЭтоГруппа']
        fields += predefined
    elif kind_en == 'ChartOfAccounts':
        code()
        description()
        fields.append('Родитель')
        if _int(one, ORDER_LENGTH):
            fields.append('Порядок')
        fields += ['Вид', 'Забалансовый'] + predefined
        count = _int(one, EXT_DIMENSION_COUNT)
        if count:
            types = one.get(EXT_DIMENSION_TYPES)
            notes.append(f'субконто: до {count}' + (f' ({types})' if types else '')
                         + ', табличная часть ВидыСубконто')
        else:
            notes.append('без субконто')
        absent.append('ЭтоГруппа')
    elif kind_en == 'ChartOfCalculationTypes':
        code()
        description()
        period = one.get(ACTION_PERIOD) == 'Да'
        if period:
            fields.append('ПериодДействияБазовый')
        fields += predefined
        sections = ['ВедущиеВидыРасчета']
        if one.get(DEPENDENCE, 'НеИспользовать') != 'НеИспользовать':
            sections.append('БазовыеВидыРасчета')
        if period:
            sections.append('ВытесняющиеВидыРасчета')
        notes.append(f"табличные части {', '.join(sections)}")
    elif kind_en == 'ExchangePlan':
        code()
        description()
        fields += ['НомерОтправленного', 'НомерПринятого', 'ЭтотУзел']
    elif kind_en == 'Document':
        fields.append('Дата')
        number()
        if one.get(POSTING, 'Разрешить') == 'Разрешить':
            fields.append('Проведен')
        else:
            notes.append('не проводится')
            absent.append('Проведен')
        fields.append('МоментВремени')
        absent += ['Код', 'Наименование']
    elif kind_en == 'BusinessProcess':
        fields.append('Дата')
        number()
        fields += ['ВедущаяЗадача', 'Стартован', 'Завершен']
    elif kind_en == 'Task':
        fields.append('Дата')
        number()
        description()
        fields += ['Выполнена', 'БизнесПроцесс', 'ТочкаМаршрута']

    line = 'Стандартные реквизиты: ' + ', '.join(fields)
    line += ''.join(f'; {n}' for n in notes)
    if absent:
        line += f"; {'поля' if len(absent) == 1 else 'полей'} {', '.join(absent)} нет"
    return line
