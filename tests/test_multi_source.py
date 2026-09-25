"""
Multi-source tests: verify that when a project has multiple sources
(main + extensions) that share object names, search and details correctly
carry source_id / source_label and can be filtered by source.
"""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import SearchEngine

FIXTURE = Path(__file__).parent / 'fixtures' / 'report_en.txt'


def _build_multi_source_db(tmpdir: str) -> SearchEngine:
    """Create DB with two sources that both contain the same report (simulates duplicate Account)."""
    db_path = os.path.join(tmpdir, 'index.db')
    db = Database(db_path)
    db.connect()
    db.init_schema()
    indexer = Indexer(db)
    indexer.index_report(str(FIXTURE), source_id='main', source_label='Ядро', source_type='main')
    indexer.index_report(str(FIXTURE), source_id='ext1', source_label='access_inventory', source_type='extension')
    return SearchEngine(db)


def test_list_sources():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        sources = engine.list_sources()
        ids = {s['id'] for s in sources}
        assert ids == {'main', 'ext1'}, f"Expected main+ext1, got {ids}"
        by_id = {s['id']: s for s in sources}
        assert by_id['main']['source_type'] == 'main'
        assert by_id['ext1']['source_type'] == 'extension'
        assert by_id['main']['label'] == 'Ядро'
        engine.db.close()


def test_search_metadata_returns_source_labels():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        results = engine.search_metadata('Account', limit=20)
        # Both sources should appear
        labels = {r['source_label'] for r in results}
        assert 'Ядро' in labels
        assert 'access_inventory' in labels
        # Every row must carry source_id AND source_label
        for r in results:
            assert r.get('source_id'), f"missing source_id: {r}"
            assert r.get('source_label'), f"missing source_label: {r}"
        engine.db.close()


def test_search_metadata_with_source_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        results = engine.search_metadata('Account', source_id='ext1', limit=20)
        assert results, "Expected at least one result"
        for r in results:
            assert r['source_id'] == 'ext1'
            assert r['source_label'] == 'access_inventory'
        engine.db.close()


def test_get_object_details_auto_picks_main():
    """When source_id is not given and the same full_name exists in 'main' and 'extension',
    main source is returned first (by ORDER BY source_type)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        d = engine.get_object_details('Справочник.Account')
        assert d is not None
        assert d['source_id'] == 'main', f"Expected main, got {d['source_id']}"
        assert d['source_label'] == 'Ядро'
        # Full attributes should be present (main has 4)
        assert len(d['attributes']) == 4
        engine.db.close()


def test_get_object_details_returns_matches_list():
    """When multiple sources have the same object, 'matches' lists them all."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        d = engine.get_object_details('Справочник.Account')
        matches = d.get('matches', [])
        source_ids = {m['source_id'] for m in matches}
        assert source_ids == {'main', 'ext1'}, f"Expected both sources in matches, got {source_ids}"
        engine.db.close()


def test_get_object_details_with_explicit_source():
    """Explicit source_id selects the right one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        d_main = engine.get_object_details('Справочник.Account', source_id='main')
        d_ext = engine.get_object_details('Справочник.Account', source_id='ext1')
        assert d_main['source_id'] == 'main'
        assert d_ext['source_id'] == 'ext1'
        # Both copies should have same structure (same fixture indexed twice)
        assert len(d_main['attributes']) == len(d_ext['attributes'])
        assert d_main['object']['id'] != d_ext['object']['id'], \
            "Object IDs from different sources must differ"
        engine.db.close()


def test_get_object_details_bad_source_returns_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        d = engine.get_object_details('Справочник.Account', source_id='nonexistent')
        assert d is None
        engine.db.close()


def test_search_attributes_source_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        results = engine.search_attributes('Username', source_id='main', limit=10)
        assert results, "Expected at least one result"
        for r in results:
            assert r['source_id'] == 'main'
            assert r.get('source_label') == 'Ядро'
        engine.db.close()


def test_list_objects_source_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        objs = engine.list_objects(kind='Справочник', source_id='ext1', limit=50)
        assert objs, "Expected some catalogs in ext1"
        for o in objs:
            assert o['source_id'] == 'ext1'
            assert o['kind'] == 'Справочник'
        engine.db.close()


def test_find_references_source_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        # Without filter — both sources should show up
        refs_all = engine.find_references('AppMS', limit=50)
        src_all = {r['source_id'] for r in refs_all}
        assert {'main', 'ext1'}.issubset(src_all)
        # With filter — only main
        refs_main = engine.find_references('AppMS', source_id='main', limit=50)
        assert refs_main
        for r in refs_main:
            assert r['source_id'] == 'main'
        engine.db.close()


def test_format_output_contains_source_badge():
    """format_search_results and format_object_details should include @source notation."""
    from src.core.search import format_search_results, format_object_details
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_multi_source_db(tmpdir)
        results = engine.search_metadata('Account', limit=3)
        text = format_search_results(results)
        assert '@Ядро' in text or '@access_inventory' in text, \
            f"Source label not in output:\n{text}"

        details = engine.get_object_details('Справочник.Account')
        details_text = format_object_details(details)
        assert '@' in details_text, "Object details should show source"
        # Ambiguity warning when object is in multiple sources
        assert 'источник' in details_text.lower(), \
            "Should mention source/sources when object exists in multiple"
        engine.db.close()


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [
        test_list_sources,
        test_search_metadata_returns_source_labels,
        test_search_metadata_with_source_filter,
        test_get_object_details_auto_picks_main,
        test_get_object_details_returns_matches_list,
        test_get_object_details_with_explicit_source,
        test_get_object_details_bad_source_returns_none,
        test_search_attributes_source_filter,
        test_list_objects_source_filter,
        test_find_references_source_filter,
        test_format_output_contains_source_badge,
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
