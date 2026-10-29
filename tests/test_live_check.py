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

import pytest

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
def native_server():
    """Сервер без Docker, как при разработке: python -m src.main."""
    live_check = _load_live_check()
    web_port, mcp_port = _free_port(), _free_port()
    with tempfile.TemporaryDirectory() as tmpdir:
        env = {**os.environ, 'DATA_DIR': os.path.join(tmpdir, 'data'), 'WEB_PORT': str(web_port),
               'MCP_HTTP_PORT': str(mcp_port), 'LISTEN_ADDR': '127.0.0.1',
               'PYTHONIOENCODING': 'utf-8'}
        env.pop('INDEX_DIR', None)
        log_path = os.path.join(tmpdir, 'server.log')
        with open(log_path, 'w', encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'src.main'], cwd=ROOT, env=env,
                                    stdout=log, stderr=subprocess.STDOUT)
            ctx = live_check.LiveContext(web=f'http://127.0.0.1:{web_port}',
                                         mcp=f'http://127.0.0.1:{mcp_port}/mcp', quick=True)
            try:
                deadline = time.time() + 60
                while True:
                    try:
                        if ctx.web_get('/health').status_code == 200:
                            break
                    except Exception:
                        pass
                    if proc.poll() is not None or time.time() > deadline:
                        pytest.fail("сервер не поднялся:\n" + Path(log_path).read_text(encoding='utf-8'))
                    time.sleep(0.3)
                yield live_check, ctx, log_path
            finally:
                proc.terminate()
                try:
                    proc.wait(20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(20)


def test_live_check_passes_and_cleans_up(native_server, capsys):
    live_check, ctx, log_path = native_server
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
    log = Path(log_path).read_text(encoding='utf-8')
    assert 'Traceback' not in log, log[-3000:]


def test_live_check_reports_unreachable_server(capsys):
    live_check = _load_live_check()
    port = _free_port()
    code = live_check.main(['--web', f'http://127.0.0.1:{port}', '--mcp', f'http://127.0.0.1:{port}/mcp'])
    assert code == 2
    assert 'недоступен' in capsys.readouterr().err
