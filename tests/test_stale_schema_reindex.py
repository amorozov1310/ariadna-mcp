"""Legacy database schemas must trigger a source rebuild, not an empty index."""

import io
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
        assert _wait_until_done(manager) == 'ready'
        assert manager.get_search('p').search_metadata('Склад')
        assert manager.get_db('p').get_stats()['modules'] > 0
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
