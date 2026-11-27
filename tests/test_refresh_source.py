"""Обновление источника для робота выгрузки (src/web/routes_update.py):
одним запросом заменить выгрузку (по желанию), переиндексировать и дождаться
результата. Роль user достаточно."""

import io
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore
from src.core.project_manager import ProjectManager
from src.core import source_delta
from tests.license_helper import install_team_license

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}
MODULE = 'CommonModules/ЦеныСервер/Ext/Module.bsl'


def _zip(root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in root.rglob('*'):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    return buf.getvalue()


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        shutil.copytree(FIXTURES / 'xml_ru', pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml')
        pm.add_source('p1', 'main', 'Основная')
        pm.reindex('p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST), Path(tmpdir)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def _procs(pm):
    return {r[0] for r in pm.get_db('p1').read_conn().execute('SELECT name FROM procedures')}


def _manifest_version(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def test_refresh_from_folder_waits_for_result(web):
    pm, client, _ = web
    module = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nПроцедура ДобавленаРоботом() Экспорт\nКонецПроцедуры\n', encoding='utf-8-sig')
    resp = client.post('/api/projects/p1/sources/main/refresh')
    body = resp.json()
    assert resp.status_code == 200 and body['status'] == 'ready', body
    assert body['index']['total_procedures'] > 0 and 'duration_sec' in body
    assert 'ДобавленаРоботом' in _procs(pm)


def test_refresh_with_archive_replaces_dump(web):
    pm, client, tmp = web
    dump = tmp / 'новая выгрузка'
    shutil.copytree(FIXTURES / 'xml_ru', dump)
    module = dump / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nПроцедура ИзАрхива() Экспорт\nКонецПроцедуры\n', encoding='utf-8-sig')
    resp = client.post('/api/projects/p1/sources/main/refresh',
                       files={'xml_archive': ('dump.zip', _zip(dump), 'application/zip')})
    assert resp.status_code == 200 and resp.json()['status'] == 'ready', resp.text
    assert 'ИзАрхива' in _procs(pm)


def test_delta_refresh_updates_one_file_and_indexes(web):
    pm, client, _ = web
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    assert set(manifest) == {'version', 'files'}
    changed = MODULE
    path = pm.get_source_xml_path('p1', 'main') / Path(*changed.split('/'))
    payload = path.read_bytes() + '\nПроцедура НоваяРазница() Экспорт\nКонецПроцедуры\n'.encode()
    target_files = dict(manifest['files'])
    target_files[changed] = {'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}
    target = {'version': _manifest_version(target_files), 'files': target_files}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(changed, payload)
    archive.seek(0)
    response = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': manifest['version'], 'target_manifest': json.dumps(target, ensure_ascii=False),
        'wait': '1', 'timeout': '30'},
        files={'delta_archive': ('delta.zip', archive.getvalue(), 'application/zip')})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready'
    assert 'НоваяРазница' in _procs(pm)


def test_delta_without_configuration_xml_clears_old_metadata(web):
    """Разница удалила Configuration.xml и файлы объектов: индекс не должен
    остаться ready со старыми объектами источника."""
    pm, client, _ = web
    conn = pm.get_db('p1').read_conn()
    assert conn.execute('SELECT COUNT(*) FROM metadata_objects').fetchone()[0] > 0
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    keep = {name: spec for name, spec in manifest['files'].items() if name.endswith('Module.bsl')}
    assert 'Configuration.xml' not in keep and keep
    target = {'version': _manifest_version(keep), 'files': keep}
    response = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': manifest['version'], 'target_manifest': json.dumps(target, ensure_ascii=False),
        'wait': '1', 'timeout': '30'},
        files={'delta_archive': ('delta.zip', _zip_from_files({}), 'application/zip')})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready'
    conn = pm.get_db('p1').read_conn()
    assert conn.execute('SELECT COUNT(*) FROM metadata_objects').fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM modules WHERE source_id='main'").fetchone()[0] > 0


def test_delta_refresh_rejects_stale_manifest(web):
    _, client, _ = web
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    target = {'version': _manifest_version(manifest['files']), 'files': manifest['files']}
    response = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': 'stale-version', 'target_manifest': json.dumps(target),
        'wait': '1', 'timeout': '30'}, files={'delta_archive': ('delta.zip', b'', 'application/zip')})
    assert response.status_code == 409
    assert 'заново' in response.json()['detail']


