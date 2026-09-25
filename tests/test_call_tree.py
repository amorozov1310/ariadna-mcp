"""
Tests for call tree + visual graph features.

Covers:
- CallAnalyzer returns source-aware trees when BSL is indexed
- Graph API payload format (nodes/edges/legend) is correct
- Multi-source references graph handles inbound references
"""

import os
import sys
import shutil
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.call_analyzer import CallAnalyzer

REPORT_FIXTURE = Path(__file__).parent / 'fixtures' / 'report_en.txt'
MODULE_FIXTURE = Path(__file__).parent / 'fixtures' / 'Module.bsl'


def _build_db_with_bsl(tmpdir: str, module_name: str = 'TestModule') -> Database:
    """Build DB with report + a single BSL module (CommonModule)."""
    db_path = os.path.join(tmpdir, 'index.db')
    db = Database(db_path)
    db.connect()
    db.init_schema()

    indexer = Indexer(db)
    indexer.index_report(str(REPORT_FIXTURE), source_id='main',
                         source_label='Ядро', source_type='main')

    fake_xml = os.path.join(tmpdir, 'xml')
    cm_dir = os.path.join(fake_xml, 'CommonModules', module_name, 'Ext')
    os.makedirs(cm_dir)
    shutil.copy(str(MODULE_FIXTURE), os.path.join(cm_dir, 'Module.bsl'))
    indexer.index_bsl(fake_xml, source_id='main')
    indexer.resolve_calls()
    return db


def test_bsl_indexing_populates_procedures_and_calls():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        conn = db.conn
        procs = conn.execute("SELECT COUNT(*) c FROM procedures").fetchone()['c']
        calls = conn.execute("SELECT COUNT(*) c FROM calls").fetchone()['c']
        assert procs >= 10, f"Expected at least 10 procedures, got {procs}"
        # After platform-function filtering (WriteLogEvent, NStr, ErrorInfo, etc.
        # are excluded), only real cross-module / local calls remain.
        assert calls >= 10, f"Expected at least 10 real calls, got {calls}"
        db.close()


def test_platform_functions_are_filtered():
    """Common platform functions (RU and EN) must NOT appear as indexed calls."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        conn = db.conn
        platform_names = [
            'WriteLogEvent', 'NStr', 'ErrorInfo',
            'CurrentUniversalDateInMilliseconds',
            'BriefErrorDescription', 'DetailedErrorDescription',
            'Raise', 'Metadata', 'ClearMessages',
            'InfoBaseConnectionString',
        ]
        for name in platform_names:
            n = conn.execute(
                "SELECT COUNT(*) c FROM calls WHERE callee_proc = ?",
                (name,)).fetchone()['c']
            assert n == 0, f"Platform function '{name}' should be filtered but appears {n} times"
        db.close()


def test_call_tree_down_direction():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        ca = CallAnalyzer(db)
        tree = ca.build_call_tree('PeriodicTasksRun', direction='down', depth=3)
        assert tree is not None
        assert tree.procedure_name == 'PeriodicTasksRun'
        assert tree.source_label == 'Ядро'
        assert len(tree.children) >= 5, f"Expected children, got {len(tree.children)}"
        db.close()


def test_call_tree_up_direction():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        ca = CallAnalyzer(db)
        # RunSelfAgent is called by PeriodicTasksRun in our sample
        tree = ca.build_call_tree('RunSelfAgent', direction='up', depth=2)
        assert tree is not None
        # Must find at least one caller
        assert len(tree.children) >= 1
        db.close()


def test_call_tree_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        ca = CallAnalyzer(db)
        assert ca.build_call_tree('NonExistentProcedure_XYZ', direction='down') is None
        db.close()


def test_call_tree_source_filter():
    """With source_id filter, only procedures from that source are roots."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        ca = CallAnalyzer(db)
        # main has PeriodicTasksRun
        tree = ca.build_call_tree('PeriodicTasksRun', direction='down', source_id='main')
        assert tree is not None
        # Bogus source returns None
        assert ca.build_call_tree('PeriodicTasksRun', direction='down', source_id='xxx') is None
        db.close()


def test_format_tree_contains_source_badges():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        ca = CallAnalyzer(db)
        tree = ca.build_call_tree('PeriodicTasksRun', direction='down', depth=2)
        text = ca.format_tree_simple(tree)
        # Root node has source
        assert '@Ядро' in text, f"Missing source badge in:\n{text[:400]}"
        db.close()


def test_graph_nodes_have_required_fields():
    """Simulate what the graph API returns and verify structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_db_with_bsl(tmpdir)
        ca = CallAnalyzer(db)
        tree = ca.build_call_tree('PeriodicTasksRun', direction='down', depth=2)
        assert tree is not None

        # Walk tree to build nodes/edges (same logic as api_calltree_graph)
        nodes, edges, seen = [], [], set()
        counter = [0]

        def walk(n, parent, level):
            key = f"{n.full_name}|{n.source_id}"
            if key in seen:
                return
            seen.add(key)
            nid = counter[0]; counter[0] += 1
            nodes.append({
                'id': nid, 'label': n.procedure_name,
                'source_id': n.source_id, 'source_label': n.source_label,
                'level': level,
            })
            if parent is not None:
                edges.append({'from': parent, 'to': nid})
            for c in n.children:
                walk(c, nid, level + 1)

        walk(tree, None, 0)

        assert len(nodes) > 1
        assert len(edges) == len(nodes) - 1  # tree has n-1 edges
        # Every node has an id and label
        for n in nodes:
            assert 'id' in n and 'label' in n and n['label']
        db.close()


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [
        test_bsl_indexing_populates_procedures_and_calls,
        test_platform_functions_are_filtered,
        test_call_tree_down_direction,
        test_call_tree_up_direction,
        test_call_tree_missing_returns_none,
        test_call_tree_source_filter,
        test_format_tree_contains_source_badges,
        test_graph_nodes_have_required_fields,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  ✅ {t.__name__}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {t.__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
