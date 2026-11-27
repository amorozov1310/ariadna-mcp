"""
Tests for source update/remove operations.

Verifies:
- update_source replaces the XML dump and resets indexed_at
- remove_source wipes both files AND DB rows (no orphaned data)
- After update + reindex, the new content is reflected in queries
- Updating only metadata (label/source_type) doesn't touch files
"""

import os
import sys
import shutil
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from fixture_zip import zip_dir

from src.core.project_manager import ProjectManager

META_FIXTURE = Path(__file__).parent / 'fixtures' / 'xml_meta_en'
META_FIXTURE_RU = Path(__file__).parent / 'fixtures' / 'xml_meta_ru'


def _setup(tmpdir: str) -> ProjectManager:
    pm = ProjectManager(data_dir=tmpdir)
    pm.create_project('test', 'Test Project')
    return pm


def test_update_source_metadata_only():
    """Updating just label/type should NOT touch files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        pm.add_source('test', 'main', 'Old Label', source_type='main', xml_archive=zip_dir(META_FIXTURE), xml_archive_filename='x.zip')

        # Snapshot file mtime
        conf = Path(tmpdir) / 'projects' / 'test' / 'sources' / 'main' / 'xml' / 'Configuration.xml'
        original_mtime = conf.stat().st_mtime

        # Update only label
        pm.update_source('test', 'main', label='New Label', source_type='extension')

        # File untouched
        assert conf.stat().st_mtime == original_mtime, "File mtime should not change"
        # Metadata updated
        proj = pm.get_project('test')
        src = next(s for s in proj.sources if s.id == 'main')
        assert src.label == 'New Label'
        assert src.source_type == 'extension'
        pm.close_all()


def test_update_source_replaces_xml_archive():
    """Replacing the XML dump should overwrite the old one entirely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        pm.add_source('test', 'main', 'Source 1', xml_archive=zip_dir(META_FIXTURE), xml_archive_filename='x.zip')

        pm.update_source('test', 'main', xml_archive=zip_dir(META_FIXTURE_RU), xml_archive_filename='x.zip')

        xml = Path(tmpdir) / 'projects' / 'test' / 'sources' / 'main' / 'xml'
        assert (xml / 'Configuration.xml').read_bytes() == (META_FIXTURE_RU / 'Configuration.xml').read_bytes()
        assert not (xml / 'Catalogs' / 'Users.xml').exists(), "old dump files must be gone"

        # indexed_at reset (because data is now stale)
        proj = pm.get_project('test')
        src = next(s for s in proj.sources if s.id == 'main')
        assert src.indexed_at == '', "indexed_at should be reset after file replacement"
        pm.close_all()


def test_update_source_then_reindex_reflects_new_content():
    """The whole flow: index → replace file → reindex → query sees new data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        pm.add_source('test', 'main', 'Initial', xml_archive=zip_dir(META_FIXTURE), xml_archive_filename='x.zip')
        pm.reindex('test', source_id='main')

        # Initial: object Users exists in the EN fixture
        engine = pm.get_search('test')
        results = engine.search_metadata('Users', limit=10)
        assert any('Users' in r['full_name'] for r in results), \
            "Should find Users before update"
        initial_count = len(results)
        assert initial_count > 0

        # Replace the dump with another configuration (RU fixture, no Users)
        pm.update_source('test', 'main', xml_archive=zip_dir(META_FIXTURE_RU), xml_archive_filename='x.zip')
        pm.reindex('test', source_id='main')

        engine = pm.get_search('test')
        new_results = engine.search_metadata('Users', limit=10)
        assert len(new_results) < initial_count, \
            f"Expected fewer results after empty reindex (was {initial_count}, now {len(new_results)})"
        pm.close_all()


def test_remove_source_wipes_db_rows():
    """After remove_source, no rows for that source should remain in DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        # Add and index two sources
        pm.add_source('test', 'main', 'Main', source_type='main', xml_archive=zip_dir(META_FIXTURE), xml_archive_filename='x.zip')
        pm.add_source('test', 'ext1', 'Ext 1', source_type='extension', xml_archive=zip_dir(META_FIXTURE), xml_archive_filename='x.zip')
        pm.reindex('test')

        db = pm.get_db('test')
        conn = db.conn

        # Both sources have data
        main_count = conn.execute(
            "SELECT COUNT(*) c FROM metadata_objects WHERE source_id=?",
            ('main',)).fetchone()['c']
        ext_count = conn.execute(
            "SELECT COUNT(*) c FROM metadata_objects WHERE source_id=?",
            ('ext1',)).fetchone()['c']
        assert main_count > 0
        assert ext_count > 0

        # Remove ext1
        pm.remove_source('test', 'ext1')

        # ext1 rows must be gone, main untouched
        main_after = conn.execute(
            "SELECT COUNT(*) c FROM metadata_objects WHERE source_id=?",
            ('main',)).fetchone()['c']
        ext_after = conn.execute(
            "SELECT COUNT(*) c FROM metadata_objects WHERE source_id=?",
            ('ext1',)).fetchone()['c']
        assert main_after == main_count, "Main source should be untouched"
        assert ext_after == 0, f"Ext source rows should be wiped, found {ext_after}"

        # sources table also cleaned
        src_row = conn.execute("SELECT 1 FROM sources WHERE id=?", ('ext1',)).fetchone()
        assert src_row is None

        # Files removed
        ext_dir = Path(tmpdir) / 'projects' / 'test' / 'sources' / 'ext1'
        assert not ext_dir.exists()

        # Registry updated
        proj = pm.get_project('test')
        assert not any(s.id == 'ext1' for s in proj.sources)
        pm.close_all()


def test_remove_last_source_leaves_no_searchable_procedures():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        xml_fixture = Path(__file__).parent / 'fixtures' / 'xml_ru'
        pm.add_source('test', 'main', 'Main', xml_archive=zip_dir(xml_fixture),
                      xml_archive_filename='x.zip')
        pm.reindex('test')
        assert pm.get_db('test').get_stats()['procedures'] > 0

        pm.remove_source('test', 'main')

        assert pm.get_db('test').get_stats()['procedures'] == 0
        assert pm.get_search('test').search_procedures('') == []
        pm.close_all()


def test_remove_nonexistent_source_is_noop():
    """Removing a source that doesn't exist should not crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        # Should not raise
        pm.remove_source('test', 'nonexistent')
        pm.close_all()


def test_update_nonexistent_source_raises():
    """Updating a missing source should raise ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        try:
            pm.update_source('test', 'doesntexist', label='X')
            assert False, "Should have raised"
        except ValueError as e:
            assert 'not found' in str(e).lower()
        pm.close_all()


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [
        test_update_source_metadata_only,
        test_update_source_replaces_xml_archive,
        test_update_source_then_reindex_reflects_new_content,
        test_remove_source_wipes_db_rows,
        test_remove_nonexistent_source_is_noop,
        test_update_nonexistent_source_raises,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  ✅ {t.__name__}')
            passed += 1
        except Exception as e:
            import traceback
            print(f'  ❌ {t.__name__}: {e}')
            traceback.print_exc()
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
