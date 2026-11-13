"""
Сырьё для знаний о базе (knowledge_sources): запросы схем компоновки данных
отчётов в индексе, объекты, к которым они обращаются, и обратная связь
«регистр → отчёты». Схема — синтетическая, дописывается к копии фикстуры
xml_meta_ru во временном каталоге (сама фикстура не меняется).
"""

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.knowledge_sources import format_sources, query_refs
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'
QUERY = ('ВЫБРАТЬ РАЗРЕШЕННЫЕ\n    Продажи.Номенклатура КАК Номенклатура,\n    Продажи.СуммаОборот КАК Сумма\n'
         'ИЗ\n    РегистрНакопления.Продажи.Обороты(&НачалоПериода, &КонецПериода, , ) КАК Продажи\n'
         '    ЛЕВОЕ СОЕДИНЕНИЕ Документ.Ссылка КАК Ложный ПО ЛОЖЬ\n'
         'ГДЕ Продажи.Номенклатура.ВидНоменклатуры <> ЗНАЧЕНИЕ(Справочник.ВидыНоменклатуры.ПустаяСсылка)')
TEMPLATE_META = ('<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
                 '<Template><Properties><Name>ОсновнаяСхема</Name><TemplateType>DataCompositionSchema</TemplateType>'
                 '</Properties></Template></MetaDataObject>')
SCHEMA = ('<?xml version="1.0" encoding="UTF-8"?>\n'
          '<DataCompositionSchema xmlns="http://v8.1c.ru/8.1/data-composition-system/schema" '
          'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
          '<dataSet xsi:type="DataSetQuery"><name>Продажи</name><query>{q}</query></dataSet>'
          '</DataCompositionSchema>')


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


def _add_schema(dump: Path):
    from html import escape
    report = dump / 'Reports' / 'АнализПродаж.xml'
    report.write_text(report.read_text(encoding='utf-8-sig').replace(
        '<Form>ФормаОтчета</Form>', '<Form>ФормаОтчета</Form>\n\t\t\t<Template>ОсновнаяСхема</Template>'), encoding='utf-8')
    tdir = dump / 'Reports' / 'АнализПродаж' / 'Templates'
    (tdir / 'ОсновнаяСхема' / 'Ext').mkdir(parents=True)
    (tdir / 'ОсновнаяСхема.xml').write_text(TEMPLATE_META, encoding='utf-8')
    (tdir / 'ОсновнаяСхема' / 'Ext' / 'Template.xml').write_text(SCHEMA.format(q=escape(QUERY)), encoding='utf-8')


@pytest.fixture(scope='module')
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        dump = Path(tmpdir) / 'dump'
        shutil.copytree(FIXTURES / 'xml_meta_ru', dump)
        _add_schema(dump)
        pm = ProjectManager(str(Path(tmpdir) / 'reg'))
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Main', xml_archive=_zip(dump), xml_archive_filename='x.zip')
        pm.reindex('p1')
        try:
            yield pm
        finally:
            pm.close_all()


def test_query_refs_kinds_without_virtual_tables():
    assert query_refs(QUERY) == ['Документ.Ссылка', 'РегистрНакопления.Продажи', 'Справочник.ВидыНоменклатуры']
    assert query_refs('ВЫБРАТЬ 1 ИЗ AccumulationRegister.Sales.Turnovers() КАК T') == ['РегистрНакопления.Sales']


def test_report_with_schema_query(pm):
    items = pm.get_search('p1').knowledge_sources()
    assert [i['full_name'] for i in items] == ['Отчет.АнализПродаж']   # только отчёты со схемами
    item = items[0]
    assert item['queries'] == [{'template': 'ОсновнаяСхема', 'dataset': 'Продажи', 'chars': len(QUERY)}]
    # «Документ.Ссылка» — псевдоним, такого объекта нет — в источники не попадает.
    assert item['uses'] == ['РегистрНакопления.Продажи', 'Справочник.ВидыНоменклатуры']
    assert len(item['digest']) == 12
    full = pm.get_search('p1').knowledge_sources(full_name='отчет.анализпродаж', include_query=True)
    assert full[0]['queries'][0]['query'] == QUERY
    assert 'РегистрНакопления.Продажи.Обороты(&НачалоПериода' in format_sources(full)


def test_register_used_by_reports(pm):
    regs = {i['full_name']: i for i in pm.get_search('p1').knowledge_sources(kind='AccumulationRegister')}
    assert regs['РегистрНакопления.Продажи']['used_by'] == ['Отчет.АнализПродаж']
    assert regs['РегистрНакопления.ТоварыНаСкладах']['used_by'] == []


def test_mcp_tool_text(pm):
    text = execute_tool(pm, 'knowledge_sources', {'project_id': 'p1'})
    assert 'Отчет.АнализПродаж (Анализ продаж)' in text and 'источники: РегистрНакопления.Продажи' in text
    assert 'ВЫБРАТЬ' not in text     # без include_query — только сводка
    one = execute_tool(pm, 'knowledge_sources', {'project_id': 'p1', 'full_name': 'Отчет.АнализПродаж', 'include_query': True})
    assert 'ВЫБРАТЬ РАЗРЕШЕННЫЕ' in one
    regs = execute_tool(pm, 'knowledge_sources', {'project_id': 'p1', 'kind': 'РегистрНакопления'})
    assert 'в отчётах (1): Отчет.АнализПродаж' in regs
