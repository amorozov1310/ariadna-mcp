"""
Релизные свойства сервера: версия из одного источника (src/VERSION),
безопасность транспорта, isError у ошибок инструментов.
"""

import sys
import tempfile
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
    assert src.__version__ == '0.2.0'
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
    assert len(port_lines) == 3, port_lines
    for line in port_lines:
        assert line.startswith('- "${BIND_ADDR:-127.0.0.1}:'), line
    # Переменная с хоста сама в контейнер не попадает.
    assert '- MCP_ALLOWED_HOSTS=${MCP_ALLOWED_HOSTS:-}' in compose


# ============================================
# Защита от DNS rebinding
# ============================================

from src.security import allowed_hosts, web_allowed_hosts


def test_allowed_hosts_default_and_env_extension():
    assert allowed_hosts({}) == ['127.0.0.1:*', 'localhost:*', '[::1]:*']
    hosts = allowed_hosts({'MCP_ALLOWED_HOSTS': ' 192.168.1.10 , ariadna.lan:19879,,'})
    assert hosts[:3] == ['127.0.0.1:*', 'localhost:*', '[::1]:*']
    assert '192.168.1.10' in hosts and '192.168.1.10:*' in hosts
    assert 'ariadna.lan:19879' in hosts and 'ariadna.lan:19879:*' not in hosts
    assert web_allowed_hosts({'MCP_ALLOWED_HOSTS': '192.168.1.10'}) == \
        ['127.0.0.1', 'localhost', '[::1]', '192.168.1.10']


def test_web_ui_rejects_foreign_host_and_health_works():
    from fastapi.testclient import TestClient
    from src.web.app import app
    client = TestClient(app)
    assert client.get('/health', headers={'Host': 'evil.example'}).status_code == 400
    assert client.get('/health', headers={'Host': 'evil.example:19878'}).status_code == 400
    for host in ('127.0.0.1:19878', 'localhost:19878', 'localhost:9878', '[::1]:19878'):
        assert client.get('/health', headers={'Host': host}).status_code == 200, host


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


def test_mcp_sse_rejects_foreign_host(monkeypatch):
    from fastapi.testclient import TestClient
    from src.security import mcp_transport_security
    monkeypatch.delenv('MCP_ALLOWED_HOSTS', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm, mcp = _mcp_server(tmpdir)
        try:
            app = mcp.sse_app(host='0.0.0.0', transport_security=mcp_transport_security())
            # SDK отвечает 421 и затем бросает ValueError внутри обработчика SSE —
            # для клиента это просто ответ 421.
            with TestClient(app, raise_server_exceptions=False) as client:
                assert client.get('/sse', headers={'Host': 'evil.example:19877'}).status_code == 421
                # Свой Host проходит проверку — дальше ошибка уже про сессию, не про хост.
                own = client.post('/messages/', json={}, headers={'Host': '127.0.0.1:19877'})
                assert own.status_code not in (421, 403), own.text
        finally:
            pm.close_all()


@pytest.mark.parametrize('starter, request_kw', [
    ('_start_mcp_streamable_http', {'method': 'POST', 'url': '/mcp', 'json': _INIT}),
    # POST, а не GET /sse: без защиты GET открыл бы бесконечный поток.
    ('_start_mcp_sse', {'method': 'POST', 'url': '/messages/?session_id=0', 'json': {}}),
])
def test_main_wires_transport_security(monkeypatch, starter, request_kw):
    """main.py собирает приложения MCP с защитой: чужой Host — 421."""
    pytest.importorskip("mcp.server.mcpserver")
    import uvicorn
    from fastapi.testclient import TestClient
    from src import main as main_module
    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: captured.setdefault('app', app))
    monkeypatch.delenv('MCP_ALLOWED_HOSTS', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        getattr(main_module, starter)(tmpdir, 0)
        with TestClient(captured['app'], raise_server_exceptions=False) as client:
            resp = client.request(headers={**_MCP_HEADERS, 'Host': 'evil.example:1'}, **request_kw)
            assert resp.status_code == 421, resp.text
