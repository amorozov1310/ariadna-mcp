"""Регрессии ограниченных кэшей и соединений индексов."""

from concurrent.futures import ThreadPoolExecutor
from src.core.db import Database
from src.core.project_manager import ProjectManager


def test_project_database_pool_is_lru_bounded_and_reopens(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 2)
    pm = ProjectManager(str(tmp_path / 'data'))
    for project_id in ('p0', 'p1', 'p2'):
        pm.create_project(project_id, project_id)

    first = pm.get_db('p0')
    reader = first.read_conn()
    first_writer = first.conn
    pm.get_db('p1')
    pm.get_db('p2')

    assert list(pm._db_pool) == ['p1', 'p2']
    assert first.conn is None
    assert first_writer is not None
    assert reader._closed

    reopened = pm.get_db('p0')
    assert reopened is not first
    assert reopened.read_conn().execute('SELECT 1').fetchone()[0] == 1
    assert len(pm._db_pool) == 2
    pm.close_all()


def test_reader_connections_are_bounded_under_concurrent_pool_access(tmp_path, monkeypatch):
    monkeypatch.setattr(Database, 'MAX_READERS', 4)
    db = Database(str(tmp_path / 'index.db'))
    db.connect()
    db.init_schema()

    def query(_):
        return db.read_conn().execute('SELECT 1').fetchone()[0]

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert list(pool.map(query, range(60))) == [1] * 60

    assert len(db._readers) <= 4
    assert sum(not conn._closed for conn in db._readers) <= 4
    db.close()


def test_project_pool_is_thread_safe_when_projects_are_evicted(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 3)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(8)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)

    def query(worker_no):
        for turn in range(30):
            project_id = project_ids[(worker_no + turn) % len(project_ids)]
            assert pm.get_db(project_id).read_conn().execute('SELECT 1').fetchone()[0] == 1

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(query, range(12)))

    assert len(pm._db_pool) <= 3
    pm.close_all()


def test_analysis_and_term_caches_have_fixed_limits(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    from src.core import cards

    monkeypatch.setattr(project_manager, 'BROKEN_CALL_CACHE_LIMIT', 2)
    pm = ProjectManager(str(tmp_path / 'data'))
    for project_id in ('p0', 'p1', 'p2'):
        pm.create_project(project_id, project_id)
        pm.broken_calls(project_id)
    assert len(pm._broken_calls) == 2

    db = pm.get_db('p0')
    conn = db.read_conn()
    for i in range(300):
        cards._term_share(db, conn, f'term{i}')
    assert len(db.__dict__['_card_term_share']) <= 256
    pm.close_all()


def test_deleting_project_releases_database_and_cached_results(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 2)
    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('demo', 'demo')
    db = pm.get_db('demo')
    db.read_conn()
    pm._broken_calls['demo'] = ('key', {})
    pm._base_diffs[('demo', 'other', None, None)] = ('key', object())

    pm.delete_project('demo')

    assert 'demo' not in pm._db_pool
    assert db.conn is None
    assert not db._readers
    assert 'demo' not in pm._broken_calls
    assert not pm._base_diffs
    pm.close_all()
