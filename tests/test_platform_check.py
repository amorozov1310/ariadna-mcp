"""Проверка платформы через обычные точки входа проекта и MCP."""

import asyncio
import json
import os
import shutil
import struct
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, current_thread, enumerate as running_threads, get_ident
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from src.core import platform_check
from src.core.platform_help import compatibility_warning, minimum_compatibility_mode
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool
from src.web import app as web
from tests.fixtures.generate_platform_help import _book, context_book


FIXTURE = Path(__file__).parent / 'fixtures' / 'platform_check'
VERSION = '8.3.27.1'


@pytest.fixture
def project(tmp_path):
    pm = ProjectManager(str(tmp_path))
    source = _index_fixture(pm, 'demo')
    pm.platform_help.build_from_bytes(VERSION, context_book())
    try:
        yield pm, source
    finally:
        pm.close_all()


def _index_fixture(pm, project_id):
    pm.create_project(project_id, project_id)
    source = pm.projects_dir / project_id / 'sources' / 'sample' / 'xml'
    shutil.copytree(FIXTURE, source)
    pm.add_source(project_id, 'sample', 'Основная')
    pm.reindex(project_id)
    return source


def _report_workers(pm, project_id='demo'):
    db = pm.get_db(project_id)
    with platform_check._REPORTS_LOCK:
        return [worker for (building_db, _), worker in platform_check._REPORT_JOBS.items()
                if building_db is db]


def _join_workers(workers):
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()


def _join_help_warm(pm, version):
    _join_workers([t for t in running_threads() if t.name == f'platform-check-warm-{version}'])
    _join_workers(_report_workers(pm))


def _report_archive(pm, version=None):
    engine = pm.get_search('demo')
    index = pm.platform_help.get(version or pm.platform_help_version('demo'))
    revision = platform_check._index_revision(engine.db.read_conn())
    key = platform_check._cache_key(engine, index, pm.platform_mode('demo'), revision)
    return platform_check._archive_path(pm._index_path('demo'), key)


def _assert_persisted_report(pm, version):
    pm.close_all()
    path = _report_archive(pm)
    with ZipFile(path) as archive:
        key = json.loads(archive.read('report.json'))[0]
    assert path.name == f'platform-check-{key}.zip'
    assert f'по справке {version}' in execute_tool(pm, 'check_platform_calls', {})


def _upload_help(client, version):
    response = client.post('/api/admin/platform-help', data={'version': version},
                           files={'context': ('shcntx_ru.hbk', context_book(version))})
    assert response.status_code == 200 and 'Справка загружена' in response.text


def _ready_check(pm, **args):
    result = execute_tool(pm, 'check_platform_calls', args)
    if 'Отчёт готовится.' in result or 'Отчёт уже готовится.' in result:
        _join_workers(_report_workers(pm))
        result = execute_tool(pm, 'check_platform_calls', args)
    return result


def test_platform_calls(project):
    pm, _ = project
    result = _ready_check(pm)
    assert 'режим совместимости 8.3.24' in result
    assert 'CommonModules/Пример/Ext/Module.bsl:3 — Ошибка' in result
    assert 'НесуществующийВызов()' in result
    assert 'Module.bsl:5 — Предупреждение — метод требует режима совместимости 8.3.25' in result
    assert 'Module.bsl:6 — Ошибка' in result  # СтрНайти есть только в глобальном контексте.
    assert 'Module.bsl:7 — Не проверено' in result and 'Получатель.НетМетода()' in result
    assert result.count('доступен только на сервере') == 3
    assert 'требует режима совместимости 8.3.26' in result
    assert 'Внутренняя()' not in result and 'ТолькоСлово' not in result
    assert result.count('Ошибка') == 2 and result.count('Не проверено') == 1


def test_report_error_cache_is_bounded_and_errors_remain_visible(project):
    pm, _ = project
    db = pm.get_db('demo')
    with platform_check._REPORTS_LOCK:
        for number in range(platform_check._MAX_REPORT_ERRORS + 1):
            platform_check._remember_report_error(db, f'key-{number}', f'ошибка {number}')
        errors = platform_check._REPORT_ERRORS[db]
        assert len(errors) == platform_check._MAX_REPORT_ERRORS
        assert 'key-0' not in errors
        with pytest.raises(platform_check.ReportUnavailable, match='ошибка 16'):
            platform_check._known_report((db, 'key-16'), warm=False)
        assert 'key-16' not in errors


def test_help_upload_warms_reports_for_sixteen_projects_during_pool_churn(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path))
    project_ids = [f'help_{index:02d}' for index in range(16)]
    for project_id in project_ids:
        _index_fixture(pm, project_id)
        project = pm.get_project(project_id)
        project.platform_version = ''
        pm._save_project_to_disk(project_id, project)

    previous = web._pm
    web.set_pm(pm)
    new_version = '8.3.28.1'
    try:
        client = TestClient(web.app, headers={'Host': '127.0.0.1'})
        readers_started = Barrier(33)

        def read(index):
            readers_started.wait(timeout=10)
            total = 0
            for turn in range(64):
                project_id = project_ids[(index + turn) % len(project_ids)]
                with pm.read_lease(project_id) as db:
                    total += db.read_conn().execute('SELECT COUNT(*) FROM modules').fetchone()[0]
            return total

        with ThreadPoolExecutor(max_workers=32) as pool:
            reads = [pool.submit(read, index) for index in range(32)]
            readers_started.wait(timeout=10)
            _upload_help(client, new_version)
            assert all(result.result(timeout=20) > 0 for result in reads)

        _join_workers([t for t in running_threads() if t.name == f'platform-check-warm-{new_version}'])
        from src.core.platform_check import wait_report_jobs
        wait_report_jobs(list(pm._db_pool.values()))

        archives = {project_id: list(platform_check.report_directory(pm._index_path(project_id)).glob('platform-check-*.zip'))
                    for project_id in project_ids}
        assert all(archives.values()), {pid: len(paths) for pid, paths in archives.items()}
        for project_id in project_ids:
            first_check = execute_tool(pm, 'check_platform_calls', {'project_id': project_id})
            assert 'Отчёт готовится' not in first_check
            assert new_version in first_check
        assert not pm._db_pins
        assert len(pm._db_pool) <= 8
        assert not any(reader.active for db in pm._db_pool.values() for reader in db._readers)
    finally:
        web.set_pm(previous)
        pm.close_all()


def test_reindex_warms_first_report_while_other_projects_evict_pool(tmp_path, monkeypatch):
    """Первый отчёт должен пережить вытеснение во время его построения."""
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path))
    project_ids = [f'warm_{index:02d}' for index in range(16)]
    for project_id in project_ids:
        _index_fixture(pm, project_id)
        project = pm.get_project(project_id)
        project.platform_version = ''
        pm._save_project_to_disk(project_id, project)
    pm.platform_help.build_from_bytes(VERSION, context_book())

    target_id = project_ids[0]
    target_module = pm.get_source_xml_path(target_id, 'sample') / 'CommonModules/Пример/Ext/Module.bsl'
    build_started = Event()
    readers_done = Event()
    active = {'target': False}
    original_build = platform_check._Report._build

    def delayed_build(report, engine, conn, index, mode):
        if active['target'] and engine.db.db_path == str(pm._index_path(target_id)):
            build_started.set()
            assert readers_done.wait(timeout=20)
        return original_build(report, engine, conn, index, mode)

    monkeypatch.setattr(platform_check._Report, '_build', delayed_build)
    previous = web._pm
    web.set_pm(pm)
    try:
        client = TestClient(web.app, headers={'Host': '127.0.0.1'})
        for turn in range(3):
            with target_module.open('a', encoding='utf-8') as module:
                module.write(f'\nПроцедура Обновление{turn}() Экспорт\n'
                             f'    НесуществующийВызов{turn}();\nКонецПроцедуры\n')
            build_started.clear()
            readers_done.clear()
            active['target'] = True
            start = Barrier(33)

            def read_other(index):
                start.wait(timeout=10)
                build_started.wait(timeout=10)
                total = 0
                for offset in range(12):
                    project_id = project_ids[1 + (index + offset) % 15]
                    with pm.read_lease(project_id) as db:
                        total += db.read_conn().execute(
                            'SELECT COUNT(*) FROM modules').fetchone()[0]
                return total

            with ThreadPoolExecutor(max_workers=32) as pool:
                reads = [pool.submit(read_other, index) for index in range(32)]
                start.wait(timeout=10)
                response = client.post(
                    f'/api/projects/{target_id}/sources/sample/refresh', data={'wait': '0'})
                assert response.status_code == 202, response.text
                assert build_started.wait(timeout=15)
                assert all(read.result(timeout=20) > 0 for read in reads)
                readers_done.set()

            deadline = time.monotonic() + 20
            while pm.get_project(target_id).status == 'indexing' and time.monotonic() < deadline:
                time.sleep(.01)
            assert pm.get_project(target_id).status == 'ready'
            first_check = execute_tool(pm, 'check_platform_calls', {'project_id': target_id})
            assert 'Отчёт готовится' not in first_check, first_check
            assert f'НесуществующийВызов{turn}' in first_check, first_check
            active['target'] = False
        assert not pm._db_pins
        assert len(pm._db_pool) <= 8
    finally:
        readers_done.set()
        web.set_pm(previous)
        pm.close_all()


def test_help_mode_by_project(project):
    pm, source = project
    pm.create_project('other', 'Другой')
    args = {'query': 'ValueTable.Count', 'version': VERSION}
    assert 'Недоступно' in execute_tool(pm, 'platform_help', dict(args, project_id='demo'))
    assert 'Недоступно' not in execute_tool(pm, 'platform_help', args)
    assert 'Недоступно' not in execute_tool(pm, 'platform_help', dict(args, project_id='other'))
    # Режим берётся из индекса: у непроиндексированного проекта он «неизвестен», а не «не задан».
    assert 'пока неизвестен' in execute_tool(pm, 'platform_help', dict(args, project_id='other'))
    assert 'пока неизвестен' not in execute_tool(pm, 'platform_help', dict(args, project_id='demo'))
    config = source / 'Configuration.xml'
    xml = config.read_text(encoding='utf-8')
    for mode in ('Version8_3_25', 'DontUse'):
        config.write_text(xml.replace('Version8_3_24', mode), encoding='utf-8')
        pm.reindex('demo')
        assert 'Недоступно' not in execute_tool(pm, 'platform_help', dict(args, project_id='demo'))
    config.write_text(xml.replace('Version8_3_24', 'Version8_3_9'), encoding='utf-8')
    pm.reindex('demo')
    result = execute_tool(pm, 'platform_help', {'project_id': 'demo', 'query': 'ПримерРежима'})
    assert 'требует режима совместимости 8.3.26' in result and 'режим совместимости 8.3.9' in result


