"""
Эксплуатация (docs/operations.md): /ready — готовность проектов, отдельно от
/health; /api/projects/{id}/manifest — из чего и чем построен индекс.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import __version__
from src.core import manifest
from src.core.cards import CARD_FORMAT
from src.core.parser_fingerprint import parser_fingerprint
from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        with open(FIXTURES / 'report_ru.txt', 'rb') as f:
            manager.add_source('p1', 'main', 'Main', report_file=f,
                               xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        # Метаданные из XML-выгрузки (Configuration.xml), без кода.
        manager.add_source('p1', 'meta', 'Meta', source_type='extension',
                           xml_archive=_zip(FIXTURES / 'xml_meta_ru'), xml_archive_filename='x.zip')
        manager.add_source('p1', 'ext', 'Ext', source_type='extension',
                           xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')
        manager.reindex('p1')
        manager.create_project('empty', 'Пустой')
        try:
            yield manager
        finally:
            manager.close_all()


@pytest.fixture
def client(pm):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        yield TestClient(app_module.app, headers=HOST)
    finally:
        app_module._pm = previous


def test_readiness_per_project(pm):
    data = manifest.readiness(pm)
    by_id = {p['id']: p for p in data['projects']}
    assert by_id['p1'] == {'id': 'p1', 'status': 'ready', 'ready': True, 'reason': ''}
    assert by_id['empty']['ready'] is False
    assert by_id['empty']['reason'] == 'нет источников'
    assert data['ready'] is False


def test_readiness_reports_progress_while_indexing(pm):
    pm.update_project('p1', status='indexing')
    pm.get_db('p1').update_state(progress_phase='bsl', progress_current=3, progress_total=10)
    item = manifest.project_readiness(pm, 'p1')
    assert item['ready'] is False and item['reason'] == 'идёт переиндексация'
    assert item['progress'] == {'progress_phase': 'bsl', 'progress_current': 3,
                                'progress_total': 10}


def test_empty_registry_is_ready():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert manifest.readiness(ProjectManager(tmpdir)) == {'ready': True, 'projects': []}


def test_manifest_describes_sources(pm):
    data = manifest.project_manifest(pm, 'p1')
    assert data['schema_version'] == manifest.MANIFEST_SCHEMA_VERSION
    assert data['ariadna_version'] == __version__
    assert data['ready'] is True and data['index']['state'] == 'ready'
    assert data['formats'] == {'parser_fingerprint': parser_fingerprint(),
                               'card_format': CARD_FORMAT}
    assert data['index']['procedures'] > 0
    src = {s['id']: s for s in data['sources']}
    assert set(src) == {'main', 'meta', 'ext'}
    main = src['main']
    assert main['metadata_from'] == 'report' and main['config_name']
    assert main['indexed_at'] and main['modules'] > 0
    assert main['parser_current'] is True
    assert src['meta']['metadata_from'] == 'xml' and src['meta']['config_name']
    assert src['ext']['source_type'] == 'extension'
    for s in (src['main'], src['ext']):
        cards = s['cards']
        assert cards['modules_with_code'] > 0
        assert cards['modules_current'] == cards['modules_with_code']
    assert data['issues'] == []


def test_manifest_flags_stale_parser_and_cards(pm):
    db = pm.get_db('p1')
    db.set_parser_fingerprint('main', 'old')
    db.conn.execute('UPDATE card_modules SET card_format = 0 WHERE module_id IN '
                    "(SELECT id FROM modules WHERE source_id = 'ext')")
    db.conn.commit()
    data = manifest.project_manifest(pm, 'p1')
    src = {s['id']: s for s in data['sources']}
    assert src['main']['parser_current'] is False
    assert src['ext']['cards']['modules_current'] == 0
    assert any('main' in i and 'парсер' in i for i in data['issues'])
    assert any('ext' in i and 'карточки' in i for i in data['issues'])


def test_manifest_unindexed_source(pm):
    data = manifest.project_manifest(pm, 'empty')
    assert data['sources'] == [] and data['ready'] is False


def test_health_stays_liveness(client):
    assert client.get('/health').json() == {'status': 'ok'}


def test_ready_endpoint(client, pm):
    r = client.get('/ready')
    assert r.status_code == 503 and r.json()['ready'] is False
    r = client.get('/ready', params={'project': 'p1'})
    assert r.status_code == 200 and r.json()['ready'] is True
    assert client.get('/ready', params={'project': 'empty'}).status_code == 503
    r = client.get('/ready', params={'project': 'nope'})
    assert r.status_code == 404 and 'nope' in r.json()['detail']
    pm.delete_project('empty')
    assert client.get('/ready').status_code == 200


def test_manifest_endpoint(client):
    r = client.get('/api/projects/p1/manifest')
    assert r.status_code == 200
    assert {s['id'] for s in r.json()['sources']} == {'main', 'meta', 'ext'}
    assert client.get('/api/projects/nope/manifest').status_code == 404