def test_empty_delta_reindexes_a_stale_index(web):
    pm, client, _ = web
    path = pm.get_source_xml_path('p1', 'main') / Path(*MODULE.split('/'))
    path.write_bytes(path.read_bytes() + '\nПроцедура ДискОпережаетИндекс() Экспорт\nКонецПроцедуры\n'.encode())
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    target = {'version': _manifest_version(manifest['files']), 'files': manifest['files']}
    empty_archive = io.BytesIO()
    with zipfile.ZipFile(empty_archive, 'w'):
        pass
    response = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': manifest['version'], 'target_manifest': json.dumps(target),
        'wait': '1', 'timeout': '30'},
        files={'delta_archive': ('empty.zip', empty_archive.getvalue(), 'application/zip')})
    assert response.status_code == 200, response.text
    assert 'ДискОпережаетИндекс' in _procs(pm)


def test_first_delta_populates_source_without_dump(web):
    pm, client, _ = web
    pm.add_source('p1', 'empty', 'Пустой')
    baseline = client.get('/api/projects/p1/sources/empty/manifest').json()
    assert baseline['files'] == {}
    payloads = {}
    for file in (FIXTURES / 'xml_ru').rglob('*'):
        if file.is_file() and not file.name.startswith('Template.'):
            payloads[file.relative_to(FIXTURES / 'xml_ru').as_posix()] = file.read_bytes()
    target_files = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                    for name, data in payloads.items()}
    target = {'version': _manifest_version(target_files), 'files': target_files}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in payloads.items():
            zf.writestr(name, data)
    response = client.post('/api/projects/p1/sources/empty/delta', data={
        'base_version': baseline['version'], 'target_manifest': json.dumps(target, ensure_ascii=False),
        'wait': '1', 'timeout': '30'}, files={'delta_archive': ('first.zip', archive.getvalue(), 'application/zip')})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready'
    assert pm.get_db('p1').read_conn().execute(
        "SELECT COUNT(*) FROM modules WHERE source_id='empty'").fetchone()[0] > 0


def test_delta_body_limit_is_checked_before_form_parse(web, monkeypatch):
    _, client, _ = web
    monkeypatch.setenv('ARIADNA_DELTA_MAX_BODY', '32')
    response = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': 'unused', 'target_manifest': '{}'},
        files={'delta_archive': ('delta.zip', b'archive contents', 'application/zip')})
    assert response.status_code == 413


def test_manifest_recovers_pending_transaction_before_returning_version(web, monkeypatch):
    pm, client, _ = web
    before = client.get('/api/projects/p1/sources/main/manifest').json()
    rel = MODULE
    path = pm.get_source_xml_path('p1', 'main') / Path(*rel.split('/'))
    old_bytes = path.read_bytes()
    new_bytes = old_bytes + b'\n// changed\n'
    files = dict(before['files'])
    files[rel] = {'sha256': hashlib.sha256(new_bytes).hexdigest(), 'size': len(new_bytes)}
    target = {'version': _manifest_version(files), 'files': files}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr(rel, new_bytes)
    archive.seek(0)
    workspace = pm._source_delta_workspace('p1', 'main')

    def fail_at(point):
        if point == 'e':
            raise RuntimeError('simulated crash after installing files')

    real_recover = source_delta.recover
    recover_calls = 0

    def fail_rollback(*args, **kwargs):
        nonlocal recover_calls
        recover_calls += 1
        if recover_calls == 1:
            return real_recover(*args, **kwargs)
        raise OSError('rollback unavailable')

    monkeypatch.setattr(source_delta, '_checkpoint', fail_at)
    monkeypatch.setattr(source_delta, 'recover', fail_rollback)
    with pytest.raises(OSError):
        source_delta.apply(pm.get_source_xml_path('p1', 'main'), archive, target,
                           before['version'], before, workspace)
    assert path.read_bytes() == new_bytes
    monkeypatch.setattr(source_delta, 'recover', real_recover)
    after = client.get('/api/projects/p1/sources/main/manifest')
    assert after.status_code == 200
    assert after.json()['version'] == before['version']
    assert path.read_bytes() == old_bytes
    assert not source_delta._journal_path(workspace).exists()


