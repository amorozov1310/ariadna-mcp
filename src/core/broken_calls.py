"""Вызовы, которые упадут с «Метод объекта не обнаружен»: проверка кода по
индексу, без запуска.

  missing     — `ОбщийМодуль.Метод()` / `Справочники.Х.Метод()`: модуль в
                выгрузке есть, процедуры с таким именем в нём нет (ни в
                основной конфигурации, ни в расширениях). defined_in —
                где процедура с таким именем есть на самом деле.
  not_export  — процедура есть, но без Экспорт, а вызывается из другого
                модуля. Резолвер такой вызов разрешает (по имени), поэтому
                diagnose_index его не показывает.
  dynamic     — такой же вызов, но через переменную-модуль (`Модуль =
                ОбщегоНазначения.ОбщийМодуль("Имя"); Модуль.Метод()`): так библиотеки
                и типовые зовут методы, которых в этой версии библиотеки может
                не быть (так же — `Модуль = Вычислить("Имя")`), — платформа
                такие вызовы не проверяет, код обычно
                защищён проверкой ПодсистемаСуществует. Только счётчик.
  external    — модуля нет в выгрузке вообще: внешняя библиотека (тестовый
                фреймворк, не выгруженная подсистема) или опечатка в имени
                модуля. Сводкой по модулю — отдельно от ошибок, чтобы тысячи
                вызовов библиотеки не заслоняли настоящие.

Только вызовы через имя модуля (common_module / manager): локальный вызов
несуществующей процедуры парсер от вызова платформенного метода не отличит.
Классификация модуля — та же, что у search._unresolved_breakdown (ключ
резолвера, все источники, без учёта регистра).
"""


import re

from .bsl_reference import module_context_methods

# Платформенные методы менеджеров, которых нет в контексте модуля менеджера
# (bsl_reference) и в стоп-листе парсера: вызов `Справочники.Х.Метод()` с
# таким именем — платформа, а не процедура модуля менеджера. Здесь, а не в
# парсере: его правка переразбирает все индексы (parser_fingerprint).
_PLATFORM_MANAGER_EXTRA = {s.lower() for s in (
    'ПолучитьДанныеВыбора', 'GetChoiceData', 'ПолучитьФормуВыбора', 'GetChoiceForm',
    'ПолучитьФормуСписка', 'GetListForm', 'ПолучитьФормуОбъекта', 'GetObjectForm',
    'ПолучитьФормуНовогоОбъекта', 'GetNewObjectForm', 'ПолучитьИменаПредопределенных',
    'GetPredefinedItemNames', 'Индекс', 'IndexOf', 'Получить', 'Get',
    'ПолучитьПредставлениеПоляДанных', 'ПолучитьФормуЗаписи', 'GetRecordForm',
    'Остатки', 'Balance', 'Обороты', 'Turnovers', 'ОстаткиИОбороты', 'BalanceAndTurnovers',
    'ОборотыДтКт', 'TurnoversDrCr', 'ДвиженияССубконто', 'RecordsWithExtDimensions',
    'ВыбратьПоРегистратору', 'SelectByRecorder', 'СрезПоследних', 'SliceLast',
    'СрезПервых', 'SliceFirst', 'ПолучитьПоследнее', 'GetLast', 'ПолучитьПервое', 'GetFirst',
    'Создать', 'Create', 'ПолучитьСсылку', 'GetRef', 'ПустаяСсылка', 'EmptyRef',
    'Выбрать', 'Select', 'ВыбратьИерархически', 'SelectHierarchically',
    'НайтиПоКоду', 'FindByCode', 'НайтиПоНаименованию', 'FindByDescription',
    'НайтиПоНомеру', 'FindByNumber', 'НайтиПоРеквизиту', 'FindByAttribute',
    'ПолучитьМакет', 'GetTemplate', 'ПолучитьФорму', 'GetForm', 'ЭтотУзел', 'ThisNode',
    'СоздатьКлючЗаписи', 'CreateRecordKey', 'ПустойКлюч', 'EmptyKey',
    'СоздатьНаборЗаписей', 'CreateRecordSet', 'СоздатьМенеджерЗаписи', 'CreateRecordManager',
    'Количество', 'Count', 'ПолучитьИмяПредопределенного', 'GetPredefinedItemName',
    'УстановитьИнициализациюПредопределенныхДанных', 'SetPredefinedDataInitialization',
    'ПолучитьИнициализациюПредопределенныхДанных', 'GetPredefinedDataInitialization',
    # Итоги регистров накопления и бухгалтерии
    'УстановитьИспользованиеИтогов', 'SetTotalsUse', 'ПолучитьИспользованиеИтогов', 'GetTotalsUse',
    'УстановитьИспользованиеТекущихИтогов', 'SetPresentTotalsUse',
    'ПолучитьИспользованиеТекущихИтогов', 'GetPresentTotalsUse',
    'УстановитьИспользованиеРазделенияИтогов', 'SetTotalsSplittingMode',
    'ПолучитьИспользованиеРазделенияИтогов', 'GetTotalsSplittingMode',
    'ПересчитатьИтоги', 'RecalcTotals', 'ПересчитатьТекущиеИтоги', 'RecalcPresentTotals',
    'ПересчитатьИтогиЗаПериод', 'RecalcTotalsForPeriod',
    'УстановитьПериодРассчитанныхИтогов', 'SetTotalsPeriod',
    'ПолучитьПериодРассчитанныхИтогов', 'GetTotalsPeriod',
    'УстановитьМаксимальныйПериодРассчитанныхИтогов', 'SetMaxTotalsPeriod',
    'ПолучитьМаксимальныйПериодРассчитанныхИтогов', 'GetMaxTotalsPeriod',
    'УстановитьМинимальныйПериодРассчитанныхИтогов', 'SetMinTotalsPeriod',
    'ПолучитьМинимальныйПериодРассчитанныхИтогов', 'GetMinTotalsPeriod')}


