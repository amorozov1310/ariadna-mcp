"""
Сведения для запроса к данным в get_object_details (src/core/register_info.py):
вид регистра и виртуальные таблицы, периодичность, движения документа и
документы-регистраторы — из XML-выгрузки и, без движений, из отчёта.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.register_info import object_properties, query_hints
from src.core.report_parser import MetadataObject
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


def _pm(tmpdir: str, **source) -> ProjectManager:
    pm = ProjectManager(tmpdir)
    pm.create_project('p1', 'p1')
    pm.add_source('p1', 'main', 'Main', **source)
    pm.reindex('p1')
    return pm


@pytest.fixture(scope='module')
def xml_pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir, xml_archive=_zip(FIXTURES / 'xml_meta_ru'), xml_archive_filename='x.zip')
        try:
            yield pm
        finally:
            pm.close_all()


def _details(pm, name):
    return execute_tool(pm, 'get_object_details', {'project_id': 'p1', 'full_name': name})


@pytest.mark.parametrize('props, expected', [
    ({'RegisterType': 'Balance'}, [('ВидРегистра', 'Остатки')]),
    ({'ВидРегистра': 'Обороты'}, [('ВидРегистра', 'Обороты')]),
    ({'InformationRegisterPeriodicity': 'Nonperiodical', 'WriteMode': 'RecorderSubordinate'},
     [('Периодичность', 'Непериодический'), ('РежимЗаписи', 'ПодчинениеРегистратору')]),
    ({'ПериодичностьРегистраСведений': 'В пределах секунды'}, [('Периодичность', 'Секунда')]),
    ({'ChartOfAccounts': 'ChartOfAccounts.Хозрасчетный', 'Correspondence': 'true'},
     [('ПланСчетов', 'ПланСчетов.Хозрасчетный'), ('Корреспонденция', 'Да')]),
    ({'Синоним': 'не свойство регистра'}, []),
])
def test_properties_normalized_from_xml_and_report(props, expected):
    assert object_properties(MetadataObject(properties=props)) == expected


def test_document_records_become_properties():
    obj = MetadataObject(register_records=['РегистрНакопления.А', 'РегистрБухгалтерии.Б'])
    assert object_properties(obj) == [('Движения', 'РегистрНакопления.А'),
                                      ('Движения', 'РегистрБухгалтерии.Б')]


def test_hints_per_register_kind():
    balance = query_hints('РегистрНакопления.Р', 'РегистрНакопления', {'ВидРегистра': ['Остатки']}, ['Сумма'])
    assert 'РегистрНакопления.Р.Остатки(&Период, Условие) — СуммаОстаток' in balance[1]
    turnovers = query_hints('РегистрНакопления.Р', 'РегистрНакопления', {'ВидРегистра': ['Обороты']}, ['Сумма'])
    assert 'СуммаОборот' in turnovers[1] and '.Остатки' not in turnovers[1]
    flat = query_hints('РегистрСведений.С', 'РегистрСведений', {'Периодичность': ['Непериодический']}, [])
    assert 'без среза по дате' in flat[1]
    accounting = query_hints('РегистрБухгалтерии.Х', 'РегистрБухгалтерии',
                             {'ПланСчетов': ['ПланСчетов.Хозрасчетный'], 'Корреспонденция': ['Да']}, ['Сумма'])
    assert accounting[0] == 'План счетов: ПланСчетов.Хозрасчетный; с корреспонденцией'
    assert query_hints('Справочник.А', 'Справочник', {}, []) == []


def test_accumulation_register_details(xml_pm):
    text = _details(xml_pm, 'РегистрНакопления.ТоварыНаСкладах')
    assert 'Вид регистра: остатки' in text
    assert 'РегистрНакопления.ТоварыНаСкладах.Остатки(&Период, Условие) — КоличествоОстаток' in text
    assert ('Движения делают документы (5): Документ.ВозвратТоваровПоставщику, '
            'Документ.ИнвентаризацияТоваров') in text


def test_information_register_details(xml_pm):
    text = _details(xml_pm, 'РегистрСведений.КурсыВалют')
    assert 'Периодичность: День; запись: независимый' in text
    assert 'РегистрСведений.КурсыВалют.СрезПоследних(&Период, Условие)' in text


def test_document_records_details(xml_pm):
    text = _details(xml_pm, 'Документ.РеализацияТоваров')
    assert 'Движения по регистрам (2): РегистрНакопления.ТоварыНаСкладах, РегистрНакопления.Продажи' in text
    assert 'Движения делают' not in text


def test_reindex_does_not_duplicate_properties(xml_pm):
    before = _details(xml_pm, 'РегистрНакопления.ТоварыНаСкладах')
    xml_pm.reindex('p1')
    assert _details(xml_pm, 'РегистрНакопления.ТоварыНаСкладах') == before


def test_report_source_has_register_kind_but_no_records():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(FIXTURES / 'report_ru.txt', 'rb') as f:
            pm = _pm(tmpdir, report_file=f)
        try:
            text = _details(pm, 'РегистрНакопления.ТоварыНаСкладах')
            assert 'Вид регистра: остатки' in text
            assert 'Движения делают' not in text   # движения в отчёте не разбираются
            assert 'Периодичность: День' in _details(pm, 'РегистрСведений.КурсыВалют')
        finally:
            pm.close_all()
