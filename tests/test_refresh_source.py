"""Обновление источника для робота выгрузки (src/web/routes_update.py):
одним запросом заменить выгрузку (по желанию), переиндексировать и дождаться
результата. Роль user достаточно."""

import io
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import shutil
import stat
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore
from src.core.project_manager import ProjectManager
from src.core.source_manifest import (DELTA_META, MANIFEST_NAME, UnsafeArchiveError,
                                      apply_delta, build_manifest, manifest_version,
                                      read_or_create_manifest, write_manifest)
from tests.license_helper import install_team_license

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}
MODULE = next(path.relative_to(FIXTURES / 'xml_ru').as_posix()
              for path in (FIXTURES / 'xml_ru').rglob('Module.bsl'))
def _zip(root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in root.rglob('*'):
            if f.is_file() and f.name != MANIFEST_NAME:
                z.write(f, f.relative_to(root).as_posix())
    return buf.getvalue()


def _manifest(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob('*')):
        if (path.is_file() and not path.name.startswith('Template.')
                and path.name != MANIFEST_NAME):
            data = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {
                'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return {'version': hashlib.sha256(encoded).hexdigest(), 'files': files}


def _delta(base: dict, target: dict, root: Path, names: set[str] | None = None) -> bytes:
    changed = {p for p, value in target['files'].items() if base['files'].get(p) != value}
    deleted = sorted(set(base['files']) - set(target['files']))
    meta = {'base_version': base['version'], 'target_version': target['version'],
            'files': target['files'], 'deleted': deleted}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(DELTA_META, json.dumps(meta, ensure_ascii=False))
        for name in sorted(changed if names is None else names):
            z.write(root / Path(name), name)
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


def test_refresh_from_folder_waits_for_result(web):
    pm, client, _ = web
    module = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nProcedure RefreshMarker() Export\nEndProcedure\n', encoding='utf-8-sig')
    resp = client.post('/api/projects/p1/sources/main/refresh', data={'timeout': '10'})
    body = resp.json()
    assert resp.status_code == 200 and body['status'] == 'ready', body
    assert body['index']['total_procedures'] > 0
    assert 'RefreshMarker' in _procs(pm)


def test_refresh_retries_transient_index_start_failure(web, monkeypatch):
    pm, client, _ = web
    module = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nProcedure StartRetryMarker() Export\nEndProcedure\n', encoding='utf-8-sig')
    original = pm._mark_indexing
    attempts = 0
    def fail_once(project_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('injected start failure')
        return original(project_id)
    monkeypatch.setattr(pm, '_mark_indexing', fail_once)
    response = client.post('/api/projects/p1/sources/main/refresh', data={'timeout': '10'})
    assert response.status_code == 200 and response.json()['status'] == 'ready', response.text
    assert attempts >= 2 and 'StartRetryMarker' in _procs(pm)

def test_refresh_with_archive_replaces_dump(web):
    pm, client, tmp = web
    dump = tmp / 'archive-dump'
    shutil.copytree(FIXTURES / 'xml_ru', dump)
    module = dump / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nProcedure ArchiveMarker() Export\nEndProcedure\n', encoding='utf-8-sig')
    resp = client.post('/api/projects/p1/sources/main/refresh',
                       files={'xml_archive': ('dump.zip', _zip(dump), 'application/zip')})
    assert resp.status_code == 200 and resp.json()['status'] == 'ready', resp.text
    assert 'ArchiveMarker' in _procs(pm)

def test_refresh_no_wait_and_unknown_source(web):
    _, client, _ = web
    resp = client.post('/api/projects/p1/sources/main/refresh', data={'wait': '0'})
    assert resp.status_code == 202 and resp.json()['status'] == 'queued'
    assert client.post('/api/projects/p1/sources/нет/refresh').status_code == 404
    assert client.post('/api/projects/нет/sources/main/refresh').status_code == 404


def test_manifest_hashes_legacy_source_once_and_then_uses_cache(web, monkeypatch):
    _, client, _ = web
    from src.core import source_manifest
    original = source_manifest._hash_file
    calls = 0

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(source_manifest, '_hash_file', counted)
    first = client.get('/api/projects/p1/sources/main/manifest')
    after_first = calls
    second = client.get('/api/projects/p1/sources/main/manifest')
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls == after_first


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


def test_update_clients_fall_back_without_python_after_manifest_check():
    ps1 = (Path(__file__).parents[1] / 'scripts' / 'update_from_infobase.ps1').read_text(encoding='utf-8')
    sh = (Path(__file__).parents[1] / 'scripts' / 'update_from_infobase.sh').read_text(encoding='utf-8')
    end = chr(10) + '}' + chr(10)
    ps_delta = ps1.split('function Invoke-DeltaRefresh($Item) {', 1)[1].split(end, 1)[0]
    sh_delta = sh.split('delta_refresh() {', 1)[1].split(end, 1)[0]
    assert ps_delta.index("if ($status -ne '200')") < ps_delta.index('Get-Command python')
    assert 'Python 3 is unavailable' in ps_delta and 'return $false' in ps_delta
    assert sh_delta.index('[ "$status" = 200 ]') < sh_delta.index('command -v python3')
    assert 'Python 3 is unavailable' in sh_delta and 'return 1' in sh_delta

def test_delta_updates_only_changed_file_and_index(web):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    dump = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    module = dump / MODULE
    content = module.read_text(encoding='utf-8-sig') + '\nПроцедура DeltaОбновлена() Экспорт\nКонецПроцедуры\n'
    module.write_text(content, encoding='utf-8-sig')
    target = _manifest(dump)
    manifest_path = _ / 'server_manifest.json'
    delta_path = _ / 'delta.zip'
    manifest_path.write_text(json.dumps(base, ensure_ascii=False), encoding='utf-8')
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / 'scripts' /
                                                  'build_source_delta.py'), str(dump),
                             str(manifest_path), str(delta_path)], check=True,
                            capture_output=True, text=True)
    summary = json.loads(result.stdout)
    assert summary['changed_files'] == 1
    payload = delta_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert [n for n in z.namelist() if n != DELTA_META] == [MODULE]
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200 and response.json()['status'] == 'ready', response.text
    assert 'DeltaОбновлена' in _procs(pm)
    assert client.get('/api/projects/p1/sources/main/manifest').json()['version'] == target['version']


