"""
Regression test for a registry-clobbering bug found while verifying
Этап 4/D7: main.py runs the Web UI and the MCP server as two independent
ProjectManager instances against the same data_dir (see main.py's
_start_web/_start_mcp). Each caches the whole projects.json registry in
memory the first time it's loaded and never refreshes it.

reindex_async() can now run for minutes in a background thread. The old
_save_registry() dumps this *entire* cached registry back to disk on every
write — so if instance A (say, the MCP server) starts a long reindex of
project X, and instance B (the Web UI) does anything that saves the
registry for an unrelated project Y in the meantime, B's save clobbers
whatever A's reindex thread later writes for X (or vice versa) with B's
stale cached copy. Verified live: an actual bshp reindex's status='ready'
got overwritten back to 'indexing' by an unrelated save from a second
ProjectManager instance. Fixed by having reindex()'s status writes touch
only their own project's entry in the on-disk JSON (_save_project_to_disk),
re-reading the file first instead of trusting a long-cached in-memory copy
of every other project.
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager


def test_reindex_status_write_does_not_clobber_other_project_from_stale_instance():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Instance A creates two projects.
        pm_a = ProjectManager(tmpdir)
        pm_a.create_project('x', 'Project X')
        pm_a.create_project('y', 'Project Y')

        # Instance B loads the registry now — it will cache 'y' as status
        # 'empty' forever unless something makes it re-read.
        pm_b = ProjectManager(tmpdir)
        assert pm_b.get_project('y').status == 'empty'

        # Instance A "reindexes" X — simulated directly via the same narrow
        # save reindex() uses, without needing real source files.
        proj_x = pm_a.get_project('x')
        proj_x.status = 'ready'
        pm_a._save_project_to_disk('x')

        # Meanwhile A also updates Y through the normal (whole-registry)
        # path, simulating another quick CRUD call elsewhere.
        pm_a.update_project('y', status='ready')

        # Instance B — still holding 'y' as 'empty' in its own memory —
        # now does a narrow save for X (as reindex() would on completion).
        # It must NOT touch Y's on-disk entry at all.
        proj_x_b = pm_b.get_project('x')
        proj_x_b.status = 'error'
        pm_b._save_project_to_disk('x')

        # Reload from disk with a fresh instance: X reflects B's last
        # write (expected — B legitimately owns that write), Y must still
        # be 'ready' from A, not clobbered back to B's stale 'empty'.
        pm_c = ProjectManager(tmpdir)
        assert pm_c.get_project('x').status == 'error'
        assert pm_c.get_project('y').status == 'ready', \
            "Instance B's narrow save must not have clobbered Y with its stale cached copy"

        pm_a.close_all()
        pm_b.close_all()
        pm_c.close_all()


if __name__ == '__main__':
    try:
        test_reindex_status_write_does_not_clobber_other_project_from_stale_instance()
        print('  ✅ test_reindex_status_write_does_not_clobber_other_project_from_stale_instance')
    except Exception as e:
        import traceback
        print(f'  ❌ {e}')
        traceback.print_exc()
