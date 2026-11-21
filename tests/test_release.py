"""
Релизные свойства сервера: версия из одного источника (src/VERSION),
безопасность транспорта, isError у ошибок инструментов.
"""

import os
import re
import sys
import logging
import tempfile
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import src
from src.core.project_manager import ProjectManager

ROOT = Path(__file__).parent.parent


# ============================================
# Версия
# ============================================

def test_version_single_source():
    assert src.__version__ == (ROOT / 'src' / 'VERSION').read_text(encoding='utf-8').strip()
    # Не конкретный номер (его пришлось бы править при каждом выпуске), а то,
    # что у текущей версии есть раздел в CHANGELOG: выпуск без записи о нём
    # не пройдёт.
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert re.search(rf'^## {re.escape(src.__version__)}\s*$', changelog, re.M), \
        f"в CHANGELOG.md нет раздела «## {src.__version__}»"
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {file = "src/VERSION"}' in pyproject


def test_mcp_server_reports_package_version():
    pytest.importorskip("mcp.server.mcpserver")
    from src.mcp_server.server import create_mcp_server
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            assert create_mcp_server(pm).version == src.__version__
        finally:
            pm.close_all()


# ============================================
# Порты: по умолчанию только 127.0.0.1
# ============================================

def test_compose_publishes_ports_on_loopback_by_default():
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    port_lines = [line.strip() for line in compose.splitlines()
                  if line.strip().startswith('- "') and ':98' in line]
    assert len(port_lines) == 2, port_lines
    for line in port_lines:
        assert line.startswith('- "${BIND_ADDR:-127.0.0.1}:'), line
    # Переменная с хоста сама в контейнер не попадает.
    assert '- MCP_ALLOWED_HOSTS=${MCP_ALLOWED_HOSTS:-}' in compose


# ============================================
# Защита от DNS rebinding
# ============================================

from src.security import NetworkLicenseMiddleware, allowed_hosts, web_allowed_hosts
from tests.license_helper import install_team_license


def test_allowed_hosts_default_and_env_extension():
    assert allowed_hosts({}) == [
        '127.0.0.1', 'localhost', '[::1]', '127.0.0.1:*', 'localhost:*', '[::1]:*']
    hosts = allowed_hosts({'MCP_ALLOWED_HOSTS': ' 192.168.1.10 , ariadna.lan:19879,,'})
    assert hosts[:6] == allowed_hosts({})
    assert '192.168.1.10' in hosts and '192.168.1.10:*' in hosts
    assert 'ariadna.lan:19879' in hosts and 'ariadna.lan:19879:*' not in hosts
    assert web_allowed_hosts({'MCP_ALLOWED_HOSTS': '192.168.1.10'}) == \
        ['127.0.0.1', 'localhost', '[::1]', '192.168.1.10']


@pytest.mark.parametrize('host', ['127.0.0.1', '127.0.0.1:19878', 'localhost',
                                  '[::1]', '[::1]:19878', 'LOCALHOST:65535', 'localhost:1'])
def test_network_license_allows_loopback_without_checking_key(host):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def unexpected_license_check():
        pytest.fail('Для локального Host ключ не требуется')

    app = FastAPI()
    app.add_middleware(NetworkLicenseMiddleware, license_valid=unexpected_license_check)
    assert TestClient(app).get('/openapi.json', headers={'Host': host}).status_code == 200


@pytest.mark.parametrize('host', ['192.168.1.10:19878', 'ariadna.lan', 'localhost.evil.example',
                                  '127.0.0.1:abc', '[::1].evil.example'])
def test_network_license_requires_valid_key_for_other_hosts(tmp_path, monkeypatch, host):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.license import LicenseStore

    app = FastAPI()
    store = LicenseStore(tmp_path)
    app.add_middleware(NetworkLicenseMiddleware, license_valid=lambda: store.state().valid)
    client = TestClient(app, headers={'Host': host, 'Accept': 'application/json'})
    denied = client.get('/openapi.json')
    assert denied.status_code == 400
    assert denied.json()['error'] == 'license_required'
    install_team_license(monkeypatch, tmp_path)
    assert client.get('/openapi.json').status_code == 200


