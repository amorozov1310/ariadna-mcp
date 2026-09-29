"""
Tests for Этап 6 (IMPROVEMENT_PLAN.md): MCP tool ergonomics.

Exercises src.mcp_server.tools.execute_tool() directly rather than going
through the actual MCPServer/SDK — that's the point of keeping it
transport-agnostic (see tools.py's docstring). Going through the real SDK
runs sync tool functions on a worker thread pool (mcp 2.x), which would
open extra per-thread DB reader connections that ProjectManager.close_all()
can't reach (see db.py Database.close()) and break tempdir teardown on
Windows (Этап 0) — calling execute_tool() directly stays on the test's own
thread and avoids that entirely.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.db import Database
from src.core.call_analyzer import CallAnalyzer
from src.core.search import format_object_details
from src.mcp_server.tools import execute_tool

REPORT_FIXTURE = Path(__file__).parent / 'fixtures' / 'report_en.txt'


def _pm_with_project(tmpdir: str, project_id: str = 'p1') -> ProjectManager:
    pm = ProjectManager(data_dir=tmpdir)
    pm.create_project(project_id, project_id)
    with open(REPORT_FIXTURE, 'rb') as f:
        pm.add_source(project_id, 'main', 'Main', report_file=f)
    pm.reindex(project_id)
    return pm


# ============================================
# D12: project_id optional when there's exactly one project
# ============================================

def test_project_id_defaults_when_single_project():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'get_index_status', {})
            assert 'Status: ready' in result
        finally:
            pm.close_all()


def test_project_id_error_lists_available_when_multiple_projects():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir, 'p1')
        pm.create_project('p2', 'p2')
        try:
            result = execute_tool(pm, 'search_metadata', {'query': 'x'})
            assert 'Error' in result
            assert 'p1' in result and 'p2' in result
        finally:
            pm.close_all()


def test_unknown_project_id_lists_available():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir, 'p1')
        try:
            result = execute_tool(pm, 'search_metadata', {'project_id': 'nope', 'query': 'x'})
            assert "'nope' not found" in result
            assert 'p1' in result
        finally:
            pm.close_all()


def test_list_projects_needs_no_project_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'list_projects', {})
            assert 'p1' in result
        finally:
            pm.close_all()


def test_set_active_project_removed():
    """D12: set_active_project never persisted anything — removed outright."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'set_active_project', {'project_id': 'p1'})
            assert result == "Unknown tool: set_active_project"
        finally:
            pm.close_all()


# ============================================
# Pagination: limit/offset + "shown X of Y" / "use offset=N for more"
# ============================================

def test_search_metadata_pagination_trailer():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            # report_en.txt has enough Catalog objects to page through.
            more = execute_tool(pm, 'search_metadata', {'project_id': 'p1', 'query': 'a', 'limit': 2, 'offset': 0})
            assert 'use offset=2 for more' in more or 'shown' in more

            # Walk to the end and confirm the terminal page says "shown X of Y".
            offset = 0
            last = None
            for _ in range(50):
                page = execute_tool(pm, 'search_metadata', {'project_id': 'p1', 'query': 'a', 'limit': 5, 'offset': offset})
                last = page
                if 'use offset=' not in page:
                    break
                offset += 5
            assert last is not None
            assert 'shown' in last and ' of ' in last
        finally:
            pm.close_all()


def test_list_objects_offset_beyond_results():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'list_objects', {'project_id': 'p1', 'offset': 100000, 'limit': 10})
            assert 'No further results at offset=100000' in result
        finally:
            pm.close_all()


# ============================================
# get_object_details: detail=brief|full (Этап 6 token budget)
# ============================================

def test_format_object_details_brief_truncates_attributes_to_30():
    details = {
        'object': {'full_name': 'Справочник.Тест', 'synonym': 'Тест'},
        'attributes': [{'name': f'Attr{i}', 'kind': 'Реквизит'} for i in range(35)],
        'tabular_sections': {'ТЧ1': [{'name': 'Col1'}, {'name': 'Col2'}]},
        'forms': [], 'enum_values': [], 'module': None, 'matches': [],
    }
    brief = format_object_details(details, detail='brief')
    assert 'Attr29' in brief
    assert 'Attr30' not in brief
    assert '... and 5 more' in brief
    assert 'ТЧ1 (2 реквизитов' in brief
    assert 'Col1' not in brief

    full = format_object_details(details, detail='full')
    assert 'Attr34' in full
    assert '... and' not in full
    assert 'Col1' in full


def test_get_object_details_default_is_brief():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            # Find any indexed object to inspect.
            listing = pm.get_search('p1').list_objects(limit=1)
            assert listing, "fixture should index at least one object"
            full_name = listing[0]['full_name']
            result = execute_tool(pm, 'get_object_details', {'project_id': 'p1', 'full_name': full_name})
            assert 'Object not found' not in result
        finally:
            pm.close_all()


# ============================================
# remove_source: confirm gate (Этап 6/D13)
# ============================================

def test_remove_source_requires_confirm():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            preview = execute_tool(pm, 'remove_source', {'project_id': 'p1', 'source_id': 'main'})
            assert 'confirm=true' in preview
            assert 'Metadata objects:' in preview
            # Nothing deleted yet.
            assert 'main' in execute_tool(pm, 'list_sources', {'project_id': 'p1'})

            deleted = execute_tool(pm, 'remove_source', {'project_id': 'p1', 'source_id': 'main', 'confirm': True})
            assert 'removed' in deleted
            assert 'No sources indexed yet.' == execute_tool(pm, 'list_sources', {'project_id': 'p1'})
        finally:
            pm.close_all()


