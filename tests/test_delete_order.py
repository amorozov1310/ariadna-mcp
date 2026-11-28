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
import stat
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core import project_manager as pm_module
from src.core.fs_safety import is_link_like
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
    pm.add_source('p1', 'main', 'Main', xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def test_project_leaves_registry_before_index_is_closed(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed(os.path.join(tmpdir, 'data'), os.path.join(tmpdir, 'index'))
        seen = []
        real = ProjectManager._retire_project_db

        def spy(self, project_id):
            # Свежий экземпляр читает реестр с диска.
            seen.append([p.id for p in ProjectManager(str(self.data_dir)).list_projects()])
            return real(self, project_id)

        monkeypatch.setattr(ProjectManager, '_retire_project_db', spy)
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
            assert pm.delete_project('p1') is False  # повторное удаление идемпотентно
        finally:
            pm.close_all()


def test_retry_after_report_archive_removal_failure_cleans_unregistered_project(monkeypatch, tmp_path):
    data_dir = tmp_path / 'data'
    pm = ProjectManager(str(data_dir))
    pm.create_project('p', 'p')
    from src.core.platform_check import report_directory
    report_dir = report_directory(pm._index_path('p'))
    report_dir.mkdir(parents=True)
    archive = report_dir / 'platform-check.zip'
    archive.write_bytes(b'archive')
    real_remove = pm_module.remove_path_safely
    fail_once = {True}

    def busy_report(path, *args, **kwargs):
        if Path(path) == report_dir and fail_once:
            fail_once.clear()
            raise PermissionError(13, 'файл занят другим процессом', str(path))
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(pm_module, 'remove_path_safely', busy_report)
    try:
        with pytest.raises(ProjectFilesNotRemovedError):
            pm.delete_project('p')
        assert 'p' not in [project.id for project in pm.list_projects()]
        assert archive.exists()
        # Состояние незавершённого удаления переживает перезапуск и удерживает ID.
        restarted = ProjectManager(str(data_dir))
        with pytest.raises(ValueError, match='ещё удаляется'):
            restarted.create_project('p', 'new')
        assert restarted.delete_project('p') is True
        assert not report_dir.exists()
        assert not archive.exists()
        assert restarted.create_project('p', 'new').id == 'p'
        restarted.close_all()
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


def test_web_api_does_not_report_delete_success_when_helper_returns_false(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory(dir=tmp_path) as tmpdir:
        pm = ProjectManager(str(Path(tmpdir) / 'data'))
        pm.create_project('p', 'p')
        from src.core.platform_check import report_directory
        report_dir = report_directory(pm._index_path('p'))
        report_dir.mkdir(parents=True)
        archive = report_dir / 'platform-check-test.zip'
        archive.write_bytes(b'archive')
        monkeypatch.setattr(app_module, '_pm', pm)
        real_remove = pm_module.remove_path_safely
        monkeypatch.setattr(pm_module, 'remove_path_safely', lambda *a, **kw: False)
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        try:
            for _ in range(3):
                response = client.delete('/api/projects/p')
                assert response.status_code == 500
                assert 'удал' in response.json()['detail']
                assert archive.exists()
            monkeypatch.setattr(pm_module, 'remove_path_safely', real_remove)
            assert client.delete('/api/projects/p').status_code == 200
            assert not archive.exists()
        finally:
            pm.close_all()


@pytest.mark.parametrize('method, path', [
    ('delete', '/api/projects/'),
    ('delete', '/api/projects//'),
    ('post', '/api/projects//delete'),
    ('post', '/api/projects//delete/'),
])
def test_empty_project_ids_on_both_delete_routes_are_bad_request(method, path):
    """Матрица пустого ID: со слэшем и без, JSON и форма — всегда 400 с
    объяснением, без 307-редиректа (405/404)."""
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'},
                        follow_redirects=False)
    response = getattr(client, method)(path)
    assert response.status_code == 400
    assert 'location' not in response.headers
    assert 'ID проекта' in response.json()['detail']


def test_injected_removal_reason_reaches_delete_responses(monkeypatch, tmp_path):
    """Причина отказа общего helper'а доходит до ответа без подмены — и для
    JSON API, и для формы, и для каталога проекта."""
    from fastapi.testclient import TestClient
    from src.core import fs_safety, platform_check
    from src.web import app as app_module

    pm = ProjectManager(str(tmp_path / 'data'))
    pm.create_project('p', 'p')
    pm.create_project('q', 'q')
    temp_archive = pm._index_path('p').parent / '.platform-check-x.zip.1.tmp'
    temp_archive.write_bytes(b'tmp')
    project_dir_q = pm.projects_dir / 'q'
    monkeypatch.setattr(app_module, '_pm', pm)
    real_remove = fs_safety.remove_path_safely

    def injected(path, *args, **kwargs):
        if Path(path) in (temp_archive, project_dir_q):
            return fs_safety.RemovalResult(False, 'INJECTED_BUSY')
        return real_remove(path, *args, **kwargs)

    # Временный архив чистит platform_check, каталог проекта — project_manager:
    # отказ должен дойти от обеих точек.
    monkeypatch.setattr(platform_check, 'remove_path_safely', injected)
    monkeypatch.setattr(pm_module, 'remove_path_safely', injected)
    client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
    try:
        for method, url in (('delete', '/api/projects/p'), ('post', '/api/projects/p/delete')):
            response = getattr(client, method)(url)
            assert response.status_code == 500, response.text
            assert 'INJECTED_BUSY' in response.json()['detail']
        assert temp_archive.exists()
        response = client.delete('/api/projects/q')
        assert response.status_code == 500, response.text
        assert 'INJECTED_BUSY' in response.json()['detail']
        assert project_dir_q.exists()
    finally:
        pm.close_all()


def test_delete_does_not_follow_project_symlink(tmp_path):
    data_dir = tmp_path / 'data'
    projects_dir = data_dir / 'projects'
    pm = ProjectManager(str(data_dir), str(projects_dir))
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'index.db').write_text('index')
    (outside / 'platform-check-report.zip').write_text('archive')
    (outside / 'other.txt').write_text('keep')
    project_dir = pm.projects_dir / 'p'
    try:
        pm.create_project('p', 'p')
        shutil.rmtree(project_dir)
        try:
            project_dir.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            if os.name not in {'nt', 'posix'}:
                pytest.skip(f'directory symlinks are unsupported: {exc}')
            pytest.fail(f'directory symlink creation failed on supported platform: {exc}')

        assert pm.delete_project('p') is True
        assert not project_dir.is_symlink()
        assert sorted(p.name for p in outside.iterdir()) == [
            'index.db', 'other.txt', 'platform-check-report.zip']
    finally:
        pm.close_all()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction нужен для проверки границы проекта')
def test_intermediate_junction_to_other_project_blocks_cleanup(tmp_path):
    from src.core.fs_safety import directory_is_safe, remove_path_safely
    projects = tmp_path / 'projects'
    own = projects / 'own'
    other = projects / 'other'
    target = other / 'shared'
    target.mkdir(parents=True)
    nested = target / 'nested'
    nested.mkdir()
    marker = nested / 'keep.txt'
    marker.write_text('keep')
    own.mkdir(parents=True)
    junction = own / 'hop'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
    guarded = (other,)
    assert not directory_is_safe(junction / 'nested', projects, protected_roots=guarded)
    removal = remove_path_safely(junction / 'nested', projects, protected_roots=guarded)
    assert not removal
    assert marker.read_text() == 'keep'


def test_unknown_windows_reparse_tag_is_link_like(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.core import fs_safety
    info = SimpleNamespace(st_file_attributes=0x400, st_reparse_tag=0x9000001A,
                           st_mode=stat.S_IFDIR)
    monkeypatch.setattr(fs_safety.os, 'lstat', lambda _path: info)
    assert fs_safety.is_link_like(tmp_path / 'unknown-reparse')


def test_startup_report_temp_cleanup_does_not_follow_symlink(tmp_path):
    data_dir = tmp_path / 'data'
    pm = ProjectManager(str(data_dir))
    try:
        pm.create_project('p', 'p')
        report_dir = pm._index_path('p').parent.parent / '.ariadna-service' / 'reports' / 'p'
    finally:
        pm.close_all()

    outside = tmp_path / 'outside-reports'
    outside.mkdir()
    temp_archive = outside / '.platform-check-crash.zip.1.tmp'
    temp_archive.write_text('keep')
    cached_archive = outside / 'platform-check.zip'
    cached_archive.write_text('keep')
    report_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name not in {'nt', 'posix'}:
            pytest.skip(f'directory symlinks are unsupported: {exc}')
        pytest.fail(f'directory symlink creation failed on supported platform: {exc}')

    restarted = ProjectManager(str(data_dir))
    try:
        from src.core.platform_check import prune_report_archives

        assert temp_archive.read_text() == 'keep'
        prune_report_archives(restarted._index_path('p'))
        assert cached_archive.read_text() == 'keep'
        assert restarted.delete_project('p') is True
        assert not report_dir.is_symlink()
        assert temp_archive.read_text() == 'keep'
    finally:
        restarted.close_all()


def test_delete_skips_report_path_resolved_outside_service_root(tmp_path):
    data_dir = tmp_path / 'data'
    pm = ProjectManager(str(data_dir))
    try:
        pm.create_project('p', 'p')
        report_dir = pm._index_path('p').parent.parent / '.ariadna-service' / 'reports' / 'p'
    finally:
        pm.close_all()

    outside_reports = tmp_path / 'outside-reports'
    outside_project_reports = outside_reports / 'p'
    outside_project_reports.mkdir(parents=True)
    archive = outside_project_reports / 'platform-check.zip'
    archive.write_text('keep')
    report_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_dir.parent.symlink_to(outside_reports, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name not in {'nt', 'posix'}:
            pytest.skip(f'directory symlinks are unsupported: {exc}')
        pytest.fail(f'directory symlink creation failed on supported platform: {exc}')

    restarted = ProjectManager(str(data_dir))
    try:
        assert restarted.delete_project('p') is True
        assert archive.read_text() == 'keep'
        assert report_dir.parent.is_symlink()
    finally:
        restarted.close_all()


def test_web_delete_returns_explicit_not_found_result(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    pm = ProjectManager(str(tmp_path))
    pm.create_project('p', 'p')
    monkeypatch.setattr(app_module, '_pm', pm)
    client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
    try:
        assert client.delete('/api/projects/p').json()['status'] == 'deleted'
        response = client.delete('/api/projects/p')
        assert response.status_code == 404
        assert response.json() == {'status': 'not_found', 'project_id': 'p'}
    finally:
        pm.close_all()


def test_missing_reserved_project_returns_404_without_removing_legacy_help(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    data_dir, index_dir = tmp_path / 'data', tmp_path / 'index'
    legacy_help = index_dir / 'platform-help'
    marker = legacy_help / '8.3.27.1' / 'help.marker'
    marker.parent.mkdir(parents=True)
    marker.write_text('service data', encoding='utf-8')
    pm = ProjectManager(str(data_dir), index_dir=str(index_dir))
    monkeypatch.setattr(app_module, '_pm', pm)
    client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
    try:
        json_response = client.delete('/api/projects/platform-help')
        form_response = client.post('/api/projects/platform-help/delete', follow_redirects=False)
        assert json_response.status_code == 404
        assert form_response.status_code == 404
        assert marker.read_text(encoding='utf-8') == 'service data'
    finally:
        pm.close_all()


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction regression')
def test_delete_does_not_follow_project_junction(tmp_path):
    import subprocess

    data_dir = tmp_path / 'data'
    projects_dir = data_dir / 'projects'
    pm = ProjectManager(str(data_dir), str(projects_dir))
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'index.db').write_text('index')
    (outside / 'platform-check-report.zip').write_text('archive')
    (outside / 'other.txt').write_text('keep')
    project_dir = pm.projects_dir / 'p'
    try:
        pm.create_project('p', 'p')
        shutil.rmtree(project_dir)
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(project_dir), str(outside)],
                                capture_output=True, text=True)
        if result.returncode:
            pytest.fail(f'junction creation failed on Windows: {result.stderr or result.stdout}')

        assert is_link_like(project_dir)
        assert pm.delete_project('p') is True
        assert not os.path.lexists(project_dir)
        assert sorted(p.name for p in outside.iterdir()) == [
            'index.db', 'other.txt', 'platform-check-report.zip']
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
    """Воспроизведение: потоки MCP в цикле читают проект, Web UI (тот же
    ProjectManager процесса) его удаляет. После остановки чтения каталога
    индекса нет, а в
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
            pm = _indexed(data_dir, index_dir)
            index_parent = pm._index_path('p1').parent
            stop, warmed = threading.Event(), threading.Event()
            reads = {'ok': 0, 'refused': 0}
            errors = []

            def read():
                while not stop.is_set():
                    try:
                        pm.get_search('p1').search_procedures('Рассчитать')
                        pm.get_db('p1').get_stats()
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
                pm.delete_project('p1')
            finally:
                stop.set()
                for t in threads:
                    t.join()
                pm.close_all()
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


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction regression')
def test_service_directory_junction_never_expands_trusted_root(tmp_path):
    import subprocess

    data_dir = tmp_path / 'data'
    pm = ProjectManager(str(data_dir))
    pm.create_project('p', 'p')
    service_dir = pm._index_path('p').parent.parent / '.ariadna-service'
    pm.close_all()

    outside_service = tmp_path / 'outside-service'
    outside_report = outside_service / 'reports' / 'p'
    outside_report.mkdir(parents=True)
    archive = outside_report / 'platform-check.zip'
    archive.write_bytes(b'keep')
    service_dir.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(service_dir), str(outside_service)],
                            capture_output=True, text=True)
    if result.returncode:
        pytest.fail(f'junction creation failed on Windows: {result.stderr or result.stdout}')

    restarted = ProjectManager(str(data_dir))
    try:
        assert restarted.delete_project('p') is True
        assert archive.read_bytes() == b'keep'
        assert is_link_like(service_dir)
    finally:
        restarted.close_all()


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


def test_connection_taken_before_retire_reports_index_removed():
    """Гонка из test_delete_under_mcp_load_leaves_no_index: поток получил
    соединение из read_conn, delete_project списал Database (retire), и
    только потом поток выполнил запрос. Раньше — ProgrammingError «Cannot
    operate on a closed database» (агенту — непредвиденная ошибка), теперь
    IndexRemovedError, как у любого запроса, заставшего удаление."""
    from src.core.db import Database, IndexRemovedError
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'index.db'))
        db.connect()
        db.init_schema()
        conn = db.read_conn()
        db.retire()
        with pytest.raises(IndexRemovedError):
            conn.execute("SELECT 1")


def test_connection_closed_without_retire_keeps_programming_error():
    """Обычный close() (не удаление) — прежняя ошибка, проект не «удалён»."""
    from src.core.db import Database, IndexRemovedError
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'index.db'))
        db.connect()
        db.init_schema()
        conn = db.read_conn()
        db.close()
        with pytest.raises(sqlite3.ProgrammingError) as exc:
            conn.execute("SELECT 1")
        assert not isinstance(exc.value, IndexRemovedError)


def test_delete_removes_legacy_index_residue_but_preserves_other_project(tmp_path):
    pm = ProjectManager(str(tmp_path / 'data'), index_dir=str(tmp_path / 'index'))
    pm.create_project('other', 'Other')
    legacy_index = pm.projects_dir / 'legacy' / 'index.db'
    legacy_index.parent.mkdir(parents=True)
    legacy_index.write_bytes(b'legacy index residue')
    other_project_dir = pm.projects_dir / 'other'
    other_index = pm._index_path('other')
    other_index.parent.mkdir(parents=True, exist_ok=True)
    other_index.write_bytes(b'other index')
    try:
        assert pm.delete_project('legacy') is True
        assert not legacy_index.exists()
        assert other_project_dir.is_dir()
        assert other_index.read_bytes() == b'other index'
        assert [project.id for project in pm.list_projects()] == ['other']
    finally:
        pm.close_all()