def test_refresh_never_reports_ready_when_reindex_is_already_running(web, monkeypatch):
    pm, client, _ = web
    monkeypatch.setattr(pm, 'reindex_async', lambda *_a, **_kw: {'status': 'already_running'})
    response = client.post('/api/projects/p1/sources/main/refresh', data={'timeout': '1'})
    assert response.status_code == 409
    assert response.json()['status'] == 'busy'


def test_refresh_retries_already_running_until_launch_succeeds(web, monkeypatch):
    pm, client, _ = web
    real_reindex = pm.reindex_async
    calls = 0

    def busy_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {'status': 'already_running'}
        return real_reindex(*args, **kwargs)

    monkeypatch.setattr(pm, 'reindex_async', busy_once)
    response = client.post('/api/projects/p1/sources/main/refresh', data={'timeout': '5'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready'
    assert calls >= 2


def test_manifest_and_ui_update_do_not_observe_or_change_delta_midflight(web):
    pm, client, _ = web
    lock = pm._reindex_lock('p1')
    assert lock.acquire(blocking=False)
    try:
        assert client.get('/api/projects/p1/sources/main/manifest').status_code == 409
        response = client.post('/api/projects/p1/sources/main/update', data={'label': 'new label'})
        assert response.status_code == 409
    finally:
        lock.release()


def test_refresh_route_no_longer_accepts_delta_archive_parameter():
    import inspect
    from src.web.routes_update import refresh_source

    assert 'delta_archive' not in inspect.signature(refresh_source).parameters


def test_full_tree_update_invalidates_cached_source_hashes(web):
    pm, client, _ = web
    before = client.get('/api/projects/p1/sources/main/manifest').json()
    cache = pm._source_manifest_cache_path('p1', 'main')
    assert cache.exists()
    rel = MODULE
    old_path = pm.get_source_xml_path('p1', 'main') / Path(*rel.split('/'))
    old_stat = old_path.stat()
    old_bytes = old_path.read_bytes()
    new_bytes = bytes([old_bytes[0] ^ 1]) + old_bytes[1:]
    assert len(new_bytes) == old_stat.st_size
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr(rel, new_bytes)
    archive.seek(0)
    pm.update_source('p1', 'main', xml_archive=archive, xml_archive_filename='replacement.zip')
    assert not cache.exists()
    replaced = pm.get_source_xml_path('p1', 'main') / Path(*rel.split('/'))
    current_stat = replaced.stat()
    os.utime(replaced, ns=(current_stat.st_atime_ns, old_stat.st_mtime_ns))
    replaced = replaced.stat()
    assert replaced.st_size == old_stat.st_size and replaced.st_mtime_ns == old_stat.st_mtime_ns
    after = pm.source_file_manifest('p1', 'main')
    assert after['version'] != before['version']
    assert after['files'][rel]['sha256'] != before['files'][rel]['sha256']


def test_full_tree_update_recovers_old_delta_journal_first(web):
    pm, _, _ = web
    root = pm.get_source_xml_path('p1', 'main')
    old_path = root / Path(*MODULE.split('/'))
    old_bytes = old_path.read_bytes()
    old_path.write_bytes(b'partial-delta')
    workspace = pm._source_delta_workspace('p1', 'main')
    backup = workspace / 'rollback-pending'
    backup_path = backup / Path(*MODULE.split('/'))
    backup_path.parent.mkdir(parents=True)
    backup_path.write_bytes(old_bytes)
    source_delta._atomic_json(source_delta._journal_path(workspace), {
        'backup': str(backup), 'staging': '', 'old': [MODULE], 'new': [MODULE],
        'created_dirs': [], 'removed_dirs': [], 'committed': False,
    })
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('replacement.xml', b'full replacement')
    archive.seek(0)
    pm.update_source('p1', 'main', xml_archive=archive, xml_archive_filename='full.zip')
    new_root = pm.get_source_xml_path('p1', 'main')
    assert (new_root / 'replacement.xml').read_bytes() == b'full replacement'
    assert not (new_root / Path(*MODULE.split('/'))).exists()
    assert not source_delta._journal_path(workspace).exists()


def test_first_delta_registers_xml_path_before_committed_checkpoint(web, monkeypatch):
    pm, _, _ = web
    pm.add_source('p1', 'fresh', 'fresh')
    root = pm.projects_dir / 'p1' / 'sources' / 'fresh' / 'xml'
    baseline = pm.source_file_manifest('p1', 'fresh')
    payload = b'<Configuration/>'
    target_files = {'Configuration.xml': {'sha256': hashlib.sha256(payload).hexdigest(),
                                          'size': len(payload)}}
    target = {'version': _manifest_version(target_files), 'files': target_files}

    def crash_after_commit(point):
        if point == 'f':
            raise KeyboardInterrupt()

    monkeypatch.setattr(source_delta, '_checkpoint', crash_after_commit)
    with pytest.raises(KeyboardInterrupt):
        pm.apply_source_delta('p1', 'fresh', io.BytesIO(_zip_from_files({'Configuration.xml': payload})),
                              baseline['version'], target)
    assert pm.get_source_xml_path('p1', 'fresh') == root
    assert (root / 'Configuration.xml').read_bytes() == payload
    source_delta.recover(root, pm._source_delta_workspace('p1', 'fresh'))


def test_refresh_registers_existing_files_before_returning_ready(web):
    pm, client, _ = web
    source = pm.add_source('p1', 'unregistered', 'unregistered')
    root = pm.projects_dir / 'p1' / 'sources' / 'unregistered' / 'xml'
    root.mkdir(parents=True)
    (root / 'existing.xml').write_text('<Configuration/>', encoding='utf-8')
    source.xml_path = ''
    pm._save_registry()
    response = client.post('/api/projects/p1/sources/unregistered/refresh', data={'timeout': '30'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ready'
    assert pm.get_project('p1').sources[-1].xml_path == 'sources/unregistered/xml'


def test_removing_source_also_removes_manifest_cache(web):
    pm, _, _ = web
    cache = pm._source_manifest_cache_path('p1', 'main')
    pm.source_file_manifest('p1', 'main')
    assert cache.exists()
    pm.remove_source('p1', 'main')
    assert not cache.exists()
    pm.add_source('p1', 'main', 'recreated')
    assert pm.source_file_manifest('p1', 'main')['files'] == {}


def _zip_from_files(files):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        for path, payload in files.items():
            zf.writestr(path, payload)
    return archive.getvalue()


def test_delta_rejects_duplicate_target_json_keys_and_corrupt_zip(web):
    _, client, _ = web
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    duplicate = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': manifest['version'], 'target_manifest': '{"files":{},"files":{}}',
        'wait': '1', 'timeout': '1'}, files={'delta_archive': ('bad.zip', b'', 'application/zip')})
    assert duplicate.status_code == 400
    empty_version = _manifest_version({})
    corrupt = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': manifest['version'],
        'target_manifest': json.dumps({'version': empty_version, 'files': {}}),
        'wait': '1', 'timeout': '1'}, files={'delta_archive': ('bad.zip', b'not zip', 'application/zip')})
    assert corrupt.status_code == 400