def test_web_ui_rejects_foreign_host_and_health_works(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as web
    monkeypatch.setattr(web, '_pm', ProjectManager(tmp_path))
    client = TestClient(web.app)
    for path in ('/', '/login', '/static/style.css', '/health', '/ready'):
        denied = client.get(path, headers={'Host': 'ariadna.lan:19878'})
        assert denied.status_code == 400
        assert 'лицензионный ключ' in denied.text and 'docs/license.md' in denied.text
        assert client.get(path, headers={'Host': 'localhost:9878'}).status_code == 200
    denied = client.get('/api/projects/demo/manifest', headers={'Host': '192.168.1.10:19878'})
    assert denied.status_code == 400
    assert denied.json()['error'] == 'license_required'
    for host in ('[::1]:19878', 'LOCALHOST:65535'):
        assert client.get('/health', headers={'Host': host}).status_code == 200
    for host in ('localhost:0', '127.0.0.1:65536', '[::1]:999999'):
        denied = client.get('/health', headers={'Host': host, 'Accept': 'application/json'})
        assert denied.status_code == 400
        assert denied.json()['error'] == 'invalid_host'
    license_page = client.get('/admin/license', headers={'Host': '127.0.0.1:19878'})
    assert 'Для доступа из сети установите ключ.' in license_page.text


_INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '0'}}}
_MCP_HEADERS = {'Accept': 'application/json, text/event-stream',
                'Content-Type': 'application/json'}


def _mcp_server(tmpdir):
    pytest.importorskip("mcp.server.mcpserver")
    from src.mcp_server.server import create_mcp_server
    pm = ProjectManager(tmpdir)
    return pm, create_mcp_server(pm)