def test_delta_case_only_rename_is_published_with_target_spelling(web, tmp_path):
    pm, client, _ = web
    root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    client_dump = tmp_path / 'client'
    shutil.copytree(root, client_dump)
    old = client_dump / 'Configuration.xml'
    new = client_dump / 'configuration.xml'
    intermediate = client_dump / '.configuration-case-rename'
    old.rename(intermediate)
    intermediate.rename(new)
    target = _manifest(client_dump)
    payload = _delta(base, target, client_dump)
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200 and response.json()['status'] == 'ready', response.text
    names = {path.name for path in root.iterdir()}
    assert 'configuration.xml' in names and 'Configuration.xml' not in names
    cached = client.get('/api/projects/p1/sources/main/manifest').json()
    assert 'configuration.xml' in cached['files'] and 'Configuration.xml' not in cached['files']


def test_web_archive_replacement_invalidates_cached_manifest(web):
    pm, client, tmp = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    dump = tmp / 'replacement'
    shutil.copytree(FIXTURES / 'xml_ru', dump)
    (dump / MODULE).unlink()
    added = dump / 'CommonModules' / 'ЦеныСервер' / 'Ext' / 'AddedByWebUpload.bsl'
    added.write_text('Процедура ДобавленаВебом() Экспорт\nКонецПроцедуры\n', encoding='utf-8')
    response = client.post('/api/projects/p1/sources/main/update',
                           data={'auto_reindex': 'on'},
                           files={'xml_archive': ('replacement.zip', _zip(dump), 'application/zip')},
                           follow_redirects=False)
    assert response.status_code == 303
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    assert manifest['version'] != base['version']
    assert MODULE not in manifest['files']
    assert 'CommonModules/ЦеныСервер/Ext/AddedByWebUpload.bsl' in manifest['files']


def test_source_crud_clears_cached_manifests(web):
    pm, _, _ = web
    source_dir = pm.projects_dir / 'p1' / 'sources' / 'ext'
    root = source_dir / 'xml'
    shutil.copytree(FIXTURES / 'xml_ru', root)
    sidecar = root / MANIFEST_NAME
    write_manifest(sidecar, _manifest(root))
    pm.add_source('p1', 'ext', 'Расширение', xml_archive=io.BytesIO(_zip(FIXTURES / 'xml_en')),
                  xml_archive_filename='extension.zip')
    assert read_or_create_manifest(root, sidecar) == _manifest(root)
    source = pm.projects_dir / 'p1' / 'sources' / 'ext' / 'xml'
    cached = source / MANIFEST_NAME
    write_manifest(cached, _manifest(source))
    pm.remove_source('p1', 'ext')
    assert not source.parent.exists()


