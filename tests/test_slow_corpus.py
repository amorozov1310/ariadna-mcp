"""
Slow integration tests against the real corpora used to validate the
indexer/parser at ERP scale before touching the customer's actual export.

Skipped automatically (marker + skip check) when the corpus directory
isn't present locally — these depend on data/projects/{bshp,1idm2}
which are not meant to ship to every clone of this repo.

Run explicitly with:
    python -m pytest tests -q -m slow

В data/ тесты ничего не пишут: выгрузки корпуса только читаются, а реестр,
индексы и сгенерированный report.txt — во временном каталоге
(tests/corpus_sandbox.py: собственный projects.json с теми же проектами и
абсолютными путями к реальным источникам). Раньше тесты переиндексировали
data/projects на месте: оставляли гигабайтные data/projects/*/index.db
(в Docker индексы давно на томе ariadna-index), писали статус в общий
data/projects.json (прерванный прогон оставлял status='indexing') и
генерировали report.txt в каталог источника. После каждого теста
проверяется, что data/projects.json (mtime и размер), index.db* в
data/projects/*/ и report.txt источников не изменились, поэтому контейнер
на время прогона останавливать не нужно.
"""

import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.call_analyzer import CallAnalyzer
from src.core import report_generator

sys.path.insert(0, str(Path(__file__).parent))
from corpus_sandbox import assert_data_unchanged, data_snapshot, sandbox_project_manager

DATA_DIR = Path(__file__).parent.parent / 'data'
BSHP_XML = DATA_DIR / 'projects' / 'bshp' / 'sources' / 'main' / 'xml'
IDM2_DIR = DATA_DIR / 'projects' / '1idm2'

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def data_untouched():
    """После теста в data/ не изменилось ничего (см. докстринг модуля)."""
    if not (DATA_DIR / 'projects.json').exists():
        yield
        return
    before = data_snapshot(DATA_DIR)
    yield
    assert_data_unchanged(DATA_DIR, before)


@pytest.fixture
def work_dir():
    # Не tmp_path: pytest хранит три последних basetemp, а здесь гигабайты индексов.
    with tempfile.TemporaryDirectory(prefix='ariadna-slow-') as tmp:
        yield Path(tmp)


def _project_manager(project_id: str, work_dir: Path) -> ProjectManager:
    """Песочница с одним проектом из реального реестра; корпус — только чтение."""
    pm = sandbox_project_manager(DATA_DIR, [project_id], work_dir)
    try:
        pm.get_project(project_id)
    except KeyError:
        pm.close_all()
        pytest.skip(f"проекта {project_id} нет в data/projects.json")
    return pm


@pytest.mark.skipif(not BSHP_XML.exists(), reason="bshp corpus not present (data/projects/bshp/sources/main/xml)")
def test_bshp_full_reindex_bsl_graph(work_dir):
    """Full BSHP corpus (~19k .bsl files, main + ext_obmen) indexes without errors.

    report.txt was absent for bshp until Этап 5 (see IMPROVEMENT_PLAN Этап 0.5) —
    reindex now auto-generates it from the XML dump (src/core/report_generator.py)
    when the generate_config_report package is installed, so metadata is expected
    to be populated here too, not just BSL/call-graph.
    """
    pm = _project_manager('bshp', work_dir)
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
def test_1idm2_full_reindex_metadata_and_graph(work_dir):
    """Full 1idm2 corpus (main + 11 extensions) indexes metadata + BSL without errors."""
    pm = _project_manager('1idm2', work_dir)
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
