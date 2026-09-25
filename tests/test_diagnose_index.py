"""
Tests for Этап 7 (IMPROVEMENT_PLAN.md): call-graph resolution metrics in
diagnose_index — src.core.search.call_graph_resolution_stats().
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.search import call_graph_resolution_stats
from src.mcp_server.tools import execute_tool
from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'
REPORT_FIXTURE = FIXTURES / 'report_en.txt'


def _seed_known_graph(db: Database):
    """Hand-crafted, exact graph: 1 local-resolved call, 1 common_module-
    resolved call, 1 manager-unresolved call, 1 override-resolved call —
    so every percentage below is independently computable by hand."""
    conn = db.conn
    conn.execute("INSERT INTO sources (id, label) VALUES ('main', 'Main')")
    conn.execute("INSERT INTO modules (id, source_id, name, module_type) VALUES (1, 'main', 'Mod', 'CommonModule')")
    conn.execute("INSERT INTO procedures (id, module_id, name, kind) VALUES (1, 1, 'A', 'Процедура')")
    conn.execute("INSERT INTO procedures (id, module_id, name, kind) VALUES (2, 1, 'B', 'Процедура')")
    conn.execute("INSERT INTO procedures (id, module_id, name, kind) VALUES (3, 1, 'C', 'Процедура')")

    # call 1: A -> B, local, resolved
    conn.execute("INSERT INTO calls (id, caller_id, callee_name, callee_proc, callee_kind, line) "
                 "VALUES (1, 1, 'B', 'B', 'local', 1)")
    conn.execute("INSERT INTO calls_resolved (call_id, callee_id, confidence) VALUES (1, 2, 'exact')")

    # call 2: A -> Mod2.D, common_module, resolved
    conn.execute("INSERT INTO calls (id, caller_id, callee_name, callee_module, callee_proc, callee_kind, line) "
                 "VALUES (2, 1, 'Mod2.D', 'Mod2', 'D', 'common_module', 2)")
    conn.execute("INSERT INTO calls_resolved (call_id, callee_id, confidence) VALUES (2, 3, 'exact')")

    # call 3: A -> Catalogs.X.Method, manager, UNRESOLVED
    conn.execute("INSERT INTO calls (id, caller_id, callee_name, callee_module, callee_proc, callee_kind, line) "
                 "VALUES (3, 1, 'Catalogs.X.Method', 'Catalogs.X', 'Method', 'manager', 3)")

    # call 4: B -> A, override, resolved (B is the "interceptor")
    conn.execute("INSERT INTO calls (id, caller_id, callee_name, callee_proc, callee_kind, line) "
                 "VALUES (4, 2, 'A', 'A', 'override', 4)")
    conn.execute("INSERT INTO calls_resolved (call_id, callee_id, confidence) VALUES (4, 1, 'override')")
    conn.commit()


def test_call_graph_resolution_stats_exact_counts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        _seed_known_graph(db)
        try:
            stats = call_graph_resolution_stats(db.conn)

            assert stats['total_calls'] == 4
            assert stats['resolved_calls'] == 3
            assert stats['resolved_pct'] == 75.0

            assert stats['resolved_by_kind']['local'] == {'total': 1, 'resolved': 1, 'pct': 100.0}
            assert stats['resolved_by_kind']['common_module'] == {'total': 1, 'resolved': 1, 'pct': 100.0}
            assert stats['resolved_by_kind']['manager'] == {'total': 1, 'resolved': 0, 'pct': 0.0}
            assert stats['resolved_by_kind']['override'] == {'total': 1, 'resolved': 1, 'pct': 100.0}

            assert stats['override_procedures_count'] == 1  # only B calls out with kind='override'

            assert len(stats['top_unresolved']) == 1
            assert stats['top_unresolved'][0] == {'module': 'Catalogs.X', 'name': 'Method', 'freq': 1}

            # worst_modules requires >= 5 calls by default — none qualify here.
            assert stats['worst_modules'] == []
        finally:
            db.close()


def test_call_graph_resolution_stats_worst_modules_threshold():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        _seed_known_graph(db)
        try:
            stats = call_graph_resolution_stats(db.conn, min_calls_for_module=1)
            assert len(stats['worst_modules']) == 1
            wm = stats['worst_modules'][0]
            assert wm['module'] == 'Mod'
            assert wm['total'] == 4
            assert wm['resolved'] == 3
            assert wm['pct'] == 75.0
        finally:
            db.close()


def test_call_graph_resolution_stats_empty_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            stats = call_graph_resolution_stats(db.conn)
            assert stats['total_calls'] == 0
            assert stats['resolved_calls'] == 0
            assert stats['resolved_pct'] == 0.0
            assert stats['resolved_by_kind'] == {}
            assert stats['worst_modules'] == []
            assert stats['top_unresolved'] == []
            assert stats['override_procedures_count'] == 0
        finally:
            db.close()


def test_diagnose_index_tool_includes_graph_section():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        pm.create_project('p1', 'p1')
        with open(REPORT_FIXTURE, 'rb') as f:
            pm.add_source('p1', 'main', 'Main', report_file=f)
        pm.reindex('p1')
        try:
            text = execute_tool(pm, 'diagnose_index', {'project_id': 'p1'})
            assert '--- Call graph resolution ---' in text
            assert 'Resolved edges:' in text
        finally:
            pm.close_all()