def test_empty_source_can_receive_its_first_delta(web):
    pm, client, tmp = web
    root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    shutil.rmtree(root)
    source = next(item for item in pm.get_project('p1').sources if item.id == 'main')
    source.xml_path = ''
    pm._save_registry()
    base_response = client.get('/api/projects/p1/sources/main/manifest')
    base = base_response.json()
    assert base_response.status_code == 200 and base['files'] == {}
    client_dump = tmp / 'first-dump'
    shutil.copytree(FIXTURES / 'xml_ru', client_dump)
    target = _manifest(client_dump)
    payload = _delta(base, target, client_dump)
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200 and response.json()['status'] == 'ready', response.text
    source = next(item for item in pm.get_project('p1').sources if item.id == 'main')
    assert source.xml_path == 'sources/main/xml'
    assert pm.get_project('p1').index_stats.total_modules > 0
    assert client.get('/api/projects/p1/sources/main/manifest').json()['version'] == target['version']

@pytest.mark.parametrize('direction', ['file-to-directory', 'directory-to-file'])
def test_delta_handles_file_directory_conflicts(tmp_path, direction):
    root = tmp_path / 'sources' / 'main' / 'xml'
    root.mkdir(parents=True)
    if direction == 'file-to-directory':
        (root / 'example').write_text('old', encoding='utf-8')
    else:
        (root / 'example').mkdir()
        (root / 'example' / 'child').write_text('old', encoding='utf-8')
    sidecar = root / MANIFEST_NAME
    base = build_manifest(root)
    write_manifest(sidecar, base)
    client = tmp_path / 'client'
    shutil.copytree(root, client)
    (client / MANIFEST_NAME).unlink()
    if direction == 'file-to-directory':
        (client / 'example').unlink()
        (client / 'example').mkdir()
        (client / 'example' / 'child').write_text('new', encoding='utf-8')
    else:
        (client / 'example' / 'child').unlink()
        (client / 'example').rmdir()
        (client / 'example').write_text('new', encoding='utf-8')
    target = _manifest(client)
    payload = _delta(base, target, client)
    result = apply_delta(root, sidecar, io.BytesIO(payload), 'delta.zip', base['version'])
    assert result['changed'] is True
    assert build_manifest(root) == target


def test_failed_manifest_write_does_not_publish_delta(tmp_path, monkeypatch):
    from src.core import source_manifest
    root = tmp_path / 'sources' / 'main' / 'xml'
    root.mkdir(parents=True)
    (root / 'file.txt').write_text('old', encoding='utf-8')
    sidecar = root / MANIFEST_NAME
    base = build_manifest(root)
    write_manifest(sidecar, base)
    client = tmp_path / 'client'
    shutil.copytree(root, client)
    (client / MANIFEST_NAME).unlink()
    (client / 'file.txt').write_text('new', encoding='utf-8')
    target = _manifest(client)
    payload = _delta(base, target, client)
    original = source_manifest.replace_with_retry
    failed_once = False
    def fail_manifest(src, dest):
        nonlocal failed_once
        if Path(dest) == root / MANIFEST_NAME and not failed_once:
            failed_once = True
            raise OSError('simulated disk error')
        return original(src, dest)
    monkeypatch.setattr(source_manifest, 'replace_with_retry', fail_manifest)
    with pytest.raises(OSError, match='simulated disk error'):
        apply_delta(root, sidecar, io.BytesIO(payload), 'delta.zip', base['version'])
    assert (root / 'file.txt').read_text(encoding='utf-8') == 'old'
    assert read_or_create_manifest(root, sidecar) == base

