"""Web UI работает без интернета: ни одна страница не грузит скрипты, стили,
шрифты и картинки с внешних адресов (раньше граф брал vis-network с CDN), а
локальная копия библиотеки графа отдаётся сервером."""

import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}
# Ресурс, который браузер загрузит сам: src=, href= у link, url() в стилях.
_EXTERNAL = re.compile(r'''(?:\bsrc=|<link\b[^>]*\bhref=|url\()\s*["']?(?:https?:)?//''', re.I)

PAGES = ['/', '/projects/new', '/login', '/admin/tokens', '/admin/license',
         '/projects/p1', '/projects/p1/settings', '/projects/p1/search?q=Номенклатура',
         '/projects/p1/object/Справочник.Номенклатура', '/projects/p1/calltree',
         '/projects/p1/graph', '/projects/p1/playground', '/projects/p1/glossary',
         '/projects/p1/checks', '/projects/p1/diff']


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture(scope='module')
def client():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Main', xml_archive=_zip(FIXTURES / 'xml_meta_ru'),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


@pytest.mark.parametrize('path', PAGES)
def test_page_loads_nothing_from_outside(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    found = _EXTERNAL.findall(resp.text)
    assert not found, f'{path}: внешние ресурсы {found}'


def test_static_has_no_external_urls():
    static = Path(__file__).parent.parent / 'src' / 'web' / 'static'
    for path in list(static.glob('*.css')) + list(static.glob('vendor/*/*.css')):
        assert not _EXTERNAL.findall(path.read_text(encoding='utf-8')), path


def test_graph_library_is_served_locally(client):
    page = client.get('/projects/p1/graph').text
    for asset in ('vis-network.min.js', 'vis-network.min.css'):
        url = re.search(rf'["\'](/static/vendor/vis-network/{re.escape(asset)}[^"\']*)', page).group(1)
        resp = client.get(url)
        assert resp.status_code == 200 and len(resp.content) > 100_000, asset
    assert 'alert(' not in page                     # ошибки — сообщением на странице


def test_icons_are_served(client):
    """Знак «Лабиринт»: иконка вкладки (ICO и SVG), значок для телефона,
    /favicon.ico, который браузеры просят сами, — без 404."""
    page = client.get('/').text
    links = re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="([^"]+)"', page)
    assert len(links) == 3
    for url in links + ['/favicon.ico']:
        resp = client.get(url)
        assert resp.status_code == 200 and resp.content, url
    assert client.get('/favicon.ico').headers['content-type'] == 'image/x-icon'
    assert '#FFD02C' in client.get('/static/favicon.svg').text