def test_delta_wait_zero_does_not_wait_before_applying(web, monkeypatch):
    import src.web.routes_update as routes_update
    _, client, _ = web
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    target_files = manifest['files']
    target = {'version': _manifest_version(target_files), 'files': target_files}

    async def should_not_wait(*_args):
        raise AssertionError('wait=0 must not wait for project idle')

    monkeypatch.setattr(routes_update, '_wait_idle', should_not_wait)
    response = client.post('/api/projects/p1/sources/main/delta', data={
        'base_version': manifest['version'], 'target_manifest': json.dumps(target),
        'wait': '0', 'timeout': '1'}, files={'delta_archive': ('empty.zip', _zip_from_files({}), 'application/zip')})
    assert response.status_code == 202
    assert response.json()['status'] == 'started'


def test_robot_scripts_retry_busy_statuses():
    bash = (Path(__file__).parent.parent / 'scripts' / 'update_from_infobase.sh').read_text(encoding='utf-8')
    powershell = (Path(__file__).parent.parent / 'scripts' / 'update_from_infobase.ps1').read_text(encoding='utf-8')
    assert '[ "$get_status" = 409 ] && [ "$SECONDS" -lt "$deadline" ]' in bash
    assert '[ "$post_status" = 409 ] && [ "$SECONDS" -lt "$deadline" ]' in bash
    assert 'local get_timeout=$((deadline - SECONDS))' in bash
    assert '[System.Diagnostics.Stopwatch]::StartNew()' in powershell
    assert '[Environment]::TickCount64' not in powershell
    assert '$Deadline - $script:DeltaStopwatch.ElapsedMilliseconds' in powershell
    assert "--max-time', \"$getTimeout\"" in powershell


