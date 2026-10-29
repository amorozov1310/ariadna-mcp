"""
Метаданные прямо из XML-выгрузки (src/core/xml_metadata.py).

tests/fixtures/xml_meta_ru и xml_meta_en — выгрузки той же синтетической
модели, что и report_ru.txt/report_en.txt (собирает
tests/fixtures/generate_metadata.py), поэтому разбор XML сверяется с
разбором отчёта целиком: объекты, реквизиты с типами, табличные части,
формы, значения перечислений, флаги общих модулей, состав подсистем.
"""

import io
import logging
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.report_parser import parse_report
from src.core.xml_metadata import _Lang, has_configuration, parse_xml_dump

FIXTURES = Path(__file__).parent / 'fixtures'


def _attrs(attrs):
    return [(a.name, a.kind, a.type_desc, a.synonym, a.is_indexed, a.check_fill, a.usage) for a in attrs]


def _shape(obj):
    """Всё, что индексатор берёт из объекта метаданных."""
    mi = obj.module_info
    return {
        'name': obj.name, 'kind': obj.kind, 'synonym': obj.synonym, 'comment': obj.comment,
        'attributes': _attrs(obj.attributes),
        'tabular_sections': {name: _attrs(lst) for name, lst in obj.tabular_sections.items()},
        'forms': [f.name for f in obj.forms],
        'enum_values': [(e.name, e.synonym, e.comment) for e in obj.enum_values],
        'module': (mi.is_global, mi.is_client, mi.is_server, mi.is_external, mi.server_call,
                   mi.is_privileged, mi.reuse) if mi else None,
        # Пустые элементы отчёта — объекты чужой конфигурации; в XML у них UUID.
        'content': [c for c in obj.subsystem_info.content if c] if obj.subsystem_info else None,
    }


@pytest.mark.parametrize('lang', ['ru', 'en'])
def test_xml_dump_matches_configurator_report(lang):
    report_config, report_objects = parse_report(FIXTURES / f'report_{lang}.txt')
    config, objects = parse_xml_dump(FIXTURES / f'xml_meta_{lang}')

    assert vars(config) == vars(report_config)
    expected = {o.full_name: _shape(o) for o in report_objects}
    actual = {o.full_name: _shape(o) for o in objects}
    assert sorted(actual) == sorted(expected)
    for full_name, shape in expected.items():
        assert actual[full_name] == shape, full_name


def test_ru_types_and_values_are_russian():
    _, objects = parse_xml_dump(FIXTURES / 'xml_meta_ru')
    nom = next(o for o in objects if o.full_name == 'Справочник.Номенклатура')
    attrs = {a.name: a for a in nom.attributes}
    assert attrs['Артикул'].type_desc == 'Строка(25, Переменная)'
    assert attrs['Артикул'].is_indexed and attrs['Артикул'].usage == 'ДляЭлемента'
    assert attrs['ВидНоменклатуры'].type_desc == 'СправочникСсылка.ВидыНоменклатуры'
    assert attrs['ВидНоменклатуры'].check_fill == 'ВыдаватьОшибку'
    assert attrs['Вес'].type_desc == 'ОпределяемыйТип.Количество'
    assert attrs['Значение' if 'Значение' in attrs else 'Описание'].type_desc
    assert nom.tabular_sections['ДополнительныеРеквизиты'][1].type_desc == \
        'Строка(100, Переменная), Число(15, 2)'
    sub = next(o for o in objects if o.full_name == 'Подсистема.Продажи.Подсистемы.Отчеты')
    assert sub.subsystem_info.content == ['Отчет.АнализПродаж']


def test_en_uuid_only_subsystem_content_is_dropped():
    _, objects = parse_xml_dump(FIXTURES / 'xml_meta_en')
    jobs = next(o for o in objects if o.full_name == 'Подсистема.Operations.Subsystems.Jobs')
    assert jobs.subsystem_info.content == []
    users = next(o for o in objects if o.full_name == 'Справочник.Users')
    assert {a.name: a.type_desc for a in users.attributes}['Login'] == 'String(50, Variable)'


def _type(xml: str, russian: bool) -> str:
    el = ET.fromstring(
        '<Type xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:cfg="cfg" xmlns:xs="xs">' + xml + '</Type>')
    return _Lang(russian).render_type(el)


