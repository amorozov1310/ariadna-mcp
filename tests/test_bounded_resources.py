"""Регрессии ограниченных кэшей и соединений индексов."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
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


def test_reader_limit_reuses_live_connection_instead_of_closing_between_queries(
        tmp_path, monkeypatch):
    """A caller may retain read_conn() across several SQL statements."""
    monkeypatch.setattr(Database, 'MAX_READERS', 2)
    db = Database(str(tmp_path / 'index.db'))
    db.connect()
    db.init_schema()
    first_query_done = threading.Event()
    continue_query = threading.Event()

    def multi_query():
        conn = db.read_conn()
        assert conn.execute('SELECT 1').fetchone()[0] == 1
        first_query_done.set()
        assert continue_query.wait(5)
        # The connection remains valid even after other threads requested
        # readers while this operation was between its SQL statements.
        return conn.execute('SELECT 2').fetchone()[0]

    def other_queries():
        assert first_query_done.wait(5)
        with ThreadPoolExecutor(max_workers=10) as pool:
            return list(pool.map(lambda _: db.read_conn().execute('SELECT 1').fetchone()[0],
                                 range(40)))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(multi_query)
            assert first_query_done.wait(5)
            others = pool.submit(other_queries)
            assert others.result(timeout=8) == [1] * 40
            continue_query.set()
            assert first.result(timeout=5) == 2
        assert len(db._readers) <= 2
        assert all(not reader._closed for reader in db._readers)
    finally:
        continue_query.set()
        db.close()


def test_http_calltree_holds_reader_across_recursive_sql_under_pool_pressure(
        tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(16)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)
        pm.get_db(project_id)

    db = pm.get_db('p0')
    db.conn.execute("INSERT INTO sources(id, label) VALUES ('main', 'synthetic')")
    module_id = db.conn.execute(
        "INSERT INTO modules(source_id, name, module_type, name_cf) "
        "VALUES ('main', 'CommonModule.Stress', 'common', 'commonmodule.stress')"
    ).lastrowid
    procedure_ids = []
    for index in range(40):
        procedure_ids.append(db.conn.execute(
            "INSERT INTO procedures(module_id, name, kind, name_cf) VALUES (?, ?, 'procedure', ?)",
            (module_id, f'Work{index:02d}', f'work{index:02d}')).lastrowid)
    for index, caller_id in enumerate(procedure_ids[:-1]):
        call_id = db.conn.execute(
            "INSERT INTO calls(caller_id, callee_name, callee_module, callee_proc, callee_kind, line) "
            "VALUES (?, ?, 'CommonModule.Stress', ?, 'local', ?)",
            (caller_id, f'Work{index + 1:02d}', f'Work{index + 1:02d}', index + 1)
        ).lastrowid
        db.conn.execute(
            "INSERT INTO calls_resolved(call_id, callee_id, confidence) VALUES (?, ?, 'exact')",
            (call_id, procedure_ids[index + 1]))
    db.conn.commit()

    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1'})

        def request(index):
            if index % 2 == 0:
                return client.get('/api/projects/p0/graph/calltree',
                                  params={'procedure': 'Work00', 'depth': 4})
            return client.get(f'/api/projects/p{index % 16}/status')

        with ThreadPoolExecutor(max_workers=16) as pool:
            responses = list(pool.map(request, range(96)))
        assert [response.status_code for response in responses] == [200] * len(responses)
        trees = [response.json() for index, response in enumerate(responses) if index % 2 == 0]
        assert all(len(tree['nodes']) == 5 for tree in trees)
        assert not pm._db_pins
        assert len(pm._db_pool) <= 8
        assert not any(reader.active for database in pm._db_pool.values()
                       for reader in database._readers)
    finally:
        app_module.set_pm(previous)
        pm.close_all()


def test_status_request_stays_pinned_while_eight_other_projects_open(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    from fastapi.testclient import TestClient
    from src.core.db import Database
    from src.web import app as app_module

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path / 'data'))
    for project_id in ['p'] + [f'other{i}' for i in range(8)]:
        pm.create_project(project_id, project_id)
    pm.get_db('p')
    entered, release = threading.Event(), threading.Event()
    original = Database.get_index_progress

    def delayed_progress(db):
        if db.db_path == str(pm._index_path('p')):
            entered.set()
            assert release.wait(5)
        return original(db)

    monkeypatch.setattr(Database, 'get_index_progress', delayed_progress)
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            response = pool.submit(client.get, '/api/projects/p/status')
            assert entered.wait(3)
            with ThreadPoolExecutor(max_workers=8) as openers:
                list(openers.map(pm.get_db, [f'other{i}' for i in range(8)]))
            assert len(pm._db_pool) <= 8
            assert pm._db_pool.get('p') is not None
            release.set()
            assert response.result(timeout=5).status_code == 200
    finally:
        release.set()
        app_module.set_pm(previous)
        pm.close_all()


def test_retired_writer_rejects_state_update(tmp_path):
    from src.core.db import IndexRemovedError

    db = Database(str(tmp_path / 'index.db'))
    db.connect()
    db.init_schema()
    db.retire()
    with pytest.raises(IndexRemovedError):
        db.update_state(status='error')


def test_reader_does_not_treat_partially_published_schema_as_stale(tmp_path, monkeypatch):
    import src.core.db as db_module

    path = str(tmp_path / 'index.db')
    writer = Database(path)
    writer.connect()
    entered, release = threading.Event(), threading.Event()

    def pause_schema():
        entered.set()
        assert release.wait(5)

    writer.conn.create_function('pause_schema', 0, pause_schema)
    monkeypatch.setattr(db_module, 'SCHEMA_SQL', """
        CREATE TABLE calls (id INTEGER);
        CREATE TABLE index_state (id INTEGER PRIMARY KEY, progress_phase TEXT);
        SELECT pause_schema();
        CREATE TABLE module_text (id INTEGER);
    """)
    reader = Database(path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            initializing = pool.submit(writer.init_schema)
            assert entered.wait(3)
            opening = pool.submit(reader.connect)
            time.sleep(.05)
            assert not opening.done()
            release.set()
            initializing.result(timeout=5)
            opening.result(timeout=5)
        assert not reader.stale_schema_recreated
        assert not reader.rebuild_required
        assert writer._current_file_id() == reader._current_file_id()
    finally:
        release.set()
        reader.close()
        writer.close()


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
            with pm.read_lease(project_id) as db:
                assert db.read_conn().execute('SELECT 1').fetchone()[0] == 1

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(query, range(12)))

    assert len(pm._db_pool) <= 3
    pm.close_all()


def test_concurrent_get_db_opens_one_database_per_project(tmp_path, monkeypatch):
    monkeypatch.setattr('src.core.project_manager.DB_POOL_LIMIT', 2)
    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('p1', 'p1')
    original = Database.connect
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    lock = threading.Lock()

    def delayed_connect(db):
        nonlocal calls
        with lock:
            calls += 1
        entered.set()
        assert release.wait(5)
        return original(db)

    monkeypatch.setattr(Database, 'connect', delayed_connect)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(pm.get_db, 'p1') for _ in range(8)]
            assert entered.wait(2)
            release.set()
            databases = [future.result(timeout=5) for future in futures]
        assert calls == 1
        assert len({id(db) for db in databases}) == 1
    finally:
        release.set()
        pm.close_all()


def test_pool_overflows_while_all_databases_are_leased(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 1)
    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('held', 'held')
    pm.create_project('overflow', 'overflow')

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pm.read_lease('held'):
                future = pool.submit(pm.get_db, 'overflow')
                opened = future.result(timeout=5)
                assert pm._index_path('overflow').exists()
                assert len(pm._db_pool) == 1
                assert 'overflow' not in pm._db_pool
                assert opened._retired
            assert len(pm._db_pool) == 1
            assert 'overflow' not in pm._db_pool
            assert opened._retired
    finally:
        pm.close_all()


def test_delete_waits_for_reserved_database_open_to_abort(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('p1', 'p1')
    original = Database.connect
    opening, release = threading.Event(), threading.Event()

    def delayed_connect(db):
        opening.set()
        assert release.wait(5)
        return original(db)

    monkeypatch.setattr(Database, 'connect', delayed_connect)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(pm.get_db, 'p1')
            assert opening.wait(2)
            deletion = pool.submit(pm.delete_project, 'p1')
            time.sleep(.05)
            assert not deletion.done()
            release.set()
            with pytest.raises(Exception, match='p1'):
                pending.result(timeout=5)
            deletion.result(timeout=5)
        assert not pm._index_path('p1').parent.exists()
        assert 'p1' not in pm._db_pool
    finally:
        release.set()
        pm.close_all()


def test_close_all_closes_overflow_after_leased_operation_finishes(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 1)
    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('held', 'held')
    pm.create_project('waiting', 'waiting')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            with pm.read_lease('held'):
                pending = pool.submit(pm.get_db, 'waiting')
                overflow = pending.result(timeout=5)
                closing = pool.submit(pm.close_all)
                time.sleep(.05)
                assert not closing.done()
            closing.result(timeout=5)
        assert overflow._retired and overflow.conn is None
        assert not pm._db_pool
        # Новый запрос после завершённого close_all снова разрешён.
        assert pm.get_db('waiting').conn is not None
    finally:
        pm.close_all()


def test_reindex_warms_after_all_independent_writers_finish(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path / 'data'))
    for i in range(8):
        pm.create_project(f'p{i}', f'p{i}')
    barrier = threading.Barrier(8)
    mark = pm._mark_indexing

    def check_writer(project_id, *, db=None):
        assert db is not None
        assert db not in pm._db_pool.values()
        return mark(project_id, db=db)

    monkeypatch.setattr(pm, '_mark_indexing', check_writer)
    monkeypatch.setattr(pm, '_warm_related', lambda _project_id: barrier.wait(timeout=10))
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda pid: pm.reindex(pid), [f'p{i}' for i in range(8)]))
        assert len(results) == 8
    finally:
        pm.close_all()


def test_slow_old_broken_call_calculation_cannot_replace_new_revision(tmp_path, monkeypatch):
    from src.core import broken_calls

    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('p1', 'p1')
    revision = ['old']
    monkeypatch.setattr(pm, '_diff_key', lambda _db: ('p1', revision[0]))
    started, release = threading.Event(), threading.Event()
    calls = 0

    def calculate(_conn, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(5)
            label = 'old'
        else:
            label = 'new'
        return {'missing': [label], 'not_export': []}

    monkeypatch.setattr(broken_calls, 'find_broken_calls', calculate)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(pm.broken_calls, 'p1')
            assert started.wait(2)
            revision[0] = 'new'
            assert pm.broken_calls('p1')['missing'] == ['new']
            release.set()
            assert old.result(timeout=5)['missing'] == ['old']
        assert pm._broken_calls['p1'] == (('p1', 'new'), {'missing': ['new'], 'not_export': []})
    finally:
        release.set()
        pm.close_all()


def test_close_all_waits_for_checks_and_blocks_new_warmups(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('p1', 'p1')
    entered, release = threading.Event(), threading.Event()

    def slow_broken_calls(_project_id):
        entered.set()
        assert release.wait(5)
        return {'missing': [], 'not_export': []}

    monkeypatch.setattr(pm, 'broken_calls', slow_broken_calls)
    try:
        assert not pm.warm_broken_calls('p1')
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(pm.close_all)
            deadline = time.monotonic() + 2
            while not pm._closing_checks and time.monotonic() < deadline:
                time.sleep(.01)
            assert pm._closing_checks
            assert not pm.warm_broken_calls('p1')
            release.set()
            closing.result(timeout=5)
        assert all(not thread.is_alive() for thread in pm._checks_warming.values())
    finally:
        release.set()
        pm.close_all()


def test_analysis_and_term_caches_have_fixed_limits(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    from src.core import cards

    monkeypatch.setattr(project_manager, 'BROKEN_CALL_CACHE_LIMIT', 2)
    monkeypatch.setattr(project_manager, 'PROJECT_ERROR_CACHE_LIMIT', 2)
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
    for i in range(3):
        pm._set_index_error(f'p{i}', f'error{i}')
    assert len(pm._index_errors) == 2
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


def test_database_lease_release_trims_soft_pool_overflow_and_schema_locks(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    import src.core.db as db_module

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 2)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(12)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)

    all_open = threading.Barrier(len(project_ids))
    overflow_seen = threading.Event()

    def leased_read(project_id):
        with pm.read_lease(project_id) as db:
            all_open.wait(timeout=8)
            if len(pm._db_pool) > 2:
                overflow_seen.set()
            return db.read_conn().execute('SELECT 1').fetchone()[0]

    try:
        with ThreadPoolExecutor(max_workers=len(project_ids)) as pool:
            assert list(pool.map(leased_read, project_ids)) == [1] * len(project_ids)
        assert overflow_seen.is_set()  # мягкий предел во время активных аренд
        assert len(pm._db_pool) <= 2

        keys = {Database._schema_lock_key(str(pm._index_path(pid))) for pid in project_ids}
        assert keys <= db_module._SCHEMA_LOCKS.keys()
        for project_id in project_ids:
            pm.delete_project(project_id)
        assert not (keys & db_module._SCHEMA_LOCKS.keys())
    finally:
        all_open.abort()
        pm.close_all()


def test_manifest_and_call_graph_http_leases_survive_concurrent_eviction(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    from fastapi.testclient import TestClient
    from src.core.call_analyzer import CallAnalyzer
    from src.web import app as app_module

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(16)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)
        pm.get_db(project_id)

    def manifest(project_id, source_id):
        db = pm.get_db(project_id)
        conn = db.read_conn()
        conn.execute('SELECT 1').fetchone()
        time.sleep(.005)
        conn.execute('SELECT 1').fetchone()
        return {'project_id': project_id, 'source_id': source_id}

    def call_tree(_analyzer, *args, **kwargs):
        conn = _analyzer.db.read_conn()
        conn.execute('SELECT 1').fetchone()
        time.sleep(.005)
        conn.execute('SELECT 1').fetchone()
        return None

    monkeypatch.setattr(pm, 'source_file_manifest', manifest)
    monkeypatch.setattr(CallAnalyzer, 'build_call_tree', call_tree)
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})

        def request(index):
            project_id = project_ids[index % len(project_ids)]
            if index % 2:
                return client.get(f'/api/projects/{project_id}/sources/main/manifest')
            return client.get(f'/api/projects/{project_id}/graph/calltree',
                              params={'procedure': 'Work'})

        with ThreadPoolExecutor(max_workers=32) as pool:
            responses = list(pool.map(request, range(128)))
        assert [response.status_code for response in responses] == [200] * 128
        assert not pm._db_pins
        assert len(pm._db_pool) <= 8
        open_connections = sum(bool(db.conn) + sum(not reader._closed for reader in db._readers)
                               for db in pm._db_instances)
        # На каждом из восьми Database остаются ленивые thread-local readers;
        # общий предел — не больше writer + MAX_READERS на объект пула.
        assert open_connections <= 8 * (1 + Database.MAX_READERS)
    finally:
        app_module.set_pm(previous)
        pm.close_all()


def test_check_warming_remains_project_scoped_under_pool_eviction(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(16)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)
        pm.get_db(project_id)

    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1'})

        def request(index):
            project_id = project_ids[index % len(project_ids)]
            paths = (
                f'/api/projects/{project_id}/checks/warming',
                f'/api/projects/{project_id}/manifest',
                f'/api/projects/{project_id}/status',
                f'/projects/{project_id}/search',
                f'/projects/{project_id}/calltree?procedure=Missing',
                f'/api/projects/{project_id}/graph/calltree?procedure=Missing',
                f'/projects/{project_id}/graph?procedure=Missing',
            )
            responses = [client.get(path) for path in paths]
            from src.mcp_server.tools import execute_tool
            mcp_results = (
                execute_tool(pm, 'get_index_status', {'project_id': project_id}),
                execute_tool(pm, 'get_call_tree', {
                    'project_id': project_id, 'procedure_name': 'Missing'}),
            )
            return responses, mcp_results

        with ThreadPoolExecutor(max_workers=24) as pool:
            responses = list(pool.map(request, range(16 * 12)))
        http_responses = [response for batch, _ in responses for response in batch]
        mcp_results = [result for _, batch in responses for result in batch]
        assert [response.status_code for response in http_responses] == [200] * len(http_responses)
        warming = [response for batch, _ in responses for response in batch
                   if str(response.url).endswith('/checks/warming')]
        assert all(not response.json()['ready'] for response in warming)
        assert all(not result.startswith('Error') for result in mcp_results)
        assert not pm._db_pins
        assert len(pm._db_pool) <= 8
        assert not any(reader.active for db in pm._db_pool.values() for reader in db._readers)
    finally:
        app_module.set_pm(previous)
        pm.close_all()


def test_index_manifest_and_call_graph_requests_survive_pool_eviction(tmp_path, monkeypatch):
    """Именно манифест индекса, а не файловый манифест источника."""
    import src.core.project_manager as project_manager
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(16)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)

    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})

        def request(index):
            project_id = project_ids[index % len(project_ids)]
            if index % 2:
                return client.get(f'/api/projects/{project_id}/manifest')
            return client.get(f'/api/projects/{project_id}/graph/calltree',
                              params={'procedure': 'Work'})

        with ThreadPoolExecutor(max_workers=32) as pool:
            responses = list(pool.map(request, range(256)))
        assert [response.status_code for response in responses] == [200] * 256
        assert all(response.json().get('project_id') or response.json().get('nodes') is not None
                   for response in responses)
        assert not pm._db_pins
        assert len(pm._db_pool) <= 8
    finally:
        app_module.set_pm(previous)
        pm.close_all()


def test_http_request_dependency_does_not_release_reader_on_another_thread(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.core.db import _ReaderConnection
    from src.web import app as app_module

    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('p', 'p')
    pm.get_db('p')
    wrong_thread_releases = []
    original_release = _ReaderConnection.release

    def checked_release(conn):
        if not conn.leased_by_current_thread():
            wrong_thread_releases.append(conn)
        return original_release(conn)

    monkeypatch.setattr(_ReaderConnection, 'release', checked_release)
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        with ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(
                lambda _: client.get('/projects/p/search?q=synthetic'), range(40)))
        assert [response.status_code for response in responses] == [200] * 40
        assert not wrong_thread_releases
        assert not pm._db_pins
    finally:
        app_module.set_pm(previous)
        pm.close_all()


def test_concurrent_unleased_get_db_opens_trim_pool_to_limit(tmp_path, monkeypatch):
    import src.core.project_manager as project_manager

    monkeypatch.setattr(project_manager, 'DB_POOL_LIMIT', 8)
    pm = ProjectManager(str(tmp_path / 'data'))
    project_ids = [f'p{i}' for i in range(16)]
    for project_id in project_ids:
        pm.create_project(project_id, project_id)

    start = threading.Barrier(16)

    def open_unleased(project_id):
        start.wait()
        return pm.get_db(project_id)

    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(open_unleased, project_ids))
        assert len(pm._db_pool) <= 8
        assert not pm._db_pins
        assert all(not any(reader.active for reader in db._readers)
                   for db in pm._db_pool.values())
    finally:
        pm.close_all()


def test_process_lock_maps_keep_only_active_locks(tmp_path):
    import gc
    from src.core import glossary
    from src.core.project_manager import _REGISTRY_LOCKS, _REINDEX_LOCKS

    pm = ProjectManager(str(tmp_path / 'data'))
    project_lock = pm._reindex_lock('temporary')
    project_lock_key = (str(pm.data_dir), 'temporary')
    registry_lock_key = pm._registry_lock_key
    glossary_lock_key = str((tmp_path / 'glossary.json').resolve())
    assert pm._reindex_lock('temporary') is project_lock
    registry_lock = pm._registry_lock()
    glossary_lock = glossary._lock(tmp_path / 'glossary.json')
    assert len(_REINDEX_LOCKS) >= 1 and len(_REGISTRY_LOCKS) >= 1

    del project_lock, registry_lock, glossary_lock
    gc.collect()

    assert project_lock_key not in _REINDEX_LOCKS
    assert registry_lock_key not in _REGISTRY_LOCKS
    assert glossary_lock_key not in glossary._locks
    pm.close_all()
