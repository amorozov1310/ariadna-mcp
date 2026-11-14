"""Карточка объекта в Web UI (шаг 8 плана UI): вкладки, типы-ссылки,
реквизиты табличных частей без повторов, «Где используется», регистр и
движения документа, модули с процедурами."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

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
        pm.add_source('p1', 'main', 'Основная', xml_archive=_zip(FIXTURES / 'xml_meta_ru'),
                      xml_archive_filename='x.zip')
        pm.add_source('p1', 'code', 'Код', source_type='extension',
                      xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        pm.reindex('p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def _page(client, full_name, **params):
    q = '&'.join(f'{k}={quote(str(v))}' for k, v in params.items())
    resp = client.get(f'/projects/p1/object/{quote(full_name)}' + (f'?{q}' if q else ''))
    assert resp.status_code == 200, resp.text[:300]
    html = resp.text
    return html[html.index('<main'):html.index('</main>')]


def test_type_links_and_no_tabular_duplicates(client):
    main = _page(client, 'Документ.ПоступлениеТоваров', source_id='main')
    assert 'href="/projects/p1/object/Справочник.' in main                    # тип — ссылка
    assert 'ТЧ.' not in main.split('class="tabs"')[1].split('</table>')[0]     # шапка без ТЧ
    tables = _page(client, 'Документ.ПоступлениеТоваров', tab='tables', source_id='main')
    assert 'class="disclosure obj-ts"' in tables


def test_document_records_and_register_hints(client):
    main = _page(client, 'Документ.РеализацияТоваров', source_id='main')
    assert 'Движения по регистрам' in main and 'РегистрНакопления.' in main
    reg = _page(client, 'РегистрНакопления.ТоварыНаСкладах', source_id='main')
    assert '.Остатки(' in reg and 'Движения делают документы' in reg


def test_where_used(client):
    refs = _page(client, 'Справочник.Номенклатура', tab='refs', source_id='main')
    assert 'Документ.ПоступлениеТоваров' in refs
    assert 'НоменклатураПоставщиков' not in refs           # только точное имя в типе


def test_modules_tab_links_procedures(client):
    main = _page(client, 'Справочник.Склады', tab='modules', source_id='code')
    assert '/projects/p1/procedure?module=' in main and 'Справочник.Склады.МодульОбъекта' in main
    forms = _page(client, 'Справочник.Склады', tab='forms', source_id='code')
    assert 'во вкладке «Модули и процедуры»' in forms


def test_defined_type_value_type_links(client):
    from src.web.app import _type_links
    html = str(_type_links('СправочникСсылка.Склады, Строка(25, Переменная), CatalogRef.Units', 'p1'))
    assert html.count('<a ') == 2 and 'Строка(25, Переменная)' in html
    assert '/object/Справочник.Units' in html
    page = _page(client, 'ОпределяемыйТип.Сумма', source_id='main')
    assert 'Тип:' in page and '15, 2' in page


def test_no_empty_module_flags_line(client):
    main = _page(client, 'Справочник.Склады', source_id='main')
    assert 'Флаги модуля' not in main
