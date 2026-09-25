"""
Честная диагностика нерезолвленных вызовов (Этап 7): вместо одной корзины
«нерезолвлено» — три причины для common_module/manager вызовов, см.
src.core.search._unresolved_breakdown():
  module_missing — модуля нет в выгрузке (внешняя БСП и т.п.);
  method_missing — модуль есть, метода нет (+ где метод определён на самом деле);
  other          — кандидаты на баг парсера/резолвера.
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import call_graph_resolution_stats

_MODULES = [
    # (id, source, name, [процедуры])
    (1, 'main', 'ОбщийМодуль.ОбщегоНазначения.Модуль', ['СообщитьПользователю', 'Вызывающая']),
    (2, 'main', 'ОбщийМодуль.ОбщегоНазначенияКлиентСервер.Модуль', ['ЗначениеВМассиве']),
    (3, 'ext', 'ОбщийМодуль.РасшМодуль.Модуль', ['РасшМетод']),
    (4, 'main', 'ОтчетныйМодуль', []),   # строка из report.txt, кода нет
    (5, 'main', 'Справочник.Номенклатура.МодульМенеджера', ['ПолучитьЦену']),
    (6, 'main', 'Справочник.Склады.МодульОбъекта', ['ПередЗаписью']),
    (7, 'main', 'ОбщийМодуль.Common.Модуль', ['SendMessage']),
]

_CALLS = [
    # (kind, module, proc, freq, resolved)
    ('common_module', 'СтандартныеПодсистемыСервер', 'ПриНачалеРаботы', 2, False),
    ('common_module', 'ОтчетныйМодуль', 'Сформировать', 1, False),
    ('common_module', 'CommonUse', 'Foo', 1, False),
    ('common_module', 'ОбщегоНазначения', 'ЗначениеВМассиве', 3, False),
    ('common_module', 'общегоназначения', 'РасшМетод', 1, False),
    ('manager', 'Справочник.Номенклатура.МодульМенеджера', 'НетТакого', 1, False),
    ('common_module', 'COMMON', 'Missing', 1, False),
    ('common_module', 'Номенклатура', 'Метод', 1, False),
    ('manager', 'Справочник.Склады.МодульМенеджера', 'Метод', 1, False),
    ('common_module', 'ОбщегоНазначения', 'СообщитьПользователю', 1, False),
    ('common_module', 'ОбщегоНазначения', 'СообщитьПользователю', 5, True),
    ('local', '', 'ГдеТоЕщё', 4, False),
]


def _seed(db: Database):
    conn = db.conn
    conn.execute("INSERT INTO sources (id, label, source_type) VALUES ('main', 'Main', 'main')")
    conn.execute("INSERT INTO sources (id, label, source_type) VALUES ('ext', 'Ext', 'extension')")
    pid = 0
    proc_ids = {}
    for mid, src, name, procs in _MODULES:
        conn.execute("INSERT INTO modules (id, source_id, name, module_type) VALUES (?, ?, ?, 'Модуль')",
                     (mid, src, name))
        for p in procs:
            pid += 1
            proc_ids[(mid, p)] = pid
            conn.execute("INSERT INTO procedures (id, module_id, name, kind) VALUES (?, ?, ?, 'Процедура')",
                         (pid, mid, p))
    caller = proc_ids[(1, 'Вызывающая')]
    target = proc_ids[(1, 'СообщитьПользователю')]
    cid = 0
    for kind, module, proc, freq, resolved in _CALLS:
        for _ in range(freq):
            cid += 1
            conn.execute("INSERT INTO calls (id, caller_id, callee_name, callee_module, callee_proc, "
                         "callee_kind, line) VALUES (?, ?, ?, ?, ?, ?, 1)",
                         (cid, caller, f'{module}.{proc}', module, proc, kind))
            if resolved:
                conn.execute("INSERT INTO calls_resolved (call_id, callee_id, confidence) "
                             "VALUES (?, ?, 'exact')", (cid, target))
    conn.commit()


def _with_seeded_db(fn):
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            _seed(db)
            return fn(db)
        finally:
            db.close()


def _pairs(group: dict) -> list[tuple[str, str, int]]:
    return [(i['module'], i['name'], i['freq']) for i in group['top']]


def test_groups_counts_and_percentages():
    b = _with_seeded_db(lambda db: call_graph_resolution_stats(db.conn)['unresolved_breakdown'])
    # local-вызовы и резолвнутые не считаются: 2+1+1+3+1+1+1+1+1+1 = 13
    assert b['total'] == 13
    assert b['module_missing']['count'] == 4
    assert b['method_missing']['count'] == 6
    assert b['other']['count'] == 3
    assert b['module_missing']['pct'] == round(100 * 4 / 13, 1)
    # Проценты округлены до 0.1 каждый, сумма может отличаться от 100 на округление.
    assert abs(sum(b[g]['pct'] for g in ('module_missing', 'method_missing', 'other')) - 100) <= 0.2


def test_module_missing_includes_report_only_module_without_code():
    b = _with_seeded_db(lambda db: call_graph_resolution_stats(db.conn)['unresolved_breakdown'])
    assert _pairs(b['module_missing']) == [
        ('СтандартныеПодсистемыСервер', 'ПриНачалеРаботы', 2),
        ('CommonUse', 'Foo', 1),
        ('ОтчетныйМодуль', 'Сформировать', 1),
    ]


def test_method_missing_with_hints_case_insensitive_and_across_sources():
    b = _with_seeded_db(lambda db: call_graph_resolution_stats(db.conn)['unresolved_breakdown'])
    by_pair = {(i['module'], i['name']): i for i in b['method_missing']['top']}
    assert _pairs(b['method_missing'])[0] == ('ОбщегоНазначения', 'ЗначениеВМассиве', 3)
    assert by_pair[('ОбщегоНазначения', 'ЗначениеВМассиве')]['defined_in'] == [
        {'module': 'ОбщийМодуль.ОбщегоНазначенияКлиентСервер.Модуль', 'source_id': 'main'}]
    # Регистр в вызове не важен; подсказка ведёт в модуль расширения.
    assert by_pair[('общегоназначения', 'РасшМетод')]['defined_in'] == [
        {'module': 'ОбщийМодуль.РасшМодуль.Модуль', 'source_id': 'ext'}]
    assert by_pair[('COMMON', 'Missing')]['defined_in'] == []
    assert by_pair[('Справочник.Номенклатура.МодульМенеджера', 'НетТакого')]['kind'] == 'manager'


def test_other_group_reasons():
    b = _with_seeded_db(lambda db: call_graph_resolution_stats(db.conn)['unresolved_breakdown'])
    reasons = {(i['module'], i['name']): i['reason'] for i in b['other']['top']}
    assert reasons == {
        # Справочник, а не общий модуль — вероятно, переменная, принятая за модуль.
        ('Номенклатура', 'Метод'): 'name_matches',
        # У объекта есть модули, но не модуль менеджера.
        ('Справочник.Склады.МодульМенеджера', 'Метод'): 'name_matches',
        # И модуль, и процедура есть, а резолва нет — устаревший calls_resolved.
        ('ОбщегоНазначения', 'СообщитьПользователю'): 'resolvable',
    }


def test_top_limit_applies_per_group():
    stats = _with_seeded_db(lambda db: call_graph_resolution_stats(db.conn, unresolved_names_limit=1))
    b = stats['unresolved_breakdown']
    assert [len(b[g]['top']) for g in ('module_missing', 'method_missing', 'other')] == [1, 1, 1]
    assert b['method_missing']['count'] == 6   # счётчик — по всем, не по топу
    assert len(stats['top_unresolved']) == 1   # старое поле не изменилось


def test_empty_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            b = call_graph_resolution_stats(db.conn)['unresolved_breakdown']
            assert b['total'] == 0
            assert all(b[g] == {'count': 0, 'pct': 0.0, 'top': []}
                       for g in ('module_missing', 'method_missing', 'other'))
        finally:
            db.close()


def test_real_pipeline_classifies_parser_output():
    """Через настоящий Indexer: русская и английская выгрузка в одном проекте."""
    def write(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8-sig')

    with tempfile.TemporaryDirectory() as tmpdir:
        xml_dir = Path(tmpdir) / 'xml'
        write(xml_dir / 'CommonModules' / 'ОбщегоНазначения' / 'Ext' / 'Module.bsl', '''
Процедура СообщитьПользователю(Текст) Экспорт
КонецПроцедуры
''')
        write(xml_dir / 'CommonModules' / 'ОбщегоНазначенияКлиентСервер' / 'Ext' / 'Module.bsl', '''
Функция ЗначениеВМассиве(Значение) Экспорт
	Возврат Неопределено;
КонецФункции
''')
        write(xml_dir / 'CommonModules' / 'CommonUse' / 'Ext' / 'Module.bsl', '''
Procedure Notify(Text) Export
EndProcedure
''')
        write(xml_dir / 'Catalogs' / 'Товары' / 'Ext' / 'ObjectModule.bsl', '''
Процедура ПередЗаписью(Отказ)
	ОбщегоНазначения.СообщитьПользователю("ок");
	Массив = ОбщегоНазначения.ЗначениеВМассиве(1);
	СтандартныеПодсистемыСервер.ПриНачалеРаботы();
	CommonUse.Notify("ok");
	CommonUse.Absent();
КонецПроцедуры
''')
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            known_modules, known_objects = Indexer.scan_known_names([str(xml_dir)])
            indexer = Indexer(db)
            indexer.index_bsl(str(xml_dir), source_id='main',
                              known_modules=known_modules, known_objects=known_objects)
            indexer.resolve_calls()
            b = call_graph_resolution_stats(db.conn)['unresolved_breakdown']
            assert _pairs(b['module_missing']) == [('СтандартныеПодсистемыСервер', 'ПриНачалеРаботы', 1)]
            method_missing = {(i['module'], i['name']): i['defined_in'] for i in b['method_missing']['top']}
            assert method_missing == {
                ('ОбщегоНазначения', 'ЗначениеВМассиве'): [
                    {'module': 'ОбщийМодуль.ОбщегоНазначенияКлиентСервер.Модуль', 'source_id': 'main'}],
                ('CommonUse', 'Absent'): [],
            }
            assert b['other']['count'] == 0
        finally:
            db.close()


def test_diagnose_index_tool_prints_breakdown():
    import io
    import zipfile
    from src.core.project_manager import ProjectManager
    from src.mcp_server.tools import execute_tool

    fixtures = Path(__file__).parent / 'fixtures'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (fixtures / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(fixtures / 'xml_ru'))
    buf.seek(0)

    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        try:
            pm.create_project('p1', 'p1')
            with open(fixtures / 'report_ru.txt', 'rb') as f:
                pm.add_source('p1', 'main', 'Main', report_file=f,
                              xml_archive=buf, xml_archive_filename='xml.zip')
            pm.reindex('p1')
            text = execute_tool(pm, 'diagnose_index', {'project_id': 'p1'})
            # Старый вывод на месте.
            assert 'Resolved edges:' in text
            assert 'Top unresolved callee names' in text
            section = text.split('Unresolved common_module/manager calls by cause:')[1]
            assert '[module_missing] модуль отсутствует в выгрузке' in section
            assert '[method_missing] модуль есть, метода в нём нет' in section
            assert '[other] прочее' in section
            # В урезанной фикстуре нет общего модуля ЗарплатаКадры.
            assert 'ЗарплатаКадры.' in section.split('[method_missing]')[0]
        finally:
            pm.close_all()
