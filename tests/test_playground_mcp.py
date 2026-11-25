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

# Аргументы для каждого инструмента (без project_id) — так, чтобы
# ответ был содержательным на фикстуре. Дополняются умолчаниями схемы.
CASES = {
    'list_projects': {},
    'list_sources': {},
    'get_index_status': {},
    'search_metadata': {'query': 'Виды', 'limit': '1'},
    'get_object_details': {'full_name': 'Справочник.Номенклатура', 'detail': 'full'},
    'search_attributes': {'query': 'Основн'},
    'find_references': {'object_name': 'ВидыНоменклатуры'},
    'list_objects': {'offset': '0'},
    'knowledge_sources': {'kind': 'РегистрНакопления'},
    'search_procedures': {'query': 'Рассчитать', 'limit': '2', 'offset': '1'},
    'get_procedure_code': {'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'},
    'get_procedure_context': {'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'},
    'add_glossary_term': {'target': 'Справочник.Склады', 'terms': 'места хранения'},
    'list_glossary': {},
    # Удаление дважды подряд успешным не бывает — сверяется одинаковая ошибка.
    'remove_glossary_term': {'entry_id': 'нет'},
    'get_module_outline': {'module_name': 'ЦеныСервер'},
    'get_call_tree': {'procedure_name': 'РассчитатьЦену', 'direction': 'up', 'depth': '2'},
    'platform_help': {'query': 'СтрНайти'},
    'search_code': {'query': 'Возврат'},
    'semantic_search_code': {'query': 'расчет цены со скидкой', 'limit': '3'},
    'diagnose_index': {},
    'find_broken_calls': {'limit': '5'},
    'check_platform_calls': {'limit': '5'},
    # Базы у проекта нет — сверяется одинаковая ошибка с подсказкой.
    'diff_from_base': {},
    'update_conflicts': {'release_project': 'p1'},
    'reindex': {},
    'remove_source': {'source_id': 'main'},          # без confirm — только предпросмотр
}


