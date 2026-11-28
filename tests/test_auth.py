"""Личные токены и роли (src/auth.py, src/web/routes_auth.py): общий
командный сервер. Без токенов всё работает как раньше; с первым токеном —
вход по токену, роль user не управляет проектами, источниками и токенами."""

import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Empty
from pathlib import Path
from multiprocessing import Event, Process, Queue, get_context

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore, main as auth_cli
from src.core.project_manager import ProjectManager
from tests.license_helper import install_team_license

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}


def _verify_during_revoke(data_dir, token, start, done, results):
    store = TokenStore(data_dir)
    try:
        start.wait()
        verified = 0
        while not done.is_set():
            verified += store.verify(token) is not None
        time.sleep(0.13)  # дольше окна проверки кэша в 100 мс
        results.put(('verify_summary', verified))
        results.put(('final', store.verify(token) is None))
    except Exception as exc:  # передаём исключения родительскому тесту
        results.put(('error', repr(exc)))


def _revoke_at_start(data_dir, token_id, start, done, results):
    try:
        start.wait()
        TokenStore(data_dir).revoke(token_id)
        results.put(('revoked', True))
    except Exception as exc:
        results.put(('error', repr(exc)))
    finally:
        done.set()


def _first_verify_many(data_dir, token, start, results):
    try:
        store = TokenStore(data_dir)
        start.wait()
        results.put(('verified', all(store.verify(token) is not None for _ in range(20))))
    except Exception as exc:
        results.put(('error', repr(exc)))


def _hold_token_lock(path, entered, release):
    from src.auth import _token_file_lock
    with _token_file_lock(Path(path)):
        entered.set()
        release.wait(15)


def _stop_process(process, timeout=2):
    """Завершить дочерний процесс и дождаться его даже после таймаута теста."""
    if process.is_alive():
        process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(timeout)
    if process.is_alive() and hasattr(process, 'kill'):
        process.kill()
        process.join(timeout)


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


@pytest.mark.parametrize('token_count', [1, 2])
def test_failed_revoke_does_not_publish_candidate_state(tmp_path, monkeypatch, token_count):
    import src.auth as auth
    store = TokenStore(tmp_path)
    store.limit = lambda: 2
    records = [store.create(f'Синтетический {i}', 'admin') for i in range(token_count)]
    target, token = records[0]
    disk_before = store.path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise PermissionError('synthetic replace denied')

    monkeypatch.setattr(auth, 'replace_with_retry', fail_replace)
    with pytest.raises(PermissionError):
        store.revoke(target.id)

    assert store.enabled()
    assert store.verify(token) is not None
    assert store.path.read_bytes() == disk_before
    assert {item.id for item in store.list()} == {item.id for item, _ in records}


def test_failed_create_does_not_publish_candidate_state(tmp_path, monkeypatch):
    import src.auth as auth
    store = TokenStore(tmp_path)
    initial, token = store.create('Синтетический админ', 'admin')
    store.limit = lambda: 2
    disk_before = store.path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise PermissionError('synthetic replace denied')

    monkeypatch.setattr(auth, 'replace_with_retry', fail_replace)
    with pytest.raises(PermissionError):
        store.create('Новый синтетический пользователь')

    assert store.enabled() and store.verify(token) is not None
    assert store.path.read_bytes() == disk_before
    assert [item.id for item in store.list()] == [initial.id]


