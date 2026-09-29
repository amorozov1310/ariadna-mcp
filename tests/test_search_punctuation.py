"""
Пунктуация в поисковом запросе (Б1).

Самые естественные запросы агента — «Справочник.Номенклатура»,
«ОбщегоНазначения.СообщитьПользователю», «Запрос.Текст =» — уходили в FTS5
MATCH как голые слова с точкой/`=`, и fts5 падал с синтаксической ошибкой
(`fts5: syntax error near "."`) раньше, чем срабатывал резервный поиск.
Теперь запрос разбивается на слова так же, как токенизатор unicode61, и
каждое слово передаётся строкой с префиксом, а ошибка fts5 ведёт в
резервный путь.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.project_manager import ProjectManager
from src.core.search import SearchEngine
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


def _fts(query: str) -> str:
    return SearchEngine.__new__(SearchEngine)._fts_query(query)


@pytest.mark.parametrize('query, expected', [
    ('Номенклатура', '"Номенклатура"*'),
    ('Справочник.Номенклатура', '"Справочник"* "Номенклатура"*'),
    ('ОбщегоНазначения.СообщитьПользователю', '"ОбщегоНазначения"* "СообщитьПользователю"*'),
    ('Запрос.Текст =', '"Запрос"* "Текст"*'),
    ('Контрагент.', '"Контрагент"*'),
    ('a:b, c/d & e', '"a"* "b"* "c"* "d"* "e"*'),
    ('Имя_С_Подчёркиванием', '"Имя_С_Подчёркиванием"*'),
    ('"кавычки" (скобки) NOT -минус', '"кавычки"* "скобки"* "NOT"* "минус"*'),
    ('', '""'),
    ('   ', '""'),
    ('.,=:;/&', '""'),
])
def test_fts_query_splits_like_unicode61(query, expected):
    assert _fts(query) == expected


@pytest.fixture(scope='module')
def pm():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_en').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_en'))
    buf.seek(0)
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        with open(FIXTURES / 'report_en.txt', 'rb') as f:
            manager.add_source('p1', 'main', 'Main', report_file=f,
                               xml_archive=buf, xml_archive_filename='x.zip')
        manager.reindex('p1')
        try:
            yield manager
        finally:
            manager.close_all()


def _run(pm, tool: str, query: str) -> str:
    return execute_tool(pm, tool, {'project_id': 'p1', 'query': query})


def test_search_metadata_dotted_full_name(pm):
    text = _run(pm, 'search_metadata', 'Справочник.Account')
    assert not text.startswith('Error'), text
    first_line = text.splitlines()[1]
    assert first_line.startswith('1. Справочник.Account '), text


def test_search_metadata_with_equals(pm):
    text = _run(pm, 'search_metadata', 'Account =')
    assert not text.startswith('Error'), text
    assert 'Справочник.Account' in text, text


def test_search_code_dotted_and_equals(pm):
    for query in ('SessionParameters.OTelSettings', 'OTelSettings = SessionParameters'):
        text = _run(pm, 'search_code', query)
        assert not text.startswith('Error'), text
        assert 'OTelSettings = SessionParameters.OTelSettings' in text, text


def test_search_attributes_dotted_type_and_equals(pm):
    for query in ('CatalogRef.Role', 'Role =', 'Role.'):
        text = _run(pm, 'search_attributes', query)
        assert not text.startswith('Error'), (query, text)
        assert 'CatalogRef.Role' in text, (query, text)

