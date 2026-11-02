"""
get_procedure_context: всё о процедуре одним вызовом, без кода — объявление,
строки, объект, область, комментарий, кто вызывает и что вызывает.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from fixture_zip import zip_dir

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture(scope='module')
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        manager.add_source('p1', 'main', 'Main', xml_archive=zip_dir(FIXTURES / 'xml_ru'),
                           xml_archive_filename='x.zip')
        manager.reindex('p1')
        try:
            yield manager
        finally:
            manager.close_all()


def _ctx(pm, module, procedure, **kw):
    return execute_tool(pm, 'get_procedure_context',
                        {'project_id': 'p1', 'module_name': module, 'procedure_name': procedure, **kw})


def test_declaration_place_and_comment(pm):
    text = _ctx(pm, 'ЦеныСервер', 'РассчитатьЦену')
    lines = text.splitlines()
    assert lines[0] == '// ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену  @Main'
    assert lines[1] == 'Функция РассчитатьЦену(Товар) Экспорт'
    assert 'Строки: 3-5 (3), файл CommonModules/ЦеныСервер/Ext/Module.bsl' in text
    assert text.count('Описание: ') == 1
    assert 'get_procedure_code(module_name="ОбщийМодуль.ЦеныСервер.Модуль", procedure_name="РассчитатьЦену")' in text


def test_callers_are_exactly_the_resolved_ones(pm):
    """Вызывающие одноимённой СкидкиСервер.РассчитатьЦену сюда не попадают."""
    text = _ctx(pm, 'ЦеныСервер', 'РассчитатьЦену')
    assert 'Вызывают (1, мест вызова: 1):' in text
    assert 'ОбщийМодуль.СкидкиСервер.Модуль.ПрименитьСкидку  (стр. 10)' in text


def test_callees_listed(pm):
    text = _ctx(pm, 'СкидкиСервер', 'ПрименитьСкидку')
    assert 'Вызывает (' in text
    assert 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену' in text.split('Вызывает (', 1)[1]


def test_object_module_has_object_and_region(pm):
    text = _ctx(pm, 'Справочник.Склады.МодульОбъекта', 'ТипСкладаПоУмолчанию')
    assert 'Объект: Справочник.Склады — Склады' in text
    assert 'Область: СлужебныеПроцедурыИФункции' in text
    assert 'Справочник.Склады.МодульОбъекта.ПередЗаписью' in text


def test_case_insensitive_and_not_found(pm):
    assert _ctx(pm, 'ценысервер', 'рассчитатьцену').startswith('// ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену')
    assert "Procedure 'НетТакой' not found in module ОбщийМодуль.ЦеныСервер.Модуль" in _ctx(pm, 'ЦеныСервер', 'НетТакой')
    assert "Module 'НетМодуля' not found" in _ctx(pm, 'НетМодуля', 'Х')


def test_entry_point_has_no_callers(pm):
    text = _ctx(pm, 'Справочник.Склады.МодульОбъекта', 'ПередЗаписью')
    assert 'Вызывают: никто в проиндексированном коде' in text
