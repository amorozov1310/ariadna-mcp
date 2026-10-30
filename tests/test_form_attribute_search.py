"""
Реквизиты форм в search_attributes: «где на форме индекс урожайности» —
реквизит формы из Form.xml с заголовком и типом, а не только реквизиты
объектов метаданных.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.xml_walker import read_form_attribute_details
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'
FORM = 'Справочник.Склады.Форма.ФормаЭлемента'
FORM_XML = Path('Catalogs/Склады/Forms/ФормаЭлемента/Ext/Form.xml')


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        with open(FIXTURES / 'report_ru.txt', 'rb') as f:
            manager.add_source('p1', 'main', 'Main', report_file=f,
                               xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        manager.reindex('p1')
        try:
            yield manager
        finally:
            manager.close_all()


def _attrs(pm, query, **kw):
    return execute_tool(pm, 'search_attributes', {'project_id': 'p1', 'query': query, **kw})


def _form_rows(pm) -> list[tuple]:
    return pm.get_db('p1').conn.execute(
        """SELECT m.name, fa.name, fa.title, fa.type_desc FROM form_attributes fa
           JOIN modules m ON m.id = fa.module_id ORDER BY m.name, fa.name""").fetchall()


def test_reader_gives_title_and_type():
    details = read_form_attribute_details(FIXTURES / 'xml_ru' / FORM_XML)
    by_name = {a.name: a for a in details}
    assert by_name['ПроцентЗаполнения'].title == 'Процент заполнения склада'
    assert by_name['ПроцентЗаполнения'].type_desc == 'Число(5, 2, Неотрицательный)'
    assert by_name['Объект'].type_desc == 'СправочникОбъект.Склады'
    en = read_form_attribute_details(FIXTURES / 'xml_ru' / FORM_XML, russian=False)
    assert {a.name: a.type_desc for a in en}['ПроцентЗаполнения'] == 'Number(5, 2, Nonnegative)'
    assert read_form_attribute_details(FIXTURES / 'нет.xml') is None


def test_found_by_name_title_and_words(pm):
    for query in ('ПроцентЗаполнения', 'заполнения', 'процент заполнения склада'):
        text = _attrs(pm, query)
        assert (f'{FORM}.ПроцентЗаполнения (Процент заполнения склада) '
                f'[Число(5, 2, Неотрицательный)]') in text, (query, text)


def test_object_attributes_come_first(pm):
    text = _attrs(pm, 'Склады', limit=100)
    lines = [l for l in text.splitlines() if l[:1].isdigit()]
    kinds = ['.Форма.' in l for l in lines]
    assert True in kinds and False in kinds
    assert kinds == sorted(kinds), 'реквизиты форм — после реквизитов объектов'


def test_paging_across_object_and_form_attributes(pm):
    whole = [l.split('. ', 1)[1] for l in _attrs(pm, 'Склады', limit=100).splitlines()
             if l[:1].isdigit()]
    paged = []
    offset = 0
    while True:
        text = _attrs(pm, 'Склады', limit=2, offset=offset)
        page = [l.split('. ', 1)[1] for l in text.splitlines() if l[:1].isdigit()]
        paged += page
        if 'use offset=' not in text:
            break
        offset += 2
    assert paged == whole


def test_type_filter_applies_to_form_attributes(pm):
    assert 'ПроцентЗаполнения' in _attrs(pm, 'заполнения', type_filter='Число')
    assert 'ПроцентЗаполнения' not in _attrs(pm, 'заполнения', type_filter='Строка')


def test_existing_index_is_backfilled_without_reparse(pm):
    """Индекс, построенный до таблицы form_attributes: reindex без изменений
    дочитывает реквизиты, не переразбирая модули."""
    db = pm.get_db('p1')
    before = _form_rows(pm)
    assert before
    db.conn.execute('DELETE FROM form_attributes')
    db.conn.commit()
    stats = pm.reindex('p1')
    assert _form_rows(pm) == before
    assert stats.total_procedures   # индекс на месте


def test_form_change_updates_and_removal_deletes(pm):
    xml_root = pm.get_source_xml_path('p1', 'main')
    path = xml_root / FORM_XML
    path.write_text(path.read_text(encoding='utf-8-sig').replace(
        'ПроцентЗаполнения', 'ДоляЗаполнения'), encoding='utf-8-sig')
    pm.reindex('p1')
    names = {r[1] for r in _form_rows(pm)}
    assert 'ДоляЗаполнения' in names and 'ПроцентЗаполнения' not in names

    pm.remove_source('p1', 'main')
    assert _form_rows(pm) == []
