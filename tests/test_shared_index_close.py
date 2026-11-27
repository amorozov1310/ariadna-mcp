"""
Индекс удалённого проекта закрывается для всех потоков процесса.

Раньше в одном процессе было три ProjectManager (Web UI, MCP SSE, MCP HTTP)
со своими пулами Database, а delete_project закрывал Database только у
себя. Вживую:
- Docker (Linux): Web UI удалил проект, создал заново без источников и
  переиндексировал, а MCP get_index_status по-прежнему отдавал
  «Procedures: 2045» и находил процедуры — его пул читал удалённый файл;
- без Docker на Windows: DELETE /api/projects/{id} → 500 WinError 32,
  файл индекса держали соединения пула MCP.

Теперь ProjectManager на процесс один (main.build_shared_state), и те же
сценарии проверяются на нём: запросы MCP идут из других потоков, у каждого
своё читающее соединение к индексу. Во второй части — защита от второго
процесса: файл индекса удалили или заменили снаружи.
"""

import io
import os
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

from src.core.project_manager import ProjectManager
from src.core.db import IndexRemovedError
from src.mcp_server.tools import execute_tool
from test_db_readers import _Worker, _closed

FIXTURES = Path(__file__).parent / 'fixtures'


def _add_source(pm: ProjectManager, project_id: str = 'p1') -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    pm.add_source(project_id, 'main', 'Main', xml_archive=buf, xml_archive_filename='x.zip')


def _procedures(pm: ProjectManager) -> list:
    return pm.get_search('p1').search_procedures('Рассчитать')