def test_manifest_read_recovers_backup_after_interrupted_publish(tmp_path):
    from src.core.source_manifest import recover_source
    root = tmp_path / 'sources' / 'main' / 'xml'
    root.mkdir(parents=True)
    (root / 'old.txt').write_text('new', encoding='utf-8')
    journal = root.parent / f'.{root.name}-delta-journal'
    backup = journal / 'backup'
    backup.mkdir(parents=True)
    (backup / '00000000.bak').write_text('old', encoding='utf-8')
    (journal / 'journal.json').write_text(json.dumps({'state': 'applying', 'operations': [
        {'path': 'old.txt', 'backup': '00000000.bak', 'existed': True}]}), encoding='utf-8')
    recover_source(root)
    assert (root / 'old.txt').read_text(encoding='utf-8') == 'old'
    assert not journal.exists()

def test_project_manager_recovers_interrupted_delta_at_startup(tmp_path):
    data = tmp_path / 'data'
    root = data / 'projects' / 'p1' / 'sources' / 'main' / 'xml'
    root.mkdir(parents=True)
    (root / 'old.txt').write_text('new', encoding='utf-8')
    journal = root.parent / f'.{root.name}-delta-journal'
    backup = journal / 'backup'
    backup.mkdir(parents=True)
    (backup / '00000000.bak').write_text('old', encoding='utf-8')
    (journal / 'journal.json').write_text(json.dumps({'state': 'applying', 'operations': [
        {'path': 'old.txt', 'backup': '00000000.bak', 'existed': True}]}), encoding='utf-8')
    pm = ProjectManager(data)
    try:
        assert (root / 'old.txt').read_text(encoding='utf-8') == 'old'
        assert not journal.exists()
    finally:
        pm.close_all()

def test_delta_limits_stream_size_archive_entries_and_path_length(tmp_path, monkeypatch):
    from src.core import source_manifest
    root = tmp_path / 'xml'
    root.mkdir()
    (root / 'old.txt').write_text('old', encoding='utf-8')
    sidecar = root / MANIFEST_NAME
    base = build_manifest(root)
    write_manifest(sidecar, base)
    with monkeypatch.context() as patch:
        patch.setattr(source_manifest, 'MAX_UPLOAD_BYTES', 3)
        with pytest.raises(UnsafeArchiveError):
            apply_delta(root, sidecar, io.BytesIO(b'four'), 'delta.zip', base['version'])
    assert (root / 'old.txt').read_text(encoding='utf-8') == 'old'

    target_files = {'new.txt': {'sha256': hashlib.sha256(b'new').hexdigest(), 'size': 3}}
    meta = {'base_version': base['version'], 'target_version': manifest_version(target_files),
            'files': target_files, 'deleted': ['old.txt']}
    monkeypatch.setenv('ARIADNA_ARCHIVE_MAX_FILES', '1')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr(DELTA_META, json.dumps(meta))
        archive.writestr('folder/', '')
        archive.writestr('new.txt', b'new')
    with pytest.raises(UnsafeArchiveError):
        apply_delta(root, sidecar, io.BytesIO(buf.getvalue()), 'delta.zip', base['version'])

    buf = io.BytesIO()
    too_long = 'a' * (source_manifest.MAX_PATH_BYTES + 1)
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr(DELTA_META, json.dumps(meta))
        archive.writestr(too_long, b'new')
    with pytest.raises(UnsafeArchiveError):
        apply_delta(root, sidecar, io.BytesIO(buf.getvalue()), 'delta.zip', base['version'])
    assert (root / 'old.txt').read_text(encoding='utf-8') == 'old'


def test_manifest_rejects_reparse_root_and_delta_reuses_unchanged_files(tmp_path, monkeypatch):
    from src.core import source_manifest
    root = tmp_path / 'xml'
    root.mkdir()
    (root / 'unchanged.txt').write_text('same', encoding='utf-8')
    (root / 'change.txt').write_text('old', encoding='utf-8')
    (root / 'Template.keep').write_text('keep', encoding='utf-8')
    sidecar = root / MANIFEST_NAME
    base = build_manifest(root)
    write_manifest(sidecar, base)
    client = tmp_path / 'client'
    shutil.copytree(root, client)
    (client / MANIFEST_NAME).unlink()
    (client / 'change.txt').write_text('new', encoding='utf-8')
    target = _manifest(client)
    payload = _delta(base, target, client)

    original_link = os.link
    linked = []
    monkeypatch.setattr(source_manifest.os, 'link', lambda src, dst: (linked.append((src, dst)), original_link(src, dst))[1])
    monkeypatch.setattr(source_manifest.shutil, 'copytree',
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError('full copy attempted')))
    result = apply_delta(root, sidecar, io.BytesIO(payload), 'delta.zip', base['version'])
    assert result['changed'] and not linked
    assert (root / 'unchanged.txt').read_text(encoding='utf-8') == 'same'
    assert (root / 'Template.keep').read_text(encoding='utf-8') == 'keep'

    reparse_root = tmp_path / 'junction'
    reparse_root.mkdir()
    monkeypatch.setattr(source_manifest, '_is_reparse', lambda path: Path(path) == reparse_root)
    with pytest.raises(UnsafeArchiveError, match='Корень'):
        build_manifest(reparse_root)


