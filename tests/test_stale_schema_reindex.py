"""Legacy database schemas must trigger a source rebuild, not an empty index."""

import io
import queue
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool


def _archive(source_dir):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zf:
        for path in source_dir.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir))
    archive.seek(0)
    return archive


def _make_ready_project(tmp_path):
    fixture = Path(__file__).parent / 'fixtures' / 'xml_ru'
    manager = ProjectManager(str(tmp_path))
    manager.create_project('p', 'Project')
    manager.add_source('p', 'main', 'Main', xml_archive=_archive(fixture),
                       xml_archive_filename='source.zip')
    manager.reindex('p')
    manager.close_all()
    return tmp_path / 'projects' / 'p' / 'index.db'


def _remove_new_table(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('DROP TABLE module_text')
        conn.commit()
    finally:
        conn.close()


def _wait_until_done(manager, project_id='p'):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = manager.get_project(project_id).status
        if status in ('ready', 'error'):
            return status
        time.sleep(0.02)
    raise AssertionError('schema rebuild did not finish')


def test_legacy_schema_rebuilds_from_xml_and_restores_search(tmp_path):
    db_path = _make_ready_project(tmp_path)
    _remove_new_table(db_path)

    manager = ProjectManager(str(tmp_path))
    try:
        db = manager.get_db('p')
        assert manager.get_project('p').status == 'indexing'
        assert db.stale_schema_recreated
        assert db.rebuild_required
        assert _wait_until_done(manager) == 'ready'
        assert manager.get_search('p').search_metadata('Склад')
        assert manager.get_db('p').get_stats()['modules'] > 0
        assert not db.rebuild_required
    finally:
        manager.close_all()


def test_concurrent_opens_queue_only_one_schema_rebuild(tmp_path, monkeypatch):
    db_path = _make_ready_project(tmp_path)
    _remove_new_table(db_path)
    manager = ProjectManager(str(tmp_path))
    runs = 0
    lock = threading.Lock()
    original = manager.reindex

    def counted_reindex(*args, **kwargs):
        nonlocal runs
        with lock:
            runs += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, 'reindex', counted_reindex)
    threads = [threading.Thread(target=manager.get_db, args=('p',)) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert _wait_until_done(manager) == 'ready'
        assert runs == 1
    finally:
        manager.close_all()


def test_legacy_schema_without_xml_is_reported_as_error(tmp_path):
    manager = ProjectManager(str(tmp_path))
    manager.create_project('p', 'Project')
    manager.add_source('p', 'main', 'Main')
    manager.update_project('p', status='ready')
    manager.get_db('p')
    manager.close_all()
    _remove_new_table(tmp_path / 'projects' / 'p' / 'index.db')

    manager = ProjectManager(str(tmp_path))
    try:
        manager.get_db('p')
        assert manager.get_project('p').status == 'error'
        message = manager.index_error('p')
        assert '\u0441\u0442\u0430\u0440\u043e\u0439 \u0441\u0445\u0435\u043c\u0435' in message
        assert '\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 XML \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430' in message
        assert manager.get_db('p').get_stats()['status'] == 'error'
        mcp_status = execute_tool(manager, 'get_index_status', {'project_id': 'p'})
        assert 'Error:' in mcp_status and 'XML' in mcp_status
    finally:
        manager.close_all()

    manager = ProjectManager(str(tmp_path))
    try:
        assert manager.index_error('p') == message
        manager.get_db('p')
        assert manager.index_error('p') == message
    finally:
        manager.close_all()


def test_interrupted_schema_rebuild_is_requeued_on_startup(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / 'fixtures' / 'xml_ru'
    manager = ProjectManager(str(tmp_path))
    manager.create_project('p', 'Project')
    manager.add_source('p', 'main', 'Main', xml_archive=_archive(fixture),
                       xml_archive_filename='source.zip')
    manager.get_db('p')  # Empty, current-schema index, as after a crash mid-rebuild.
    project = manager.get_project('p')
    project.status = 'indexing'
    project.index_stats.last_indexed = '2026-01-01T00:00:00'
    manager._save_project_to_disk('p', project)
    marker = Path(str(manager._index_path('p')) + '.reindex-required')
    marker.write_text('interrupted', encoding='utf-8')
    manager.close_all()

    manager = ProjectManager(str(tmp_path))
    # Keep the repair queued so the assertion observes startup recovery before
    # a fast fixture index can finish.
    monkeypatch.setattr(manager, 'reindex_async',
                        lambda project_id, source_id=None: {'status': 'started'})
    try:
        assert 'p' in manager.reset_stale_indexing()
        assert manager.get_project('p').status == 'indexing'
        assert manager.get_project('p').status != 'ready'
        assert marker.exists()
    finally:
        manager.close_all()


def test_queue_enqueue_at_worker_exit_starts_a_new_worker(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / 'fixtures' / 'xml_ru'
    manager = ProjectManager(str(tmp_path))
    manager.create_project('p', 'Project')
    manager.add_source('p', 'main', 'Main', xml_archive=_archive(fixture),
                       xml_archive_filename='source.zip')
    db = manager.get_db('p')
    monkeypatch.setattr(manager, 'reindex_async',
                        lambda project_id, source_id=None: {'status': 'started'})

    empty_seen, release_worker = threading.Event(), threading.Event()
    original_get_nowait = manager._schema_reindex_queue.get_nowait
    pause_once = True

    def pause_on_empty():
        nonlocal pause_once
        try:
            return original_get_nowait()
        except queue.Empty:
            if pause_once:
                pause_once = False
                empty_seen.set()
                assert release_worker.wait(5)
            raise

    monkeypatch.setattr(manager._schema_reindex_queue, 'get_nowait', pause_on_empty)
    try:
        manager._handle_stale_schema('p', db)
        assert empty_seen.wait(5)
        enqueue = threading.Thread(target=manager._handle_stale_schema, args=('p', db))
        enqueue.start()
        release_worker.set()
        enqueue.join(5)
        assert not enqueue.is_alive()
        manager._schema_reindex_queue.join()
        deadline = time.monotonic() + 5
        while manager._schema_reindex_worker_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not manager._schema_reindex_pending
        assert not manager._schema_reindex_worker_running
    finally:
        release_worker.set()
        manager.close_all()
