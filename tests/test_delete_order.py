"""
Удаление проекта не пересоздаёт его индекс.

Вживую (Docker): четыре потока MCP в цикле опрашивали zz_check, Web UI его
удалил — через 2 с проекта не было ни в реестре, ни в data/projects/, но
в 4 прогонах из 5 на томе оставался /index/zz_check/index.db: новый
пустой индекс, созданный во время удаления. Пока шли закрытие индекса и
rmtree (секунды), проект ещё был в реестре, и запрос MCP через get_db
создавал индекс заново.
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core import project_manager as pm_module
from src.core.project_manager import ProjectFilesNotRemovedError, ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'


def _indexed(data_dir: str, index_dir: str | None) -> ProjectManager:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    pm = ProjectManager(data_dir, index_dir)
    pm.create_project('p1', 'p1')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def test_project_leaves_registry_before_index_is_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        seen = []
        real = ProjectManager._close_project_db_everywhere

        def spy(self, project_id):
            # Свежий экземпляр читает реестр с диска.
            seen.append([p.id for p in ProjectManager(str(self.data_dir)).list_projects()])
            return real(self, project_id)

        monkeypatch.setattr(ProjectManager, '_close_project_db_everywhere', spy)
        try:
            pm.delete_project('p1')
            assert seen == [[]]
        finally:
            pm.close_all()


def test_failed_file_removal_reports_path_and_retry_cleans_up(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        index_dir = os.path.join(tmpdir, 'index')
        pm = _indexed(os.path.join(tmpdir, 'data'), index_dir)
        index_parent = pm._index_path('p1').parent
        real_rmtree = shutil.rmtree

        def busy(path, *a, **kw):
            if Path(path) == index_parent:
                raise PermissionError(13, 'файл занят другим процессом', str(path))
            return real_rmtree(path, *a, **kw)

        monkeypatch.setattr(pm_module.shutil, 'rmtree', busy)
        try:
            with pytest.raises(ProjectFilesNotRemovedError) as exc:
                pm.delete_project('p1')
            assert str(index_parent) in str(exc.value)
            assert 'p1' not in [p.id for p in pm.list_projects()]
            assert index_parent.exists()

            monkeypatch.setattr(pm_module.shutil, 'rmtree', real_rmtree)
            pm.delete_project('p1')                 # повтор дочищает
            assert not index_parent.exists()
            assert not (pm.projects_dir / 'p1').exists()
            with pytest.raises(KeyError):
                pm.delete_project('p1')             # удалять больше нечего
        finally:
            pm.close_all()


def test_web_ui_reports_failed_removal_as_500(monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed(os.path.join(tmpdir, 'data'), None)
        monkeypatch.setattr(app_module, '_pm', pm)

        def busy(path, *a, **kw):
            raise PermissionError(13, 'файл занят другим процессом', str(path))

        real_rmtree = shutil.rmtree
        monkeypatch.setattr(pm_module.shutil, 'rmtree', busy)
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        try:
            r = client.delete('/api/projects/p1')
            assert r.status_code == 500, r.text
            assert 'Повторите удаление' in r.json()['detail']
            assert str(pm.projects_dir / 'p1') in r.json()['detail']
            monkeypatch.setattr(pm_module.shutil, 'rmtree', real_rmtree)
            assert client.delete('/api/projects/p1').status_code == 200
            assert not (pm.projects_dir / 'p1').exists()
        finally:
            pm.close_all()


# ============================================
# Чтение не создаёт индекс как побочный эффект
# ============================================

def _index_files(index_parent: Path) -> list[str]:
    return sorted(p.name for p in index_parent.glob('index.db*')) if index_parent.exists() else []


def _open_handles_under(path: Path) -> list[str]:
    """Файлы под path, открытые этим процессом (Linux, /proc/self/fd). На
    Windows открытый файл не удалить (WinError 32) — здесь та же проверка."""
    fd_dir = Path('/proc/self/fd')
    found = []
    for fd in fd_dir.iterdir() if fd_dir.is_dir() else ():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(str(path)):
            found.append(target)
    return found


def test_delete_under_read_load_leaves_no_index(monkeypatch):
    """Воспроизведение: второй экземпляр (MCP) в цикле читает проект, первый
    (Web UI) его удаляет. После остановки чтения каталога индекса нет, а в
    момент rmtree на файлы каталога не открыто ни одного дескриптора (на
    Windows с ними rmtree падает)."""
    real_rmtree = shutil.rmtree
    held = []

    def checked_rmtree(path, *a, **kw):
        held.extend(_open_handles_under(Path(path)))
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(pm_module.shutil, 'rmtree', checked_rmtree)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir, index_dir = os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index')
        for run in range(20):
            web = _indexed(data_dir, index_dir)
            mcp = ProjectManager(data_dir, index_dir)
            index_parent = web._index_path('p1').parent
            stop, warmed = threading.Event(), threading.Event()
            reads = {'ok': 0, 'refused': 0}
            errors = []

            def read():
                while not stop.is_set():
                    try:
                        mcp.get_search('p1').search_procedures('Рассчитать')
                        mcp.get_db('p1').get_stats()
                        reads['ok'] += 1
                        if reads['ok'] >= 2:
                            warmed.set()
                    except (KeyError, sqlite3.OperationalError, sqlite3.ProgrammingError):
                        reads['refused'] += 1
                    except Exception as e:          # pragma: no cover
                        errors.append(e)
                        warmed.set()
                        return

            threads = [threading.Thread(target=read) for _ in range(2)]
            for t in threads:
                t.start()
            assert warmed.wait(30)
            try:
                web.delete_project('p1')
            finally:
                stop.set()
                for t in threads:
                    t.join()
                web.close_all()
                mcp.close_all()
            assert not errors, errors
            assert not index_parent.exists(), \
                f"прогон {run}: остался индекс {_index_files(index_parent)}"
            assert not held, f"прогон {run}: при rmtree открыты {held}"
            assert 'p1' not in [p.id for p in ProjectManager(data_dir, index_dir).list_projects()]


def test_get_db_for_unregistered_project_creates_nothing():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        try:
            with pytest.raises(KeyError):
                pm.get_db('нет-такого')
            assert not pm._index_path('нет-такого').exists()
            assert not pm._index_path('нет-такого').parent.exists()
            with pytest.raises(KeyError):
                pm.get_search('нет-такого')
            assert not pm._index_path('нет-такого').exists()
        finally:
            pm.close_all()


@pytest.mark.parametrize('how', ['close', 'retire'])
def test_read_conn_of_closed_database_does_not_recreate_deleted_file(how):
    from src.core.db import Database
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, 'index.db')
        db = Database(path)
        db.connect()
        db.init_schema()
        db.get_stats()
        getattr(db, how)()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        with pytest.raises(sqlite3.OperationalError, match='индекс удалён'):
            db.read_conn()
        assert not os.path.exists(path)


def test_retired_database_refuses_even_while_file_exists():
    """Файл ещё не удалён (rmtree впереди), но Database списан — новое
    соединение к нему не открывается (на Windows оно помешало бы rmtree)."""
    from src.core.db import Database
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'index.db'))
        db.connect()
        db.init_schema()
        db.retire()
        with pytest.raises(sqlite3.OperationalError, match='индекс удалён'):
            db.get_stats()


def test_first_request_to_new_empty_project_still_creates_index():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        try:
            pm.create_project('new', 'new')
            assert not pm._index_path('new').exists()
            stats = pm.get_db('new').get_stats()
            assert stats['procedures'] == 0 and stats['metadata_objects'] == 0
            assert pm._index_path('new').exists()
            assert pm.get_search('new').search_procedures('x') == []
        finally:
            pm.close_all()