def test_pages_and_missing_help(project):
    pm, _ = project

    def check(**args):
        return _ready_check(pm, **args)

    first = check(limit=1, offset=-1, source_id='sample')
    assert 'offset=1' in first and 'НесуществующийВызов()' in first and 'cOuNt' not in first
    second = check(limit=0, offset=1)
    assert 'offset=2' in second and 'Таблица.cOuNt()' in second and 'НесуществующийВызов' not in second
    assert 'записей больше нет' in check(offset=50)
    assert 'урезан до 500' in check(limit=1000)
    assert check(source_id='other').startswith('Error:')
    pm.update_project('demo', platform_version='8.3.21.1')
    assert '8.3.21.1 не загружена' in check() and 'Настройки' in check()
    pm.platform_help.remove(VERSION)
    assert 'не загружена' in check()


def test_scope_types_and_multiline_calls(project):
    pm, source = project
    module = source / 'CommonModules/Пример/Ext/Module.bsl'
    module.write_text('''&AtClient
Procedure Check(
    ExampleTable)
    ExampleTable.Unknown();
    T = New ValueTable();
    If T.Count() Then
        T = ExampleTable;
    EndIf;
    T.AfterBranch();
    #If Server Then
    StrFind("fake()", "second()");
    #EndIf
    Пример.Внутренняя();
EndProcedure
Процедура Внутренняя()
    ExampleTable
        .Count();
КонецПроцедуры
ОпечаткаВМодуле();
''', encoding='utf-8')
    assert 'ОпечаткаВМодуле()' not in _ready_check(pm)
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:4 — Не проверено' in result and 'ExampleTable.Unknown()' in result
    assert 'Module.bsl:6 — Предупреждение — метод требует режима совместимости 8.3.25' in result
    assert 'Module.bsl:9 — Не проверено' in result and 'T.AfterBranch()' in result
    assert 'StrFind' not in result and 'Пример.Внутренняя' not in result
    assert 'Module.bsl:17 — Предупреждение — получатель' in result
    assert 'Module.bsl:19 — Ошибка' in result and 'ОпечаткаВМодуле()' in result
    module.unlink()
    assert execute_tool(pm, 'check_platform_calls', {}) == result


def test_page_cap_on_large_result(project):
    pm, source = project
    module = source / 'CommonModules/Пример/Ext/Module.bsl'
    module.write_text(''.join(f'Опечатка{i}();\n' for i in range(505)), encoding='utf-8')
    pm.reindex('demo')
    first = execute_tool(pm, 'check_platform_calls', {'limit': 501})
    assert first.count('— Ошибка') == 500 and 'Следующая страница: offset=500' in first
    last = execute_tool(pm, 'check_platform_calls', {'offset': 500})
    assert last.count('— Ошибка') == 5 and 'Опечатка500()' in last and 'Следующая страница' not in last


def _write_module(source, path, code):
    module = source / path
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(code, encoding='utf-8')
    return module


def _extension(pm, source):
    ext = source.parents[1] / 'sample_ext' / 'xml'
    ext.mkdir(parents=True)
    shutil.copyfile(source / 'Configuration.xml', ext / 'Configuration.xml')
    (ext / 'CommonModules').mkdir()
    shutil.copyfile(source / 'CommonModules/Пример.xml', ext / 'CommonModules/Пример.xml')
    pm.add_source('demo', 'sample_ext', 'Расширение', source_type='extension')
    return ext


def test_constant_managers_ru_and_en_in_main_and_extension(project):
    pm, source = project
    ext = _extension(pm, source)
    _write_module(source, 'Constants/Проба/Ext/ManagerModule.bsl',
                  'Функция НашМетод() Экспорт\n    Возврат 1;\nКонецФункции\n')
    _write_module(ext, 'Constants/Example/Ext/ManagerModule.bsl',
                  'Function CustomMethod() Export\n    Return 1;\nEndFunction\n')
    code = '''Константы.Проба.НашМетод();
Constants.Example.CustomMethod();
Constants.Example.Get();
Constants.Example.Typo();
'''
    for root in (source, ext):
        _write_module(root, 'Ext/SessionModule.bsl', code)
    pm.reindex('demo')
    for source_id in ('sample', 'sample_ext'):
        result = execute_tool(pm, 'check_platform_calls', {'source_id': source_id})
        session = [line for line in result.splitlines() if 'SessionModule.bsl' in line]
        assert len(session) == 1 and 'SessionModule.bsl:4 — Ошибка' in session[0]
        assert 'Constants.Example.Typo()' in result
        assert 'НашМетод' not in result and 'CustomMethod' not in result


@pytest.mark.parametrize(('directory', 'collection'), [
    ('ChartsOfCalculationTypes', 'ПланыВидовРасчета'),
    ('DocumentJournals', 'ЖурналыДокументов'),
    ('SettingsStorages', 'ХранилищаНастроек'),
    ('FilterCriteria', 'Критерии'),
])
def test_managers_of_additional_metadata_types(project, directory, collection):
    pm, source = project
    _write_module(source, f'{directory}/Проба/Ext/ManagerModule.bsl',
                  'Процедура НашМетод() Экспорт\nКонецПроцедуры\n')
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', f'{collection}.Проба.НашМетод();\n')
    pm.reindex('demo')
    assert 'Замечаний нет' in execute_tool(pm, 'check_platform_calls', {})


@pytest.mark.parametrize(('mode', 'unavailable'), [
    ('Version8_3_6', False), ('Version8_3_5', True), ('Version8_3_4', True),
    ('Version8_3_7', False), ('DontUse', False), ('', False),
])
def test_conflicting_compatibility_in_both_tools(project, mode, unavailable):
    pm, source = project
    config = source / 'Configuration.xml'
    xml = config.read_text(encoding='utf-8')
    old = '<CompatibilityMode>Version8_3_24</CompatibilityMode>'
    config.write_text(xml.replace(old, old.replace('Version8_3_24', mode) if mode else ''),
                      encoding='utf-8')
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'СтрШаблон("%1", 1);\n')
    pm.reindex('demo')
    calls = execute_tool(pm, 'check_platform_calls', {})
    help_text = execute_tool(pm, 'platform_help', {'query': 'СтрШаблон'})
    assert ('требует режима совместимости 8.3.6' in calls) == unavailable
    assert ('Замечаний нет' in calls) != unavailable
    assert ('Недоступно' in help_text) == unavailable


@pytest.mark.parametrize(('since', 'compat', 'mode', 'min_mode'), [
    ('8.3.6', '', '8.3.5', '8.3.6'),
    ('8.3.6', '', '8.3.6', ''),
    ('8.3.6', '', '8.3.7', ''),
    ('8.3.6', '8.3.10', '8.3.7', '8.3.10'),
    ('8.3.6', '8.3.10', '8.3.10', ''),
])
def test_minimum_compatibility_mode(since, compat, mode, min_mode):
    warning = compatibility_warning(minimum_compatibility_mode(since, compat), mode)
    expected = (f'требует режима совместимости {min_mode} или новее, '
                f'а у конфигурации режим совместимости {mode}') if min_mode else ''
    assert warning == expected


@pytest.mark.parametrize(('mode', 'regex_unavailable'), [('8_3_8', True), ('8_3_9', False), ('8_3_17', False)])
def test_verified_platform_compatibility_in_both_tools(project, mode, regex_unavailable):
    pm, source = project
    functions = [
        ('СтрНайтиПоРегулярномуВыражению', 'StrFindByRegularExpression', '8.3.23'),
        ('ВопросАсинх', 'DoQueryBoxAsync', '8.3.18'),
        ('НачатьПодключениеРасширенияПолученияИнформацииОКомпьютере',
         'BeginAttachingComputerInformationExtension', '8.3.18'),
        ('НачатьУстановкуРасширенияПолученияИнформацииОКомпьютере',
         'BeginInstallingComputerInformationExtension', '8.3.18'),
        ('НачатьПолучениеИнформацииОСетевыхАдаптерах', 'BeginGettingNetworkAdaptersInformation', '8.3.18'),
    ]
    pm.platform_help.build_from_bytes(VERSION, _book([
        (f'objects/Global context/methods/{en}.html', ru,
         f'<h1>{ru} ({en})</h1><p class="V8SH_chapter">Использование в версии:</p>'
         f'Доступен начиная с версии {since}.') for ru, en, since in functions
    ]))
    config = source / 'Configuration.xml'
    config.write_text(config.read_text(encoding='utf-8').replace('8_3_24', mode), encoding='utf-8')
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl',
                  ''.join(f'{en}();\n' for _, en, _ in functions))
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert result.count('— Предупреждение') == int(regex_unavailable)
    for i, (ru, _, _) in enumerate(functions):
        help_text = execute_tool(pm, 'platform_help', {'query': ru})
        assert ('Недоступно' in help_text) == (i == 0 and regex_unavailable)


@pytest.mark.parametrize(('opening', 'closing'), [('#If Server Then', '#EndIf'),
                                                     ('#Если Сервер Тогда', '#КонецЕсли')])
def test_preprocessor_does_not_turn_comparison_into_assignment(project, opening, closing):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', f'''Procedure Check()
    T = New Array;
    Flag =
    {opening}
    T = New Structure;
    T.Add(1);
    {closing}
EndProcedure
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:6 — Не проверено' in result
    assert '— Ошибка' not in result


@pytest.mark.parametrize('second', ['Count', 'Add'])
def test_preprocessor_can_split_call_after_dot(project, second):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', f'''Procedure Check()
    T = New Array;
    T.
    #If Server Then
        Count();
    #Else
        {second}();
    #EndIf