@pytest.mark.parametrize('xml, ru, en', [
    ('<v8:Type>xs:dateTime</v8:Type><v8:DateQualifiers><v8:DateFractions>Date</v8:DateFractions>'
     '</v8:DateQualifiers>', 'Дата(Дата)', 'Date(Date)'),
    ('<v8:Type>xs:dateTime</v8:Type><v8:DateQualifiers><v8:DateFractions>Time</v8:DateFractions>'
     '</v8:DateQualifiers>', 'Дата(Время)', 'Date(Time)'),
    ('<v8:Type>xs:string</v8:Type><v8:StringQualifiers><v8:Length>36</v8:Length>'
     '<v8:AllowedLength>Fixed</v8:AllowedLength></v8:StringQualifiers>',
     'Строка(36, Фиксированная)', 'String(36, Fixed)'),
    ('<v8:Type>xs:string</v8:Type>', 'Строка(0, Переменная)', 'String(0, Variable)'),
    ('<v8:Type>xs:decimal</v8:Type><v8:NumberQualifiers><v8:Digits>7</v8:Digits>'
     '<v8:FractionDigits>0</v8:FractionDigits><v8:AllowedSign>Nonnegative</v8:AllowedSign>'
     '</v8:NumberQualifiers>', 'Число(7, 0, Неотрицательный)', 'Number(7, 0, Nonnegative)'),
    ('<v8:Type>xs:base64Binary</v8:Type>', 'ДвоичныеДанные', 'BinaryData'),
    ('<v8:Type>v8:ValueStorage</v8:Type>', 'ХранилищеЗначения', 'ValueStorage'),
    ('<v8:Type>v8:UUID</v8:Type>', 'УникальныйИдентификатор', 'UUID'),
    ('<v8:TypeSet>cfg:AnyIBRef</v8:TypeSet>', 'ЛюбаяСсылка', 'AnyRef'),
    ('<v8:TypeSet>cfg:Characteristic.Свойства</v8:TypeSet>', 'Характеристика.Свойства',
     'Characteristic.Свойства'),
    ('<v8:Type>cfg:ChartOfCharacteristicTypesRef.Виды</v8:Type>',
     'ПланВидовХарактеристикСсылка.Виды', 'ChartOfCharacteristicTypesRef.Виды'),
    ('<v8:Type>cfg:InformationRegisterRecordSet.Цены</v8:Type>',
     'РегистрСведенийНаборЗаписей.Цены', 'InformationRegisterRecordSet.Цены'),
    ('<v8:TypeSet>cfg:DocumentRef</v8:TypeSet>', 'ДокументСсылка', 'DocumentRef'),
    ('<v8:Type>xs:boolean</v8:Type><v8:Type>cfg:CatalogRef.Склады</v8:Type>',
     'Булево, СправочникСсылка.Склады', 'Boolean, CatalogRef.Склады'),
])
def test_type_rendering(xml, ru, en):
    assert _type(xml, True) == ru
    assert _type(xml, False) == en


def test_unreadable_object_file_is_skipped_with_warning(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / 'xml'
        shutil.copytree(FIXTURES / 'xml_meta_ru', xml)
        (xml / 'Catalogs' / 'Склады.xml').write_text('<MetaDataObject><Catalog>', encoding='utf-8')
        with caplog.at_level(logging.WARNING, logger='ariadna'):
            _, objects = parse_xml_dump(xml)
    names = {o.full_name for o in objects}
    assert 'Справочник.Склады' not in names and 'Справочник.Номенклатура' in names
    assert len(objects) == 47
    assert 'Склады.xml' in caplog.text


def test_has_configuration():
    assert has_configuration(FIXTURES / 'xml_meta_ru')
    assert not has_configuration(FIXTURES / 'xml_ru')   # фикстура кода — без метаданных
    assert not has_configuration(FIXTURES / 'нет-такого-каталога')


def _zip(*dirs: Path) -> io.BytesIO:
    buf = io.BytesIO()
    added = set()
    with zipfile.ZipFile(buf, 'w') as zf:
        for src in dirs:   # при совпадении путей берётся файл из первого каталога
            for path in src.rglob('*'):
                name = path.relative_to(src).as_posix()
                if path.is_file() and name not in added:
                    zf.write(path, name)
                    added.add(name)
    buf.seek(0)
    return buf


def test_project_from_xml_only_gets_metadata_and_code():
    """Источник — только XML-выгрузка (как из Web UI): метаданные из
    Configuration.xml и файлов объектов, код — из модулей той же выгрузки."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        try:
            pm.create_project('p1', 'p1')
            source = pm.add_source('p1', 'main', 'Main',
                                   xml_archive=_zip(FIXTURES / 'xml_meta_ru', FIXTURES / 'xml_ru'),
                                   xml_archive_filename='x.zip')
            assert source.report_path == ''
            stats = pm.reindex('p1')
            assert pm.get_project('p1').status == 'ready'
            assert stats.total_objects == 48
            assert stats.total_procedures > 0
            engine = pm.get_search('p1')
            assert [r['full_name'] for r in engine.search_metadata('Склады', kind='Справочник')] == \
                ['Справочник.Склады']
            details = engine.get_object_details('Справочник.Склады')
            assert {a['name'] for a in details['attributes']} == {'ТипСклада', 'Ответственный'}
            assert {f['name'] for f in details['forms']} == {'ФормаЭлемента', 'ФормаСписка'}
            assert [r['name'] for r in engine.search_procedures('РассчитатьЦену')]
            # report.txt больше не создаётся.
            assert not list(Path(tmpdir).rglob('report.txt'))
            assert pm.get_project('p1').sources[0].report_path == ''
        finally:
            pm.close_all()


def test_xml_metadata_preferred_over_uploaded_report():
    """Есть и отчёт, и выгрузка с Configuration.xml — метаданные из выгрузки:
    она согласована с индексируемым кодом, отчёт мог устареть."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        try:
            pm.create_project('p1', 'p1')
            with open(FIXTURES / 'report_en.txt', 'rb') as f:
                pm.add_source('p1', 'main', 'Main', report_file=f,
                              xml_archive=_zip(FIXTURES / 'xml_meta_ru'), xml_archive_filename='x.zip')
            stats = pm.reindex('p1')
            assert stats.total_objects == 48
            row = pm.get_db('p1').conn.execute("SELECT config_name FROM sources WHERE id='main'").fetchone()
            assert row['config_name'] == 'ДемоКонфигурация'
        finally:
            pm.close_all()
