"""
Словарь проекта (src/core/glossary.py): слова пользователей → объекты,
реквизиты, процедуры. Сопоставление по основам слов, хранение вне индекса,
поиск ставит найденное по словарю первым; инструменты MCP и Web UI.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from fixture_zip import zip_dir

from src.core.glossary import Glossary, split_terms, term_score
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.mark.parametrize('term, query, score', [
    ('пайщики', 'сколько должны пайщикам', 1),
    ('долги пайщикам', 'долг пайщику за 2025 год', 2),
    ('долги пайщикам', 'пайщики', 0),                 # нужны все слова термина
    ('НДС', 'сумма ндс по ставке', 1),
    ('остатки', 'остаток на складе', 1),
    ('остатки', 'отстающие', 0),
])
def test_term_score(term, query, score):
    assert term_score(term, query) == score


def test_split_terms():
    assert split_terms('пайщики, долги пайщикам;пайщики\n ') == ['пайщики', 'долги пайщикам']
    assert split_terms(['А', 'а', ' ']) == ['А']


def test_glossary_file_merge_replace_remove():
    with tempfile.TemporaryDirectory() as tmp:
        g = Glossary(Path(tmp) / 'p' / 'glossary.json')
        assert g.entries() == []
        e = g.save('Справочник.А', 'object', ['альфа'], note='первое', author='агент')
        e2 = g.save('справочник.а', 'object', ['бета'], author='агент')          # дописать, примечание то же
        assert e2.id == e.id and e2.terms == ['альфа', 'бета'] and e2.note == 'первое'
        e3 = g.save('Справочник.А', 'object', ['гамма'], author='Web UI', merge=False)
        assert e3.terms == ['гамма'] and e3.note == '' and e3.author == 'Web UI'
        assert [x.target for x, _ in g.match('нужна гамма')] == ['Справочник.А']
        data = json.loads((Path(tmp) / 'p' / 'glossary.json').read_text(encoding='utf-8'))
        assert data['entries'][0]['terms'] == ['гамма']
        assert g.remove(e.id).id == e.id and g.entries() == [] and g.remove(e.id) is None


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        manager.add_source('p1', 'main', 'Main', xml_archive=zip_dir(FIXTURES / 'xml_ru'),
                           xml_archive_filename='x.zip')
        manager.reindex('p1')
        try:
            yield manager
        finally:
            manager.close_all()


def _t(pm, tool, **args):
    return execute_tool(pm, tool, {'project_id': 'p1', **args})


def test_add_validates_target_and_kinds(pm):
    assert '(объект)' in _t(pm, 'add_glossary_term', target='РегистрНакопления.ТоварыНаСкладах', terms='остатки')
    assert '(реквизит)' in _t(pm, 'add_glossary_term', target='Справочник.Номенклатура.Артикул', terms='код товара')
    text = _t(pm, 'add_glossary_term', target='ЦеныСервер.РассчитатьЦену', terms='расчет цены продажи')
    assert 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену (процедура)' in text
    assert _t(pm, 'add_glossary_term', target='Справочник.НетТакого', terms='х').startswith('Error:')
    assert _t(pm, 'add_glossary_term', target='Справочник.Склады', terms=' , ').startswith('Error:')


def test_search_puts_glossary_first(pm):
    before = _t(pm, 'search_metadata', query='остатки на складах', limit=3)
    assert before.splitlines()[1].startswith('1. Справочник.Склады')
    _t(pm, 'add_glossary_term', target='РегистрНакопления.ТоварыНаСкладах', terms='остатки')
    after = _t(pm, 'search_metadata', query='остатки на складах', limit=3)
    assert after.splitlines()[1].startswith('1. РегистрНакопления.ТоварыНаСкладах')
    assert 'словарь: «остатки»' in after

    _t(pm, 'add_glossary_term', target='Справочник.Номенклатура.Артикул', terms='код товара')
    attrs = _t(pm, 'search_attributes', query='код товара')
    assert attrs.splitlines()[1].startswith('1. Справочник.Номенклатура.Артикул')

    _t(pm, 'add_glossary_term', target='ЦеныСервер.РассчитатьЦену', terms='расчет цены продажи')
    sem = _t(pm, 'semantic_search_code', query='расчет цены продажи', limit=2)
    first = next(l for l in sem.splitlines() if l.startswith('1. '))
    assert 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену' in first and 'словарь' in first


def test_details_and_context_show_entries(pm):
    _t(pm, 'add_glossary_term', target='Справочник.Номенклатура.Артикул', terms='код товара', note='на этикетке')
    assert 'Словарь, Артикул: «код товара» — на этикетке' in _t(pm, 'get_object_details', full_name='Справочник.Номенклатура')
    _t(pm, 'add_glossary_term', target='ЦеныСервер.РассчитатьЦену', terms='расчет цены продажи')
    ctx = _t(pm, 'get_procedure_context', module_name='ЦеныСервер', procedure_name='РассчитатьЦену')
    assert 'Словарь: «расчет цены продажи»' in ctx


def test_list_and_remove(pm):
    assert _t(pm, 'list_glossary') == 'Словарь пуст.'
    _t(pm, 'add_glossary_term', target='РегистрНакопления.ТоварыНаСкладах', terms='остатки')
    _t(pm, 'add_glossary_term', target='Справочник.Склады', terms='места хранения')
    assert _t(pm, 'list_glossary').count('\n[') == 1          # две записи
    found = _t(pm, 'list_glossary', query='где места хранения')
    assert 'Справочник.Склады' in found and 'ТоварыНаСкладах' not in found
    entry_id = pm.get_glossary('p1').for_target('Справочник.Склады').id
    assert _t(pm, 'remove_glossary_term', entry_id=entry_id).startswith('Удалено из словаря:')
    assert _t(pm, 'remove_glossary_term', entry_id=entry_id).startswith('Error:')


def test_glossary_survives_reindex_and_lives_outside_index(pm):
    _t(pm, 'add_glossary_term', target='РегистрНакопления.ТоварыНаСкладах', terms='остатки')
    pm.reindex('p1')
    assert 'словарь: «остатки»' in _t(pm, 'search_metadata', query='остатки', limit=1)
    assert (pm.projects_dir / 'p1' / 'glossary.json').is_file()


def test_web_ui_page_save_and_delete(pm):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        assert 'Словарь пуст' in client.get('/projects/p1/glossary').text
        r = client.post('/api/projects/p1/glossary', data={'target': 'Справочник.Склады', 'terms': 'места хранения'},
                        follow_redirects=False)
        assert r.status_code == 303 and 'saved=' in r.headers['location']
        page = client.get('/projects/p1/glossary').text
        assert 'места хранения' in page
        # Сначала список: форма скрыта до «Добавить слово»; объект — ссылка;
        # у кнопок-значков есть aria-label.
        assert 'id="glossary-form-card" hidden' in page
        assert 'href="/projects/p1/object/%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA.%D0%A1%D0%BA%D0%BB%D0%B0%D0%B4%D1%8B"' in page
        assert 'aria-label="Изменить: Справочник.Склады"' in page and 'aria-label="Удалить: Справочник.Склады"' in page
        assert 'notice notice-success' in client.get('/projects/p1/glossary?saved=x').text
        # С ошибкой или с готовым объектом — форма открыта.
        assert 'id="glossary-form-card" hidden' not in client.get('/projects/p1/glossary?error=x').text
        assert 'id="glossary-form-card" hidden' not in client.get('/projects/p1/glossary?target=x').text
        # Подсказка полного имени.
        found = client.get('/api/projects/p1/glossary/suggest', params={'q': 'Склады'}).json()
        assert any(f['value'] == 'Справочник.Склады' for f in found)
        assert client.get('/api/projects/p1/glossary/suggest', params={'q': 'С'}).json() == []
        entry = pm.get_glossary('p1').for_target('Справочник.Склады')
        # правка: термины заменяются
        client.post('/api/projects/p1/glossary', data={'target': 'Справочник.Склады', 'terms': 'склады хранения',
                                                      'entry_id': entry.id})
        assert pm.get_glossary('p1').get(entry.id).terms == ['склады хранения']
        r = client.post('/api/projects/p1/glossary', data={'target': 'Нет.Такого', 'terms': 'х'},
                        follow_redirects=False)
        assert 'error=' in r.headers['location']
        client.post(f'/api/projects/p1/glossary/{entry.id}/delete')
        assert pm.get_glossary('p1').entries() == []
        assert client.get('/projects/нет/glossary').status_code == 404
    finally:
        app_module._pm = previous


def test_target_link():
    from src.web.routes_glossary import target_link
    assert target_link('p', 'Справочник.X', 'object') == '/projects/p/object/%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA.X'
    assert target_link('p', 'Справочник.X.Цена', 'attribute').endswith('.X?tab=attributes')
    assert target_link('p', 'ОбщегоНазначения.Сообщить', 'procedure') == (
        '/projects/p/procedure?module=%D0%9E%D0%B1%D1%89%D0%B5%D0%B3%D0%BE%D0%9D%D0%B0%D0%B7%D0%BD%D0%B0%D1%87%D0%B5%D0%BD%D0%B8%D1%8F'
        '&name=%D0%A1%D0%BE%D0%BE%D0%B1%D1%89%D0%B8%D1%82%D1%8C')
