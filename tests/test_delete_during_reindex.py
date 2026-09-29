"""
Удаление источника или проекта во время переиндексации.

remove_source и delete_project чистили строки тем же writer'ом, которым
пишет индексатор, не беря блокировку переиндексации — удаление посреди
прогона могло оставить в индексе строки уже удалённого источника. Теперь
они берут блокировку проекта без ожидания и при идущем прогоне отказывают:
MCP — «Error: идёт переиндексация…», Web UI — 409.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.project_manager import ProjectManager, ReindexInProgressError
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


def _pm(tmpdir: str) -> ProjectManager:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    pm = ProjectManager(tmpdir)
    pm.create_project('p1', 'p1')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def _modules(pm) -> int:
    return pm.get_db('p1').conn.execute(
        "SELECT COUNT(*) FROM modules WHERE source_id='main'").fetchone()[0]


def test_remove_source_and_delete_project_refuse_while_reindexing():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            before = _modules(pm)
            assert before > 0
            lock = pm._reindex_lock('p1')
            lock.acquire()                       # «идёт переиндексация»
            try:
                with pytest.raises(ReindexInProgressError):
                    pm.remove_source('p1', 'main')
                with pytest.raises(ReindexInProgressError):
                    pm.delete_project('p1')

                text = execute_tool(pm, 'remove_source', {'project_id': 'p1', 'source_id': 'main',
                                                          'confirm': True})
                assert text.startswith("Error: идёт переиндексация проекта 'p1'"), text
                assert 'get_index_status' in text, text

                # Ничего не удалено.
                assert [s.id for s in pm.get_project('p1').sources] == ['main']
                assert (pm.projects_dir / 'p1' / 'sources' / 'main').is_dir()
                assert _modules(pm) == before
            finally:
                lock.release()

            # Прогон закончился — удаление работает.
            pm.remove_source('p1', 'main')
            assert pm.get_project('p1').sources == []
            assert _modules(pm) == 0
            pm.delete_project('p1')
            assert 'p1' not in [p.id for p in pm.list_projects()]
            assert not lock.locked()
        finally:
            pm.close_all()


def test_web_ui_answers_409_while_reindexing(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        monkeypatch.setattr(app_module, '_pm', pm)
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        lock = pm._reindex_lock('p1')
        try:
            lock.acquire()
            try:
                for method, url in (('post', '/api/projects/p1/sources/main/delete'),
                                    ('delete', '/api/projects/p1'),
                                    ('post', '/api/projects/p1/delete')):
                    r = client.request(method, url, follow_redirects=False)
                    assert r.status_code == 409, (url, r.status_code, r.text)
                    assert 'переиндексация' in r.text, r.text
                assert [s.id for s in pm.get_project('p1').sources] == ['main']
            finally:
                lock.release()
            r = client.post('/api/projects/p1/sources/main/delete', follow_redirects=False)
            assert r.status_code == 303
            assert client.delete('/api/projects/p1').status_code == 200
        finally:
            pm.close_all()
