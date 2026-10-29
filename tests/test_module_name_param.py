"""
Единое имя параметра модуля в MCP — module_name (0.3.0).

Раньше одно и то же «имя модуля» называлось module_path в
get_procedure_code/get_module_outline и module_name в get_call_tree.
Синонима module_path нет: SDK (mcp 2.2) проверяет аргументы по модели
pydantic, неизвестные отбрасывает до обработчика (а **kwargs в инструменте
не поддерживает), так что старое имя до tools.py не дошло бы — вызов
с module_path получает ошибку «module_name: Field required».
"""

import asyncio
import tempfile

import pytest

from src.mcp_server.tools import execute_tool
from test_mcp_usability import _pm_with_xml_project

pytest.importorskip("mcp.server.mcpserver")

MODULE_TOOLS = ('get_procedure_code', 'get_module_outline', 'get_call_tree')


def _sdk(mcp, coro_fn):
    from mcp import Client

    async def go():
        async with Client(mcp) as client:
            return await coro_fn(client)
    return asyncio.run(go())


def _text(result) -> str:
    return '\n'.join(c.text for c in result.content if getattr(c, 'text', None))


def test_schema_has_module_name_only():
    from src.mcp_server.server import create_mcp_server
    from src.core.project_manager import ProjectManager
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            tools = _sdk(create_mcp_server(pm), lambda c: c.list_tools()).tools
        finally:
            pm.close_all()
    schemas = {t.name: t.input_schema for t in tools}
    for name in MODULE_TOOLS:
        props = schemas[name]['properties']
        assert 'module_name' in props, name
        assert 'module_path' not in props, name
    for name in ('get_procedure_code', 'get_module_outline'):
        assert 'module_name' in schemas[name]['required'], name
    # Описание одно на все три инструмента.
    descriptions = {schemas[n]['properties']['module_name']['description'].split('. Без него')[0]
                    for n in MODULE_TOOLS}
    assert len(descriptions) == 1, descriptions
    # Фильтры по части имени — другой смысл, не переименованы.
    assert 'module_filter' in schemas['search_procedures']['properties']
    assert 'type_filter' in schemas['search_attributes']['properties']


def test_module_name_works_and_old_module_path_is_rejected():
    from src.mcp_server.server import create_mcp_server
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            mcp = create_mcp_server(pm)
            ok = _sdk(mcp, lambda c: c.call_tool('get_procedure_code', {
                'module_name': 'ОбщегоНазначения', 'procedure_name': 'ЗначениеРеквизитаОбъекта'}))
            assert ok.is_error is False and '"общий"' in _text(ok), _text(ok)
            outline = _sdk(mcp, lambda c: c.call_tool('get_module_outline', {
                'module_name': 'ОбщегоНазначения'}))
            assert outline.is_error is False and 'ЗначениеРеквизитаОбъекта' in _text(outline)

            for tool, args in (('get_procedure_code', {'module_path': 'ОбщегоНазначения',
                                                       'procedure_name': 'ЗначениеРеквизитаОбъекта'}),
                               ('get_module_outline', {'module_path': 'ОбщегоНазначения'})):
                old = _sdk(mcp, lambda c: c.call_tool(tool, args))
                assert old.is_error is True, tool
                assert 'module_name' in _text(old) and 'Field required' in _text(old), _text(old)
        finally:
            pm.close_all()


def test_hint_for_missing_procedure_names_module_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            out = execute_tool(pm, 'get_procedure_code', {
                'module_name': 'ОбщегоНазначения', 'procedure_name': 'НетТакой'})
            assert 'get_module_outline(module_name="' in out, out
            assert 'module_path' not in out
        finally:
            pm.close_all()


def test_playground_form_uses_module_name():
    """Формы playground строятся из схемы MCP — имя то же."""
    from src.web.routes_playground import tool_forms
    from src.mcp_server.server import create_mcp_server
    from src.core.project_manager import ProjectManager
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            tools = _sdk(create_mcp_server(pm), lambda c: c.list_tools()).tools
        finally:
            pm.close_all()
    schemas = [{'name': t.name, 'description': t.description, 'input_schema': t.input_schema}
               for t in tools]
    params = {t['name']: [p['name'] for p in t['params']] for t in tool_forms(schemas)}
    assert 'module_name' in params['get_module_outline']
    assert 'module_name' in params['get_call_tree']
    assert 'module_name' in params['get_procedure_code']
    assert not any('module_path' in p for p in params.values())
