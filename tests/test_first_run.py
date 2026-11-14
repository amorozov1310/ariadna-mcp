"""Первый запуск в Web UI: пустой список → проект → выгрузка → индекс без
отдельного нажатия; список проектов таблицей; причина упавшей индексации."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}


def _zip(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    return buf.getvalue()


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def test_empty_list_explains_steps(web):
    _, client = web
    page = client.get('/').text
    assert 'Начните с проекта' in page and 'Выгрузить конфигурацию в' in page
    assert 'href="/projects/new"' in page


def test_create_upload_and_index_without_extra_clicks(web):
    pm, client = web
    form = client.get('/projects/new').text
    assert 'Код проекта' in form and 'name="project_id"' in form
    resp = client.post('/api/projects', data={'project_id': 'demo', 'name': 'Демо'},
                       follow_redirects=False)
    assert resp.headers['location'] == '/projects/demo'
    page = client.get('/projects/demo').text
    assert 'Добавьте выгрузку конфигурации' in page and 'name="auto_reindex"' in page

    resp = client.post('/api/projects/demo/sources',
                       data={'source_id': 'main', 'label': 'Демо', 'source_type': 'main', 'auto_reindex': 'on'},
                       files={'xml_archive': ('dump.zip', _zip(FIXTURES / 'xml_ru'), 'application/zip')},
                       follow_redirects=False)
    assert resp.status_code == 303
    pm.close_all()                                  # дождаться фоновой индексации
    project = pm.get_project('demo')
    assert project.status == 'ready' and project.index_stats.total_objects > 0

    page = client.get('/').text
    assert 'projects-table' in page and '>demo<' in page and 'конфигурация' in page


def test_add_without_auto_reindex_does_not_index(web):
    pm, client = web
    pm.create_project('p1', 'p1')
    client.post('/api/projects/p1/sources', data={'source_id': 'main', 'label': 'x'},
                files={'xml_archive': ('dump.zip', _zip(FIXTURES / 'xml_ru'), 'application/zip')})
    assert pm.get_project('p1').status != 'indexing'
    assert pm.get_project('p1').index_stats.total_objects == 0


def test_index_error_reason_is_shown(web, monkeypatch):
    pm, client = web
    pm.create_project('p1', 'p1')
    pm.add_source('p1', 'main', 'x', xml_archive=io.BytesIO(_zip(FIXTURES / 'xml_ru')),
                  xml_archive_filename='x.zip')

    def broken(*args, **kwargs):
        raise RuntimeError('выгрузка повреждена: нет Configuration.xml')

    from src.core import indexer
    monkeypatch.setattr(indexer.Indexer, 'index_xml_metadata', broken)
    with pytest.raises(RuntimeError):
        pm.reindex('p1')
    status = client.get('/api/projects/p1/status').json()
    assert status['status'] == 'error' and 'нет Configuration.xml' in status['error']
    page = client.get('/projects/p1').text
    assert 'Индекс не построен' in page and 'нет Configuration.xml' in page and 'Повторить' in page


def test_broken_dump_error_is_readable():
    import xml.etree.ElementTree as ET
    from src.core.project_manager import _index_error_text
    try:
        ET.fromstring('не xml')
    except ET.ParseError as e:
        text = _index_error_text(e)
    assert text.startswith('Файл выгрузки не читается как XML') and 'Конфигуратора' in text
    assert _index_error_text(RuntimeError('диск полон')) == 'диск полон'
