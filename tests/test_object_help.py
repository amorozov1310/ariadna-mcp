"""
Встроенная справка объектов (Ext/Help/ru.html) в индексе: search_metadata находит
объект по тексту его справки, get_object_details показывает её начало.

Справка — синтетическая, дописывается к копии фикстуры xml_meta_ru во временном
каталоге (сама фикстура не меняется).
"""

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.search import format_object_details, format_search_results
from src.core.xml_metadata import help_to_text, read_help

FIXTURES = Path(__file__).parent / 'fixtures'
HELP = ('<html><head><style>p{color:red}</style></head><body>'
        '<h1>Реализация товаров</h1>'
        '<p>Документ&nbsp;отражает отгрузку оптовым покупателям. Розничная выручка магазинов '
        'сюда не попадает — её отражает отдельный отчёт кассира.</p>'
        '<p>Проведение формирует движения по складу и взаиморасчётам.</p></body></html>')


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
        dump = Path(tmpdir) / 'dump'
        shutil.copytree(FIXTURES / 'xml_meta_ru', dump)
        help_dir = dump / 'Documents' / 'РеализацияТоваров' / 'Ext' / 'Help'
        help_dir.mkdir(parents=True)
        (help_dir / 'ru.html').write_text(HELP, encoding='utf-8')
        pm = ProjectManager(str(Path(tmpdir) / 'reg'))
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Main', xml_archive=_zip(dump), xml_archive_filename='x.zip')
        pm.reindex('p1')
        try:
            yield pm.get_search('p1')
        finally:
            pm.close_all()


def test_help_to_text_drops_markup_keeps_paragraphs():
    text = help_to_text(HELP)
    assert text.splitlines()[0] == 'Реализация товаров'
    assert 'Документ отражает отгрузку' in text and 'color' not in text and '<' not in text


def test_read_help_missing_is_empty(tmp_path):
    assert read_help(tmp_path) == ''


def test_object_found_by_its_help(engine):
    rows = engine.search_metadata('где розничная выручка магазинов')
    hit = next((r for r in rows if r['full_name'] == 'Документ.РеализацияТоваров'), None)
    assert hit and 'розничн' in hit.get('help', '').casefold(), [r['full_name'] for r in rows]
    assert 'справка: «' in format_search_results([hit])


def test_card_shows_help(engine):
    details = engine.get_object_details('Документ.РеализацияТоваров')
    assert details['help'].startswith('Реализация товаров')
    brief = format_object_details(details)
    assert 'Справка: Реализация товаров' in brief and 'Розничная выручка' in brief
    other = format_object_details(engine.get_object_details('Справочник.Номенклатура'))
    assert 'Справка:' not in other
