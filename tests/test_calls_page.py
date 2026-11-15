"""«Вызовы» (шаг 9 плана UI): одна форма для видов «Дерево» и «Схема»,
выбор при одноимённых процедурах, дерево со ссылками на страницы процедур,
подсказка имён, схема ссылок на объект."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlencode

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


def _main(client, path, **params):
    html = client.get(f'/projects/p1/{path}?' + urlencode(params)).text
    return html[html.index('<main'):html.index('</main>')]


def test_tree_nodes_link_to_procedure_pages(client):
    main = _main(client, 'calltree', procedure='ОформитьЗаказ', direction='down', depth=3)
    assert 'class="calltree"' in main and '/projects/p1/procedure?module=' in main
    assert 'РассчитатьЦену()' in main and 'стр. 5' in main
    assert '[L' not in main and '<pre' not in main              # не текстовое дерево


def test_same_name_in_several_modules_asks_to_choose(client):
    main = _main(client, 'calltree', procedure='РассчитатьЦену', direction='up')
    assert 'несколько — выберите нужную' in main and 'class="calltree"' not in main
    assert main.count('class="result"') >= 2
    chosen = _main(client, 'calltree', procedure='РассчитатьЦену', direction='up',
                   module='ОбщийМодуль.ЦеныСервер.Модуль')
    assert 'class="calltree"' in chosen


def test_view_switch_keeps_parameters(client):
    main = _main(client, 'calltree', procedure='ОформитьЗаказ', direction='up', depth=2)
    switch = main[main.index('class="segmented"'):main.index('</nav>')]
    assert switch.count('procedure=') == 2 and switch.count('direction=up') == 2 and 'depth=2' in switch
    graph = _main(client, 'graph', procedure='ОформитьЗаказ', direction='up', depth=2)
    assert 'aria-current="page">Схема' in graph and '"direction": "up"' in graph


def test_not_found_is_inline_notice(client):
    main = _main(client, 'calltree', procedure='НетТакой')
    assert 'не найдена' in main and 'role="alert"' in main
    graph = _main(client, 'graph', procedure='НетТакой')
    assert 'не найдена' in graph and 'alert(' not in graph


def test_suggest_and_object_graph(client):
    data = client.get('/api/projects/p1/procedures/suggest?q=Оформ').json()
    assert any(d['name'] == 'ОформитьЗаказ' and d['module'] for d in data)
    assert client.get('/api/projects/p1/procedures/suggest?q=О').json() == []
    obj = _main(client, 'graph', object='Справочник.Склады')
    assert 'Ссылки на объект' in obj and 'loadReferencesGraph' in client.get(
        '/projects/p1/graph?object=Справочник.Склады').text
