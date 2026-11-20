"""Проверка платформы через обычные точки входа проекта и MCP."""

import shutil
from pathlib import Path

import pytest

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool
from tests.fixtures.generate_platform_help import context_book


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
    assert 'метод появился в 8.3.25' in result and 'Таблица.cOuNt()' in result
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
    assert 'offset=2' in second and 'cOuNt' in second and 'НесуществующийВызов' not in second
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
    assert 'Module.bsl:6 — Предупреждение' in result and 'T.Count()' in result
    assert 'Module.bsl:9 — Не проверено' in result and 'T.AfterBranch()' in result
    assert 'StrFind' not in result and 'Пример.Внутренняя' not in result
    assert 'Module.bsl:17 — Предупреждение' in result and 'получатель exampletable появился в 8.3.27' in result
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
