"""Tests for SearchEngine."""

import sys, os, shutil
import pytest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import SearchEngine, format_search_results, format_object_details

FIXTURE = str(Path(__file__).parent / 'fixtures' / 'report_ru.txt')
TD = '/tmp/test_search'

_last_db = None


def _engine():
    """Build a fresh SearchEngine, closing the previous one's connection first
    (Windows locks open sqlite files, so shutil.rmtree would fail otherwise)."""
    global _last_db
    if _last_db is not None:
        _last_db.close()
        _last_db = None
    if os.path.exists(TD):
        shutil.rmtree(TD)
    db = Database(f'{TD}/s.db')
    db.connect()
    db.init_schema()
    Indexer(db).index_report(FIXTURE, source_id='main')
    _last_db = db
    return SearchEngine(db)


@pytest.fixture(scope='module', autouse=True)
def _close_last_db_at_module_end():
    yield
    global _last_db
    if _last_db is not None:
        _last_db.close()
        _last_db = None
    if os.path.exists(TD):
        shutil.rmtree(TD)


def test_search_fts():
    e = _engine()
    r = e.search_metadata('Удобрен')
    assert len(r) > 5
    assert any('Удобрен' in x['full_name'] for x in r)


def test_search_like_fallback():
    e = _engine()
    r = e.search_metadata('Поле')
    assert len(r) > 0, "LIKE fallback should find results for 'Поле'"


def test_search_by_kind():
    e = _engine()
    r = e.search_metadata('Культуры', kind='Справочник')
    assert len(r) >= 1
    assert all(x['kind'] == 'Справочник' for x in r)


def test_search_attributes():
    e = _engine()
    r = e.search_attributes('Культура')
    assert len(r) > 0


def test_object_details():
    e = _engine()
    d = e.get_object_details('Справочник.ВредоносныеОбъекты')
    assert d is not None
    assert d['object']['kind'] == 'Справочник'
    assert len(d['attributes']) > 5
    assert len(d['forms']) == 3


def test_object_details_register():
    e = _engine()
    d = e.get_object_details('РегистрСведений.ЦеныСемян')
    assert d is not None
    dims = [a for a in d['attributes'] if a['kind'] == 'Измерение']
    res = [a for a in d['attributes'] if a['kind'] == 'Ресурс']
    assert len(dims) > 0
    assert len(res) > 0


def test_object_details_module():
    e = _engine()
    d = e.get_object_details('ОбщийМодуль.ОбщиеНаСервере')
    assert d is not None
    assert d['module'] is not None
    assert d['module']['is_server'] == 1


def test_find_references():
    e = _engine()
    r = e.find_references('Культуры')
    assert len(r) > 5
    assert any('СправочникСсылка.Культуры' in x['type_desc'] for x in r)


def test_list_objects():
    e = _engine()
    assert len(e.list_objects()) == 100  # default limit
    assert len(e.list_objects(limit=200)) > 100
    assert len(e.list_objects(kind='Справочник')) == 42


def test_format_search():
    e = _engine()
    text = format_search_results(e.search_metadata('Семена'))
    assert 'Found' in text
    assert 'Справочник.Семена' in text


def test_format_details():
    e = _engine()
    text = format_object_details(e.get_object_details('Справочник.ВредоносныеОбъекты'))
    assert 'ВредоносныеОбъекты' in text
    assert 'Реквизиты' in text


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  ✅ {name}')
            except Exception as e:
                print(f'  ❌ {name}: {e}')
    if os.path.exists(TD):
        shutil.rmtree(TD)