@pytest.fixture
def readers_in_threads():
    """Один ProjectManager процесса; проект проиндексирован, и три потока
    (как потоки пула MCP) читают его — у каждого своё соединение."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        workers = [_Worker() for _ in range(3)]
        try:
            pm.create_project('p1', 'p1')
            _add_source(pm)
            pm.reindex('p1')
            readers = []
            for w in workers:
                assert w.call(lambda: _procedures(pm))
                assert w.call(lambda: pm.get_db('p1').get_stats()['procedures']) > 0
                readers.append(w.call(lambda: pm.get_db('p1').read_conn()))
            assert 'Procedures: 0' not in w.call(
                lambda: execute_tool(pm, 'get_index_status', {'project_id': 'p1'}))
            yield pm, workers, readers
        finally:
            for w in workers:
                w.stop()
            pm.close_all()


def test_recreated_project_is_empty_for_other_threads(readers_in_threads):
    pm, workers, _ = readers_in_threads
    pm.delete_project('p1')
    pm.create_project('p1', 'p1')      # тот же id, без источников
    pm.reindex('p1')

    assert pm.get_db('p1').get_stats()['procedures'] == 0
    assert _procedures(pm) == []
    for w in workers:
        assert w.call(lambda: pm.get_db('p1').get_stats()['procedures']) == 0
        assert w.call(lambda: _procedures(pm)) == []
        status = w.call(lambda: execute_tool(pm, 'get_index_status', {'project_id': 'p1'}))
        assert 'Procedures: 0' in status, status


def test_delete_project_closes_connections_of_all_threads(readers_in_threads):
    """На Windows без этого rmtree каталога индекса падал с WinError 32; на
    Linux проверяем сами соединения потоков."""
    pm, workers, readers = readers_in_threads
    index_parent = pm._index_path('p1').parent
    writer = pm._db_pool['p1'].conn

    pm.delete_project('p1')
    assert not index_parent.exists()
    assert not (pm.projects_dir / 'p1').exists()
    assert 'p1' not in pm._db_pool
    assert _closed(writer) and all(_closed(c) for c in readers)


def test_async_reindex_database_is_pinned_during_pool_pressure(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        pm.create_project('p1', 'p1')
        _add_source(pm)
        pm.reindex('p1')
        entered, resume = threading.Event(), threading.Event()
        original = pm._reindex_locked

        def paused(project_id, source_id=None):
            entered.set()
            assert resume.wait(10)
            return original(project_id, source_id)

        monkeypatch.setattr(pm, '_reindex_locked', paused)
        try:
            assert pm.reindex_async('p1')['status'] == 'started'
            assert entered.wait(10)
            for i in range(8):
                project_id = f'other{i}'
                pm.create_project(project_id, project_id)
                pm.get_db(project_id)
            assert pm.get_db('p1').get_stats()['status'] == 'indexing'
            resume.set()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = pm.get_db('p1').get_stats()['status']
                if status in ('ready', 'error'):
                    break
                time.sleep(.02)
            assert status in ('ready', 'error'), 'переиндексация застряла в indexing'
        finally:
            resume.set()
            pm.close_all()


def test_ninth_parallel_status_reader_waits_and_reindex_does_not_close_lease(
        readers_in_threads, monkeypatch):
    pm, _, _ = readers_in_threads
    db = pm.get_db('p1')
    barrier = threading.Barrier(9)

    def status():
        barrier.wait(timeout=10)
        return db.get_stats()['status']

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(status) for _ in range(9)]
        assert [future.result(timeout=15) for future in futures] == ['ready'] * 9

    original = db.read_conn
    acquired, resume = threading.Event(), threading.Event()

    def delayed_read_conn(*args, **kwargs):
        conn = original(*args, **kwargs)
        acquired.set()
        assert resume.wait(10)
        return conn

    monkeypatch.setattr(db, 'read_conn', delayed_read_conn)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(db.get_stats)
            assert acquired.wait(10)
            pm.reindex('p1')
            resume.set()
            assert pending.result(timeout=10)['status'] == 'ready'
    finally:
        resume.set()


def test_evicted_search_engine_stays_retired_and_project_can_be_deleted(monkeypatch):
    import src.core.project_manager as project_manager
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        pm.create_project('p1', 'p1')
        _add_source(pm)
        pm.reindex('p1')
        engine = pm.get_search('p1')
        engine.search_procedures('Рассчитать')
        old_db = engine.db
        pm.create_project('p2', 'p2')
        monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 1)
        try:
            pm.get_db('p2')
            assert old_db._retired and old_db.conn is None
            with pytest.raises(IndexRemovedError):
                engine.search_procedures('Рассчитать')
            pm.delete_project('p1')
            assert not pm._index_path('p1').parent.exists()
        finally:
            pm.close_all()


def test_close_all_waits_for_existing_reader_lease():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(os.path.join(tmpdir, 'data'))
        pm.create_project('p1', 'p1')
        db = pm.get_db('p1')
        reader = db.read_conn(lease=True)
        retiring = threading.Event()
        original = reader.close

        def marked_close():
            retiring.set()
            original()

        reader.close = marked_close
        closer = threading.Thread(target=pm.close_all)
        closer.start()
        try:
            assert retiring.wait(10)
            assert reader.execute('SELECT 1').fetchone()[0] == 1
        finally:
            reader.release()
            closer.join(10)
        assert not closer.is_alive()
        assert db.conn is None and reader._closed


# ============================================
# get_db: файл индекса удалён или заменён снаружи
# ============================================

def _index_files(pm: ProjectManager) -> list[Path]:
    path = pm._index_path('p1')
    return [Path(f'{path}{s}') for s in ('', '-wal', '-shm') if Path(f'{path}{s}').exists()]


def _release_files(db) -> None:
    """Отпустить файлы, оставив Database в пуле как есть (db.conn не None):
    так его держал бы пул, пока файл удаляют снаружи — другой процесс,
    ariadna-index, docker volume rm. На Windows открытый файл не удалить."""
    db.conn.close()
    for reader in list(db._readers):
        reader.close()


@pytest.mark.parametrize('how', ['removed', 'replaced'])
def test_get_db_reopens_index_removed_or_replaced_outside(how):
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir, index_dir = os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index')
        pm = ProjectManager(data_dir, index_dir)
        try:
            pm.create_project('p1', 'p1')
            _add_source(pm)
            pm.reindex('p1')
            old = pm.get_db('p1')
            assert old.get_stats()['procedures'] > 0
            assert pm.get_db('p1') is old            # файл тот же — тот же объект

            _release_files(old)
            index_path = pm._index_path('p1')
            if how == 'removed':
                for f in _index_files(pm):
                    os.remove(f)
            else:
                # Другой файл на том же месте: пустой индекс, построенный
                # «другим процессом».
                other = ProjectManager(os.path.join(tmpdir, 'other'))
                other.create_project('p1', 'p1')
                other_path = other._index_path('p1')
                other.get_db('p1')
                other.close_all()
                for f in _index_files(pm):
                    os.remove(f)
                os.replace(other_path, index_path)

            db = pm.get_db('p1')
            assert db is not old
            assert db.get_stats()['procedures'] == 0
            assert _procedures(pm) == []
        finally:
            pm.close_all()
