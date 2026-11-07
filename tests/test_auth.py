"""Личные токены и роли (src/auth.py, src/web/routes_auth.py): общий
командный сервер. Без токенов всё работает как раньше; с первым токеном —
вход по токену, роль user не управляет проектами, источниками и токенами."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore, main as auth_cli
from src.core.project_manager import ProjectManager
from tests.license_helper import install_team_license

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}


def test_token_store_create_verify_revoke(tmp_path):
    store = TokenStore(tmp_path)
    assert not store.enabled() and store.verify('ar_x') is None
    info, token = store.create('Иван', 'admin')
    assert token.startswith('ar_') and store.enabled()
    raw = (tmp_path / 'tokens.json').read_text(encoding='utf-8')
    assert token not in raw and info.hash in raw          # на диске только хеш
    assert store.verify(token).name == 'Иван' and store.verify(token + 'x') is None
    # Второй экземпляр (другой процесс — CLI) видит токен через файл.
    assert TokenStore(tmp_path).verify(token).role == 'admin'
    with pytest.raises(ValueError):
        store.create('', 'user')
    with pytest.raises(ValueError):
        store.create('Пётр', 'root')
    store.revoke(info.id)
    assert not store.enabled() and store.verify(token) is None


def test_broken_tokens_file_keeps_server_closed(tmp_path):
    (tmp_path / 'tokens.json').write_text('{не json', encoding='utf-8')
    store = TokenStore(tmp_path)
    assert store.enabled() and store.verify('ar_любой') is None


def test_cli_create_and_list(tmp_path, capsys):
    assert auth_cli(['--data-dir', str(tmp_path), 'create', '--name', 'Робот', '--role', 'user']) == 0
    token = capsys.readouterr().out.splitlines()[1]
    assert TokenStore(tmp_path).verify(token).name == 'Робот'
    auth_cli(['--data-dir', str(tmp_path), 'list'])
    assert 'Робот' in capsys.readouterr().out


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def test_web_open_without_tokens(web):
    _, client = web
    assert client.get('/').status_code == 200
    assert client.get('/api/projects/p1/status').status_code == 200


def test_first_token_from_web_is_admin_and_logs_in(web):
    pm, client = web
    page = client.post('/api/admin/tokens', data={'name': 'Владелец', 'role': 'user'})
    assert page.status_code == 200 and 'ar_' in page.text
    assert TokenStore(pm.data_dir).list()[0].role == 'admin'      # первый — всегда admin
    assert client.get('/').status_code == 200                      # cookie уже стоит

    from fastapi.testclient import TestClient
    from src.web import app as app_module
    anon = TestClient(app_module.app, headers=HOST)
    page = anon.get('/', follow_redirects=False)
    assert page.status_code == 303 and page.headers['location'].startswith('/login')
    assert anon.get('/api/projects/p1/status').status_code == 401
    assert anon.get('/health').status_code == 200                  # healthcheck без токена


def test_roles_and_bearer(web, monkeypatch):
    pm, client = web
    install_team_license(monkeypatch, pm.data_dir)
    store = TokenStore(pm.data_dir)
    _, admin = store.create('Админ', 'admin')
    _, user = store.create('Разработчик', 'user')
    as_user = {'Authorization': f'Bearer {user}'}
    as_admin = {'Authorization': f'Bearer {admin}'}

    assert client.get('/api/projects/p1/status', headers=as_user).status_code == 200
    assert client.get('/projects/p1', headers=as_user).status_code == 200
    # Пользователь: без управления проектами, источниками и токенами.
    assert client.post('/api/projects', headers=as_user, data={'id': 'p2', 'name': 'p2'}).status_code == 403
    assert client.post('/api/projects/p1/delete', headers=as_user).status_code == 403
    assert client.post('/api/projects/p1/sources/main/delete', headers=as_user).status_code == 403
    assert client.get('/admin/tokens', headers=as_user).status_code == 403
    assert client.post('/api/projects/p1/reindex', headers=as_user).status_code != 403
    # Playground не обходит роль: удаление источника — только admin.
    res = client.post('/api/projects/p1/playground', headers=as_user,
                      json={'tool': 'remove_source', 'params': {'source_id': 'main', 'confirm': 'true'}})
    assert 'только для администратора' in res.json()['result']
    # Администратор — всё.
    assert client.get('/admin/tokens', headers=as_admin).status_code == 200
    assert client.post('/api/projects/p1/delete', headers=as_admin, follow_redirects=False).status_code != 403


def test_login_logout_and_safe_next(web):
    pm, client = web
    _, token = TokenStore(pm.data_dir).create('Иван', 'user')
    client.cookies.clear()
    bad = client.post('/login', data={'token': 'ar_нет', 'next': '/'}, follow_redirects=False)
    assert 'error=1' in bad.headers['location']
    ok = client.post('/login', data={'token': token, 'next': 'https://evil.example/'},
                     follow_redirects=False)
    assert ok.headers['location'] == '/'                           # не открытый редирект
    assert client.get('/projects/p1').status_code == 200
    client.get('/logout')
    assert client.get('/projects/p1', follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- MCP

_INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '0'}}}
_MCP = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json',
        'Host': '127.0.0.1:19879'}


def _rpc(client, headers, body):
    resp = client.post('/mcp', json=body, headers=headers)
    if resp.status_code != 200:
        return resp.status_code, None
    text = resp.text
    data = [line[5:].strip() for line in text.splitlines() if line.startswith('data:')]
    return 200, json.loads(data[-1] if data else text)


def test_mcp_requires_token_and_admin_for_remove_source(monkeypatch):
    pytest.importorskip('mcp.server.mcpserver')
    from fastapi.testclient import TestClient
    from src.auth import MCPAuthMiddleware
    from src.mcp_server.server import create_mcp_server
    from src.security import mcp_transport_security
    monkeypatch.delenv('MCP_ALLOWED_HOSTS', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        install_team_license(monkeypatch, tmpdir)
        store = TokenStore(tmpdir)
        _, admin = store.create('Админ', 'admin')
        _, user = store.create('Разработчик', 'user')
        try:
            mcp = create_mcp_server(pm)
            app = MCPAuthMiddleware(mcp.streamable_http_app(
                host='0.0.0.0', transport_security=mcp_transport_security()), store)
            with TestClient(app) as client:
                assert client.post('/mcp', json=_INIT, headers=_MCP).status_code == 401
                assert client.post('/mcp', json=_INIT, headers={
                    **_MCP, 'Authorization': 'Bearer ar_wrong'}).status_code == 401

                def session(token):
                    h = {**_MCP, 'Authorization': f'Bearer {token}'}
                    resp = client.post('/mcp', json=_INIT, headers=h)
                    assert resp.status_code == 200, resp.text
                    sid = resp.headers.get('mcp-session-id')
                    if sid:
                        h['mcp-session-id'] = sid
                    client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                headers=h)
                    return h

                def call(h, name, args):
                    _, msg = _rpc(client, h, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                              'params': {'name': name, 'arguments': args}})
                    return msg['result']

                hu = session(user)
                assert 'p1' in call(hu, 'list_projects', {})['content'][0]['text']
                denied = call(hu, 'remove_source', {'project_id': 'p1', 'source_id': 'x', 'confirm': True})
                assert denied['isError'] and 'только для администратора' in denied['content'][0]['text']
                preview = call(hu, 'remove_source', {'project_id': 'p1', 'source_id': 'x'})
                assert 'только для администратора' not in preview['content'][0]['text']

                ha = session(admin)
                allowed = call(ha, 'remove_source', {'project_id': 'p1', 'source_id': 'x', 'confirm': True})
                assert 'только для администратора' not in allowed['content'][0]['text']
        finally:
            pm.close_all()