EndProcedure
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    if second == 'Count':
        assert 'Замечаний нет' in result
    else:
        for line in (5, 7):
            assert f'Module.bsl:{line} — Не проверено' in result
        assert '— Ошибка' not in result


def test_property_chain_requires_known_receiver_and_documented_member(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Procedure Check(Unknown)
    T = New ValueTable;
    T.Columns.Add("A");
    T.Колонки.Опечатка();
    Unknown.Columns.Add("B");
EndProcedure
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:3 —' not in result
    assert 'Module.bsl:4 — Не проверено' in result
    assert 'Module.bsl:5 — Не проверено' in result


@pytest.mark.parametrize(('availability', 'warn'), [
    ('Сервер', True), ('Сервер, внешнее соединение', True),
    ('Server, External connection, Mobile application (server)', True),
    ('External connection', True), ('Сервер, клиент', False),
    ('Server, Thick client', False), ('Тонкий клиент', False),
    ('Мобильное приложение', False), ('Mobile application (client)', False),
    ('Толстый клиент', False), ('Client', False), ('', False),
])
def test_client_availability_list(availability, warn):
    assert bool(platform_check._restrictions([('', availability)], '', True, 'метод')) == warn


def test_external_processor_connect_on_client(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''&НаКлиенте
Процедура Проверка()
    ВнешниеОбработки.Подключить(Путь);
КонецПроцедуры
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:3 — Предупреждение' in result and 'недоступен на клиенте' in result


def test_client_context_is_scoped_to_preprocessor_branch(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''&AtClient
Procedure Check()
    ExternalDataProcessors.Connect(Path);
    #If Server Then
    ExternalDataProcessors.Connect(Path);
    #EndIf
    ExternalDataProcessors.Connect(Path);
EndProcedure

Procedure CheckBranch()
    #If Client Then
    ExternalDataProcessors.Connect(Path);
    #Else
    ExternalDataProcessors.Connect(Path);
    #EndIf
EndProcedure

&AtServer
Procedure CheckServer()
    #If Client Then
    ExternalDataProcessors.Connect(Path);
    #EndIf
EndProcedure
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    for line in (3, 7, 12):
        assert f'Module.bsl:{line} — Предупреждение' in result
    for line in (5, 14, 21):
        assert f'Module.bsl:{line} —' not in result


def test_client_context_crosses_procedure_boundaries_and_nested_branches(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''#Если Клиент Тогда
Процедура Первая()
    ВнешниеОбработки.Подключить(Путь);
    #Если Сервер Тогда
        ВнешниеОбработки.Подключить(Путь);
    #ИначеЕсли (ТонкийКлиент Или ВебКлиент) И Не Сервер Тогда
        ВнешниеОбработки.Подключить(Путь);
        #Если ТонкийКлиент Тогда
            ВнешниеОбработки.Подключить(Путь);
        #КонецЕсли
    #Иначе
        ВнешниеОбработки.Подключить(Путь);
    #КонецЕсли
    ВнешниеОбработки.Подключить(Путь);
КонецПроцедуры
Процедура Вторая()
    ВнешниеОбработки.Подключить(Путь);
КонецПроцедуры
#Иначе
Процедура Третья()
    ВнешниеОбработки.Подключить(Путь);
КонецПроцедуры
#КонецЕсли
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    for line in (3, 7, 9, 12, 14, 17):
        assert f'Module.bsl:{line} — Предупреждение' in result
    for line in (5, 21):
        assert f'Module.bsl:{line} —' not in result


def test_deleted_procedures_do_not_resolve_live_calls(project):
    pm, source = project
    ext = _extension(pm, source)
    _write_module(ext, 'CommonModules/Пример/Ext/Module.bsl', '''#Delete
Procedure DeletedGlobal() Export
EndProcedure
#EndDelete
Procedure Check()
    DeletedGlobal();
EndProcedure
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {'source_id': 'sample_ext'})
    assert 'Module.bsl:6 — Ошибка' in result and 'DeletedGlobal()' in result


def test_manager_private_member_and_missing_object_in_both_tools(project):
    pm, source = project
    ext = _extension(pm, source)
    _write_module(ext, 'Constants/MyConst/Ext/ManagerModule.bsl', '''Procedure PrivateMember()
EndProcedure
Procedure PublicMember() Export
    PrivateMember();
EndProcedure
''')
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Procedure Check()
    Constants.MyConst.PrivateMember();
    Constants.MyConst.PublicMember();
    Constants.MyConst.Get();
    Catalogs.NoSuchCatalog.FindByCode("X");
    Документы.НетДокумента.НайтиПоНомеру("X");
    Constants.NoSuchConstant.Get();
EndProcedure
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {'source_id': 'sample'})
    assert 'Module.bsl:2 — Ошибка — метод менеджера без «Экспорт»' in result
    for line in (5, 6, 7):
        assert f'Module.bsl:{line} — Ошибка — объект метаданных' in result
    assert 'PublicMember()' not in result and 'Constants.MyConst.Get()' not in result
    broken = pm.broken_calls('demo', source_id='sample')
    assert [(item['line'], item['name']) for item in broken['not_export']] == [(2, 'PrivateMember')]
    assert [(item['line'], item.get('missing_object')) for item in broken['missing']] == [
        (5, True), (6, True), (7, True),
    ]
    assert 'объекта метаданных нет' in execute_tool(pm, 'find_broken_calls', {'source_id': 'sample'})


@pytest.mark.parametrize(('code', 'level'), [
    pytest.param('Если Метаданные.Справочники.Найти("Проба") = Неопределено Тогда '
                 'Возврат Неопределено; КонецЕсли; {call}', 'warning', id='early-return'),
    pytest.param('Если Metadata.FindByFullName("Catalog.Проба") <> Undefined Тогда '
                 'Число = 1; Иначе ВызватьИсключение "Проба"; КонецЕсли; {call}',
                 'warning', id='else-raises'),
    pytest.param('МД = Метаданные.Справочники.Найти("ПРОБА"); '
                 'Если Неопределено = МД Тогда Возврат Неопределено; КонецЕсли; {call}',
                 'warning', id='lookup-variable'),
    pytest.param('Если Metadata.Catalogs.Contains(Metadata.FindByFullName("Справочник.Проба")) Тогда '
                 '{call} КонецЕсли;', 'warning', id='membership-branch'),
    pytest.param('Пока Истина Цикл '
                 'Если Метаданные.Справочники.Найти("Проба") = Неопределено Тогда '
                 'Продолжить; КонецЕсли; {call} КонецЦикла;', 'warning', id='continue'),
    pytest.param('{call}', 'error', id='unguarded'),
    pytest.param('Если Метаданные.Справочники.Найти("Проба") = Неопределено Тогда '
                 'СтрНайти("Нет объекта", "Нет"); КонецЕсли; {call}', 'error', id='no-exit'),
    pytest.param('Если Метаданные.Документы.Найти("Проба") = Неопределено Тогда '
                 'Возврат Неопределено; КонецЕсли; {call}', 'error', id='other-collection'),
    pytest.param('Если Метаданные.Справочники.Найти("Проба") = Неопределено Тогда '
                 'Если Флаг Тогда Возврат Неопределено; КонецЕсли; КонецЕсли; {call}',
                 'error', id='conditional-exit'),
    pytest.param('Попытка Если Метаданные.Справочники.Найти("Проба") = Неопределено Тогда '
                 'ВызватьИсключение "Проба"; КонецЕсли; Исключение Флаг = Ложь; КонецПопытки; {call}',
                 'error', id='caught-exception'),
    pytest.param('Если Флаг Тогда МД = Метаданные.Справочники.Найти("Проба"); Возврат МД; '
                 'ИначеЕсли МД = Неопределено Тогда Возврат МД; КонецЕсли; {call}',
                 'error', id='other-branch-assignment'),
    pytest.param('Если Метаданные.Справочники.Найти("Проба") = Неопределено Тогда\n'
                 '#Если Сервер Тогда\nВозврат Неопределено;\n#КонецЕсли\nКонецЕсли; {call}',
                 'error', id='conditional-compilation'),
])
def test_missing_metadata_guard(project, code, level):
    pm, source = project
    call = 'Справочники.Проба.ПолучитьСсылку(Ключ)'
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl',
                  'Функция Проверка(Флаг, Ключ)\n'
                  + code.format(call=f'Возврат {call};') + '\nКонецФункции\n')
    pm.reindex('demo')
    result = _ready_check(pm, level=level)
    message = 'объект метаданных справочник.проба отсутствует в выгрузке'
    if level == 'warning':
        message = f'Предупреждение — {message} (необязательная зависимость)'
    else:
        message = f'Ошибка — {message}'
    assert f'{message}\n  {call}' in result
    other_level = 'error' if level == 'warning' else 'warning'
    assert call not in _ready_check(pm, level=other_level)


def test_broken_calls_detects_manager_removed_on_incremental_reindex(project):
    pm, source = project
    manager = _write_module(source, 'Catalogs/Пример/Ext/ManagerModule.bsl',
                            'Процедура Метод() Экспорт\nКонецПроцедуры\n')
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Procedure Check()
    Catalogs.Пример.FindByCode("X");
    Catalogs.Пример.Метод();
    Catalogs.Пример.CreateItem();
EndProcedure
''')
    pm.reindex('demo')
    assert not pm.broken_calls('demo')['missing']

    manager.unlink()
    config = source / 'Configuration.xml'
    config.write_text(config.read_text(encoding='utf-8').replace('<Catalog>Пример</Catalog>', ''),
                      encoding='utf-8')
    pm.reindex('demo')
    missing = pm.broken_calls('demo')['missing']
    assert [(item['line'], item.get('missing_object')) for item in missing] == [
        (2, True), (3, True), (4, True),
    ]


def test_incremental_reindex_resolves_manager_added_after_caller(project):
    pm, source = project
    caller = _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Procedure Check()
    Catalogs.Добавленный.Метод();
    Catalogs.Добавленный.FindByCode("X");
EndProcedure
''')
    pm.reindex('demo')
    assert len(pm.broken_calls('demo')['missing']) == 2
    assert 'Ошибка — объект метаданных' in execute_tool(pm, 'check_platform_calls', {})
    caller_mtime = caller.stat().st_mtime_ns

    config = source / 'Configuration.xml'
    config.write_text(config.read_text(encoding='utf-8').replace(
        '<Catalog>Пример</Catalog>', '<Catalog>Пример</Catalog><Catalog>Добавленный</Catalog>'),
        encoding='utf-8')
    catalog = source / 'Catalogs/Пример.xml'
    catalog.with_name('Добавленный.xml').write_text(
        catalog.read_text(encoding='utf-8').replace('Пример', 'Добавленный'),
        encoding='utf-8')
    _write_module(source, 'Catalogs/Добавленный/Ext/ManagerModule.bsl',
                  'Процедура Метод() Экспорт\nКонецПроцедуры\n')
    pm.reindex('demo')
    assert caller.stat().st_mtime_ns == caller_mtime
    assert not pm.broken_calls('demo')['missing']
    assert 'Замечаний нет' in execute_tool(pm, 'check_platform_calls', {})
    tree = execute_tool(pm, 'get_call_tree', {'module_name': 'ОбщийМодуль.Пример.Модуль',
                                             'procedure_name': 'Check', 'direction': 'down'})
    assert 'Справочник.Добавленный.МодульМенеджера.Метод' in tree
    assert 'FindByCode' not in tree


def test_filter_and_counts_cover_whole_report_across_pages(project):
    pm, source = project
    code = 'Получатель.Неизвестно();\n' * 503 + 'Опечатка();\nПримерРежима();\nОпечаткаДва();\n'
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', code)
    pm.reindex('demo')

    def check(**args):
        return execute_tool(pm, 'check_platform_calls', args)

    first = check(level='error', limit=1)
    assert 'Модулей — 1. Ошибки — 2, предупреждения — 1, не проверено — 503.' in first.splitlines()[1]
    assert 'Опечатка()' in first and 'offset=1' in first
    assert 'ОпечаткаДва()' in check(level='error', offset=1)
    assert 'Получатель.Неизвестно()' not in first
    pair = check(level=['warning', 'error', 'warning'])
    assert pair.index('Опечатка()') < pair.index('ПримерРежима()') < pair.index('ОпечаткаДва()')
    assert check(level='unknown').startswith('Error:')
    assert 'Замечаний нет' in check(level=[])
    assert 'Получатель.Неизвестно()' in check(level='unchecked', offset=502)
    assert 'Опечатка' not in check(level='unchecked', offset=503)

    from mcp import Client
    from src.mcp_server.server import create_mcp_server

    async def verify_mcp():
        async with Client(create_mcp_server(pm)) as client:
            for level in ('error', ['warning', 'error']):
                args = {'level': level, 'limit': 1}
                result = await client.call_tool('check_platform_calls', args)
                assert not result.is_error
                assert result.content[0].text == check(**args)

    asyncio.run(verify_mcp())


def test_platform_report_changes_only_after_reindex(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'Опечатка();\n')
    session = _write_module(source, 'Ext/SessionModule.bsl', 'ДругаяОпечатка();\n')
    pm.reindex('demo')
    before = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:1 — Ошибка' in before and 'ДругаяОпечатка()' in before
    session.write_text('НовыйВызов();\n', encoding='utf-8')
    assert execute_tool(pm, 'check_platform_calls', {}) == before
    pm.reindex('demo')
    after = execute_tool(pm, 'check_platform_calls', {})
    assert 'НовыйВызов()' in after and 'ДругаяОпечатка()' not in after


def test_removed_module_stays_in_report_until_reindex(project):
    pm, source = project
    ext = _extension(pm, source)
    module = _write_module(ext, 'Ext/SessionModule.bsl', 'ДругаяОпечатка();\n')
    pm.reindex('demo')
    args = {'source_id': 'sample_ext', 'level': 'unchecked'}
    before = execute_tool(pm, 'check_platform_calls', args)
    assert '@sample_ext Ext/SessionModule.bsl:1' in before
    module.unlink()
    assert execute_tool(pm, 'check_platform_calls', args) == before
    assert 'SessionModule.bsl' not in execute_tool(pm, 'check_platform_calls', {'source_id': 'sample'})
    pm.reindex('demo', source_id='sample_ext')
    assert 'SessionModule.bsl' not in execute_tool(pm, 'check_platform_calls', args)


def test_nested_extension_deletions_preserve_line_numbers(project):
    pm, source = project
    ext = _extension(pm, source)
    _write_module(ext, 'CommonModules/Пример/Ext/Module.bsl', '''&ИзменениеИКонтроль("Проверка")
Процедура Расш_Проверка()
    #Удаление
    УдаленныйВызов();
        #Delete
        ВложенныйВызов();
        #EndDelete
    ЕщеУдаленныйВызов();
    #КонецУдаления
    #Вставка
    Опечатка();
    #КонецВставки
КонецПроцедуры
#Удаление
Процедура Удаленная()
    ЕщеОдинВызов();
КонецПроцедуры
#КонецУдаления
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {'source_id': 'sample_ext'})
    assert result.count('— Ошибка') == 1
    assert 'Module.bsl:11 — Ошибка' in result and 'Опечатка()' in result


def test_comparison_preserves_type_and_execute_invalidates_it(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Процедура Проверка()
    А = Новый Структура;
    Флаг = А = Новый Массив;
    А.Свойство("Ключ");
    А.Опечатка();
    М = Новый Массив;
    Выполнить("М = Новый Структура;");
    М.Свойство("Ключ");
    М = New Array;
    Execute("М = New Structure;");
    М.Property("Ключ");
КонецПроцедуры
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert result.count('— Ошибка') == 1
    assert 'Module.bsl:5 — Ошибка — в справке нет метода типа структура' in result
    assert 'Module.bsl:8 — Не проверено' in result
    assert 'Module.bsl:11 — Не проверено' in result
    assert 'Module.bsl:4' not in result and 'Execute(' not in result


def test_execute_after_dot_is_a_method(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Процедура Проверка(Параметр)
    М = Новый Структура;
    М.Выполнить();
    Параметр.Execute();
    М.Свойство("Ключ");
КонецПроцедуры
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:3 — Ошибка' in result and 'М.Выполнить()' in result
    assert 'Module.bsl:4 — Не проверено' in result and 'Параметр.Execute()' in result
    assert 'Module.bsl:5' not in result


def test_page_reuses_diagnoses_and_updates_after_source_change(project, monkeypatch):
    pm, source = project
    module = _write_module(source, 'CommonModules/Пример/Ext/Module.bsl',
                           ''.join(f'Опечатка{i}();\n' for i in range(50)))
    observed = []
    diagnose = platform_check._diagnosis

    def track(call, *args):
        observed.append(call.name)
        return diagnose(call, *args)

    monkeypatch.setattr(platform_check, '_diagnosis', track)
    pm.reindex('demo')
    assert observed == [f'опечатка{i}' for i in range(50)]
    first = execute_tool(pm, 'check_platform_calls', {'limit': 1})
    last = execute_tool(pm, 'check_platform_calls', {'limit': 1, 'offset': 48})
    assert 'Опечатка0()' in first and 'Опечатка48()' in last
    assert observed == [f'опечатка{i}' for i in range(50)]
    module.write_text('НоваяОпечатка();\n', encoding='utf-8')
    assert execute_tool(pm, 'check_platform_calls', {'limit': 1}) == first
    filtered = execute_tool(pm, 'check_platform_calls', {'level': 'error'})
    assert 'Опечатка49()' in filtered and '— Не проверено' not in filtered
    pm.reindex('demo')
    assert 'НоваяОпечатка()' in execute_tool(pm, 'check_platform_calls', {})


def test_pages_cross_packed_blocks(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl',
                  ''.join(f'Опечатка{i}();\n' for i in range(1502)))
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {'offset': 999, 'limit': 500})
    findings = [line.strip() for line in result.splitlines() if line.startswith('  ')]
    assert findings == [f'Опечатка{i}()' for i in range(999, 1499)]
    assert 'offset=1499' in result


def test_first_request_after_reindex_loads_persisted_report_by_source(project, monkeypatch):
    pm, source = project
    ext = _extension(pm, source)
    _write_module(ext, 'CommonModules/Пример/Ext/Module.bsl', 'ОпечаткаРасширения();\n')
    pm.reindex('demo')
    cache = _report_archive(pm)
    assert cache.is_file()
    args = {'source_id': 'sample_ext', 'level': ['error', 'unchecked']}
    expected = execute_tool(pm, 'check_platform_calls', args)
    platform_check._REPORTS.clear()
    pm.close_all()

    def fail(*args):
        pytest.fail('готовый снимок должен прочитаться с диска')

    monkeypatch.setattr(platform_check, '_diagnosis', fail)
    actual = execute_tool(pm, 'check_platform_calls', args)
    assert actual == expected
    assert 'Модулей — 1. Ошибки — 1, предупреждения — 0, не проверено — 0.' in actual
    assert 'ОпечаткаРасширения()' in actual


def test_parallel_requests_restore_the_archive_once(project, monkeypatch):
    pm, _ = project
    pm.reindex('demo')
    looked_up, restores = set(), []
    all_requests = Event()
    original_lookup = platform_check._known_report
    original_restore = platform_check._Report.restore

    def lookup(*args, **kwargs):
        looked_up.add(get_ident())  # Вызовы под _REPORTS_LOCK.
        if len(looked_up) == 4:
            all_requests.set()
        return original_lookup(*args, **kwargs)

    def restore(*args):
        restores.append(1)
        assert all_requests.wait(timeout=5)
        original_restore(*args)

    monkeypatch.setattr(platform_check, '_known_report', lookup)
    monkeypatch.setattr(platform_check._Report, 'restore', restore)
    with ThreadPoolExecutor(4) as pool:
        pending = [pool.submit(execute_tool, pm, 'check_platform_calls', {}) for _ in range(4)]
        for future in pending:
            assert 'НесуществующийВызов()' in future.result(timeout=10)
    assert restores == [1]


def test_invalid_deflate_in_report_header_triggers_background_build(project):
    pm, _ = project
    pm.reindex('demo')
    cache = _report_archive(pm)
    with ZipFile(cache) as archive:
        offset = archive.getinfo('report.json').header_offset
    contents = bytearray(cache.read_bytes())
    name_len, extra_len = struct.unpack_from('<HH', contents, offset + 26)
    contents[offset + 30 + name_len + extra_len] = 7  # Зарезервированный тип блока DEFLATE.
    cache.write_bytes(contents)
    with ZipFile(cache) as archive, pytest.raises(zlib.error):
        archive.read('report.json')

    assert 'Отчёт готовится' in execute_tool(pm, 'check_platform_calls', {})
    _join_workers(_report_workers(pm))
    assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {})
    with ZipFile(cache) as archive:
        assert archive.testzip() is None


def test_legacy_shared_archive_is_ignored_and_removed_on_reindex(project):
    pm, _ = project
    assert 'НесуществующийВызов()' in _ready_check(pm)
    old = _report_archive(pm)
    legacy = old.with_name('platform-check.zip')
    old.rename(legacy)
    pm.close_all()

    assert 'Отчёт готовится' in execute_tool(pm, 'check_platform_calls', {})
    _join_workers(_report_workers(pm))
    assert old.is_file()
    pm.reindex('demo')
    assert not legacy.exists() and not old.exists()
    _assert_persisted_report(pm, VERSION)


def test_reindex_archive_wins_over_ongoing_background_publication(project, monkeypatch):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'НоваяОпечатка();\n')
    replacing_old, release_old, saving_new, replacing_new = Event(), Event(), Event(), Event()
    old_archive = _report_archive(pm)
    original_replace = platform_check.replace_with_retry
    original_save = platform_check._Report.save
    order = []

    def save(report, *args, **kwargs):
        if current_thread().name == 'reindex-demo':
            saving_new.set()
        return original_save(report, *args, **kwargs)

    def replace(temp, path):
        if current_thread().name == 'platform-check-demo':
            replacing_old.set()
            assert release_old.wait(timeout=10)
            order.append('старый')
        else:
            replacing_new.set()
            order.append('новый')
        original_replace(temp, path)

    monkeypatch.setattr(platform_check._Report, 'save', save)
    monkeypatch.setattr(platform_check, 'replace_with_retry', replace)
    try:
        assert 'Отчёт готовится' in execute_tool(pm, 'check_platform_calls', {})
        assert replacing_old.wait(timeout=5)
        assert pm.reindex_async('demo')['status'] == 'started'
        assert saving_new.wait(timeout=5)
        assert not replacing_new.wait(timeout=0.1)
    finally:
        threads = _report_workers(pm) + [t for t in running_threads() if t.name == 'reindex-demo']
        release_old.set()
        _join_workers(threads)
    assert order == ['старый', 'новый']
    assert not old_archive.exists()
    assert _report_archive(pm).is_file()
    assert 'НоваяОпечатка()' in execute_tool(pm, 'check_platform_calls', {})


def test_parallel_fallback_builds_report_once_and_writes_cache(project, monkeypatch):
    pm, _ = project
    cache = _report_archive(pm)
    assert not cache.exists()  # Справка появилась после индексации.
    original = platform_check._Report._build
    builds = []
    start = Barrier(2)
    release = Event()
    started = Event()

    def track(*args):
        builds.append(1)
        started.set()
        assert release.wait(timeout=10)
        return original(*args)

    def request():
        start.wait(timeout=5)
        return execute_tool(pm, 'check_platform_calls', {'level': 'error'})

    monkeypatch.setattr(platform_check._Report, '_build', track)
    try:
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.submit(request), pool.submit(request)
            messages = {first.result(timeout=5), second.result(timeout=5)}
            assert {msg.split('.')[0] for msg in messages} == {'Отчёт готовится', 'Отчёт уже готовится'}
            assert started.wait(timeout=5)
            assert not cache.exists()
    finally:
        workers = _report_workers(pm)
        release.set()
        _join_workers(workers)
    assert builds == [1]
    assert cache.is_file()
    assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {'level': 'error'})


def test_failed_background_build_is_logged_and_reported(project, monkeypatch, caplog):
    pm, _ = project

    def fail(*args):
        raise RuntimeError('Пробный сбой построения')

    with monkeypatch.context() as patch:
        patch.setattr(platform_check._Report, '_build', fail)
        assert 'Отчёт готовится' in execute_tool(pm, 'check_platform_calls', {})
        _join_workers(_report_workers(pm))
        error = execute_tool(pm, 'check_platform_calls', {})
        assert error.startswith('Error: не удалось подготовить отчёт')
        assert 'Пробный сбой построения' in error and 'Пробный сбой построения' in caplog.text
    assert 'НесуществующийВызов()' in _ready_check(pm)


@pytest.mark.parametrize('old_fingerprint', ['старый отпечаток', '', None])
def test_old_index_skips_cached_report_and_partial_reindex_warm(project, monkeypatch, old_fingerprint):
    pm, source = project
    ext = _extension(pm, source)
    _write_module(ext, 'CommonModules/Пример/Ext/Module.bsl', 'ОпечаткаРасширения();\n')
    pm.reindex('demo')
    assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {})
    db = pm.get_db('demo')
    if old_fingerprint is None:
        db.conn.execute("DELETE FROM parser_state WHERE source_id='sample_ext'")
        db.conn.commit()
    else:
        db.set_parser_fingerprint('sample_ext', old_fingerprint)
    message = 'Индекс проекта «demo» построен старой версией Ариадны.'
    result = execute_tool(pm, 'check_platform_calls', {'source_id': 'sample'})
    assert message in result and 'reindex' in result and 'Ошибки' not in result
    assert message in execute_tool(pm, 'find_broken_calls', {})

    def fail(*args):
        pytest.fail('частичная переиндексация не должна греть отчёт со старым источником')

    monkeypatch.setattr(platform_check._Report, '_build', fail)
    pm.reindex('demo', source_id='sample')
    assert execute_tool(pm, 'check_platform_calls', {}) == result
    assert not _report_workers(pm)


