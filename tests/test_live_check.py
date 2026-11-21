"""
scripts/live_check.py против настоящего сервера: `python -m src.main` на
свободных портах с временным DATA_DIR, проверки идут по сети.

Маркер live: тест поднимает отдельный процесс сервера и идёт ~15 с, поэтому
в быстрые тесты не входит; в CI запускается отдельным шагом
(`python -m pytest tests -q -m live`) на Ubuntu и Windows.
"""

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.fixtures.generate_platform_help import context_book, query_book

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.live


def _load_live_check():
    spec = importlib.util.spec_from_file_location('live_check', ROOT / 'scripts' / 'live_check.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['live_check'] = module      # dataclass ищет свой модуль в sys.modules
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def native_server(request, monkeypatch):
    """Сервер без Docker, как при разработке: python -m src.main."""
    live_check = _load_live_check()
    web_port, mcp_port = _free_port(), _free_port()
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, 'DATA_DIR': os.path.join(tmpdir, 'data'), 'WEB_PORT': str(web_port),
               'MCP_HTTP_PORT': str(mcp_port), 'LISTEN_ADDR': '127.0.0.1',
               'MCP_ALLOWED_HOSTS': 'ariadna.lan',
               'PYTHONIOENCODING': 'utf-8'}
        command = [sys.executable, '-m', 'src.main']
        licensed = getattr(request, 'param', None)
        if licensed is not None:
            env['LISTEN_ADDR'] = '0.0.0.0'
        if licensed:
            from src import license
            from tests.license_helper import install_team_license

            install_team_license(monkeypatch, env['DATA_DIR'])
            command = [sys.executable, '-c', 'import sys; from src import license, main; '
                       'license.PUBLIC_KEY = sys.argv[1]; main.main()', license.PUBLIC_KEY]
        platform = Path(tmpdir) / 'platform' / '8.3.27.1' / 'bin'
        platform.mkdir(parents=True)
        (platform / 'shcntx_ru.hbk').write_bytes(context_book())
        (platform / 'shquery_ru.hbk').write_bytes(query_book())
        env['ARIADNA_PLATFORM'] = str(platform)
        env.pop('INDEX_DIR', None)
        log_path = os.path.join(tmpdir, 'server.log')
        with open(log_path, 'w', encoding='utf-8') as log:
            proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            ctx = live_check.LiveContext(web=f'http://127.0.0.1:{web_port}',
                                         mcp=f'http://127.0.0.1:{mcp_port}/mcp', quick=True)
            try:
                deadline = time.time() + 60
                while True:
                    try:
                        if ctx.web_get('/health').status_code == 200:
                            with socket.create_connection(('127.0.0.1', mcp_port), timeout=1):
                                break
                    except Exception:
                        pass
                    if proc.poll() is not None or time.time() > deadline:
                        pytest.fail("сервер не поднялся:\n" + Path(log_path).read_text(encoding='utf-8'))
                    time.sleep(0.3)
                yield live_check, ctx, log_path, licensed
            finally:
                proc.terminate()
                try:
                    proc.wait(20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(20)


def test_live_check_passes_and_cleans_up(native_server, capsys):
    live_check, ctx, log_path, _ = native_server
    response = ctx.web_get('/login', headers={'Host': 'ariadna.lan'})
    assert response.status_code == 400 and 'лицензионный ключ' in response.text
    assert ctx.web_get('/health', headers={'Host': 'localhost:9878'}).json() == {'status': 'ok'}
    assert 'Локальный режим' in ctx.web_get('/').text
    # Проект «владельца»: live_check читает его (--project) и не трогает.
    ctx.create_project('owner')
    ctx.reindex_web('owner')
    assert ctx.wait_ready('owner') == 'ready'

    code = live_check.main(['--web', ctx.web, '--mcp', ctx.mcp, '--quick', '--project', 'owner'])
    out = capsys.readouterr().out
    assert code == 0, out
    assert 'Итого:' in out and ', 0 FAIL' in out
    for section in ('1. Регрессия', '2. Индексация', '3. Playground == MCP', '4. Пересоздание',
                    '5. Удаление под нагрузкой', '6. Стресс', '8. Проект owner'):
        assert section in out, section

    # Временные проекты удалены, чужой остался.
    assert ctx.project_ids() == ['owner']
    answer, is_error = ctx.call('platform_help', {'query': 'СтрНайти', 'kind': 'query'})
    assert not is_error and '8.3.27.1' in answer and 'СтрНайти(А, Б)' in answer
    log = Path(log_path).read_text(encoding='utf-8')
    assert 'Traceback' not in log, log[-3000:]


def test_live_check_reports_unreachable_server(capsys):
    live_check = _load_live_check()
    port = _free_port()
    code = live_check.main(['--web', f'http://127.0.0.1:{port}', '--mcp', f'http://127.0.0.1:{port}/mcp'])
    assert code == 2
    assert 'недоступен' in capsys.readouterr().err


@pytest.mark.parametrize('native_server', [False, True], indirect=True, ids=['free', 'licensed'])
def test_native_wide_listen_addr_depends_on_license(native_server):
    _, ctx, _, licensed = native_server
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.2', 0))
        except OSError:
            pytest.skip('ОС не поддерживает второй петлевой IPv4-адрес')

    # Вторая петля проверяет широкую привязку без зависимости от адресов LAN.
    for url in (ctx.web, ctx.mcp):
        addr = ('127.0.0.2', urlsplit(url).port)
        if licensed:
            with socket.create_connection(addr, timeout=1):
                pass
        else:
            with pytest.raises((ConnectionRefusedError, TimeoutError)):
                socket.create_connection(addr, timeout=1).close()