def test_identical_delta_returns_ready_without_reindex(web, monkeypatch):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    payload = _delta(base, base, root)
    monkeypatch.setattr(pm, 'reindex', lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError('reindex started')))
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200 and response.json()['status'] == 'ready'


def test_concurrent_deltas_for_different_sources_both_reindex(web, monkeypatch, tmp_path):
    pm, client, _ = web
    root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    pm.add_source('p1', 'ext', 'Расширение', source_type='extension',
                  xml_archive=io.BytesIO(_zip(root)), xml_archive_filename='ext.zip')
    pm.reindex('p1')
    manifests = {sid: client.get(f'/api/projects/p1/sources/{sid}/manifest').json()
                 for sid in ('main', 'ext')}
    archives = {}
    for sid in ('main', 'ext'):
        local = tmp_path / sid
        shutil.copytree(FIXTURES / 'xml_ru', local)
        module = local / MODULE
        marker = 'DeltaMain' if sid == 'main' else 'DeltaExtension'
        module.write_text(module.read_text(encoding='utf-8-sig')
                          + f'\nПроцедура {marker}() Экспорт\nКонецПроцедуры\n', encoding='utf-8-sig')
        archives[sid] = _delta(manifests[sid], _manifest(local), local)

    original = pm.reindex

    def slow_reindex(project_id, source_id=None):
        time.sleep(0.1)
        return original(project_id, source_id=source_id)

    monkeypatch.setattr(pm, 'reindex', slow_reindex)
    start = threading.Barrier(3)

    def send(sid):
        start.wait()
        return client.post(f'/api/projects/p1/sources/{sid}/delta',
                           data={'base_version': manifests[sid]['version']},
                           files={'delta_archive': ('delta.zip', archives[sid], 'application/zip')})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send, sid) for sid in ('main', 'ext')]
        start.wait()
        responses = [future.result(timeout=30) for future in futures]
    assert all(r.status_code == 200 and r.json()['status'] == 'ready' for r in responses)
    assert {'DeltaMain', 'DeltaExtension'} <= _procs(pm)



