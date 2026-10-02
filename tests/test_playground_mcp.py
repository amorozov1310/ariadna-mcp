"""
Playground Web UI вызывает тот же код, что MCP (0.3.0).

Раньше routes_playground.py был отдельной реализацией: 11 инструментов из
16, без offset и потолка limit, свои умолчания и свой формат ответа. Теперь
формы строятся из схемы MCP-сервера (list_tools), а ответ — это
execute_tool с теми же аргументами, что подставляет SDK.
"""

import asyncio
import io
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

pytest.importorskip("mcp.server.mcpserver")

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import MAX_LIMIT, execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'

# Аргументы для каждого из 16 инструментов (без project_id) — так, чтобы
# ответ был содержательным на фикстуре. Дополняются умолчаниями схемы.
CASES = {
    'list_projects': {},
    'list_sources': {},
    'get_index_status': {},
    'search_metadata': {'query': 'Виды', 'limit': '1'},
    'get_object_details': {'full_name': 'Справочник.ВидыУдобрений', 'detail': 'full'},
    'search_attributes': {'query': 'Основное'},
    'find_references': {'object_name': 'ВидыУдобрений'},
    'list_objects': {'offset': '0'},
    'search_procedures': {'query': 'Рассчитать', 'limit': '2', 'offset': '1'},
    'get_procedure_code': {'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'},
    'get_module_outline': {'module_name': 'ЦеныСервер'},
    'get_call_tree': {'procedure_name': 'РассчитатьЦену', 'direction': 'up', 'depth': '2'},
    'search_code': {'query': 'Возврат'},
    'diagnose_index': {},
    'reindex': {},
    'remove_source': {'source_id': 'main'},          # без confirm — только предпросмотр
}


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


def _indexed_pm(data_dir: str) -> ProjectManager:
    pm = ProjectManager(data_dir)
    pm.create_project('p1', 'p1')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def _wait_ready(pm: ProjectManager, project_id: str = 'p1') -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        if pm.get_project(project_id).status in ('ready', 'error'):
            return
        time.sleep(0.05)
    raise AssertionError('переиндексация не закончилась')