_KINDS_CF = {k.casefold(): module_context_methods(k, 'МодульМенеджера')
             for k in ('Справочник', 'Документ', 'Перечисление', 'ПланВидовХарактеристик',
                       'ПланСчетов', 'ПланОбмена', 'БизнесПроцесс', 'Задача', 'РегистрСведений',
                       'РегистрНакопления', 'РегистрБухгалтерии', 'РегистрРасчета', 'Отчет',
                       'Обработка')}


def _platform_manager_method(module_cf: str, proc_cf: str) -> bool:
    kind = module_cf.split('.', 1)[0]
    return proc_cf in _PLATFORM_MANAGER_EXTRA or proc_cf in _KINDS_CF.get(kind, set())


def _direct(context: str, module: str, proc: str) -> bool:
    """В строке вызова модуль написан явно (`ИмяМодуля.Метод(`), а не через
    переменную-псевдоним. Строки нет — считаем прямым."""
    if not context:
        return True
    pattern = r'(?<![\wА-Яа-яЁё.])' + re.escape(module) + r'\s*\.\s*' + re.escape(proc) + r'\s*\('
    return re.search(pattern, context, re.IGNORECASE) is not None


# Глобальные объекты платформы, которые парсер может принять за общий модуль:
# в сводку «модулей нет в выгрузке» не попадают.
_PLATFORM_GLOBALS = {s.casefold() for s in (
    'ИсторияДанных', 'DataHistory', 'РегламентныеЗадания', 'ScheduledJobs',
    'ФоновыеЗадания', 'BackgroundJobs', 'ВнешниеОбработки', 'ExternalDataProcessors',
    'ВнешниеОтчеты', 'ExternalReports', 'РасширенияКонфигурации', 'ConfigurationExtensions',
    'КлиентскоеПриложение', 'ClientApplication', 'СредстваБуфераОбмена', 'ClipboardTools',
    'ПользователиИнформационнойБазы', 'InfoBaseUsers', 'ХранилищеОбщихНастроек',
    'CommonSettingsStorage', 'ХранилищеСистемныхНастроек', 'SystemSettingsStorage',
    'ХранилищеНастроекДанныхФорм', 'FormDataSettingsStorage', 'ХранилищеВариантовОтчетов',
    'ReportsVariantsStorage', 'ХранилищеПользовательскихНастроекОтчетов',
    'ReportsUserSettingsStorage', 'БиблиотекаКартинок', 'PictureLib', 'БиблиотекаСтилей',
    'StyleLib', 'ОбщиеМакеты', 'CommonTemplates', 'Метаданные', 'Metadata',
    'ПолнотекстовыйПоиск', 'FullTextSearch', 'ЖурналРегистрации', 'EventLog',
    'СредстваКриптографии', 'CryptoToolsManager', 'НастройкиКлиентскогоПриложения',
    'ClientApplicationSettings', 'СредстваГеопозиционирования', 'LocationTools',
    'СредстваМультимедиа', 'MultimediaTools', 'ДоставляемыеУведомления',
    'DeliverableNotifications', 'НастройкиЖурналаРегистрации', 'EventLogSettings')}


