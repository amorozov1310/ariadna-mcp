"""
Slow integration tests against the real corpora used to validate the
indexer/parser at ERP scale before touching the customer's actual export.

Skipped automatically (marker + skip check) when the corpus directory
isn't present locally — these depend on data/projects/{bshp,1idm2}
which are not meant to ship to every clone of this repo.

Run explicitly with:
    python -m pytest tests -q -m slow

These tests reindex the real project directories in data/projects/ in
place (same registry/index.db the Web UI and MCP server use for these
projects) rather than copying tens of thousands of files into a temp
dir — that mirrors how bshp/1idm2 are actually indexed day to day.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.call_analyzer import CallAnalyzer
from src.core import report_generator

DATA_DIR = Path(__file__).parent.parent / 'data'
BSHP_XML = DATA_DIR / 'projects' / 'bshp' / 'sources' / 'main' / 'xml'
IDM2_DIR = DATA_DIR / 'projects' / '1idm2'

pytestmark = pytest.mark.slow


def _project_manager() -> ProjectManager:
    return ProjectManager(str(DATA_DIR))


@pytest.mark.skipif(not BSHP_XML.exists(), reason="bshp corpus not present (data/projects/bshp/sources/main/xml)")
def test_bshp_full_reindex_bsl_graph():
    """Full BSHP corpus (~19k .bsl files, main + ext_obmen) indexes without errors.

    report.txt was absent for bshp until Этап 5 (see IMPROVEMENT_PLAN Этап 0.5) —
    reindex now auto-generates it from the XML dump (src/core/report_generator.py)
    when the generate_config_report package is installed, so metadata is expected
    to be populated here too, not just BSL/call-graph.
    """
    pm = _project_manager()
    try:
        t0 = time.time()
        stats = pm.reindex('bshp')
        elapsed = time.time() - t0

        proj = pm.get_project('bshp')
        assert proj.status == 'ready'
        assert stats.total_modules > 1000, f"Expected many modules, got {stats.total_modules}"
        assert stats.total_procedures > 1000, f"Expected many procedures, got {stats.total_procedures}"
        # D15 regression (IMPROVEMENT_PLAN Этап 4.5): a shadowed timer var once
        # made duration_sec ≈ epoch seconds (billions) per source.
        assert 0 < stats.duration_sec < 3600, f"duration_sec looks like epoch time: {stats.duration_sec}"

        if report_generator.is_available():
            assert stats.total_objects > 0, (
                "Этап 5: report.txt should have been auto-generated from the "
                "XML dump and metadata indexed from it")
            src = next(s for s in proj.sources if s.id == 'main')
            assert src.report_path, "auto-generated report.txt should be recorded on the source"

        print(f"\n[bshp] objects={stats.total_objects} modules={stats.total_modules} "
              f"procedures={stats.total_procedures} calls={stats.total_calls} "
              f"duration={elapsed:.1f}s")
    finally:
        pm.close_all()


@pytest.mark.skipif(not IDM2_DIR.exists(), reason="1idm2 corpus not present (data/projects/1idm2)")
def test_1idm2_full_reindex_metadata_and_graph():
    """Full 1idm2 corpus (main + 11 extensions) indexes metadata + BSL without errors."""
    pm = _project_manager()
    try:
        t0 = time.time()
        stats = pm.reindex('1idm2')
        elapsed = time.time() - t0

        proj = pm.get_project('1idm2')
        assert proj.status == 'ready'
        assert stats.total_objects > 0, "Expected metadata objects from idm2/report.txt"
        assert stats.total_modules > 0
        assert stats.total_procedures > 0
        # D15 regression (IMPROVEMENT_PLAN Этап 4.5): see test_bshp_full_reindex_bsl_graph.
        assert 0 < stats.duration_sec < 3600, f"duration_sec looks like epoch time: {stats.duration_sec}"

        print(f"\n[1idm2] objects={stats.total_objects} modules={stats.total_modules} "
              f"procedures={stats.total_procedures} calls={stats.total_calls} "
              f"duration={elapsed:.1f}s")

        # Smoke-check the graph: PeriodicTasksRun (CommonAtServer, main source)
        # should resolve and expose several outbound calls.
        db = pm.get_db('1idm2')
        ca = CallAnalyzer(db)
        tree = ca.build_call_tree('PeriodicTasksRun', direction='down', depth=2)
        assert tree is not None, "PeriodicTasksRun should be found in the indexed graph"
        assert len(tree.children) > 0

        # Smoke-check search across the full corpus.
        engine = pm.get_search('1idm2')
        results = engine.search_metadata('Account')
        assert len(results) > 0
    finally:
        pm.close_all()