@pytest.fixture
def web(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed_pm(tmpdir)
        monkeypatch.setattr(app_module, '_pm', pm)
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        try:
            yield pm, client
        finally:
            _wait_ready(pm)
            pm.close_all()


def _schemas() -> dict:
    from src.mcp_server.server import create_mcp_server

    async def go():
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = ProjectManager(tmpdir)
            try:
                return {t.name: t.input_schema for t in await create_mcp_server(pm).list_tools()}
            finally:
                pm.close_all()
    return asyncio.run(go())


def _sdk_args(schema: dict, params: dict) -> dict:
    """Аргументы, которые SDK передал бы обработчику: значения формы,
    приведённые к типам схемы, плюс умолчания."""
    args = {}
    for name, prop in schema['properties'].items():
        if name in params:
            value = params[name]
            if prop.get('type') == 'integer':
                value = int(value)
            args[name] = value
        elif 'default' in prop:
            args[name] = prop['default']
    args['project_id'] = 'p1' if 'project_id' in schema['properties'] else None
    if 'project_id' not in schema['properties']:
        del args['project_id']
    return args


def _play(client, tool: str, params: dict) -> dict:
    r = client.post('/api/projects/p1/playground', json={'tool': tool, 'params': params})
    assert r.status_code == 200, r.text
    return r.json()


def test_cases_cover_every_mcp_tool():
    assert set(CASES) == set(_schemas())
    assert len(CASES) == 16


@pytest.mark.parametrize('tool', sorted(CASES))
def test_playground_answer_equals_execute_tool(web, tool):
    pm, client = web
    params = CASES[tool]
    args = _sdk_args(_schemas()[tool], params)
    got = _play(client, tool, params)
    if tool == 'reindex':
        _wait_ready(pm)
    expected = execute_tool(pm, tool, args)
    if tool == 'reindex':
        _wait_ready(pm)
    assert got['result'] == expected
    assert got['is_error'] is False, got['result']
    # Ответ содержательный, а не «ничего не найдено».
    assert not any(s in got['result'] for s in ('No results', 'not found', 'No procedures')), got['result']


def test_playground_answer_equals_what_agent_sees_via_sdk(web):
    """Для читающих инструментов — сравнение с самим ответом SDK (то, что
    получает агент), а не только с execute_tool."""
    from mcp import Client
    from src.mcp_server.server import create_mcp_server
    pm, client = web
    mcp = create_mcp_server(pm)
    schemas = _schemas()

    async def call(tool, args):
        async with Client(mcp) as c:
            res = await c.call_tool(tool, args)
            return res.is_error, '\n'.join(x.text for x in res.content if getattr(x, 'text', None))

    for tool in ('search_procedures', 'get_procedure_code', 'list_objects', 'search_code'):
        args = _sdk_args(schemas[tool], CASES[tool])
        is_error, text = asyncio.run(call(tool, args))
        assert not is_error
        assert _play(client, tool, CASES[tool])['result'] == text, tool


def test_playground_forms_match_mcp_schema(web):
    """Набор инструментов и параметров формы == схема MCP без project_id.
    Падает, если в MCP добавят инструмент или параметр, а в форме его нет."""
    import re
    import json
    from html import unescape
    _, client = web
    page = client.get('/projects/p1/playground')
    assert page.status_code == 200
    meta = json.loads(unescape(re.search(r'const TOOLS_META = (.*?);\n', page.text).group(1)))
    forms = {t['name']: {p['name']: p for p in t['params']} for t in meta}
    schemas = _schemas()
    assert set(forms) == set(schemas)
    for tool, schema in schemas.items():
        expected = set(schema['properties']) - {'project_id'}
        assert set(forms[tool]) == expected, tool
        for name in expected:
            prop, field = schema['properties'][name], forms[tool][name]
            assert field['description'] == prop.get('description', ''), (tool, name)
            assert field['required'] == (name in schema.get('required', [])), (tool, name)
            assert field['default'] == prop.get('default'), (tool, name)
    # Постраничные — с offset; confirm — флажок, по умолчанию выключен.
    for tool in ('search_metadata', 'search_attributes', 'find_references', 'list_objects',
                 'search_procedures', 'search_code'):
        assert 'offset' in forms[tool], tool
    assert forms['remove_source']['confirm']['type'] == 'checkbox'
    assert forms['remove_source']['confirm']['default'] is False
    assert forms['list_objects']['limit']['default'] == 100
    # Подсказки значений из проекта.
    assert 'main' in forms['list_objects']['source_id']['suggestions']


def test_remove_source_needs_confirm_like_mcp(web):
    pm, client = web
    preview = _play(client, 'remove_source', {'source_id': 'main', 'confirm': False})
    assert 'confirm=true' in preview['result'] and not preview['is_error']
    assert [s.id for s in pm.get_project('p1').sources] == ['main']

    with tempfile.TemporaryDirectory() as tmpdir:
        twin = _indexed_pm(tmpdir)
        try:
            expected = execute_tool(twin, 'remove_source',
                                    {'project_id': 'p1', 'source_id': 'main', 'confirm': True})
        finally:
            twin.close_all()
    removed = _play(client, 'remove_source', {'source_id': 'main', 'confirm': True})
    assert removed['result'] == expected
    assert 'removed' in removed['result']
    assert pm.get_project('p1').sources == []


def test_limit_over_500_is_capped_like_mcp(web):
    pm, client = web
    got = _play(client, 'list_objects', {'limit': '100000'})
    assert f'урезан до {MAX_LIMIT}' in got['result']
    args = _sdk_args(_schemas()['list_objects'], {'limit': '100000'})
    assert got['result'] == execute_tool(pm, 'list_objects', args)


def test_errors_are_flagged(web):
    _, client = web
    unknown_source = _play(client, 'reindex', {'source_id': 'нет'})
    assert unknown_source['is_error'] is True
    assert unknown_source['result'].startswith("Error: source 'нет' not found")
    missing = _play(client, 'get_procedure_code', {'module_name': 'ЦеныСервер'})
    assert missing['is_error'] is True and 'procedure_name' in missing['result']
    bad_int = _play(client, 'list_objects', {'limit': 'много'})
    assert bad_int['is_error'] is True and 'limit' in bad_int['result']
    unknown_tool = _play(client, 'nope', {})
    assert unknown_tool['is_error'] is True


def test_project_id_is_not_a_form_field_and_page_project_is_used(web):
    pm, client = web
    pm.create_project('p2', 'p2')
    # Чужой project_id из запроса игнорируется — проект страницы.
    got = _play(client, 'list_sources', {'project_id': 'p2'})
    assert got['result'] == execute_tool(pm, 'list_sources', {'project_id': 'p1'})
