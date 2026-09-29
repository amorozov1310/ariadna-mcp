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


# ============================================
# Гонки при первом обращении (get_db) и при правке реестра двумя экземплярами
# ============================================

import threading


def _in_threads(n: int, target) -> list:
    barrier = threading.Barrier(n)
    results: list = [None] * n
    errors: list = []

    def run(i):
        try:
            barrier.wait()
            results[i] = target(i)
        except Exception as e:          # pragma: no cover — упадёт assert ниже
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_parallel_get_db_opens_one_database():
    """Два потока пула SDK одновременно открывают проект — раньше создавались
    два Database, и writer одного терялся."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            pm.create_project('p1', 'p1')
            dbs = _in_threads(16, lambda i: pm.get_db('p1'))
            assert len({id(db) for db in dbs}) == 1
            assert pm._db_pool['p1'] is dbs[0]
        finally:
            pm.close_all()


def test_two_instances_creating_projects_in_parallel_lose_nothing():
    """Web UI и MCP — разные ProjectManager одного процесса. Каждый делал
    «перечитать → изменить → записать», и проект, созданный другим между
    перечитыванием и записью, пропадал из реестра."""
    with tempfile.TemporaryDirectory() as tmpdir:
        managers = [ProjectManager(tmpdir), ProjectManager(tmpdir)]
        try:
            per_thread = 10

            def create(i):
                pm = managers[i % 2]
                for k in range(per_thread):
                    pm.create_project(f'p{i}_{k}', f'p{i}_{k}')

            _in_threads(8, create)
            expected = {f'p{i}_{k}' for i in range(8) for k in range(per_thread)}
            fresh = ProjectManager(tmpdir)
            assert {p.id for p in fresh.list_projects()} == expected
            fresh.close_all()
        finally:
            for pm in managers:
                pm.close_all()


# ============================================
# Windows: os.replace реестра при открытом на чтение projects.json
# ============================================

import os
import pytest

from src.core import project_manager as pm_module


def _flaky_replace(monkeypatch, failures: int) -> list:
    calls = []
    real = os.replace

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, 'Процесс не может получить доступ к файлу')
        return real(src, dst)

    monkeypatch.setattr(pm_module.os, 'replace', replace)
    monkeypatch.setattr(pm_module, '_REPLACE_DELAY_SEC', 0)
    return calls


def test_registry_write_retries_permission_error(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        calls = _flaky_replace(monkeypatch, failures=3)
        pm.create_project('p1', 'p1')
        assert len(calls) == 4
        assert [p.id for p in ProjectManager(tmpdir).list_projects()] == ['p1']
        assert not list(Path(tmpdir).glob('*.tmp')), "временный файл не остался"


def test_registry_write_gives_up_after_attempts(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        calls = _flaky_replace(monkeypatch, failures=10**6)
        with pytest.raises(PermissionError):
            pm.create_project('p1', 'p1')
        assert len(calls) == pm_module._REPLACE_ATTEMPTS
        assert not list(Path(tmpdir).glob('*.tmp'))