def test_delta_and_full_refresh_of_other_source_both_index(web, monkeypatch, tmp_path):
    pm, client, _ = web
    main_root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    pm.add_source('p1', 'ext', 'Extension', source_type='extension',
                  xml_archive=io.BytesIO(_zip(main_root)), xml_archive_filename='ext.zip')
    pm.reindex('p1')
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    local = tmp_path / 'main-delta'
    shutil.copytree(main_root, local)
    module = local / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nProcedure DeltaWithRefresh() Export\nEndProcedure\n', encoding='utf-8-sig')
    payload = _delta(base, _manifest(local), local)

    ext_root = pm.projects_dir / 'p1' / 'sources' / 'ext' / 'xml'
    ext_module = ext_root / MODULE
    ext_module.write_text(ext_module.read_text(encoding='utf-8-sig')
                          + '\nProcedure FullRefreshMarker() Export\nEndProcedure\n', encoding='utf-8-sig')
    from src.web import routes_update
    original_apply = routes_update.apply_delta
    entered, release = threading.Event(), threading.Event()
    def paused_apply(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_apply(*args, **kwargs)
    monkeypatch.setattr(routes_update, 'apply_delta', paused_apply)

    with ThreadPoolExecutor(max_workers=2) as pool:
        delta_future = pool.submit(client.post, '/api/projects/p1/sources/main/delta',
                                   data={'base_version': base['version'], 'timeout': '10'},
                                   files={'delta_archive': ('delta.zip', payload, 'application/zip')})
        assert entered.wait(10)
        refresh_future = pool.submit(client.post, '/api/projects/p1/sources/ext/refresh',
                                     data={'timeout': '10'})
        time.sleep(0.1)
        release.set()
        delta_response = delta_future.result(timeout=30)
        refresh_response = refresh_future.result(timeout=30)
    assert delta_response.status_code == 200 and delta_response.json()['status'] == 'ready'
    assert refresh_response.status_code == 200 and refresh_response.json()['status'] == 'ready'
    assert {'DeltaWithRefresh', 'FullRefreshMarker'} <= _procs(pm)
    assert client.get('/api/projects/p1/sources/main/manifest').json()['indexed']
    assert client.get('/api/projects/p1/sources/ext/manifest').json()['indexed']

def test_delta_removes_files_and_adds_a_new_file(web):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    dump = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    removed = MODULE
    (dump / removed).unlink()
    added = 'CommonModules/ЦеныСервер/Ext/New.bsl'
    (dump / added).write_text('Процедура Новая() Экспорт\nКонецПроцедуры\n', encoding='utf-8')
    target = _manifest(dump)
    payload = _delta(base, target, dump)
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200, response.text
    assert not (dump / removed).exists() and (dump / added).exists()


def test_delta_adds_a_new_object(web):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    dump = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    before_objects = pm.get_project('p1').index_stats.total_objects
    old_name, new_name = 'Склады', 'СинтетическийСклад'
    source_meta = dump / 'Catalogs' / f'{old_name}.xml'
    new_meta = dump / 'Catalogs' / f'{new_name}.xml'
    text = source_meta.read_text(encoding='utf-8-sig').replace(old_name, new_name)
    new_meta.write_text(text.replace('c7b84f52-f9e3-5e88-ae7d-473963bfbbd9', str(uuid.uuid4())),
                        encoding='utf-8-sig')
    shutil.copytree(dump / 'Catalogs' / old_name, dump / 'Catalogs' / new_name)
    config = dump / 'Configuration.xml'
    config.write_text(config.read_text(encoding='utf-8-sig').replace(
        f'<Catalog>{old_name}</Catalog>', f'<Catalog>{old_name}</Catalog>\n\t\t\t<Catalog>{new_name}</Catalog>'),
        encoding='utf-8-sig')
    target = _manifest(dump)
    payload = _delta(base, target, dump)
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200, response.text
    assert pm.get_project('p1').index_stats.total_objects == before_objects + 1


def test_delta_rejects_stale_manifest_and_traversal_without_changes(web):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    dump = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    before = (dump / MODULE).read_bytes()
    meta = {'base_version': 'wrong', 'target_version': base['version'],
            'files': base['files'], 'deleted': []}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(DELTA_META, json.dumps(meta))
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', buf.getvalue(), 'application/zip')})
    assert response.status_code == 409
    assert (dump / MODULE).read_bytes() == before
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(DELTA_META, json.dumps({**meta, 'base_version': base['version']}))
        z.writestr('../outside.txt', 'bad')
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', buf.getvalue(), 'application/zip')})
    assert response.status_code == 400
    assert (dump / MODULE).read_bytes() == before
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(DELTA_META, json.dumps({**meta, 'base_version': base['version']}))
        link = zipfile.ZipInfo('link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, '../outside.txt')
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', buf.getvalue(), 'application/zip')})
    assert response.status_code == 400
    assert (dump / MODULE).read_bytes() == before


def test_interrupted_delta_upload_keeps_source_intact(web):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    dump = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    before = (dump / MODULE).read_bytes()
    response = client.post('/api/projects/p1/sources/main/delta', data={'base_version': base['version']},
                           files={'delta_archive': ('delta.zip', b'PK\x03\x04broken', 'application/zip')})
    assert response.status_code == 400
    assert (dump / MODULE).read_bytes() == before



def test_failed_reindex_is_retried_from_manifest(web, monkeypatch):
    pm, client, tmp = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    local = tmp / 'retry-dump'
    shutil.copytree(root, local)
    module = local / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nProcedure RetryMarker() Export\nEndProcedure\n', encoding='utf-8-sig')
    payload = _delta(base, _manifest(local), local)
    failed = threading.Event()
    calls = 0
    original_mark = pm._mark_indexing
    def fail_first_mark(project_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            raise RuntimeError('injected mark-indexing failure')
        return original_mark(project_id)
    monkeypatch.setattr(pm, '_mark_indexing', fail_first_mark)
    response = client.post('/api/projects/p1/sources/main/delta',
                           data={'base_version': base['version'], 'wait': '0'},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 202 and response.json()['status'] == 'queued'
    assert failed.wait(10)
    assert client.get('/api/projects/p1/sources/main/manifest').json()['indexed'] is False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        manifest = client.get('/api/projects/p1/sources/main/manifest').json()
        if manifest['indexed']:
            break
        time.sleep(0.05)
    assert manifest['indexed'] and calls >= 2
    assert 'RetryMarker' in _procs(pm)


def test_stale_schema_rebuild_queues_while_project_lock_is_busy(web):
    pm, _, _ = web
    lock = pm._reindex_lock('p1')
    lock.acquire()
    try:
        pm._handle_stale_schema('p1', pm.get_db('p1'))
        with pm._schema_reindex_lock:
            assert ('p1', None) in pm._schema_reindex_pending
    finally:
        lock.release()
    pm._schema_reindex_queue.join()
    assert pm.get_project('p1').status == 'ready'


def test_web_archive_update_and_delta_are_serialized(web, monkeypatch, tmp_path):
    pm, client, _ = web
    base = client.get('/api/projects/p1/sources/main/manifest').json()
    source_root = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    local = tmp_path / 'delta-client'
    shutil.copytree(source_root, local)
    module = local / MODULE
    module.write_text(module.read_text(encoding='utf-8-sig')
                      + '\nProcedure DeltaBeforeUi() Export\nEndProcedure\n', encoding='utf-8-sig')
    payload = _delta(base, _manifest(local), local)
    from src.web import routes_update
    original_apply = routes_update.apply_delta
    entered, release = threading.Event(), threading.Event()
    def paused_apply(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_apply(*args, **kwargs)
    monkeypatch.setattr(routes_update, 'apply_delta', paused_apply)
    dump = tmp_path / 'ui-replacement'
    shutil.copytree(FIXTURES / 'xml_ru', dump)
    ui_module = dump / MODULE
    ui_module.write_text(ui_module.read_text(encoding='utf-8-sig')
                         + '\nProcedure UiWins() Export\nEndProcedure\n', encoding='utf-8-sig')
    with ThreadPoolExecutor(max_workers=2) as pool:
        delta_future = pool.submit(client.post, '/api/projects/p1/sources/main/delta',
                                   data={'base_version': base['version'], 'timeout': '2'},
                                   files={'delta_archive': ('delta.zip', payload, 'application/zip')})
        assert entered.wait(10)
        ui_future = pool.submit(client.post, '/api/projects/p1/sources/main/update',
                                data={'auto_reindex': 'on'},
                                files={'xml_archive': ('ui.zip', _zip(dump), 'application/zip')},
                                follow_redirects=False)
        time.sleep(0.1)
        release.set()
        delta_response = delta_future.result(timeout=30)
        ui_response = ui_future.result(timeout=30)
    assert delta_response.status_code == 409
    assert ui_response.status_code == 303
    deadline = time.monotonic() + 10
    manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    while not manifest['indexed'] and time.monotonic() < deadline:
        time.sleep(0.05)
        manifest = client.get('/api/projects/p1/sources/main/manifest').json()
    assert manifest['indexed'] and 'UiWins' in _procs(pm)
    assert 'DeltaBeforeUi' not in _procs(pm)

def test_delta_requires_user_token_when_tokens_enabled(web, monkeypatch):
    pm, client, _ = web
    install_team_license(monkeypatch, pm.data_dir)
    store = TokenStore(pm.data_dir)
    store.create('Админ', 'admin')
    _, robot = store.create('Робот выгрузки', 'user')
    base = client.get('/api/projects/p1/sources/main/manifest')
    assert base.status_code == 401
    assert client.post('/api/projects/p1/sources/main/delta', data={'base_version': 'x'},
                       files={'delta_archive': ('delta.zip', b'bad', 'application/zip')}).status_code == 401
    store_manifest = _manifest(pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml')
    payload = _delta(store_manifest, store_manifest, pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml')
    response = client.post('/api/projects/p1/sources/main/delta', headers={'Authorization': f'Bearer {robot}'},
                           data={'base_version': store_manifest['version']},
                           files={'delta_archive': ('delta.zip', payload, 'application/zip')})
    assert response.status_code == 200, response.text
