"""
Один ProjectManager на процесс — общий для Web UI и MCP.

Раньше main.py создавал по ProjectManager на каждый сервер (и ещё один
временный для обслуживания при старте), а Web UI — свой. У каждого был свой
кэш реестра и свой пул индексов, и их согласовывали заплатками. Теперь
main.build_shared_state создаёт один и передаёт его всем.
"""

import io
import os
import sys
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def shared(monkeypatch):
    """Процесс как в main.py: build_shared_state → Web UI (set_pm) и MCP."""
    from src import main as main_module
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv('ARIADNA_PLATFORM', os.path.join(tmpdir, 'platform'))
        monkeypatch.setenv('INDEX_DIR', os.path.join(tmpdir, 'index'))
        monkeypatch.setattr(app_module, '_pm', None)
        pm = main_module.build_shared_state(os.path.join(tmpdir, 'data'))
        app_module.set_pm(pm)
        try:
            yield pm
        finally:
            pm.close_all()


def _client():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    return TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})


def _zip(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    return buf.getvalue()


def _create_and_index_via_web(client, project_id: str = 'p1') -> None:
    r = client.post('/api/projects', data={'project_id': project_id, 'name': project_id},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    r = client.post(f'/api/projects/{project_id}/sources',
                    data={'source_id': 'main', 'label': 'Main'},
                    files={'xml_archive': ('x.zip', _zip(FIXTURES / 'xml_ru'))},
                    follow_redirects=False)
    assert r.status_code == 303, r.text


def test_web_ui_and_mcp_use_the_same_project_manager(shared, monkeypatch):
    """main.py отдаёт один pm и Web UI (set_pm), и обоим MCPServer. Проект,
    созданный через Web UI, оба транспорта MCP видят сразу; а подменённый
    метод общего pm видят все трое — значит, объект один."""
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio
    from mcp import Client
    from src import main as main_module
    from src.mcp_server.server import create_mcp_server
    from src.web import app as app_module
    import uvicorn

    apps = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: apps.setdefault(len(apps), app))
    main_module._start_web(shared, 0)
    assert app_module.get_pm() is shared and apps[0] is app_module.app

    with _client() as client:
        r = client.post('/api/projects', data={'project_id': 'p1', 'name': 'p1'},
                        follow_redirects=False)
        assert r.status_code == 303
    calls = []
    real = shared.list_projects
    monkeypatch.setattr(shared, 'list_projects', lambda: calls.append(1) or real())

    async def list_projects(mcp):
        async with Client(mcp) as client:
            result = await client.call_tool('list_projects', {})
            return '\n'.join(c.text for c in result.content if getattr(c, 'text', None))

    for mcp in (create_mcp_server(shared), create_mcp_server(shared)):   # два сервера MCP — один pm
        assert 'p1' in asyncio.run(list_projects(mcp))
    assert len(calls) == 2


def test_web_and_mcp_share_startup_token_store_and_fail_closed_on_disappearance(tmp_path, monkeypatch):
    import asyncio
    from src import main as main_module
    from src.auth import MCPAuthMiddleware, TokenStore
    from src.web import app as app_module

    _, token = TokenStore(tmp_path).create('Синтетический администратор', 'admin')
    pm = main_module.build_shared_state(str(tmp_path))
    previous_pm, previous_store = app_module._pm, app_module._token_store
    app_module.set_pm(pm)
    store = pm.token_store
    assert store.verify(token) is not None
    assert app_module._token_store is store
    async def downstream(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    mcp_auth = MCPAuthMiddleware(downstream, store)
    async def call_mcp():
        messages = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            messages.append(message)

        await mcp_auth({'type': 'http', 'headers': [(b'authorization',
                                                      f'Bearer {token}'.encode())]},
                       receive, send)
        return messages[0]['status']

    try:
        pm.data_dir.joinpath('tokens.json').unlink()

        with _client() as client:
            web_response = client.get('/api/projects')
        assert web_response.status_code == 401
        assert asyncio.run(call_mcp()) == 401
        assert store.enabled() and store.verify(token) is None
    finally:
        pm.close_all()
        app_module._pm, app_module._token_store = previous_pm, previous_store


def test_get_pm_without_set_pm_creates_its_own(monkeypatch):
    """Режим разработки `uvicorn src.web.app:app --reload`: Web UI один в
    процессе, ProjectManager — свой, из DATA_DIR/INDEX_DIR."""
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(app_module, '_pm', None)
        monkeypatch.setenv('DATA_DIR', tmpdir)
        monkeypatch.delenv('INDEX_DIR', raising=False)
        pm = app_module.get_pm()
        try:
            assert pm.data_dir == Path(tmpdir) and app_module.get_pm() is pm
        finally:
            pm.close_all()


def test_close_all_waits_for_startup_help_sync(tmp_path, monkeypatch):
    from src import main
    from src.core import platform_help
    from tests.fixtures.generate_platform_help import context_book

    version = '8.3.27.1'
    platform = tmp_path / 'platform' / version / 'bin'
    platform.mkdir(parents=True)
    (platform / 'shcntx_ru.hbk').write_bytes(context_book(version))
    monkeypatch.setenv('ARIADNA_PLATFORM', str(platform))
    monkeypatch.setenv('INDEX_DIR', str(tmp_path / 'index'))
    started, release, joining = threading.Event(), threading.Event(), threading.Event()
    find_installed = platform_help.find_installed

    def find(roots):
        started.set()
        assert release.wait(timeout=10)
        return find_installed(roots)

    monkeypatch.setattr(platform_help, 'find_installed', find)
    pm = main.build_shared_state(str(tmp_path / 'data'))
    try:
        assert started.wait(timeout=5)
        monkeypatch.setenv('ARIADNA_PLATFORM', str(tmp_path / 'other'))
        with pm._base_diff_lock:
            worker, = pm._platform_workers
        join_thread = worker.join

        def join(*args, **kwargs):
            joining.set()
            return join_thread(*args, **kwargs)

        monkeypatch.setattr(worker, 'join', join)
        with ThreadPoolExecutor(1) as pool:
            closing = pool.submit(pm.close_all)
            try:
                assert joining.wait(timeout=5)
                assert not closing.done()
            finally:
                release.set()
            closing.result(timeout=5)
        assert not worker.is_alive()
        assert pm.platform_help.newest() == version
    finally:
        release.set()
        pm.close_all()


def test_project_created_in_web_ui_is_visible_to_mcp(shared):
    with _client() as client:
        _create_and_index_via_web(client)
    shared.reindex('p1')
    assert 'p1' in execute_tool(shared, 'list_projects', {})
    assert 'РассчитатьЦену' in execute_tool(shared, 'search_procedures',
                                             {'project_id': 'p1', 'query': 'Рассчитать'})


def test_recreated_project_is_empty_for_mcp(shared):
    with _client() as client:
        _create_and_index_via_web(client)
        shared.reindex('p1')
        assert 'Procedures: 0' not in execute_tool(shared, 'get_index_status', {'project_id': 'p1'})
        assert client.delete('/api/projects/p1').status_code == 200
        r = client.post('/api/projects', data={'project_id': 'p1', 'name': 'p1'},
                        follow_redirects=False)
        assert r.status_code == 303
    shared.reindex('p1')
    assert 'Procedures: 0' in execute_tool(shared, 'get_index_status', {'project_id': 'p1'})
    assert 'No procedures found' in execute_tool(shared, 'search_procedures',
                                                 {'project_id': 'p1', 'query': 'Рассчитать'})


def test_delete_under_mcp_load_leaves_no_index(shared):
    """Поток в цикле читает проект через MCP, Web UI его удаляет — каталог
    индекса удалён, сироты index.db нет (на Windows rmtree не падает)."""
    with _client() as client:
        for run in range(10):
            _create_and_index_via_web(client)
            shared.reindex('p1')
            index_parent = shared._index_path('p1').parent
            stop, warmed = threading.Event(), threading.Event()
            errors = []

            def read():
                while not stop.is_set():
                    try:
                        execute_tool(shared, 'search_procedures', {'project_id': 'p1', 'query': 'Рассчитать'})
                        execute_tool(shared, 'get_index_status', {'project_id': 'p1'})
                        warmed.set()
                    except (KeyError, __import__('sqlite3').OperationalError):
                        pass
                    except Exception as e:      # pragma: no cover
                        errors.append(e)
                        warmed.set()
                        return

            t = threading.Thread(target=read)
            t.start()
            try:
                assert warmed.wait(30)
                assert client.delete('/api/projects/p1').status_code == 200
            finally:
                stop.set()
                t.join()
            assert not errors, errors
            assert not index_parent.exists(), f"прогон {run}: остался {list(index_parent.iterdir())}"
            assert 'p1' not in execute_tool(shared, 'list_projects', {})
