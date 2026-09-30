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