def test_platform_check_waits_for_indexing(project):
    pm, _ = project
    pm.get_db('demo').update_state(status='indexing')
    assert 'Дождитесь окончания переиндексации' in execute_tool(pm, 'check_platform_calls', {})
    assert not _report_workers(pm)


def test_old_parser_fingerprint_without_parsed_code_does_not_block_report(project):
    pm, source = project
    _extension(pm, source)
    pm.reindex('demo')
    pm.get_db('demo').set_parser_fingerprint('sample_ext', 'старый отпечаток')
    result = execute_tool(pm, 'check_platform_calls', {'source_id': 'sample'})
    assert 'НесуществующийВызов()' in result and 'старой версией Ариадны' not in result


@pytest.mark.parametrize('upload_index', [False, True])
def test_upload_warms_ready_projects_with_effective_version(project, tmp_path, monkeypatch, upload_index):
    pm, _ = project
    older = '8.3.9.1'
    version = older if upload_index else VERSION
    _index_fixture(pm, 'pinned')
    pm.update_project('pinned', platform_version=version)
    _index_fixture(pm, 'other')
    pm.update_project('other', platform_version=VERSION if upload_index else older)
    _index_fixture(pm, 'indexing')
    pm.get_db('indexing').update_state(status='indexing')
    _index_fixture(pm, 'old_parser')
    pm.get_db('old_parser').set_parser_fingerprint('sample', 'старый отпечаток')
    monkeypatch.setattr(web, '_pm', pm)

    original = platform_check._Report._build
    release = Event()
    started = {project_id: Event() for project_id in ('demo', 'pinned')}
    built = []
    expected = {'pinned'} if upload_index else {'demo', 'pinned'}

    def track(report, engine, *args):
        project_id = Path(engine.db.db_path).parent.name
        built.append(project_id)
        if project_id in started:
            started[project_id].set()
        assert release.wait(timeout=10)
        return original(report, engine, *args)

    monkeypatch.setattr(platform_check._Report, '_build', track)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            if upload_index:
                donor = type(pm.platform_help)(tmp_path / 'донор')
                donor.build_from_bytes(version, context_book(version))
                response = client.post('/api/admin/platform-help',
                                       files={'index': ('help.db', donor.path(version).read_bytes())})
            else:
                response = client.post('/api/admin/platform-help', data={'version': version},
                                       files={'context': ('shcntx_ru.hbk', context_book(version))})
        assert response.status_code == 200 and 'Справка загружена' in response.text
        _join_workers([t for t in running_threads() if t.name == f'platform-check-warm-{version}'])
        for project_id in expected:
            assert started[project_id].wait(timeout=5)
            result = execute_tool(pm, 'check_platform_calls', {'project_id': project_id})
            assert 'Отчёт уже готовится' in result
        assert sorted(built) == sorted(expected)
    finally:
        workers = [t for t in running_threads() if t.name.startswith('platform-check-')]
        release.set()
        _join_workers(workers)
    for project_id in expected:
        assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {'project_id': project_id})


