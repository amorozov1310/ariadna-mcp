"""
Реквизиты формы как источник «известных не-модулей» (Этап 7, остаток).

После всех правок Этапа 7 почти весь остаток нерезолвленных вызовов на
реальной ERP — переменные, которые вообще не присваиваются в BSL: это
реквизиты формы, объявленные в Ext/Form.xml (`<Attribute name="...">`) и
видимые во всех процедурах модуля формы. `КомпоновщикНастроек.
ПолучитьНастройки()`, `ТабДок.ПолучитьОбласть()`, `РазделыОтчета.
ПолучитьЭлементы()` и т.п. записывались как вызовы несуществующих общих
модулей.

Имя реквизита формы по факту не может быть общим модулем в контексте
формы — реквизиты формы перекрывают глобальный контекст. Исключение —
процедуры &НаСервереБезКонтекста: там контекста формы нет, и то же имя
честно указывает на общий модуль.
"""

import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.xml_walker import XMLWalker, read_form_attributes

FIXTURES = Path(__file__).parent / 'fixtures'

_FORM_NS = ('xmlns="http://v8.1c.ru/8.3/xcf/logform" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema"')


def _form_xml(attributes: str, extra: str = '') -> str:
    return (f'﻿<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Form {_FORM_NS} version="2.20">\n{extra}'
            f'\t<Attributes>\n{attributes}\t</Attributes>\n</Form>\n')


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


# ---------------------------------------------------------------------------
# read_form_attributes / XMLWalker
# ---------------------------------------------------------------------------

def test_fixture_forms_ru_and_en_expose_their_attributes():
    walker = XMLWalker()
    ru = {m.form_name: read_form_attributes(m.form_xml_path)
          for m in walker.walk(FIXTURES / 'xml_ru') if m.form_xml_path}
    en = {m.form_name: read_form_attributes(m.form_xml_path)
          for m in walker.walk(FIXTURES / 'xml_en') if m.form_xml_path}
    assert ru['ФормаЭлемента'] == {'объект'}
    assert 'список' in ru['ФормаСписка']
    assert en['ItemForm'] == {'object', 'person'}


def test_non_form_modules_have_no_form_xml_path():
    for m in XMLWalker().walk(FIXTURES / 'xml_ru'):
        if m.module_type != 'МодульФормы':
            assert m.form_xml_path == '', m.full_name


def test_only_top_level_attributes_not_columns_or_parameters():
    tmp = Path(tempfile.mkdtemp())
    try:
        form = tmp / 'Form.xml'
        _write(form, _form_xml(
            '\t\t<Attribute name="ТабДок" id="1"/>\n'
            '\t\t<Attribute name="Товары" id="2">\n'
            '\t\t\t<Columns>\n'
            '\t\t\t\t<Column name="Номенклатура" id="1"/>\n'
            '\t\t\t</Columns>\n'
            '\t\t</Attribute>\n',
            extra='\t<Parameters>\n\t\t<Parameter name="Ключ"/>\n\t</Parameters>\n'))
        assert read_form_attributes(form) == {'табдок', 'товары'}
    finally:
        shutil.rmtree(tmp)


def test_borrowed_extension_form_includes_base_form_attributes():
    tmp = Path(tempfile.mkdtemp())
    try:
        form = tmp / 'Form.xml'
        _write(form, _form_xml(
            '\t\t<Attribute name="РасшРеквизит" id="1000001"/>\n',
            extra=('\t<BaseForm version="2.20">\n'
                   '\t\t<Attributes>\n'
                   '\t\t\t<Attribute name="КомпоновщикНастроек" id="1"/>\n'
                   '\t\t</Attributes>\n'
                   '\t</BaseForm>\n')))
        assert read_form_attributes(form) == {'расшреквизит', 'компоновщикнастроек'}
    finally:
        shutil.rmtree(tmp)


def test_missing_or_broken_form_xml_gives_empty_set():
    tmp = Path(tempfile.mkdtemp())
    try:
        broken = tmp / 'Form.xml'
        _write(broken, '<Form><Attributes><Attribute name="X"')
        assert read_form_attributes(broken) == set()
        assert read_form_attributes(tmp / 'nope.xml') == set()
    finally:
        shutil.rmtree(tmp)