# Инструменты, у которых сверяется ожидаемая ошибка (см. CASES).
EXPECTED_ERRORS = {'remove_glossary_term', 'diff_from_base', 'update_conflicts'}


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
    pm.add_source('p1', 'main', 'Main', xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def _wait_ready(pm: ProjectManager, project_id: str = 'p1') -> None:
    # Мало дождаться статуса: 'ready' пишется до того, как фоновый поток
    # отпустит блокировку переиндексации, и следующий reindex в этом зазоре
    # отвечал «already running» (редкое падение в CI). Ждём и сам поток.
    deadline = time.time() + 30
    while time.time() < deadline:
        busy = any(t.is_alive() for t in list(pm._reindexing))
        if not busy and pm.get_project(project_id).status in ('ready', 'error'):
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
    assert len(CASES) == 27


def test_mcp_and_web_use_only_index_after_export_moves(web, tmp_path, monkeypatch):
    from tests.fixtures.generate_platform_help import context_book
    from tests.test_platform_check import _join_workers, _report_workers
    from src.mcp_server.tools import semantic_search_payload
    from src.web import app as app_module

    pm, client = web
    for pid in ('base', 'release'):
        pm.create_project(pid, pid)
        pm.add_source(pid, 'main', 'Main', xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
    for pid, value in (('p1', 120), ('release', 110)):
        path = pm.get_source_xml_path(pid, 'main') / 'CommonModules/ЦеныСервер/Ext/Module.bsl'
        code = path.read_text(encoding='utf-8-sig')
        path.write_text(code.replace('Возврат 100;', f'Возврат {value};'), encoding='utf-8-sig')
    for pid in ('base', 'release'):
        pm.reindex(pid)
    pm.set_comparison('p1', 'base', 'release')
    pm.platform_help.build_from_bytes('8.3.27.1', context_book())

    cases = {**CASES, 'diff_from_base': CASES['get_procedure_code'],
             'update_conflicts': {**CASES['get_procedure_code'], 'release_project': 'release'}}
    schemas = _schemas()

    def call(manager, tool):
        return execute_tool(manager, tool, _sdk_args(schemas[tool], cases[tool]))

    reindex_result = call(pm, 'reindex')
    assert reindex_result.startswith('Reindex started')
    _wait_ready(pm)
    glossary_result = call(pm, 'add_glossary_term')
    assert 'Записано в словарь' in glossary_result

    def mcp_results(manager):
        result = call(manager, 'check_platform_calls')
        if 'Отчёт готовится' in result or 'Отчёт уже готовится' in result:
            _join_workers(_report_workers(manager, 'p1'))
        return {tool: call(manager, tool) for tool in cases
                if tool not in ('reindex', 'add_glossary_term')}

    proc = {'module': 'ЦеныСервер', 'name': 'РассчитатьЦену'}
    graph = {'module': proc['module'], 'procedure': proc['name'], 'direction': 'up'}
    pages = [
        ('/', {}), ('/projects/p1', {}), ('/projects/p1/settings', {}),
        ('/projects/p1/search', {'q': 'Возврат 120', 'mode': 'code'}),
        ('/projects/p1/object/Справочник.Склады', {}),
        ('/projects/p1/procedure', proc),
        ('/projects/p1/procedure', {'module': proc['module'], 'line': 1}),
        ('/projects/p1/calltree', graph), ('/projects/p1/graph', graph),
        ('/api/projects/p1/graph/calltree', graph),
        ('/projects/p1/checks', {}), ('/projects/p1/diff', {}),
        ('/projects/p1/diff/procedure', proc), ('/projects/p1/diff/update', {}),
        ('/projects/p1/diff/conflict', {**proc, 'release': 'release'}),
        ('/projects/p1/glossary', {}), ('/projects/p1/playground', {}),
        ('/api/projects/p1/manifest', {}), ('/ready', {}),
    ]

    def web_results():
        responses = []
        for path, params in pages:
            response = client.get(path, params=params)
            assert response.status_code == 200, (path, response.text)
            responses.append(response.content)
        return responses

    before = mcp_results(pm)
    assert {**before, 'reindex': reindex_result, 'add_glossary_term': glossary_result}.keys() == schemas.keys()
    assert 'Возврат 120;' in before['get_procedure_code']
    assert 'Возврат 120;' in before['search_code']
    assert '8.3.24' in before['check_platform_calls']
    assert 'СтрНайти' in before['platform_help']
    assert 'Возврат 120;' in before['diff_from_base']
    assert 'Возврат 110;' in before['update_conflicts']
    assert 'места хранения' in before['list_glossary']
    assert int(before['diagnose_index'].split(': ', 1)[1].split()[0]) > 0
    semantic_args = _sdk_args(schemas['semantic_search_code'], cases['semantic_search_code'])
    semantic = semantic_search_payload(pm, semantic_args)
    assert any(item['snippet'] for item in semantic['items'])
    rendered = web_results()
    pm.close_all()

    # Переносим только index.db каждого проекта, без WAL и кэша проверки платформы.
    index_dir = tmp_path / 'index'
    shutil.copytree(pm.data_dir / 'platform-help', index_dir / 'platform-help')
    for pid in ('p1', 'base', 'release'):
        xml = pm.get_source_xml_path(pid, 'main')
        xml.parent.rename(tmp_path / f'{pid}-export')
        copied = index_dir / pid / 'index.db'
        copied.parent.mkdir(parents=True)
        shutil.copy2(pm.projects_dir / pid / 'index.db', copied)

    offline = ProjectManager(str(pm.projects_dir.parent), index_dir=str(index_dir))
    monkeypatch.setattr(app_module, '_pm', offline)
    try:
        assert mcp_results(offline) == before
        assert semantic_search_payload(offline, semantic_args) == semantic
        for (path, params), old, new in zip(pages, rendered, web_results(), strict=True):
            assert new == old, (path, params)
        assert call(offline, 'add_glossary_term') == glossary_result
        assert call(offline, 'reindex') == reindex_result
        _wait_ready(offline)
    finally:
        offline.close_all()


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
    if tool in EXPECTED_ERRORS:
        assert got['is_error'] is True, got['result']
        return
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


@pytest.mark.parametrize('content, body', [
    ('application/x-www-form-urlencoded', b'tool=search_code&query=x'),   # не JSON
    ('application/json', '{"tool": "search_code", "params": {"query": "курс"}}'.encode('cp1251')),
    ('application/json', b'["search_code"]'),                                # не объект
])
def test_malformed_body_is_400_not_500(web, content, body):
    """Некорректное тело запроса — 400 с подсказкой формата, а не 500 с
    трассировкой в логе (по логу контейнера проверяет live_check)."""
    _, client = web
    r = client.post('/api/projects/p1/playground', content=body, headers={'Content-Type': content})
    assert r.status_code == 400, r.text
    assert 'JSON' in r.json()['detail']


def test_console_groups_keep_every_tool_once(web):
    """Группы списка — только раскладка: каждый инструмент MCP ровно один раз,
    меняющие данные помечены, история и копирование — после первого вызова."""
    import json
    import re
    from html import unescape
    from src.web.routes_playground import tool_groups
    _, client = web
    page = client.get('/projects/p1/playground').text
    names = [t['name'] for t in json.loads(unescape(re.search(r'const TOOLS_META = (.*?);\n', page).group(1)))]
    grouped = [n for _, items in tool_groups(names) for n in items]
    assert sorted(grouped) == sorted(names) and len(grouped) == len(set(grouped))
    assert tool_groups(names + ['new_tool'])[-1] == ('Прочее', ['new_tool'])
    assert page.count('<optgroup') >= 5 and 'reindex — меняет данные' in page
    assert 'id="history" hidden' in page and 'id="copy-result" disabled' in page
    assert '>Ответ<' in page and 'Результат' not in page