def test_rebuild_of_help_invalidates_report(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'СтрНайти("x", "y");\n')
    pm.reindex('demo')
    assert 'Замечаний нет' in execute_tool(pm, 'check_platform_calls', {})
    cache = _report_archive(pm)
    assert cache.is_file()
    previous_cache = cache.read_bytes()
    platform_check._REPORTS.clear()
    pm.platform_help.build_from_bytes(VERSION, _book([
        ('objects/Global context/methods/Other.html', 'ДругойВызов',
         '<h1>ДругойВызов (Other)</h1>'),
    ]))
    result = _ready_check(pm)
    assert '— Ошибка' in result and 'СтрНайти(' in result
    assert _report_archive(pm) != cache
    assert cache.read_bytes() == previous_cache
    pm.platform_help.remove(VERSION)
    assert 'не загружена' in execute_tool(pm, 'check_platform_calls', {})


def test_deleting_newest_help_warms_the_automatic_fallback(project, monkeypatch):
    pm, _ = project
    older = '8.3.9.1'
    pm.platform_help.build_from_bytes(older, context_book(older))
    pm.reindex('demo')
    _index_fixture(pm, 'pinned')
    pm.update_project('pinned', platform_version=older)
    monkeypatch.setattr(web, '_pm', pm)
    built = []
    original = platform_check._Report._build

    def track(report, engine, conn, index, mode):
        built.append((Path(engine.db.db_path).parent.name, index.version))
        return original(report, engine, conn, index, mode)

    monkeypatch.setattr(platform_check._Report, '_build', track)
    with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
        response = client.post(f'/api/admin/platform-help/{VERSION}/delete')
    assert response.status_code == 200 and 'удалена' in response.text
    _join_workers([t for t in running_threads() if t.name == 'platform-check-warm-auto'])
    _join_workers(_report_workers(pm))
    assert built == [('demo', older)]
    result = execute_tool(pm, 'check_platform_calls', {'project_id': 'demo'})
    assert f'по справке {older}' in result and 'НесуществующийВызов()' in result


