"""Обновление источника для робота выгрузки (src/web/routes_update.py):
одним запросом заменить выгрузку (по желанию), переиндексировать и дождаться
результата. Роль user достаточно."""

import io
import hashlib
import json
import subprocess
import sys
import uuid
import shutil
import stat
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
DELTA_META = '.ariadna-delta.json'


def _zip(root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in root.rglob('*'):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    return buf.getvalue()


def _manifest(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.name.startswith('Template.'):
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


def test_refresh_no_wait_and_unknown_source(web):
    _, client, _ = web
    resp = client.post('/api/projects/p1/sources/main/refresh', data={'wait': '0'})
    assert resp.status_code == 202 and resp.json()['status'] == 'started'
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
    assert after_first > 0 and calls == after_first


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