def test_remove_source_unknown_source_errors_without_deleting():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'remove_source', {'project_id': 'p1', 'source_id': 'ghost'})
            assert 'Error' in result
        finally:
            pm.close_all()


def test_remove_source_confirmed_unknown_source_errors_instead_of_reporting_removal():
    """confirm=true с несуществующим source_id раньше отвечал «removed» —
    pm.remove_source молча выходит, если источника нет."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'remove_source',
                                  {'project_id': 'p1', 'source_id': 'ghost', 'confirm': True})
            assert result.startswith('Error'), result
            assert 'removed' not in result
            assert 'main' in result, "ошибка перечисляет доступные источники"
            assert [s.id for s in pm.get_project('p1').sources] == ['main']
            assert 'main' in execute_tool(pm, 'list_sources', {'project_id': 'p1'})
        finally:
            pm.close_all()


# ============================================
# reindex: неизвестный source_id
# ============================================

def test_reindex_unknown_source_errors_without_starting_background_run(monkeypatch):
    """Раньше инструмент отвечал «Reindex started», а фоновый reindex()
    падал с KeyError и оставлял проекту status='error'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            started = []
            monkeypatch.setattr(pm, 'reindex_async',
                                lambda *a, **kw: started.append((a, kw)) or {'status': 'started'})
            result = execute_tool(pm, 'reindex', {'project_id': 'p1', 'source_id': 'ghost'})
            assert result.startswith('Error'), result
            assert 'main' in result, "ошибка перечисляет доступные источники"
            assert started == [], "фоновый прогон не должен стартовать"
            assert pm.get_project('p1').status == 'ready'
        finally:
            pm.close_all()


def test_background_reindex_failure_is_logged(caplog):
    import logging
    import threading
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            with caplog.at_level(logging.ERROR, logger='ariadna'):
                assert pm.reindex_async('p1', source_id='ghost')['status'] == 'started'
                for t in threading.enumerate():
                    if t.name == 'reindex-p1':
                        t.join(timeout=30)
            assert any(r.exc_info and 'ghost' in r.getMessage() + str(r.exc_info[1])
                       for r in caplog.records), caplog.text
        finally:
            pm.close_all()


# ============================================
# get_call_tree: max_nodes hard cap (Этап 6 token budget)
# ============================================

def _insert_fanout_graph(db: Database, n_children: int = 4) -> int:
    """Root procedure calling n_children sibling procedures, each a leaf.
    Returns root's procedure id."""
    conn = db.conn
    conn.execute("INSERT INTO sources (id, label) VALUES ('main', 'Main')")
    conn.execute(
        "INSERT INTO modules (id, source_id, name, module_type) VALUES (1, 'main', 'Mod', 'CommonModule')")
    conn.execute(
        "INSERT INTO procedures (id, module_id, name, kind, name_cf) VALUES (1, 1, 'Root', 'Процедура', 'root')")
    for i in range(n_children):
        pid = i + 2
        conn.execute(
            "INSERT INTO procedures (id, module_id, name, kind, name_cf) VALUES (?, 1, ?, 'Процедура', ?)",
            (pid, f'Child{i}', f'child{i}'))
        conn.execute(
            "INSERT INTO calls (id, caller_id, callee_name, callee_proc, callee_kind, line) "
            "VALUES (?, 1, ?, ?, 'local', ?)", (i + 1, f'Child{i}', f'Child{i}', i + 1))
        conn.execute(
            "INSERT INTO calls_resolved (call_id, callee_id, confidence) VALUES (?, ?, 'exact')",
            (i + 1, pid))
    conn.commit()
    return 1


def test_build_call_tree_max_nodes_truncates():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'index.db'))
        db.connect()
        db.init_schema()
        _insert_fanout_graph(db, n_children=4)
        try:
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree('Root', direction='down', depth=3, max_nodes=3)
            assert tree is not None
            assert len(tree.children) == 2, "budget of 3 (root + 2) should cut off 2 of 4 children"
            assert tree.truncated is True

            text = ca.format_tree_simple(tree)
            assert 'truncated' in text

            # A generous budget doesn't truncate.
            tree_full = ca.build_call_tree('Root', direction='down', depth=3, max_nodes=200)
            assert len(tree_full.children) == 4
            assert tree_full.truncated is False
        finally:
            db.close()


# ============================================
# get_call_tree: direction только 'down' | 'up'
# ============================================

def test_get_call_tree_rejects_unknown_direction():
    """Раньше direction='sideways' молча работал как 'up'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(tmpdir)
        try:
            result = execute_tool(pm, 'get_call_tree', {'project_id': 'p1', 'procedure_name': 'X',
                                                        'direction': 'sideways'})
            assert result == "Error: direction must be 'down' or 'up'", result
            for ok in ('down', 'up', 'UP'):
                text = execute_tool(pm, 'get_call_tree', {'project_id': 'p1', 'procedure_name': 'X',
                                                          'direction': ok})
                assert not text.startswith('Error'), (ok, text)
        finally:
            pm.close_all()


def test_get_call_tree_direction_schema_is_enum():
    import pytest
    pytest.importorskip("mcp.server.mcpserver")
    import asyncio
    from src.mcp_server.server import create_mcp_server
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        try:
            tools = asyncio.run(create_mcp_server(pm).list_tools())
            schema = next(t for t in tools if t.name == 'get_call_tree').input_schema
            assert schema['properties']['direction']['enum'] == ['down', 'up'], schema
        finally:
            pm.close_all()
