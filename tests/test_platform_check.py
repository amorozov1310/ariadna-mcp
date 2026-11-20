"""Проверка платформы через обычные точки входа проекта и MCP."""

import asyncio
import shutil
from pathlib import Path

import pytest

from src.core import platform_check
from src.core.platform_help import compatibility_warning, minimum_compatibility_mode
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool
from tests.fixtures.generate_platform_help import _book, context_book


FIXTURE = Path(__file__).parent / 'fixtures' / 'platform_check'
VERSION = '8.3.27.1'


@pytest.fixture
def project(tmp_path):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('demo', 'Демо')
    source = pm.projects_dir / 'demo' / 'sources' / 'sample' / 'xml'
    shutil.copytree(FIXTURE, source)
    pm.add_source('demo', 'sample', 'Основная')
    pm.reindex('demo')
    pm.platform_help.build_from_bytes(VERSION, context_book())
    try:
        yield pm, source
    finally:
        pm.close_all()


def test_platform_calls(project):
    pm, _ = project
    result = execute_tool(pm, 'check_platform_calls', {})
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


def test_help_mode_by_project(project):
    pm, source = project
    pm.create_project('other', 'Другой')
    args = {'query': 'ValueTable.Count', 'version': VERSION}
    assert 'Недоступно' in execute_tool(pm, 'platform_help', dict(args, project_id='demo'))
    assert 'Недоступно' not in execute_tool(pm, 'platform_help', args)
    assert 'Недоступно' not in execute_tool(pm, 'platform_help', dict(args, project_id='other'))
    config = source / 'Configuration.xml'
    xml = config.read_text(encoding='utf-8')
    for mode in ('Version8_3_25', 'DontUse'):
        config.write_text(xml.replace('Version8_3_24', mode), encoding='utf-8')
        assert 'Недоступно' not in execute_tool(pm, 'platform_help', dict(args, project_id='demo'))
    config.write_text(xml.replace('Version8_3_24', 'Version8_3_9'), encoding='utf-8')
    result = execute_tool(pm, 'platform_help', {'project_id': 'demo', 'query': 'ПримерРежима'})
    assert 'требует режима совместимости 8.3.26' in result and 'режим совместимости 8.3.9' in result


def test_pages_and_missing_help(project):
    pm, _ = project

    def check(**args):
        return execute_tool(pm, 'check_platform_calls', args)

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
    assert 'файл изменился' in execute_tool(pm, 'check_platform_calls', {})
    pm.reindex('demo')
    result = execute_tool(pm, 'check_platform_calls', {})
    assert 'Module.bsl:4 — Не проверено' in result and 'ExampleTable.Unknown()' in result
    assert 'Module.bsl:6 — Предупреждение — метод требует режима совместимости 8.3.25' in result
    assert 'Module.bsl:9 — Не проверено' in result and 'T.AfterBranch()' in result
    assert 'StrFind' not in result and 'Пример.Внутренняя' not in result
    assert 'Module.bsl:17 — Предупреждение — получатель' in result
    assert 'Module.bsl:19 — Ошибка' in result and 'ОпечаткаВМодуле()' in result
    module.unlink()
    assert 'нет исходного текста' in execute_tool(pm, 'check_platform_calls', {})


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


def test_cached_page_rechecks_only_modules_on_page(project, monkeypatch):
    pm, source = project
    for path in ('CommonModules/Пример/Ext/Module.bsl', 'Ext/SessionModule.bsl'):
        _write_module(source, path, 'Опечатка();\n' * 500)
    pm.reindex('demo')
    execute_tool(pm, 'check_platform_calls', {'offset': 500, 'limit': 1})
    inputs = []
    original = platform_check._module_input

    def track(engine, mod):
        inputs.append(mod['file_path'])
        return original(engine, mod)

    monkeypatch.setattr(platform_check, '_module_input', track)
    result = execute_tool(pm, 'check_platform_calls', {'offset': 500, 'limit': 1})
    assert 'SessionModule.bsl:1' in result
    assert inputs == ['Ext/SessionModule.bsl']


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
    pm.reindex('demo')
    observed = []
    diagnose = platform_check._diagnosis

    def track(call, *args):
        observed.append(call.name)
        return diagnose(call, *args)

    monkeypatch.setattr(platform_check, '_diagnosis', track)
    first = execute_tool(pm, 'check_platform_calls', {'limit': 1})
    last = execute_tool(pm, 'check_platform_calls', {'limit': 1, 'offset': 48})
    assert 'Опечатка0()' in first and 'Опечатка48()' in last
    assert observed == [f'опечатка{i}' for i in range(50)]
    module.write_text('НоваяОпечатка();\n', encoding='utf-8')
    assert 'файл изменился' in execute_tool(pm, 'check_platform_calls', {})
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


def test_rebuild_of_help_invalidates_report(project):
    pm, source = project
    _write_module(source, 'CommonModules/Пример/Ext/Module.bsl', 'СтрНайти("x", "y");\n')
    pm.reindex('demo')
    assert 'Замечаний нет' in execute_tool(pm, 'check_platform_calls', {})
    pm.platform_help.build_from_bytes(VERSION, _book([
        ('objects/Global context/methods/Other.html', 'ДругойВызов',
         '<h1>ДругойВызов (Other)</h1>'),
    ]))
    result = execute_tool(pm, 'check_platform_calls', {})
    assert '— Ошибка' in result and 'СтрНайти(' in result


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
    restricted = execute_tool(pm, 'check_platform_calls', {})
    assert restricted.count('требует режима совместимости 8.3.20') == 2


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
