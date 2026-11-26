"""Обновление источника для робота выгрузки (src/web/routes_update.py):
одним запросом заменить выгрузку (по желанию), переиндексировать и дождаться
результата. Роль user достаточно."""

import io
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore
from src.core.project_manager import ProjectManager
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
