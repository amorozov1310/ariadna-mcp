"""Страница процедуры (шаг 7 плана UI): код с номерами строк, кто вызывает и
что вызывает, переход из поиска по процедурам и по тексту кода."""

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
MODULE = 'ОбщийМодуль.ЗаказыСервер.Модуль'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture(scope='module')
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


def _main(html: str) -> str:
    return html[html.index('<main'):html.index('</main>')]


def _url(name='ОформитьЗаказ', **extra):
    from urllib.parse import urlencode
    return '/projects/p1/procedure?' + urlencode({'module': MODULE, 'name': name, **extra})


def test_code_tab(web):
    _, client = web
    main = _main(client.get(_url()).text)
    assert 'ОформитьЗаказ()' in main and 'экспорт' in main
    assert 'id="L4"' in main and 'Процедура ОформитьЗаказ' in main      # код с номерами строк
    assert 'Открыть на схеме' in main


def test_callees_and_callers_link_to_procedure_pages(web):
    _, client = web
    callees = _main(client.get(_url(tab='callees')).text)
    assert 'РассчитатьЦену()' in callees and '/procedure?module=' in callees
    from urllib.parse import urlencode
    callers = _main(client.get('/projects/p1/procedure?' + urlencode(
        {'module': 'ОбщийМодуль.СкидкиСервер.Модуль', 'name': 'РассчитатьЦену', 'tab': 'callers'})).text)
    assert 'ОформитьЗаказ()' in callers


def test_line_from_code_search_finds_procedure(web):
    _, client = web
    main = _main(client.get('/projects/p1/procedure?' + __import__('urllib.parse').parse.urlencode(
        {'module': MODULE, 'line': 5})).text)
    assert 'ОформитьЗаказ()' in main and 'code-line-hit' in main
    outside = _main(client.get('/projects/p1/procedure?' + __import__('urllib.parse').parse.urlencode(
        {'module': MODULE, 'line': 1})).text)
    assert 'вне процедур' in outside and 'code-line-hit' in outside and 'id="L1"' in outside


def test_search_results_link_here(web):
    _, client = web
    procs = client.get('/projects/p1/search?mode=procedures&q=ОформитьЗаказ').text
    assert '/projects/p1/procedure?module=' in procs
    code = client.get('/projects/p1/search?mode=code&q=ОформитьЗаказ').text
    assert '/projects/p1/procedure?module=' in code and 'line=' in code


def test_not_found_and_missing_indexed_text(web):
    pm, client = web
    main = _main(client.get(_url('НетТакой')).text)
    assert 'не найдена' in main and client.get(_url('НетТакой')).status_code == 200
    with pm.get_db('p1').conn as conn:
        conn.execute('DELETE FROM module_text')
    main = _main(client.get(_url()).text)
    assert 'Текст модуля отсутствует в индексе' in main
