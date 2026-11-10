"""
Стандартные реквизиты в get_object_details (src/core/standard_attributes.py):
есть ли Код и какой длины Наименование, иерархия (Родитель, ЭтоГруппа),
Владелец, у документа — Номер и Проведен. Без этой строки агент пишет запрос
с несуществующими «Код» и «ЭтоГруппа» и получает «Поле не найдено».
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.metadata_model import MetadataObject
from src.core.project_manager import ProjectManager
from src.core.register_info import object_properties
from src.core.standard_attributes import standard_attributes_line, standard_rows
from src.core.xml_metadata import parse_xml_dump
from src.mcp_server.tools import execute_tool

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
def xml_pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Main', xml_archive=_zip(FIXTURES / 'xml_meta_ru'),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        try:
            yield pm
        finally:
            pm.close_all()


def _standard(pm, name) -> str:
    text = execute_tool(pm, 'get_object_details', {'project_id': 'p1', 'full_name': name})
    lines = [line for line in text.splitlines() if line.startswith('Стандартные реквизиты:')]
    assert len(lines) == 1, text
    return lines[0]


def _line(kind: str, rows: list[tuple[str, str]]) -> str:
    props: dict[str, list[str]] = {}
    for key, value in rows:
        props.setdefault(key, []).append(value)
    return standard_attributes_line(kind, props)


def test_catalog_without_code(xml_pm):
    line = _standard(xml_pm, 'Справочник.Сотрудники')
    assert line == ('Стандартные реквизиты: Ссылка, ПометкаУдаления, Наименование (Строка 150), '
                    'Предопределенный, ИмяПредопределенныхДанных; кода нет; не иерархический; '
                    'без владельца; полей Код, Родитель, ЭтоГруппа, Владелец нет')


def test_hierarchical_catalog(xml_pm):
    line = _standard(xml_pm, 'Справочник.Номенклатура')
    assert 'Код (Строка 11), Наименование (Строка 150), Родитель, ЭтоГруппа' in line
    assert 'иерархия групп и элементов' in line
    assert 'Код,' not in line and 'кода нет' not in line


def test_hierarchy_of_items_has_no_is_folder(xml_pm):
    line = _standard(xml_pm, 'Справочник.ВидыНоменклатуры')
    assert ', Родитель,' in line and 'иерархия элементов' in line
    assert 'полей ЭтоГруппа, Владелец нет' in line


def test_subordinate_catalog(xml_pm):
    line = _standard(xml_pm, 'Справочник.ЕдиницыИзмерения')
    assert 'Владелец (Справочник.Номенклатура)' in line
    assert 'без владельца' not in line and 'не иерархический' in line


def test_document(xml_pm):
    line = _standard(xml_pm, 'Документ.ПоступлениеТоваров')
    assert line == ('Стандартные реквизиты: Ссылка, ПометкаУдаления, Дата, Номер (Строка 11), '
                    'Проведен, МоментВремени; полей Код, Наименование нет')


def test_card_without_standard_kind_has_no_line(xml_pm):
    text = execute_tool(xml_pm, 'get_object_details',
                        {'project_id': 'p1', 'full_name': 'РегистрНакопления.ТоварыНаСкладах'})
    assert 'Стандартные реквизиты' not in text


def test_english_dump_owners_and_lengths():
    _config, objects = parse_xml_dump(FIXTURES / 'xml_meta_en')
    units = next(o for o in objects if o.full_name == 'Справочник.Units')
    assert units.properties['Owners'] == 'Справочник.Products'
    assert ('Владелец', 'Справочник.Products') in object_properties(units)
    assert ('ДлинаКода', '9') in standard_rows(units)
    assert ('ДлинаНаименования', '50') in standard_rows(units)


def test_old_index_without_properties_shows_nothing():
    # Индекс, построенный до появления свойств: строку не выдумываем.
    assert standard_attributes_line('Справочник', {}) == ''
    assert standard_attributes_line('Справочник', {'Движения': ['X']}) == ''
    assert standard_rows(MetadataObject(properties={'CodeLength': '9'})) == []


def test_document_without_number_and_posting():
    obj = MetadataObject(kind='Документ', properties={'NumberLength': '0', 'Posting': 'Deny'})
    line = _line('Документ', standard_rows(obj))
    assert 'Номер' not in line.split(';')[0] and 'Проведен' not in line.split(';')[0]
    assert 'номера нет; не проводится; полей Номер, Проведен, Код, Наименование нет' in line


def test_other_kinds():
    coa = _line('ПланСчетов', standard_rows(MetadataObject(kind='ПланСчетов', properties={
        'CodeLength': '8', 'DescriptionLength': '120', 'OrderLength': '8',
        'MaxExtDimensionCount': '3', 'ExtDimensionTypes': 'ChartOfCharacteristicTypes.ВидыАналитики'})))
    assert 'Код (Строка 8), Наименование (Строка 120), Родитель, Порядок, Вид, Забалансовый' in coa
    assert 'субконто: до 3 (ПланВидовХарактеристик.ВидыАналитики)' in coa
    cct = _line('ПланВидовХарактеристик', standard_rows(MetadataObject(
        kind='ПланВидовХарактеристик', properties={'CodeLength': '0', 'DescriptionLength': '50',
                                                    'Hierarchical': 'true'})))
    assert 'ТипЗначения, Родитель, ЭтоГруппа' in cct and 'кода нет' in cct
    calc = _line('ПланВидовРасчета', standard_rows(MetadataObject(kind='ПланВидовРасчета', properties={
        'CodeLength': '5', 'CodeType': 'Number', 'DescriptionLength': '100',
        'DependenceOnCalculationTypes': 'OnActionPeriod', 'ActionPeriodUse': 'true'})))
    assert 'Код (Число 5)' in calc and 'ПериодДействияБазовый' in calc
    assert 'ВедущиеВидыРасчета, БазовыеВидыРасчета, ВытесняющиеВидыРасчета' in calc
    task = _line('Задача', standard_rows(MetadataObject(kind='Задача', properties={
        'NumberLength': '9', 'DescriptionLength': '0'})))
    assert 'Дата, Номер (Строка 9), Выполнена' in task and 'наименования нет' in task