_IDENT = re.compile(r'[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*')


def _short(name_cf: str) -> str:
    parts = name_cf.split('.')
    return parts[1] if len(parts) > 1 else parts[0]


def _module_key(kind: str, module_cf: str) -> str:
    return f'общиймодуль.{module_cf}.модуль' if kind == 'common_module' else module_cf


def find_broken_calls(conn, source_id: str = '', module_name: str = '',
                      hints_limit: int = 3, read_module=None) -> dict:
    """{'missing': [...], 'not_export': [...], 'external': [...], 'dynamic': N}. source_id —
    только вызовы из этого источника; module_name — вызывающий или вызываемый
    модуль содержит эту строку (без учёта регистра)."""
    # name_cf модуля -> {name_cf процедуры: экспортная ли хоть в одном источнике}
    module_procs: dict[str, dict[str, bool]] = {}
    proc_homes: dict[str, set[tuple[str, str]]] = {}
    short_names: set[str] = set()
    for r in conn.execute(
            """SELECT m.name AS mname, m.name_cf AS mcf, m.source_id AS src,
                      p.name AS pname, p.name_cf AS pcf, p.is_export AS export
               FROM procedures p JOIN modules m ON m.id = p.module_id"""):
        mcf = r['mcf'] or r['mname'].casefold()
        pcf = r['pcf'] or r['pname'].casefold()
        keys = [mcf] + ([f'общиймодуль.{mcf}.модуль'] if '.' not in mcf else [])
        for key in keys:
            procs = module_procs.setdefault(key, {})
            procs[pcf] = procs.get(pcf, False) or bool(r['export'])
        proc_homes.setdefault(pcf, set()).add((r['mname'], r['src'] or ''))
        short_names.add(_short(mcf))

    module_filter = module_name.strip().casefold()

    def wanted(row) -> bool:
        if source_id and row['src'] != source_id:
            return False
        if module_filter and module_filter not in row['caller_module'].casefold() \
                and module_filter not in (row['module'] or '').casefold():
            return False
        return True

    def item(row, **extra) -> dict:
        return {'caller_module': row['caller_module'], 'caller_proc': row['caller_proc'],
                'source_id': row['src'] or '', 'line': row['line'] or 0,
                'kind': row['kind'], 'module': row['module'] or '', 'name': row['name'] or '',
                **extra}

    missing, not_export, external = [], [], {}
    dynamic = 0
    # Кандидаты — только неразрешённые вызовы и разрешённые в неэкспортную
    # процедуру: на крупной конфигурации это доли процента всех вызовов.
    calls = conn.execute(
        """SELECT c.callee_kind AS kind, c.callee_module AS module, c.callee_proc AS name,
                  c.line, c.context, p.name AS caller_proc, m.name AS caller_module,
                  m.name_cf AS caller_cf, m.source_id AS src, m.id AS caller_mid,
                  cr.call_id AS resolved
           FROM calls c
           JOIN procedures p ON p.id = c.caller_id
           JOIN modules m ON m.id = p.module_id
           LEFT JOIN calls_resolved cr ON cr.call_id = c.id
           LEFT JOIN procedures t ON t.id = cr.callee_id
           WHERE c.callee_kind IN ('common_module', 'manager')
             AND (cr.call_id IS NULL OR t.is_export = 0)""")
    for r in calls:
        module_cf = (r['module'] or '').casefold()
        proc_cf = (r['name'] or '').casefold()
        if not module_cf or not wanted(r):
            continue
        if r['kind'] == 'manager' and _platform_manager_method(module_cf, proc_cf):
            continue
        key = _module_key(r['kind'], module_cf)
        procs = module_procs.get(key)
        if procs is None:
            if (r['resolved'] is None and _short(module_cf) not in short_names
                    and module_cf not in _PLATFORM_GLOBALS):
                ext = external.setdefault(r['module'], {'module': r['module'], 'kind': r['kind'],
                                                        'calls': 0, 'methods': set(),
                                                        'sample': None, 'mids': set()})
                ext['calls'] += 1
                ext['mids'].add(r['caller_mid'])
                ext['methods'].add(r['name'] or '')
                if ext['sample'] is None:
                    ext['sample'] = item(r)
            continue
        if proc_cf not in procs:
            bucket = 'missing'
        elif not procs[proc_cf] and (r['caller_cf'] or r['caller_module'].casefold()) != key:
            # Свой модуль по имени модуля (в т.ч. заимствованный в расширении)
            # может звать неэкспортную процедуру; другой — нет.
            bucket = 'not_export'
        else:
            continue
        if r['kind'] == 'common_module' and not _direct(r['context'] or '', r['module'],
                                                         r['name'] or ''):
            dynamic += 1
            continue
        if bucket == 'missing':
            homes = sorted(proc_homes.get(proc_cf, ()))
            missing.append(item(r, defined_in=[{'module': m, 'source_id': s}
                                               for m, s in homes[:hints_limit]]))
        else:
            not_export.append(item(r))

    order = lambda i: (i['caller_module'], i['source_id'], i['line'], i['name'])  # noqa: E731
    missing.sort(key=order)
    not_export.sort(key=order)
    # `Модуль = Вычислить("ИмяМодуля")` — тоже переменная-модуль, а не модуль,
    # которого нет в выгрузке. Индекс текста модулей отбирает вызывающие
    # модули, где есть и имя, и Вычислить/Eval (позиций слов в нём нет), а
    # само присваивание проверяется по тексту этих модулей (read_module).
    for name in [n for n, e in external.items() if e['kind'] == 'common_module']:
        e = external[name]
        mids = sorted(m for m in e['mids'] if m is not None)
        if read_module is None or not mids or not _IDENT.fullmatch(name):
            continue
        try:
            hits = [r[0] for r in conn.execute(
                f"SELECT rowid FROM module_text_fts WHERE module_text_fts MATCH ? "
                f"AND rowid IN ({','.join('?' * len(mids))})",
                [f'"{name}" AND (eval OR вычислить)', *mids])]
        except Exception:               # индекс текста недоступен — оставить как есть
            hits = []
        assign = re.compile(r'(?<![\wА-Яа-яЁё.])' + re.escape(name)
                            + r'\s*=\s*(?:Вычислить|Eval)\s*\(', re.IGNORECASE)
        if any(assign.search(read_module(mid) or '') for mid in hits):
            dynamic += e['calls']
            del external[name]
    for e in external.values():
        e.pop('mids', None)
    ext_list = sorted(external.values(), key=lambda e: (-e['calls'], e['module']))
    for e in ext_list:
        e['methods'] = len(e['methods'])
    return {'missing': missing, 'not_export': not_export, 'external': ext_list,
            'dynamic': dynamic}
