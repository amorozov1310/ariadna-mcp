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


# ============================================
# reindex_async: 'indexing' записан до ответа «started»
# ============================================

import threading


def _hold_reindex(monkeypatch, pm) -> tuple[threading.Event, threading.Event]:
    """reindex() в потоке ждёт release, затем выполняется по-настоящему;
    done — поток закончил."""
    release, done = threading.Event(), threading.Event()
    real = ProjectManager.reindex

    def held(self, project_id, source_id=None):
        try:
            assert release.wait(10)
            return real(self, project_id, source_id=source_id)
        finally:
            done.set()

    monkeypatch.setattr(ProjectManager, 'reindex', held)
    return release, done


def test_reindex_async_marks_indexing_before_returning(monkeypatch):
    """Повторная переиндексация готового проекта: раньше первые ~0,1 с после
    ответа get_index_status отдавал прежний 'ready' — статус записывал
    поток уже после ответа."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            pm.reindex('p1')
            assert pm.get_db('p1').get_stats()['status'] == 'ready'

            release, done = _hold_reindex(monkeypatch, pm)
            assert pm.reindex_async('p1') == {'status': 'started', 'project_id': 'p1'}
            # Поток ещё ничего не сделал — всё записано синхронно.
            assert pm.get_db('p1').get_stats()['status'] == 'indexing'
            assert pm.get_project('p1').status == 'indexing'
            assert ProjectManager(tmpdir).get_project('p1').status == 'indexing'   # на диске
            assert _status(pm).startswith('Status: indexing'), _status(pm)
            assert pm.reindex_async('p1')['status'] == 'already_running'

            release.set()
            assert done.wait(30)
            assert pm.get_db('p1').get_stats()['status'] == 'ready'
            assert pm.get_project('p1').status == 'ready'
            assert _status(pm).startswith('Status: ready')
            assert not pm._reindex_lock('p1').locked()
        finally:
            pm.close_all()


def test_reindex_async_error_releases_lock_and_starts_nothing(monkeypatch):
    """Проект удалили между вызовами (другой экземпляр — Web UI): ошибка,
    блокировка отпущена, поток не запущен."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            pm.get_project('p1')                          # закэширован в pm
            ProjectManager(tmpdir).delete_project('p1')   # удалён другим экземпляром
            started = []
            monkeypatch.setattr(ProjectManager, 'reindex',
                                lambda self, *a, **kw: started.append(a))
            result = pm.reindex_async('p1')
            assert result['status'] == 'error' and 'p1' in result['error'], result
            assert not pm._reindex_lock('p1').locked()
            assert started == []
            assert execute_tool(pm, 'reindex', {'project_id': 'p1'}).startswith('Error')
        finally:
            pm.close_all()


def test_web_ui_reindex_marks_indexing_and_reports_error(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        monkeypatch.setattr(app_module, '_pm', pm)
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878',
                                                     'Accept': 'application/json'})
        try:
            release, done = _hold_reindex(monkeypatch, pm)
            r = client.post('/api/projects/p1/reindex')
            assert r.status_code == 200 and r.json()['status'] == 'started', r.text
            assert client.get('/api/projects/p1/status').json()['status'] == 'indexing'
            release.set()
            assert done.wait(30)
            assert client.get('/api/projects/p1/status').json()['status'] == 'ready'

            # Запись статуса не удалась — ответ с ошибкой, а не «started».
            def broken(self, project_id):
                raise OSError('диск недоступен')
            monkeypatch.setattr(ProjectManager, '_mark_indexing', broken)
            r = client.post('/api/projects/p1/reindex')
            assert r.status_code == 500 and 'диск недоступен' in r.json()['detail'], r.text
            assert not pm._reindex_lock('p1').locked()
        finally:
            pm.close_all()