def test_overlapping_deletions_warm_the_last_help_version(project, monkeypatch):
    pm, _ = project
    older, middle = '8.3.9.1', '8.3.24.1'
    store = pm.platform_help
    for version in (older, middle):
        store.build_from_bytes(version, context_book(version))
    pm.reindex('demo')
    monkeypatch.setattr(web, '_pm', pm)
    reached, release = Event(), Event()
    remove = store.remove

    def delay_remove(version):
        if version == middle:
            reached.set()
            assert release.wait(timeout=10)
        return remove(version)

    monkeypatch.setattr(store, 'remove', delay_remove)
    with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client, ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.post, f'/api/admin/platform-help/{middle}/delete')
        try:
            assert reached.wait(timeout=5)
            assert client.post(f'/api/admin/platform-help/{VERSION}/delete').status_code == 200
            _join_help_warm(pm, 'auto')
            assert f'по справке {middle}' in execute_tool(pm, 'check_platform_calls', {})
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200

    _join_help_warm(pm, 'auto')
    pm.close_all()
    assert f'по справке {older}' in execute_tool(pm, 'check_platform_calls', {})


def test_two_deletions_cannot_displace_the_ready_fallback_archive(project, monkeypatch):
    pm, _ = project
    middle, newest = '8.3.28.1', '8.3.29.1'
    monkeypatch.setattr(web, '_pm', pm)
    assert 'НесуществующийВызов()' in _ready_check(pm)
    fallback = _report_archive(pm)
    saved = fallback.read_bytes()
    pm.platform_help.build_from_bytes(middle, context_book(middle))
    publishing, release = Event(), Event()
    both_removed = Barrier(2)
    replace = platform_check.replace_with_retry
    warm = pm.warm_platform_checks

    def delay_replace(temp, path):
        publishing.set()
        assert release.wait(timeout=10)
        replace(temp, path)

    def warm_after_both_deletions(version=''):
        if not version:
            both_removed.wait(timeout=5)
        warm(version)

    monkeypatch.setattr(platform_check, 'replace_with_retry', delay_replace)
    monkeypatch.setattr(pm, 'warm_platform_checks', warm_after_both_deletions)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client, ThreadPoolExecutor(2) as pool:
            _upload_help(client, newest)
            assert publishing.wait(timeout=5)
            latest = [pool.submit(client.post, f'/api/admin/platform-help/{v}/delete')
                      for v in (middle, newest)]
            assert all(future.result(timeout=5).status_code == 200 for future in latest)
            _join_workers([t for t in running_threads() if t.name == 'platform-check-warm-auto'])
            assert pm.platform_help_version('demo') == VERSION
            assert fallback.read_bytes() == saved
    finally:
        workers = _report_workers(pm)
        release.set()
        _join_workers(workers)
    assert fallback.read_bytes() == saved
    _assert_persisted_report(pm, VERSION)


def test_project_version_change_during_publication_keeps_the_selected_archive(project, monkeypatch):
    pm, _ = project
    newest = '8.3.28.1'
    monkeypatch.setattr(web, '_pm', pm)
    pm.update_project('demo', platform_version=VERSION)
    assert 'НесуществующийВызов()' in _ready_check(pm)
    first = _report_archive(pm)
    saved = first.read_bytes()
    pm.platform_help.build_from_bytes(newest, context_book(newest))
    publishing, release = Event(), Event()
    replace = platform_check.replace_with_retry

    def delay_replace(temp, path):
        publishing.set()
        assert release.wait(timeout=10)
        replace(temp, path)

    monkeypatch.setattr(platform_check, 'replace_with_retry', delay_replace)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            for version in (newest, VERSION):
                response = client.post('/api/projects/demo/update', data={'name': 'Демо',
                                       'platform_version': version}, follow_redirects=False)
                assert response.status_code == 303
                assert publishing.wait(timeout=5)
            _join_workers([t for t in running_threads() if t.name == 'platform-check-warm-demo'])
    finally:
        workers = _report_workers(pm)
        release.set()
        _join_workers(workers)
    assert first.read_bytes() == saved
    _assert_persisted_report(pm, VERSION)


def test_rollback_while_fallback_job_is_still_registered(project, monkeypatch):
    pm, _ = project
    newest = '8.3.28.1'
    monkeypatch.setattr(web, '_pm', pm)
    fallback = _report_archive(pm)
    started, finish_build, cleanup, finish_cleanup = Event(), Event(), Event(), Event()
    build = platform_check._Report._build
    db = pm.get_db('demo')
    close_reader = db.close_thread_reader
    old_job = None

    def delayed_build(report, engine, conn, index, mode):
        if index.version == VERSION:
            started.set()
            assert finish_build.wait(timeout=10)
        return build(report, engine, conn, index, mode)

    def delayed_cleanup():
        if current_thread() is old_job:
            cleanup.set()
            assert finish_cleanup.wait(timeout=10)
        close_reader()

    monkeypatch.setattr(platform_check._Report, '_build', delayed_build)
    monkeypatch.setattr(db, 'close_thread_reader', delayed_cleanup)
    try:
        assert 'Отчёт готовится' in execute_tool(pm, 'check_platform_calls', {})
        assert started.wait(timeout=5)
        old_job, = _report_workers(pm)
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            _upload_help(client, newest)
            _join_workers([t for t in running_threads() if t.name == f'platform-check-warm-{newest}'])
            _join_workers([t for t in _report_workers(pm) if t is not old_job])
            assert _report_archive(pm, newest).is_file()
            finish_build.set()
            assert cleanup.wait(timeout=5)
            assert old_job in _report_workers(pm)
            assert client.post(f'/api/admin/platform-help/{newest}/delete').status_code == 200
            _join_workers([t for t in running_threads() if t.name == 'platform-check-warm-auto'])
    finally:
        workers = _report_workers(pm)
        finish_build.set()
        finish_cleanup.set()
        _join_workers(workers)
    assert fallback.is_file()
    _assert_persisted_report(pm, VERSION)


def test_old_help_build_cannot_overwrite_the_current_version_archive(project, monkeypatch):
    pm, _ = project
    newest = '8.3.28.1'
    monkeypatch.setattr(web, '_pm', pm)
    old_started, release_old = Event(), Event()
    original = platform_check._Report._build

    def delay_build(report, engine, conn, index, mode):
        if index.version == VERSION:
            old_started.set()
            assert release_old.wait(timeout=10)
        return original(report, engine, conn, index, mode)

    monkeypatch.setattr(platform_check._Report, '_build', delay_build)
    try:
        assert 'Отчёт готовится' in execute_tool(pm, 'check_platform_calls', {})
        assert old_started.wait(timeout=5)
        old_job, = _report_workers(pm)
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            response = client.post('/api/admin/platform-help', data={'version': newest},
                                   files={'context': ('shcntx_ru.hbk', context_book(newest))})
        assert 'Справка загружена' in response.text
        _join_workers([t for t in running_threads() if t.name == f'platform-check-warm-{newest}'])
        _join_workers([t for t in _report_workers(pm) if t is not old_job])
        cache = _report_archive(pm, newest)
        new_archive = cache.read_bytes()
    finally:
        workers = _report_workers(pm)
        release_old.set()
        _join_workers(workers)
    assert cache.read_bytes() == new_archive
    pm.close_all()
    assert f'по справке {newest}' in execute_tool(pm, 'check_platform_calls', {})


def test_help_rollback_republishes_the_memory_snapshot(project, monkeypatch):
    pm, _ = project
    newest = '8.3.28.1'
    monkeypatch.setattr(web, '_pm', pm)
    expected = _ready_check(pm)
    assert f'по справке {VERSION}' in expected
    cache = _report_archive(pm)
    with ZipFile(cache) as archive:
        old_header = archive.read('report.json')
    with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
        response = client.post('/api/admin/platform-help', data={'version': newest},
                               files={'context': ('shcntx_ru.hbk', context_book(newest))})
        assert 'Справка загружена' in response.text
        _join_help_warm(pm, newest)
        with ZipFile(_report_archive(pm, newest)) as archive:
            assert archive.read('report.json') != old_header
        cache.unlink()  # Снимок остался в памяти, архив мог быть удалён с тома.

        def fail(*args):
            pytest.fail('готовый снимок из памяти нужно записать без пересчёта')

        monkeypatch.setattr(platform_check._Report, '_build', fail)
        assert client.post(f'/api/admin/platform-help/{newest}/delete').status_code == 200
        _join_help_warm(pm, 'auto')
    with ZipFile(cache) as archive:
        assert archive.read('report.json') == old_header
    pm.close_all()
    assert execute_tool(pm, 'check_platform_calls', {}) == expected


def test_failed_reindex_archive_write_is_retried_after_ready(project, monkeypatch, caplog):
    pm, _ = project
    replacement = platform_check.replace_with_retry
    attempts = []

    def fail_once(temp, path):
        attempts.append(pm.get_db('demo').get_stats()['status'])
        if len(attempts) == 1:
            raise PermissionError('Пробная блокировка архива')
        replacement(temp, path)

    monkeypatch.setattr(platform_check, 'replace_with_retry', fail_once)
    pm.reindex('demo')
    _join_workers(_report_workers(pm))
    assert attempts == ['indexing', 'ready']
    assert 'Пробная блокировка архива' in caplog.text
    pm.close_all()
    assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {})


