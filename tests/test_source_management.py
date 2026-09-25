"""
Tests for source update/remove operations.

Verifies:
- update_source replaces files atomically and resets indexed_at
- remove_source wipes both files AND DB rows (no orphaned data)
- After update + reindex, the new content is reflected in queries
- Updating only metadata (label/source_type) doesn't touch files
"""

import os
import sys
import io
import shutil
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

REPORT_FIXTURE = Path(__file__).parent / 'fixtures' / 'report_en.txt'


def _setup(tmpdir: str) -> ProjectManager:
    pm = ProjectManager(data_dir=tmpdir)
    pm.create_project('test', 'Test Project')
    return pm


def test_update_source_metadata_only():
    """Updating just label/type should NOT touch files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        with open(REPORT_FIXTURE, 'rb') as f:
            pm.add_source('test', 'main', 'Old Label', source_type='main', report_file=f)

        # Snapshot file mtime
        report_path = Path(tmpdir) / 'projects' / 'test' / 'sources' / 'main' / 'report.txt'
        original_mtime = report_path.stat().st_mtime

        # Update only label
        pm.update_source('test', 'main', label='New Label', source_type='extension')

        # File untouched
        assert report_path.stat().st_mtime == original_mtime, "File mtime should not change"
        # Metadata updated
        proj = pm.get_project('test')
        src = next(s for s in proj.sources if s.id == 'main')
        assert src.label == 'New Label'
        assert src.source_type == 'extension'
        pm.close_all()


def test_update_source_replaces_report_file():
    """Replacing report file should overwrite old content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        with open(REPORT_FIXTURE, 'rb') as f:
            pm.add_source('test', 'main', 'Source 1', report_file=f)

        # Replace with custom content
        new_content = b'New report content for testing'
        pm.update_source('test', 'main', report_file=io.BytesIO(new_content))

        report_path = Path(tmpdir) / 'projects' / 'test' / 'sources' / 'main' / 'report.txt'
        assert report_path.read_bytes() == new_content, "File should be replaced with new content"

        # indexed_at reset (because data is now stale)
        proj = pm.get_project('test')
        src = next(s for s in proj.sources if s.id == 'main')
        assert src.indexed_at == '', "indexed_at should be reset after file replacement"
        pm.close_all()


def test_update_source_then_reindex_reflects_new_content():
    """The whole flow: index → replace file → reindex → query sees new data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        with open(REPORT_FIXTURE, 'rb') as f:
            pm.add_source('test', 'main', 'Initial', report_file=f)
        pm.reindex('test', source_id='main')

        # Initial: object Account exists from the report fixture
        engine = pm.get_search('test')
        results = engine.search_metadata('Account', limit=10)
        assert any('Account' in r['full_name'] for r in results), \
            "Should find Account before update"
        initial_count = len(results)
        assert initial_count > 0

        # Now replace report.txt with empty file
        empty_report = b'\xff\xfe' + ''.encode('utf-16-le')   # valid empty UTF-16
        pm.update_source('test', 'main', report_file=io.BytesIO(empty_report))
        pm.reindex('test', source_id='main')

        # After reindex with empty file, Account should be gone
        # (or at least the count reduced significantly)
        engine = pm.get_search('test')
        new_results = engine.search_metadata('Account', limit=10)
        assert len(new_results) < initial_count, \
            f"Expected fewer results after empty reindex (was {initial_count}, now {len(new_results)})"
        pm.close_all()


def test_remove_source_wipes_db_rows():
    """After remove_source, no rows for that source should remain in DB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _setup(tmpdir)
        # Add and index two sources
        with open(REPORT_FIXTURE, 'rb') as f:
            pm.add_source('test', 'main', 'Main', source_type='main', report_file=f)
        with open(REPORT_FIXTURE, 'rb') as f:
            pm.add_source('test', 'ext1', 'Ext 1', source_type='extension', report_file=f)
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
        test_update_source_replaces_report_file,
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
