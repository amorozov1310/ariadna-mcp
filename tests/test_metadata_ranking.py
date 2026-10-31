"""
search_metadata для вопросов своими словами (помощник по данным базы):
«сколько товаров на складах», «курс валюты» — объект по основам слов
синонима и по его реквизитам; поиск по имени — как прежде.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture(scope='module')
def engine():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Main', xml_archive=_zip(FIXTURES / 'xml_meta_ru'),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        try:
            yield pm.get_search('p1')
        finally:
            pm.close_all()


def _names(engine, query, **kw) -> list[str]:
    return [r['full_name'] for r in engine.search_metadata(query, **kw)]


@pytest.mark.parametrize('query, expected', [
    ('сколько товаров осталось на складах', 'РегистрНакопления.ТоварыНаСкладах'),
    ('курс валюты на дату', 'РегистрСведений.КурсыВалют'),
    ('штрихкод товара', 'РегистрСведений.ШтрихкодыНоменклатуры'),
])
def test_question_in_own_words_finds_object(engine, query, expected):
    assert expected in _names(engine, query)[:3], _names(engine, query)


def test_object_found_by_its_attribute(engine):
    """«ИНН» — только реквизит: ни в имени, ни в синониме объектов его нет."""
    top = _names(engine, 'ИНН')[:3]
    assert {'Справочник.Контрагенты', 'Справочник.Организации'} <= set(top), top


def test_name_query_keeps_exact_object_first(engine):
    assert _names(engine, 'Номенклатура')[0] == 'Справочник.Номенклатура'
    assert _names(engine, 'Справочник.Склады')[0] == 'Справочник.Склады'
    assert _names(engine, 'Склады', kind='Catalog')[0] == 'Справочник.Склады'
    assert all(n.startswith('Документ.') for n in _names(engine, 'товары', kind='Документ'))


def test_paging_is_consistent(engine):
    whole = _names(engine, 'товары на складе', limit=12)
    paged = [n for off in range(0, 12, 4)
             for n in _names(engine, 'товары на складе', limit=4, offset=off)]
    assert paged == whole and len(whole) == len(set(whole))


def test_nothing_found_falls_back_to_like(engine):
    # Ни одно слово не совпадает как префикс — остаётся резервный LIKE.
    assert 'Справочник.Номенклатура' in _names(engine, 'менклатур')


def test_question_words_are_ignored(engine):
    """«сколько», «где», «какие» не ищутся: как префиксы они совпадают с
    чужими именами, а предмет вопроса не уточняют."""
    assert _names(engine, 'сколько товаров на складах')[:3] == _names(engine, 'товаров на складах')[:3]
    assert 'РегистрСведений.КурсыВалют' in _names(engine, 'где хранится курс валюты')[:3]


def test_unknown_phrase_gives_nothing_instead_of_noise(engine):
    """Фразе без совпадений — пустой ответ: резервный LIKE по обрезанной
    фразе («сколько тел» → «ско») находил случайное."""
    assert _names(engine, 'сколько телят родилось') == []
    assert 'Справочник.Номенклатура' in _names(engine, 'менклатур')   # одно слово — как раньше


def test_synonym_equal_to_query_counts_as_exact(engine):
    assert _names(engine, 'Курсы валют')[0] == 'РегистрСведений.КурсыВалют'
    assert _names(engine, 'товары на складах')[0] == 'РегистрНакопления.ТоварыНаСкладах'