def test_upload_during_final_reindex_warms_the_new_help(project, monkeypatch):
    pm, _ = project
    newest = '8.3.28.1'
    monkeypatch.setattr(web, '_pm', pm)
    old_started, release_old = Event(), Event()
    built = []
    original = platform_check._Report._build

    def track(report, engine, conn, index, mode):
        built.append(index.version)
        if index.version == VERSION:
            old_started.set()
            assert release_old.wait(timeout=10)
        return original(report, engine, conn, index, mode)

    monkeypatch.setattr(platform_check._Report, '_build', track)
    try:
        assert pm.reindex_async('demo')['status'] == 'started'
        assert old_started.wait(timeout=5)
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            response = client.post('/api/admin/platform-help', data={'version': newest},
                                   files={'context': ('shcntx_ru.hbk', context_book(newest))})
        assert response.status_code == 200 and 'Справка загружена' in response.text
        _join_workers([t for t in running_threads() if t.name == f'platform-check-warm-{newest}'])
        assert 'Дождитесь окончания переиндексации' in execute_tool(pm, 'check_platform_calls', {})
    finally:
        workers = [t for t in running_threads() if t.name == 'reindex-demo']
        release_old.set()
        _join_workers(workers)
    assert built == [VERSION, newest]
    result = execute_tool(pm, 'check_platform_calls', {})
    assert f'по справке {newest}' in result and 'НесуществующийВызов()' in result


def test_close_all_waits_for_help_warm_coordinator(project, monkeypatch):
    pm, _ = project
    entered, release, joining = Event(), Event(), Event()
    original_search = pm.get_search
    db = pm.get_db('demo')

    def search(project_id):
        if current_thread().name == f'platform-check-warm-{VERSION}':
            entered.set()
            assert release.wait(timeout=10)
        return original_search(project_id)

    monkeypatch.setattr(pm, 'get_search', search)
    pm.warm_platform_checks(VERSION)
    try:
        assert entered.wait(timeout=5)
        worker, = [t for t in running_threads() if t.name == f'platform-check-warm-{VERSION}']
        original_join = worker.join

        def join(*args, **kwargs):
            joining.set()
            return original_join(*args, **kwargs)

        monkeypatch.setattr(worker, 'join', join)
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(pm.close_all)
            try:
                assert joining.wait(timeout=5)
                assert not closing.done()
            finally:
                release.set()
            closing.result(timeout=5)
    finally:
        release.set()
        _join_workers([t for t in running_threads() if t.name == 'platform-check-demo'])
    assert db.conn is None and not pm._db_pool


def test_close_all_waits_for_report_archive_republication(project, monkeypatch):
    pm, _ = project
    assert 'НесуществующийВызов()' in _ready_check(pm)
    cache = _report_archive(pm)
    cache.unlink()
    publishing, release, joining = Event(), Event(), Event()
    replace = platform_check.replace_with_retry
    db = pm.get_db('demo')

    def delay_replace(temp, path):
        publishing.set()
        assert release.wait(timeout=10)
        replace(temp, path)

    monkeypatch.setattr(platform_check, 'replace_with_retry', delay_replace)
    pm.warm_platform_checks(project_id='demo')
    try:
        assert publishing.wait(timeout=5)
        writer, = _report_workers(pm)
        original_join = writer.join

        def join(*args, **kwargs):
            joining.set()
            return original_join(*args, **kwargs)

        monkeypatch.setattr(writer, 'join', join)
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(pm.close_all)
            try:
                assert joining.wait(timeout=5)
                assert not closing.done() and db.conn is not None and not cache.exists()
            finally:
                release.set()
            closing.result(timeout=5)
        assert not writer.is_alive() and cache.is_file()
    finally:
        release.set()
    assert db.conn is None and not pm._db_pool
    _assert_persisted_report(pm, VERSION)


def test_reindex_updates_mode_in_persisted_report(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'СтрШаблон("%1", 1);\n')
    pm.reindex('demo')
    assert 'Замечаний нет' in execute_tool(pm, 'check_platform_calls', {})
    config = source / 'Configuration.xml'
    config.write_text(config.read_text(encoding='utf-8').replace('Version8_3_24', 'Version8_3_5'),
                      encoding='utf-8')
    platform_check._REPORTS.clear()
    assert 'Замечаний нет' in _ready_check(pm)
    pm.reindex('demo')
    platform_check._REPORTS.clear()
    assert 'требует режима совместимости 8.3.6' in _ready_check(pm)