def test_mcp_streamable_http_rejects_foreign_host(monkeypatch):
    from fastapi.testclient import TestClient
    from src.security import mcp_transport_security
    monkeypatch.delenv('MCP_ALLOWED_HOSTS', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            app = mcp.streamable_http_app(host='0.0.0.0', transport_security=mcp_transport_security())
            with TestClient(app) as client:
                bad = client.post('/mcp', json=_INIT, headers={**_MCP_HEADERS, 'Host': 'evil.example:19879'})
                assert bad.status_code == 421, bad.text
                bad_origin = client.post('/mcp', json=_INIT, headers={
                    **_MCP_HEADERS, 'Host': '127.0.0.1:19879', 'Origin': 'http://evil.example'})
                assert bad_origin.status_code == 403, bad_origin.text
                ok = client.post('/mcp', json=_INIT, headers={**_MCP_HEADERS, 'Host': '127.0.0.1:19879'})
                assert ok.status_code == 200, ok.text
        finally:
            pm.close_all()


def test_mcp_allowed_hosts_env_lets_remote_host_in(monkeypatch):
    from fastapi.testclient import TestClient
    from src.security import mcp_transport_security
    monkeypatch.setenv('MCP_ALLOWED_HOSTS', '192.168.1.10')
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            app = mcp.streamable_http_app(host='0.0.0.0', transport_security=mcp_transport_security())
            with TestClient(app) as client:
                ok = client.post('/mcp', json=_INIT, headers={**_MCP_HEADERS, 'Host': '192.168.1.10:19879'})
                assert ok.status_code == 200, ok.text
        finally:
            pm.close_all()


def test_mcp_streamable_http_rejection_does_not_raise(monkeypatch, caplog):
    """На streamable HTTP SDK отклонённый запрос просто получает ответ, без
    исключения и трассировки в логе, — отдельная обёртка (как была для SSE)
    не нужна."""
    from fastapi.testclient import TestClient
    from src.security import mcp_transport_security
    monkeypatch.delenv('MCP_ALLOWED_HOSTS', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            app = mcp.streamable_http_app(host='0.0.0.0', transport_security=mcp_transport_security())
            with caplog.at_level(logging.WARNING), TestClient(app) as client:
                bad = client.post('/mcp', json=_INIT, headers={**_MCP_HEADERS, 'Host': 'evil.example:19879'})
                assert bad.status_code == 421
            assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        finally:
            pm.close_all()


def test_web_and_mcp_reload_license_with_the_same_mtime(tmp_path, monkeypatch):
    """Оба сервера проверяют действительность ключа при обращении из сети."""
    pytest.importorskip("mcp.server.mcpserver")
    import uvicorn
    from fastapi.testclient import TestClient
    from src import main as main_module
    from src.license import LicenseStore, issue
    from src.web import app as web
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    monkeypatch.setenv('MCP_ALLOWED_HOSTS', 'Ariadna.lan:19879')
    pm = ProjectManager(tmp_path)
    monkeypatch.setattr(web, '_pm', pm)
    monkeypatch.setattr(web.app, 'middleware_stack', None)
    trusted_host = next(m for m in web.app.user_middleware if m.cls is TrustedHostMiddleware)
    monkeypatch.setitem(trusted_host.kwargs, 'allowed_hosts', web_allowed_hosts())
    web_client = TestClient(web.app, headers={'Host': 'ARIADNA.LAN:19878', 'Accept': 'application/json'})

    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: captured.setdefault('app', app))
    store = LicenseStore(tmp_path)
    main_module._start_mcp_streamable_http(pm, 0, check_local_interface=True)
    with TestClient(captured['app'], base_url='http://127.0.0.1:19879') as client:
        def initialize(host):
            return client.post('/mcp', json=_INIT, headers={**_MCP_HEADERS, 'Host': host})

        remote = 'ariadna.lan:19879'

        def assert_network_access_denied():
            web_denied = web_client.get('/health')
            assert web_denied.status_code == 400
            assert web_denied.json()['error'] == 'license_required'
            mcp_denied = initialize(remote)
            assert mcp_denied.status_code == 421
            assert mcp_denied.json()['error'] == 'license_required'

        denied = initialize(remote)
        assert denied.status_code == 421
        assert denied.json()['error'] == 'license_required'
        assert 'лицензионный ключ' in denied.json()['detail']
        assert denied.json()['docs'].endswith('/docs/license.md')
        for host in ('127.0.0.1:19879', 'LOCALHOST', '[::1]:19879'):
            assert initialize(host).status_code == 200
        invalid_port = initialize('localhost:65536')
        assert invalid_port.status_code == 421 and invalid_port.json()['error'] == 'invalid_host'

        key = install_team_license(monkeypatch, tmp_path)
        assert web_client.get('/health').status_code == 200
        assert initialize(remote).status_code == 200
        assert initialize('ARIADNA.LAN:19879').status_code == 200
        assert initialize('ariadna.lan:19880').status_code == 421
        valid = store.path.read_bytes()
        stamp = store.path.stat()
        # Восстановление файла из копии может сохранить время и размер.
        old = issue(key, 'Тестовая команда', 10, '2000-01-01')
        store.path.write_text(old, encoding='utf-8')
        os.utime(store.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert store.path.stat().st_mtime_ns == stamp.st_mtime_ns
        assert store.path.stat().st_size == stamp.st_size
        assert_network_access_denied()

        store.path.write_bytes(valid)
        assert initialize(remote).status_code == 200
        assert web_client.get('/health').status_code == 200
        store.path.write_bytes(b'\xff')
        assert_network_access_denied()

        store.path.write_bytes(valid)
        assert initialize(remote).status_code == 200
        assert web_client.get('/health').status_code == 200
        store.path.unlink()
        assert_network_access_denied()
        denied = client.get('/mcp', headers={'Host': remote, 'Accept': 'text/event-stream'})
        assert denied.status_code == 421
        assert denied.json()['error'] == 'license_required'


def test_native_wide_socket_requires_license_on_non_loopback_interface(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.license import LicenseStore

    app = FastAPI()
    store = LicenseStore(tmp_path)
    app.add_middleware(NetworkLicenseMiddleware, license_valid=lambda: store.state().valid,
                       check_local_interface=lambda: True)
    lan_client = TestClient(app, base_url='http://192.168.1.10', headers={'Host': 'localhost'})
    local_client = TestClient(app, base_url='http://127.0.0.1')
    install_team_license(monkeypatch, tmp_path)
    assert lan_client.get('/openapi.json').status_code == 200
    store.path.unlink()
    assert lan_client.get('/openapi.json').status_code == 400
    assert local_client.get('/openapi.json').status_code == 200


# ============================================
# isError у ошибок инструментов
# ============================================

import asyncio
import logging


def _call_via_sdk(mcp, name, args):
    from mcp import Client

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(name, args)
    return asyncio.run(go())


def _text(result) -> str:
    return '\n'.join(c.text for c in result.content if getattr(c, 'text', None))


def test_tool_error_text_becomes_is_error(caplog):
    """Сравнение с ошибкой валидации аргументов из самого SDK — формы одинаковы."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            pm.create_project('p1', 'p1')
            with caplog.at_level(logging.INFO, logger='ariadna'):
                bad = _call_via_sdk(mcp, 'search_metadata', {'project_id': 'nope', 'query': 'x'})
            assert bad.is_error is True
            # Тот же вид, что у ошибок валидации аргументов из SDK.
            assert _text(bad).startswith("Error executing tool search_metadata: project 'nope' not found"), _text(bad)
            # Ожидаемая ошибка — без трассировки в логе.
            assert not any(r.exc_info for r in caplog.records if r.name == 'ariadna')

            ok = _call_via_sdk(mcp, 'list_projects', {})
            assert ok.is_error is False and 'p1' in _text(ok)
        finally:
            pm.close_all()


def test_unexpected_exception_is_error_with_traceback_in_log(monkeypatch, caplog):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            import src.mcp_server.server as server_module

            def boom(*a, **kw):
                raise RuntimeError('boom')
            monkeypatch.setattr(server_module, 'execute_tool', boom)
            with caplog.at_level(logging.INFO, logger='ariadna'):
                res = _call_via_sdk(mcp, 'list_projects', {})
            assert res.is_error is True
            assert _text(res) == 'Error executing tool list_projects: boom'
            assert any(r.exc_info and r.name == 'ariadna' for r in caplog.records), caplog.text
        finally:
            pm.close_all()


def test_sdk_validation_error_has_same_shape():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            res = _call_via_sdk(mcp, 'search_metadata', {})     # нет обязательного query
            assert res.is_error is True
            assert _text(res).startswith('Error executing tool search_metadata:')
        finally:
            pm.close_all()


# ============================================
# Адрес прослушивания: 127.0.0.1 без Docker, 0.0.0.0 только в контейнере
# ============================================

def test_sse_transport_is_gone():
    """0.3.0: транспорт SSE убран — ни порта 9877/19877, ни MCP_PORT, ни
    _start_mcp_sse; MCP только streamable HTTP на 9879 (/mcp)."""
    from src import main as main_module
    import src.security as security
    assert not hasattr(main_module, '_start_mcp_sse')
    assert not hasattr(main_module, '_sse_app')
    assert not hasattr(security, 'HostOriginGuard')
    for name in ('Dockerfile', 'docker-compose.yml', 'scripts/entrypoint.sh', 'src/main.py'):
        text = (ROOT / name).read_text(encoding='utf-8')
        for gone in ('9877', 'MCP_PORT', 'MCP_HOST_PORT', 'sse'):
            assert gone not in text, (name, gone)
    assert '9879' in (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')


@pytest.mark.parametrize('env, licensed, in_docker, expected, overridden', [
    (None, False, False, '127.0.0.1', False),
    ('::1', False, False, '::1', False),
    ('0.0.0.0', False, False, '127.0.0.1', True),
    ('192.168.1.10', False, False, '127.0.0.1', True),
    ('0.0.0.0', True, False, '0.0.0.0', False),
    ('0.0.0.0', False, True, '0.0.0.0', False),
])
def test_main_selects_one_listen_address_for_both_servers(
        tmp_path, monkeypatch, caplog, env, licensed, in_docker, expected, overridden):
    """Web UI запускается в фоновом потоке, MCP — в основном."""
    pytest.importorskip('mcp.server.mcpserver')
    import uvicorn
    from fastapi.testclient import TestClient
    from starlette.datastructures import State
    from src import main as main_module
    from src.security import LOCAL_BIND_NOTICE
    from src.web import app as web

    calls = {}
    web_started = threading.Event()
    pm = ProjectManager(tmp_path)

    def run(app, *, host, port, **kwargs):
        calls[port] = host, threading.current_thread()
        if app is web.app:
            web_started.set()

    monkeypatch.setattr(uvicorn, 'run', run)
    monkeypatch.setattr(web, '_pm', web._pm)
    monkeypatch.setattr(web.app, 'state', State())
    monkeypatch.setattr(main_module, '_quiet_proactor_connection_reset', lambda: None)
    monkeypatch.setattr(main_module, 'build_shared_state', lambda data_dir: pm)
    monkeypatch.delenv('MCP_HTTP_PORT', raising=False)
    monkeypatch.delenv('WEB_PORT', raising=False)
    monkeypatch.delenv('INDEX_DIR', raising=False)
    if env is None:
        monkeypatch.delenv('LISTEN_ADDR', raising=False)
    else:
        monkeypatch.setenv('LISTEN_ADDR', env)
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    if licensed:
        install_team_license(monkeypatch, tmp_path)
    main_module.main(in_docker=in_docker)
    assert web_started.wait(10)
    assert calls[9879] == (expected, threading.main_thread())
    assert calls[9878][0] == expected and calls[9878][1] is not threading.main_thread()
    page = TestClient(web.app, base_url='http://127.0.0.1:9878').get('/')
    assert page.status_code == 200
    assert (LOCAL_BIND_NOTICE in page.text) is overridden
    assert (LOCAL_BIND_NOTICE in caplog.text) is overridden


def test_docker_image_listens_on_all_interfaces():
    """В контейнере без 0.0.0.0 не работает проброс портов; наружу их
    открывает только BIND_ADDR (docker-compose.yml, по умолчанию 127.0.0.1)."""
    assert 'ENV LISTEN_ADDR=0.0.0.0' in (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert 'main(in_docker=True)' in (ROOT / 'scripts/entrypoint.sh').read_text(encoding='utf-8')
    assert '${BIND_ADDR:-127.0.0.1}:' in (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