def test_revoke_retries_a_temporarily_busy_tokens_file(tmp_path, monkeypatch):
    """Windows может временно запретить замену занятого tokens.json."""
    import os
    from src.core import fsretry

    store = TokenStore(tmp_path)
    info, token = store.create('synthetic admin', 'admin')
    real_replace = os.replace
    attempts = []

    def busy_then_replace(src, dst):
        attempts.append((Path(src), Path(dst)))
        if len(attempts) <= 3:
            raise PermissionError(5, 'synthetic Windows sharing violation', str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(fsretry.os, 'replace', busy_then_replace)
    monkeypatch.setattr(fsretry, 'RETRY_DELAY_SEC', 0)
    store.revoke(info.id)

    assert len(attempts) == 4
    assert TokenStore(tmp_path).verify(token) is None
    assert not list(tmp_path.glob('tokens.json.*.tmp'))


def test_revoked_token_marks_do_not_accumulate_and_tokens_stay_revoked(tmp_path):
    store = TokenStore(tmp_path)
    revoked = []
    for number in range(5):
        info, token = store.create(f'Токен {number}', 'admin')
        assert store.verify(token) is not None
        assert info.id in store._last_persist
        store.revoke(info.id)
        revoked.append(token)
        assert info.id not in store._last_persist
        assert len(store._last_persist) <= len(store._tokens)

    assert all(store.verify(token) is None for token in revoked)
    assert store._last_persist == {}


def test_external_revoke_is_seen_when_mtime_is_restored(tmp_path):
    first = TokenStore(tmp_path)
    info, token = first.create('Синтетический токен', 'admin')
    assert first.verify(token) is not None

    second = TokenStore(tmp_path)
    old_stat = first.path.stat()
    second.revoke(info.id)
    os.utime(first.path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    # До исправления первый экземпляр оставался со старым кэшем.
    assert first.verify(token) is None


def test_external_revoke_is_seen_when_mtime_and_size_match(tmp_path):
    first = TokenStore(tmp_path)
    info, token = first.create('Синтетический токен', 'admin')
    assert first.verify(token) is not None

    second = TokenStore(tmp_path)
    old_stat = first.path.stat()
    second.revoke(info.id)
    content = first.path.read_bytes()
    first.path.write_bytes(content + b' ' * (old_stat.st_size - len(content)))
    os.utime(first.path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replaced_stat = first.path.stat()
    assert replaced_stat.st_size == old_stat.st_size
    assert replaced_stat.st_mtime_ns == old_stat.st_mtime_ns

    assert first.verify(token) is None


def test_token_store_does_not_reload_unchanged_file(tmp_path, monkeypatch):
    store = TokenStore(tmp_path)
    _, token = store.create('Синтетический токен', 'admin')
    assert store.verify(token) is not None
    reloads = 0
    original = store.path.read_bytes

    def counted_read(*args, **kwargs):
        nonlocal reloads
        reloads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(type(store.path), 'read_bytes', lambda self, *a, **kw:
                        counted_read(*a, **kw) if self == store.path else original(*a, **kw))
    for _ in range(10):
        assert store.verify(token) is not None
    assert reloads == 0


def test_parallel_verify_and_revoke(tmp_path):
    store = TokenStore(tmp_path)
    info, token = store.create('Синтетический токен', 'admin')
    assert store.verify(token) is not None

    def verify_many():
        return [store.verify(token) for _ in range(200)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(verify_many)
        store.revoke(info.id)
        results = future.result()

    assert any(result is not None for result in results)
    assert store.verify(token) is None


def test_broken_tokens_file_keeps_server_closed(tmp_path):
    (tmp_path / 'tokens.json').write_text('{не json', encoding='utf-8')
    store = TokenStore(tmp_path)
    assert store.enabled() and store.verify('ar_любой') is None


@pytest.mark.parametrize('contents', [b'', b'{broken', b'{}', b'[null]', b'[{}]'])
def test_invalid_token_file_fails_closed(tmp_path, contents):
    (tmp_path / 'tokens.json').write_bytes(contents)
    store = TokenStore(tmp_path)
    assert store.enabled()
    assert store.verify('ar_synthetic') is None


def test_disappeared_token_file_stays_fail_closed_until_restart(tmp_path):
    store = TokenStore(tmp_path)
    _, token = store.create('Синтетический владелец', 'admin')
    assert store.verify(token) is not None
    store.path.unlink()
    assert store.enabled() and store.verify(token) is None


def test_permission_error_during_token_check_fails_closed_without_raising(tmp_path, monkeypatch):
    store = TokenStore(tmp_path)
    _, token = store.create('Синтетический владелец', 'admin')
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError('synthetic denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', denied)
    assert store.enabled() and store.verify(token) is None


def test_transient_windows_read_sharing_error_retries_before_failing_closed(tmp_path, monkeypatch):
    store = TokenStore(tmp_path)
    _, token = store.create('synthetic admin', 'admin')
    original = Path.read_bytes
    attempts = 0

    def briefly_denied(path, *args, **kwargs):
        nonlocal attempts
        if path == store.path:
            attempts += 1
            if attempts <= 2:
                raise PermissionError(13, 'synthetic Windows sharing violation', str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_bytes', briefly_denied)
    assert store.verify(token) is not None
    assert attempts == 3


def test_multiprocess_revoke_and_last_used_persistence_are_serialized(tmp_path):
    store = TokenStore(tmp_path)
    info, token = store.create('Синтетический владелец', 'admin')
    ctx = get_context('spawn')
    start, done, results = ctx.Event(), ctx.Event(), ctx.Queue()
    verifier = ctx.Process(target=_verify_during_revoke,
                           args=(str(tmp_path), token, start, done, results))
    revoker = ctx.Process(target=_revoke_at_start,
                          args=(str(tmp_path), info.id, start, done, results))
    processes = (verifier, revoker)
    try:
        for process in processes:
            process.start()
        start.set()
        # Читаем очередь параллельно с ожиданием: дочерние Queue feeder threads
        # не должны зависнуть, если тест завершается по таймауту.
        deadline = time.monotonic() + 15
        messages = []
        while time.monotonic() < deadline and any(p.is_alive() for p in processes):
            try:
                messages.append(results.get(timeout=0.05))
            except Empty:
                pass
        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        if any(process.is_alive() for process in processes):
            pytest.fail('процесс verify/revoke завис')
        while True:
            try:
                messages.append(results.get_nowait())
            except Empty:
                break
        assert verifier.exitcode == revoker.exitcode == 0
        assert not [m for m in messages if m[0] == 'error'], messages
        assert any(m[0] == 'verify_summary' and m[1] > 0 for m in messages), messages
        assert ('revoked', True) in messages
        assert ('final', True) in messages
        assert TokenStore(tmp_path).verify(token) is None
        assert not list(tmp_path.glob('tokens.json.*.tmp'))
    finally:
        start.set()
        for process in processes:
            _stop_process(process)
            if process.pid is not None:
                process.close()
        results.close()
        results.join_thread()


def test_multiprocess_first_verify_uses_unique_temp_files(tmp_path):
    _, token = TokenStore(tmp_path).create('Синтетический владелец', 'admin')
    ctx = get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_first_verify_many,
                           args=(str(tmp_path), token, start, results)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        start.set()
        deadline = time.monotonic() + 15
        messages = []
        while time.monotonic() < deadline and any(w.is_alive() for w in workers):
            try:
                messages.append(results.get(timeout=0.05))
            except Empty:
                pass
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            pytest.fail('процесс verify завис')
        while True:
            try:
                messages.append(results.get_nowait())
            except Empty:
                break
        assert all(worker.exitcode == 0 for worker in workers)
        assert messages.count(('verified', True)) == 2
        assert not [message for message in messages if message[0] == 'error'], messages
        assert not list(tmp_path.glob('tokens.json.*.tmp'))
    finally:
        for worker in workers:
            _stop_process(worker)
            if worker.pid is not None:
                worker.close()
        results.close()
        results.join_thread()


def test_interprocess_lock_skips_last_used_without_waiting_but_rejects_writes(tmp_path):
    from src.auth import _TOKEN_LOCK_TIMEOUT_SEC
    store = TokenStore(tmp_path)
    info, token = store.create('Синтетический владелец', 'admin')
    store._last_persist[info.id] = 0
    ctx = get_context('spawn')
    entered, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_token_lock,
                         args=(str(store.path), entered, release))
    holder.start()
    holder_exitcode = None
    try:
        assert entered.wait(10), 'процесс не захватил блокировку'
        started = time.monotonic()
        verified = store.verify(token)
        assert verified is not None and verified.id == info.id
        assert time.monotonic() - started < 0.1

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            store.revoke(info.id)
        assert time.monotonic() - started < _TOKEN_LOCK_TIMEOUT_SEC + 1

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            store.create('Новый синтетический владелец')
        assert time.monotonic() - started < _TOKEN_LOCK_TIMEOUT_SEC + 1
        assert store.verify(token) is not None
    finally:
        release.set()
        _stop_process(holder)
        holder_exitcode = holder.exitcode
        if holder.pid is not None:
            holder.close()
    assert holder_exitcode == 0

    # Проверяющий процесс мог закэшировать токен до отзыва. После завершённого
    # отзыва проверка обязана перечитать файл и отказать.
    ctx = get_context('spawn')
    start, done, results = ctx.Event(), ctx.Event(), ctx.Queue()
    revoker = ctx.Process(target=_revoke_at_start,
                          args=(str(tmp_path), info.id, start, done, results))
    revoker.start()
    start.set()
    revoker_exitcode = None
    try:
        revoker.join(10)
        if revoker.is_alive():
            pytest.fail('процесс отзыва завис')
    finally:
        _stop_process(revoker)
        revoker_exitcode = revoker.exitcode
        if revoker.pid is not None:
            revoker.close()
    messages = []
    while True:
        try:
            messages.append(results.get(timeout=0.1))
        except Empty:
            break
    assert revoker_exitcode == 0
    assert ('revoked', True) in messages
    assert store.verify(token) is None
    results.close()
    results.join_thread()


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
    assert client.get('/admin/platform-help', headers=as_user).status_code == 403
    assert client.post('/api/admin/platform-help', headers=as_user).status_code == 403
    assert client.post('/api/admin/platform-help/8.3.27.1/delete', headers=as_user).status_code == 403
    assert client.get('/api/admin/platform-help/8.3.27.1/download', headers=as_user).status_code == 403
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
    assert client.get('/license/agreement', follow_redirects=False).status_code == 200   # соглашение открыто


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