def test_bash_robot_requires_successful_post_not_only_manifest(tmp_path):
    """Срок истёк до POST: 200 от GET манифеста не считается обновлением."""
    if os.name == 'nt' or shutil.which('bash') is None or shutil.which('python3') is None:
        pytest.skip('нужны bash и python3 (POSIX)')
    dump = tmp_path / 'dump'
    dump.mkdir()
    (dump / 'Configuration.xml').write_text('<x/>', encoding='utf-8')
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    fake_curl = fake_bin / 'curl'
    fake_curl.write_text(
        '#!/bin/sh\n'
        'out=\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        '    -w) shift 2 ;;\n'
        '    *) shift ;;\n'
        '  esac\n'
        'done\n'
        '[ -n "$out" ] && printf \'{"version":"v1","files":{}}\' > "$out"\n'
        'printf 200\n', encoding='utf-8')
    fake_curl.chmod(0o755)
    env = {**os.environ, 'PATH': f'{fake_bin}{os.pathsep}{os.environ["PATH"]}'}
    script = Path(__file__).parent.parent / 'scripts' / 'update_from_infobase.sh'
    proc = subprocess.run(['bash', str(script), '--skip-dump', '--dump-dir', str(dump),
                           '--ariadna-url', 'http://127.0.0.1:1', '--project', 'p',
                           '--timeout-minutes', '0'],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_refresh_no_wait_and_unknown_source(web):
    _, client, _ = web
    resp = client.post('/api/projects/p1/sources/main/refresh', data={'wait': '0'})
    assert resp.status_code == 202 and resp.json()['status'] == 'started'
    assert client.post('/api/projects/p1/sources/нет/refresh').status_code == 404
    assert client.post('/api/projects/нет/sources/main/refresh').status_code == 404


def test_refresh_is_allowed_for_user_role(web, monkeypatch):
    pm, client, _ = web
    install_team_license(monkeypatch, pm.data_dir)
    store = TokenStore(pm.data_dir)
    store.create('Админ', 'admin')
    _, robot = store.create('Робот выгрузки', 'user')
    auth = {'Authorization': f'Bearer {robot}'}
    assert client.post('/api/projects/p1/sources/main/refresh').status_code == 401
    resp = client.post('/api/projects/p1/sources/main/refresh', headers=auth)
    assert resp.status_code == 200 and resp.json()['status'] == 'ready'
    # Замена источника через Web UI и его удаление — по-прежнему только admin.
    assert client.post('/api/projects/p1/sources/main/update', headers=auth).status_code == 403
    assert client.post('/api/projects/p1/sources/main/delete', headers=auth).status_code == 403
