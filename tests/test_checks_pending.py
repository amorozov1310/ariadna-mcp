"""«Проверка вызовов» открывается сразу: не посчитана — «Идёт проверка…» и
расчёт в фоне; посчитана — результат; упала — причина, а не бесконечное ожидание."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Основная', xml_archive=_zip(FIXTURES / 'xml_ru'),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def test_page_opens_at_once_then_shows_result(web, monkeypatch):
    import threading
    from src.core import project_manager as pm_module
    pm, client = web
    gate = threading.Event()
    real = pm_module.ProjectManager.broken_calls

    def slow(self, *args, **kwargs):
        gate.wait(10)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pm_module.ProjectManager, 'broken_calls', slow)
    page = client.get('/projects/p1/checks').text
    assert 'Идёт проверка вызовов' in page
    assert client.get('/api/projects/p1/checks/warming').json() == {'warming': True, 'ready': False}
    gate.set()
    pm._checks_warming['p1'].join(10)
    assert client.get('/api/projects/p1/checks/warming').json()['ready'] is True
    page = client.get('/projects/p1/checks').text
    assert 'Идёт проверка' not in page and 'Метода в модуле нет' in page


def test_failure_is_shown_not_retried_forever(web, monkeypatch):
    from src.core import project_manager as pm_module
    pm, client = web

    def broken(self, *args, **kwargs):
        raise RuntimeError('индекс повреждён')

    monkeypatch.setattr(pm_module.ProjectManager, 'broken_calls', broken)
    assert 'Идёт проверка' in client.get('/projects/p1/checks').text
    pm._checks_warming['p1'].join(10)
    page = client.get('/projects/p1/checks').text
    assert 'индекс повреждён' in page and 'Идёт проверка' not in page
