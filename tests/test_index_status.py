"""
get_index_status во время переиндексации.

Indexer.index_report в конце писал в index_state status='ready', а
ProjectManager.reindex вызывает его для каждого источника первым — до
index_bsl и resolve_calls. Всю фазу разбора BSL (на ERP — минуты)
get_index_status отвечал «Status: ready» с Procedures: 0 и без строки
прогресса, и агент, дождавшись 'ready', искал по неполному индексу.
Теперь статусом владеет только ProjectManager.reindex.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.indexer import Indexer
from src.core.project_manager import ProjectManager
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
    return pm


def _status(pm) -> str:
    return execute_tool(pm, 'get_index_status', {'project_id': 'p1'})


def _spy_index_bsl(monkeypatch, pm, seen: list, fail: bool = False):
    real = Indexer.index_bsl

    def spy(self, *a, **kw):
        # Метаданные (index_report) уже записаны, BSL ещё не разобран.
        seen.append((pm.get_db('p1').get_stats()['status'], _status(pm)))
        if fail:
            raise RuntimeError('разбор BSL упал')
        return real(self, *a, **kw)

    monkeypatch.setattr(Indexer, 'index_bsl', spy)


def test_status_is_indexing_during_bsl_and_ready_after(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            seen = []
            _spy_index_bsl(monkeypatch, pm, seen)
            pm.reindex('p1')

            assert len(seen) == 1
            state, text = seen[0]
            assert state == 'indexing', text
            assert text.startswith('Status: indexing'), text
            assert 'Progress: ' in text, text

            assert pm.get_db('p1').get_stats()['status'] == 'ready'
            text = _status(pm)
            assert text.startswith('Status: ready') and 'Progress' not in text, text
            assert 'Procedures: 0' not in text, text
        finally:
            pm.close_all()


def test_status_is_error_when_bsl_fails(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            seen = []
            _spy_index_bsl(monkeypatch, pm, seen, fail=True)
            with pytest.raises(RuntimeError):
                pm.reindex('p1')
            assert seen[0][0] == 'indexing'
            assert pm.get_db('p1').get_stats()['status'] == 'error'
            assert _status(pm).startswith('Status: error')
            assert pm.get_project('p1').status == 'error'
        finally:
            pm.close_all()


def test_indexer_alone_does_not_touch_status():
    """Без ProjectManager статус — не ответственность Indexer."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from src.core.db import Database
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        db.update_state(status='indexing')
        try:
            ix = Indexer(db)
            ix.index_report(str(FIXTURES / 'report_ru.txt'), source_id='main')
            ix.index_bsl(str(FIXTURES / 'xml_ru'), source_id='main')
            state = db.get_stats()
            assert state['status'] == 'indexing'
            assert state.get('index_duration_sec') is not None
        finally:
            db.close()
