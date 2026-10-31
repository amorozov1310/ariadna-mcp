"""
Сведения для запроса к данным (get_object_details): вид регистра,
периодичность и режим записи, план счетов, движения документа — и как это
выглядит в языке запросов («РегистрНакопления.X.Остатки(&Период)», поле
«СуммаОстаток», «РегистрСведений.X.СрезПоследних»). Агенту, который
отвечает на вопрос к базе через свой MCP к данным, это нужно, чтобы сразу
составить правильный запрос, а не гадать по составу реквизитов.

Свойства приходят из XML-выгрузки (RegisterType, InformationRegisterPeriodicity…)
или из отчёта по конфигурации (ВидРегистра, ПериодичностьРегистраСведений…,
русские или английские) — object_properties приводит их к одному виду.
Движения документов (RegisterRecords) есть только в XML-выгрузке.
"""

from .report_parser import MetadataObject

REGISTER_TYPE = 'ВидРегистра'
PERIODICITY = 'Периодичность'
WRITE_MODE = 'РежимЗаписи'
CHART_OF_ACCOUNTS = 'ПланСчетов'
CORRESPONDENCE = 'Корреспонденция'
RECORDS = 'Движения'

# Ключ свойства в выгрузке или отчёте (RU/EN) → ключ в индексе.
_KEYS = {
    'RegisterType': REGISTER_TYPE, 'ВидРегистра': REGISTER_TYPE,
    'InformationRegisterPeriodicity': PERIODICITY, 'ПериодичностьРегистраСведений': PERIODICITY,
    'WriteMode': WRITE_MODE, 'РежимЗаписи': WRITE_MODE,
    'ChartOfAccounts': CHART_OF_ACCOUNTS, 'ПланСчетов': CHART_OF_ACCOUNTS,
    'Correspondence': CORRESPONDENCE, 'Корреспонденция': CORRESPONDENCE,
}
# Значение (EN из выгрузки или отчёта, RU из отчёта с пробелами) → значение в индексе.
_VALUES = {
    'balance': 'Остатки', 'остатки': 'Остатки',
    'turnovers': 'Обороты', 'обороты': 'Обороты',
    'nonperiodical': 'Непериодический', 'непериодический': 'Непериодический',
    'second': 'Секунда', 'впределахсекунды': 'Секунда', 'секунда': 'Секунда',
    'day': 'День', 'день': 'День', 'month': 'Месяц', 'месяц': 'Месяц',
    'quarter': 'Квартал', 'квартал': 'Квартал', 'year': 'Год', 'год': 'Год',
    'recorderposition': 'ПозицияРегистратора', 'позициярегистратора': 'ПозицияРегистратора',
    'independent': 'Независимый', 'независимый': 'Независимый',
    'recordersubordinate': 'ПодчинениеРегистратору', 'подчинениерегистратору': 'ПодчинениеРегистратору',
    'true': 'Да', 'истина': 'Да', 'false': 'Нет', 'ложь': 'Нет',
}


def _value(raw: str) -> str:
    value = raw.strip()
    if value.startswith('ChartOfAccounts.'):
        return 'ПланСчетов.' + value[len('ChartOfAccounts.'):]
    return _VALUES.get(value.replace(' ', '').casefold(), value)


def object_properties(obj: MetadataObject) -> list[tuple[str, str]]:
    """(ключ, значение) для таблицы object_properties: свойства регистра и
    движения документа (по строке на регистр)."""
    rows = [(_KEYS[k], _value(v)) for k, v in obj.properties.items() if k in _KEYS and v and v.strip()]
    rows += [(RECORDS, r) for r in obj.register_records]
    return rows


def _resource_fields(resources: list[str], suffixes: tuple[str, ...]) -> str:
    """«СуммаОстаток, КоличествоОстаток» — поля виртуальной таблицы по ресурсам
    (первые три, чтобы строка не разрасталась)."""
    names = resources[:3]
    fields = ', '.join(f'{r}{s}' for s in suffixes for r in names)
    return fields + (', …' if len(resources) > 3 else '')


def query_hints(full_name: str, kind: str, props: dict[str, list[str]],
                resources: list[str]) -> list[str]:
    """Строки «для запроса» по виду объекта; пусто — подсказать нечего."""
    one = {k: v[0] for k, v in props.items() if v}
    lines = []
    if kind == 'РегистрНакопления' and REGISTER_TYPE in one:
        if one[REGISTER_TYPE] == 'Остатки':
            lines.append('Вид регистра: остатки')
            lines.append(f'В запросе: {full_name}.Остатки(&Период, Условие) — '
                         f'{_resource_fields(resources, ("Остаток",)) or "<Ресурс>Остаток"}; '
                         f'.Обороты(&Начало, &Конец, Периодичность, Условие) — '
                         f'{_resource_fields(resources, ("Приход", "Расход")) or "<Ресурс>Приход, <Ресурс>Расход"}; '
                         f'.ОстаткиИОбороты(...); движения — {full_name}')
        else:
            lines.append('Вид регистра: обороты')
            lines.append(f'В запросе: {full_name}.Обороты(&Начало, &Конец, Периодичность, Условие) — '
                         f'{_resource_fields(resources, ("Оборот",)) or "<Ресурс>Оборот"}; '
                         f'движения — {full_name}')
    elif kind == 'РегистрСведений' and PERIODICITY in one:
        mode = {'ПодчинениеРегистратору': 'подчинён регистратору',
                'Независимый': 'независимый'}.get(one.get(WRITE_MODE, ''), '')
        periodicity = one[PERIODICITY]
        lines.append(f'Периодичность: {periodicity}' + (f'; запись: {mode}' if mode else ''))
        if periodicity == 'Непериодический':
            lines.append(f'В запросе: {full_name} — сама таблица, без среза по дате')
        else:
            lines.append(f'В запросе: {full_name}.СрезПоследних(&Период, Условие) — '
                         f'значения на дату; .СрезПервых(...); вся история — {full_name} (поле Период)')
    elif kind == 'РегистрБухгалтерии':
        head = []
        if CHART_OF_ACCOUNTS in one:
            head.append(f'План счетов: {one[CHART_OF_ACCOUNTS]}')
        if one.get(CORRESPONDENCE) == 'Да':
            head.append('с корреспонденцией')
        if head:
            lines.append('; '.join(head))
        lines.append(f'В запросе: {full_name}.Остатки(&Период, СчетУсловие, Субконто, Условие) — '
                     f'{_resource_fields(resources, ("ОстатокДт", "ОстатокКт")) or "<Ресурс>ОстатокДт"}; '
                     f'.Обороты(&Начало, &Конец, Периодичность, СчетУсловие, Субконто, Условие, '
                     f'КорСчетУсловие, КорСубконто); .ОстаткиИОбороты(...); .ДвиженияССубконто(...)')
    return lines
