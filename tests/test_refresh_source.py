"""Обновление источника для робота выгрузки (src/web/routes_update.py):
одним запросом заменить выгрузку (по желанию), переиндексировать и дождаться
результата. Роль user достаточно."""

import io
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
