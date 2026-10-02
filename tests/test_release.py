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


def test_main_wires_transport_security(monkeypatch):
    """main.py собирает приложения MCP с защитой: чужой Host — 421."""
    pytest.importorskip("mcp.server.mcpserver")
    import uvicorn
    from fastapi.testclient import TestClient
    from src import main as main_module
    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: captured.setdefault('app', app))
    monkeypatch.delenv('MCP_ALLOWED_HOSTS', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            main_module._start_mcp_streamable_http(pm, 0)
            with TestClient(captured['app'], raise_server_exceptions=False) as client:
                resp = client.post('/mcp', json=_INIT, headers={**_MCP_HEADERS, 'Host': 'evil.example:1'})
                assert resp.status_code == 421, resp.text
        finally:
            pm.close_all()


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

@pytest.mark.parametrize('starter', ['_start_web', '_start_mcp_streamable_http'])
@pytest.mark.parametrize('env, expected', [(None, '127.0.0.1'), ('0.0.0.0', '0.0.0.0')])
def test_servers_listen_on_localhost_unless_listen_addr_set(monkeypatch, starter, env, expected):
    """Раньше main.py всегда слушал 0.0.0.0 — при запуске без Docker серверы
    без авторизации были открыты на всех интерфейсах."""
    pytest.importorskip("mcp.server.mcpserver")
    import uvicorn
    from src import main as main_module
    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: captured.update(kw))
    # _start_web задаёт ProjectManager приложению Web UI — monkeypatch вернёт прежний.
    from src.web import app as app_module
    monkeypatch.setattr(app_module, '_pm', app_module._pm)
    if env is None:
        monkeypatch.delenv('LISTEN_ADDR', raising=False)
    else:
        monkeypatch.setenv('LISTEN_ADDR', env)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            getattr(main_module, starter)(pm, 0)
        finally:
            pm.close_all()
    assert captured['host'] == expected


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


def test_main_blocks_on_mcp_and_runs_web_ui_in_thread(monkeypatch):
    """main(): Web UI — в фоновом потоке, MCP (streamable HTTP) — в основном,
    блокирующий: его падение завершает процесс."""
    from src import main as main_module
    calls = []
    web_started = threading.Event()
    monkeypatch.setattr(main_module, '_start_web',
                        lambda pm, port: calls.append(('web', port)) or web_started.set())
    monkeypatch.setattr(main_module, '_start_mcp_streamable_http',
                        lambda pm, port: calls.append(('mcp', port, threading.current_thread())))
    monkeypatch.setattr(main_module, '_quiet_proactor_connection_reset', lambda: None)
    monkeypatch.delenv('MCP_HTTP_PORT', raising=False)
    monkeypatch.delenv('WEB_PORT', raising=False)
    monkeypatch.delenv('INDEX_DIR', raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv('DATA_DIR', tmpdir)
        main_module.main()
        assert web_started.wait(10)
    mcp = [c for c in calls if c[0] == 'mcp']
    assert mcp == [('mcp', 9879, threading.main_thread())]
    assert ('web', 9878) in calls


def test_docker_image_listens_on_all_interfaces():
    """В контейнере без 0.0.0.0 не работает проброс портов; наружу их
    открывает только BIND_ADDR (docker-compose.yml, по умолчанию 127.0.0.1)."""
    assert 'ENV LISTEN_ADDR=0.0.0.0' in (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert '${BIND_ADDR:-127.0.0.1}:' in (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