def test_module_variable_shadows_global_property(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Перем ПримерТаблицы;
Процедура Проверка()
    ПримерТаблицы.ДругойМетод();
КонецПроцедуры
Процедура ДругаяПроверка()
    Перем ExampleTable;
    ExampleTable.ДругойМетод();
КонецПроцедуры
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:3 — Не проверено' in result and 'Module.bsl:7 — Не проверено' in result
    assert '— Ошибка' not in result


def test_form_attribute_and_object_table_shadow_global_property(project):
    pm, source = project
    config = source / 'Configuration.xml'
    xml = config.read_text(encoding='utf-8').replace('</ChildObjects>', '<Catalog>Проба</Catalog></ChildObjects>')
    config.write_text(xml, encoding='utf-8')
    _write_module(source, 'Catalogs/Проба.xml', '''<MetaDataObject>
  <Catalog>
    <Properties><Name>Проба</Name></Properties>
    <ChildObjects>
      <Form>Форма</Form>
      <TabularSection><Properties><Name>ПримерТаблицы</Name></Properties></TabularSection>
    </ChildObjects>
  </Catalog>
</MetaDataObject>''')
    _write_module(source, 'Catalogs/Проба/Forms/Форма/Ext/Form.xml',
                  '<Form><Attributes><Attribute name="ПримерТаблицы"/></Attributes></Form>')
    _write_module(source, 'Catalogs/Проба/Forms/Форма/Ext/Form/Module.bsl', '''&НаКлиенте
Процедура Проверка()
    ПримерТаблицы.ДругойМетод();
КонецПроцедуры
&НаСервереБезКонтекста
Процедура ПроверкаБезКонтекста()
    ПримерТаблицы.ДругойМетод();
КонецПроцедуры
''')
    _write_module(source, 'Catalogs/Проба/Ext/ObjectModule.bsl', '''Процедура Проверка()
    ПримерТаблицы.ДругойМетод();
КонецПроцедуры
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Form/Module.bsl:3 — Не проверено' in result
    assert 'Form/Module.bsl:7 — Предупреждение — получатель' in result
    assert 'Ошибка — в справке нет метода типа таблицазначений' in result
    assert 'ObjectModule.bsl:2 — Не проверено' in result


def test_comment_with_quotes_inside_multiline_string(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Процедура Проверка()
    Текст = "Начало строки
    // Комментарий с текстом "ЛожныйВызов()".

    |ФрагментСтроки()
    |";
    Опечатка();
КонецПроцедуры
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert result.count('— Ошибка') == 1 and 'Module.bsl:7 — Ошибка' in result
    assert 'Опечатка()' in result


def test_declarations_containing_yo_are_indexed(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Процедура Проверка()
    Ёлка();
    ЕщёМетод(1);
КонецПроцедуры
Процедура Ёлка() Экспорт
КонецПроцедуры
Процедура ЕщёМетод(
    Значение)
КонецПроцедуры
''')
    pm.reindex('demo')
    assert 'Замечаний нет' in execute_tool(pm, 'check_platform_calls', {})


def test_known_platform_methods_omitted_from_help(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''Метаданные.Представление();
Metadata.Presentation();
Метаданные.ПолноеИмя();
Metadata.FullName();
Справочники.Пример.ПолучитьИмяПредопределенного(Ссылка);
Catalogs.Пример.GetPredefinedItemName(Ссылка);
Запись = Новый КаноническаяЗаписьXML;
Запись.УстановитьСтроку(ТипКаноническогоXML.КаноническийXML);
Writer = New XMLCanonicalizingWriter;
Writer.SetString(ТипКаноническогоXML.КаноническийXML);
ОбработкаСтрокиXML.УдалитьНедопустимыеСимволыXML("Проба");
XMLStringProcessing.DeleteDisallowedXMLCharacters("Проба");
Преобразователь = Новый ПреобразованиеXSL;
Преобразователь.ЗагрузитьИзСтроки("Проба");
Преобразователь.LoadFromString("Проба");
ПолучитьИнформациюОСетевыхАдаптерах();
GetNetworkAdaptersInformation();
Metadata.Typo();
''')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert result.count('— Ошибка') == 1 and 'Module.bsl:18 — Ошибка' in result
    assert 'Metadata.Typo()' in result
    config = source / 'Configuration.xml'
    xml = config.read_text(encoding='utf-8')
    config.write_text(xml.replace('Version8_3_24', 'Version8_3_19'), encoding='utf-8')
    pm.reindex('demo')
    restricted = _ready_check(pm)
    assert restricted.count('требует режима совместимости 8.3.20') == 2


def test_predefined_item_name_on_chart_managers(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', '''ПланыВидовХарактеристик.Проба.ПолучитьИмяПредопределенного(Ссылка);
ChartsOfCharacteristicTypes.Проба.GetPredefinedItemName(Ссылка);
ПланыСчетов.Проба.ПолучитьИмяПредопределенного(Ссылка);
ChartsOfAccounts.Проба.GetPredefinedItemName(Ссылка);
ПланыВидовРасчета.Проба.ПолучитьИмяПредопределенного(Ссылка);
ChartsOfCalculationTypes.Проба.GetPredefinedItemName(Ссылка);
''')
    pm.reindex('demo')
    assert 'Замечаний нет' in _ready_check(pm)


def test_global_property_cannot_be_called(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'Справочники();\nCatalogs();\n')
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert result.count('— Ошибка') == 2
    assert 'Справочники()' in result and 'Catalogs()' in result


def test_source_with_no_bsl_modules(project):
    pm, source = project
    _extension(pm, source)
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {'source_id': 'sample_ext'})
    assert 'в источнике нет модулей' in result.casefold()


def test_explicit_help_version_with_unknown_project(project):
    pm, _ = project
    args = {'query': 'СтрНайти', 'project_id': 'unknown'}
    assert 'проект «unknown» не найден' in execute_tool(pm, 'platform_help', args)
    result = execute_tool(pm, 'platform_help', dict(args, version=VERSION))
    assert f'Справка платформы {VERSION}' in result and 'СтрНайти (StrFind)' in result


def _archives(pm):
    return sorted(platform_check.report_directory(pm._index_path('demo')).glob('platform-check*.zip'))


def test_repeated_help_uploads_keep_bounded_archives(project, monkeypatch):
    """Повторная загрузка той же версии меняет ключ отчёта; архивов не больше лимита."""
    pm, _ = project
    assert 'НесуществующийВызов()' in _ready_check(pm)
    monkeypatch.setattr(web, '_pm', pm)
    with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
        for _ in range(platform_check._MAX_ARCHIVES + 3):
            _upload_help(client, VERSION)
            _join_help_warm(pm, VERSION)
    archives = _archives(pm)
    assert 0 < len(archives) <= platform_check._MAX_ARCHIVES
    assert _report_archive(pm) in archives


def test_archive_trim_stays_bounded_when_all_mtimes_match(project):
    pm, _ = project
    db_path = pm._index_path('demo')
    directory = platform_check.report_directory(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    same_time = 1_700_000_000
    selected = directory / ('platform-check-' + '0' * 64 + '.zip')
    selected.write_bytes(b'selected')
    platform_check._record_report_archive(selected, 'selected')
    platform_check.select_report_version(db_path, 'selected')

    for number in range(240):
        published = directory / f'platform-check-{number + 1:064x}.zip'
        published.write_bytes(b'report')
        platform_check._record_report_archive(published, f'version-{number}')
        for archive in directory.glob('platform-check-*.zip'):
            os.utime(archive, (same_time, same_time))
        platform_check._trim_archives(published)

    archives = list(directory.glob('platform-check-*.zip'))
    assert len(archives) <= platform_check._MAX_ARCHIVES
    assert selected in archives
    assert all(filename in {path.name for path in archives}
               for filename in platform_check._VERSION_ARCHIVES[directory.resolve()].values())


def test_project_report_namespace_does_not_collide_with_service_directories(tmp_path):
    data_dir, index_dir = tmp_path / 'data', tmp_path / 'index'
    pm = ProjectManager(str(data_dir), index_dir=str(index_dir))
    pm.platform_help.build_from_bytes(VERSION, context_book())
    # Parentheses keep Path joining explicit while representing a pre-migration archive.
    old_collision = index_dir / 'platform-help' / ('platform-check-' + 'a' * 64 + '.zip')
    old_collision.write_bytes(b'legacy report')
    try:
        for project_id in ('platform-help', 'reports', 'ordinary'):
            pm.create_project(project_id, project_id)
            report_dir = platform_check.report_directory(pm._index_path(project_id))
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / ('platform-check-' + 'b' * 64 + '.zip')).write_bytes(b'report')
            pm.delete_project(project_id)
            assert not list(report_dir.glob('platform-check*.zip'))
            assert not report_dir.exists()
        assert not old_collision.exists()
        assert pm.platform_help.path(VERSION).is_file()
    finally:
        pm.close_all()


def test_deleted_help_version_and_project_release_archive_bookkeeping(project, monkeypatch):
    pm, _ = project
    older = '8.3.9.1'
    pm.platform_help.build_from_bytes(older, context_book(older))
    db_path = pm._index_path('demo')
    directory = platform_check.report_directory(db_path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    archived = directory / ('platform-check-' + 'c' * 64 + '.zip')
    archived.write_bytes(b'old report')
    platform_check._record_report_archive(archived, older)
    platform_check.select_report_version(db_path, older)
    monkeypatch.setattr(pm, 'warm_platform_checks', lambda *args, **kwargs: None)
    monkeypatch.setattr(web, '_pm', pm)
    with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
        response = client.post(f'/api/admin/platform-help/{older}/delete')
    assert response.status_code == 200
    assert older not in platform_check._SELECTED_VERSIONS.values()
    assert older not in platform_check._VERSION_ARCHIVES.get(directory, {})

    platform_check.select_report_version(db_path, older)
    pm.delete_project('demo')
    assert directory not in platform_check._SELECTED_VERSIONS
    assert directory not in platform_check._SELECTED_ARCHIVES
    assert directory not in platform_check._VERSION_ARCHIVES
    assert not archived.exists()


def test_selected_report_archive_is_recent_for_lru_pruning(project):
    pm, _ = project
    assert 'НесуществующийВызов()' in _ready_check(pm)
    selected = _report_archive(pm)
    old = time.time() - 3600
    os.utime(selected, (old, old))

    # Выбор того же отчёта из памяти обязан обновить использование архива.
    assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {})
    assert selected.stat().st_mtime > old

    directory = selected.parent
    published = directory / ('platform-check-' + 'f' * 64 + '.zip')
    published.write_bytes(b'new')
    for number in range(3):
        path = directory / f'platform-check-{number:064x}.zip'
        path.write_bytes(b'old')
        os.utime(path, (old + number, old + number))
    platform_check._trim_archives(published)
    assert selected.exists()


def test_late_help_reports_keep_archive_for_selected_version(project, monkeypatch):
    pm, _ = project
    versions = ('8.3.23.1', '8.3.24.1', '8.3.25.1', '8.3.26.1')
    for version in versions:
        pm.platform_help.build_from_bytes(version, context_book(version))

    pm.update_project('demo', platform_version=versions[0])
    assert 'НесуществующийВызов()' in _ready_check(pm)
    selected_archive = _report_archive(pm)

    entered = Barrier(4)
    release = Event()
    original_build = platform_check._Report._build

    def delayed_build(report, engine, conn, index, mode):
        if index.version in versions[1:]:
            entered.wait(timeout=5)
            assert release.wait(timeout=5)
        return original_build(report, engine, conn, index, mode)

    monkeypatch.setattr(platform_check._Report, '_build', delayed_build)
    try:
        engine = pm.get_search('demo')
        revision = platform_check._index_revision(engine.db.read_conn())
        for version in versions[1:]:
            pm.update_project('demo', platform_version=version)
            index = pm.platform_help.get(version)
            with pytest.raises(platform_check.ReportUnavailable, match='готовится'):
                platform_check._request_report(engine, index, pm.platform_mode('demo'),
                                               revision, 'demo', warm=True)

        entered.wait(timeout=5)
        pm.update_project('demo', platform_version=versions[0])
        result = execute_tool(pm, 'check_platform_calls', {})
        assert 'НесуществующийВызов()' in result
        assert selected_archive.exists()
    finally:
        release.set()

    _join_workers(_report_workers(pm))
    assert selected_archive.exists()
    platform_check._REPORTS.clear()
    assert 'НесуществующийВызов()' in execute_tool(pm, 'check_platform_calls', {})


def test_startup_removes_incomplete_report_archives(project):
    pm, _ = project
    temp = platform_check.report_directory(pm._index_path('demo')) / ('.platform-check-' + 'a' * 64 + '.zip.crash.tmp')
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b'incomplete')
    restarted = ProjectManager(str(pm.data_dir))
    assert not temp.exists()
    restarted.close_all()


def test_project_deletion_waits_for_report_archive_writer(project, monkeypatch):
    pm, _ = project
    assert 'НесуществующийВызов()' in _ready_check(pm)
    cache = _report_archive(pm)
    cache.unlink()
    publishing, release = Event(), Event()
    replace = platform_check.replace_with_retry

    def delay_replace(temp, path):
        publishing.set()
        assert release.wait(timeout=10)
        replace(temp, path)

    monkeypatch.setattr(platform_check, 'replace_with_retry', delay_replace)
    pm.warm_platform_checks(project_id='demo')
    assert publishing.wait(timeout=5)
    writer, = _report_workers(pm)
    project_dir = pm.projects_dir / 'demo'
    index_dir = pm._index_path('demo').parent
    with ThreadPoolExecutor(1) as pool:
        deleting = pool.submit(pm.delete_project, 'demo')
        try:
            time.sleep(0.05)
            assert not deleting.done()
        finally:
            release.set()
        deleting.result(timeout=5)
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert not project_dir.exists() and not index_dir.exists()


def test_platform_warm_requests_coalesce_to_latest_project_version(project, monkeypatch):
    pm, _ = project
    newest = '8.3.28.1'
    pm.update_project('demo', platform_version=VERSION)
    pm.platform_help.build_from_bytes(newest, context_book(newest))
    entered, release = Event(), Event()
    warmed = []

    def track(engine, index, mode, project_id, *args, **kwargs):
        warmed.append(index.version)
        if len(warmed) == 1:
            entered.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(platform_check, 'warm_ready_report', track)
    pm.warm_platform_checks(project_id='demo')
    try:
        assert entered.wait(timeout=5)
        pm.update_project('demo', platform_version=newest)
        for _ in range(8):
            pm.warm_platform_checks(project_id='demo')
        coordinators = [t for t in running_threads() if t.name.startswith('platform-check-warm-')]
        assert len(coordinators) == 1
    finally:
        release.set()
    _join_workers([t for t in running_threads() if t.name.startswith('platform-check-warm-')])
    assert warmed == [VERSION, newest]


def test_busy_archive_does_not_stop_pruning(project, monkeypatch):
    """Занятый архив (Windows держит открытый файл) не прерывает удаление остальных."""
    pm, _ = project
    assert 'НесуществующийВызов()' in _ready_check(pm)
    directory = platform_check.report_directory(pm._index_path('demo'))
    stale = [directory / f'platform-check-{str(i) * 64}.zip' for i in range(1, 4)]
    for path in stale:
        path.write_bytes(b'old')
    busy = stale[0]
    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == busy:
            raise PermissionError(13, 'locked', str(path))
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', locked)
    monkeypatch.setattr(platform_check, 'retry_on_permission_error', lambda action: action())
    platform_check.prune_report_archives(pm._index_path('demo'))
    assert busy.exists()
    assert not any(path.exists() for path in stale[1:])
