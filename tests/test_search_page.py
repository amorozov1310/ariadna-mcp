"""Страница поиска (шаг 6 плана UI): одна вёрстка результатов, «Показать
ещё», список объектов по виду без запроса, подписи фильтров, пустые
состояния, подсветка найденного в коде."""

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
        pm.add_source('p1', 'main', 'Основная', xml_archive=_zip(FIXTURES / 'xml_ru'),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def _main(html: str) -> str:
    return html[html.index('<main'):html.index('</main>')]


def test_kind_without_query_lists_objects(client):
    main = _main(client.get('/projects/p1/search?kind=Справочник').text)
    links = re.findall(r'href="/projects/p1/object/(Справочник\.[^"?]+)"', main)
    assert len(links) >= 3 and 'объектов нет' not in main


def test_show_more(client, monkeypatch):
    from src.web import routes_search
    monkeypatch.setattr(routes_search, 'PAGE', 2)
    main = _main(client.get('/projects/p1/search?kind=Справочник&n=2').text)
    assert main.count('class="result"') == 2 and 'Показать ещё' in main
    more = re.search(r'href="([^"]*n=4)"', main).group(1).replace('&amp;', '&')
    assert more.count('kind=') == 1
    assert _main(client.get(more).text).count('class="result"') == 4


def test_procedures_link_and_labels(client):
    main = _main(client.get('/projects/p1/search?mode=procedures&q=Оформить').text)
    assert 'ОформитьЗаказ()' in main and '/calltree?procedure=' in main
    assert 'Только экспортные' in main and '>Модуль' in main
    assert 'экспорт' in main and 'стр.' in main
    assert 'расширение' not in main                     # одна часть — бейджей частей нет


def test_code_search_highlights_and_escapes(client):
    from src.web.app import _highlight
    assert str(_highlight('Если А < Б Тогда', 'а')) == 'Если <mark>А</mark> &lt; Б Тогд<mark>а</mark>'
    assert str(_highlight('<b>', '')) == '&lt;b&gt;'
    main = _main(client.get('/projects/p1/search?mode=code&q=ОформитьЗаказ').text)
    assert '<mark>ОформитьЗаказ</mark>' in main and 'стр.' in main


def test_empty_states(client):
    start = _main(client.get('/projects/p1/search').text)
    assert 'Например' in start and 'Вид объекта' in start
    none = _main(client.get('/projects/p1/search?mode=procedures&q=НетТакогоИмени').text)
    assert 'Ничего не найдено' in none and 'поискать в тексте кода' in none
    assert ' мс<' not in none                          # отладочных миллисекунд нет


def test_tabs_keep_query(client):
    main = _main(client.get('/projects/p1/search?q=Заказ&mode=code').text)
    tabs = main[main.index('class="tabs"'):main.index('</nav>')]
    assert tabs.count('q=') == 3 and 'aria-current="page">Текст кода' in tabs
