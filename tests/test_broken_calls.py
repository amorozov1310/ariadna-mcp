"""Проверка вызовов (src/core/broken_calls.py, инструмент find_broken_calls,
страница «Проверка»): методов, которых нет в модуле, неэкспортных методов из
другого модуля и модулей, которых нет в выгрузке."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'

CHECKS = '''
Процедура ПроверкаВызовов() Экспорт
	СкидкиСервер.НетТакогоМетода();
	СкидкиСервер.ОформитьЗаказ(1);
	ЗаказыСервер.ВнутренняяЗаказа();
	СкидкиСервер.ПрименитьСкидку(1);
	ЮТест.ОжидаетЧто(1).Равно(1);
	ЮТест.Контекст();
	Модуль = ЗаказыСервер;
	МодульСкидок = Вычислить("СкидкиСервер");
	МодульСкидок.ЧтоУгодно();
	Склады = Справочники.Склады.ПолучитьДанныеВыбора(Новый Структура);
	Склад = Справочники.Склады.НетВМенеджере();
КонецПроцедуры
'''


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p', 'p')
        xml = manager.projects_dir / 'p' / 'sources' / 'main' / 'xml'
        shutil.copytree(FIXTURES / 'xml_ru', xml)
        prices = xml / 'CommonModules' / 'ЦеныСервер' / 'Ext' / 'Module.bsl'
        prices.write_text(prices.read_text(encoding='utf-8-sig') + CHECKS, encoding='utf-8-sig')
        orders = xml / 'CommonModules' / 'ЗаказыСервер' / 'Ext' / 'Module.bsl'
        orders.write_text(orders.read_text(encoding='utf-8-sig')
                          + '\nПроцедура ВнутренняяЗаказа()\nКонецПроцедуры\n'
                          + '\nПроцедура СвояПроверка() Экспорт\n\tЗаказыСервер.ВнутренняяЗаказа();\n'
                            'КонецПроцедуры\n', encoding='utf-8-sig')
        # Реквизит своего объекта и стандартный реквизит — не модули.
        warehouses = xml / 'Catalogs' / 'Склады' / 'Ext' / 'ManagerModule.bsl'
        warehouses.write_text(warehouses.read_text(encoding='utf-8-sig')
                              + '\nПроцедура РеквизитыКакМодули() Экспорт\n'
                                '\tТипСклада.Пересчитать();\n\tСсылка.Заблокировать();\n'
                                '\tЧужойМодуль.Метод();\nКонецПроцедуры\n', encoding='utf-8-sig')
        manager.add_source('p', 'main', 'Основная')
        manager.reindex('p')
        try:
            yield manager
        finally:
            manager.close_all()


def test_broken_calls_kinds(pm):
    result = pm.broken_calls('p')
    missing = {(i['module'], i['name']) for i in result['missing']}
    # ОформитьЗаказ есть, но в ЗаказыСервер, а не в СкидкиСервер — с подсказкой.
    assert missing == {('СкидкиСервер', 'НетТакогоМетода'), ('СкидкиСервер', 'ОформитьЗаказ'),
                       ('Справочник.Склады.МодульМенеджера', 'НетВМенеджере')}
    hint = next(i for i in result['missing'] if i['name'] == 'ОформитьЗаказ')
    assert [d['module'] for d in hint['defined_in']] == ['ОбщийМодуль.ЗаказыСервер.Модуль']
    assert hint['caller_module'] == 'ОбщийМодуль.ЦеныСервер.Модуль'
    assert hint['caller_proc'] == 'ПроверкаВызовов' and hint['line'] > 0

    # Неэкспортная — из другого модуля ошибка, из своего (по имени модуля) — нет.
    not_export = [(i['caller_module'], i['name']) for i in result['not_export']]
    assert not_export == [('ОбщийМодуль.ЦеныСервер.Модуль', 'ВнутренняяЗаказа')]

    # Платформенный метод менеджера и экспортная процедура — не ошибки.
    assert ('Справочник.Склады.МодульМенеджера', 'ПолучитьДанныеВыбора') not in missing
    assert all(i['name'] != 'ПрименитьСкидку' for i in result['missing'] + result['not_export'])

    external = {e['module']: e for e in result['external']}
    assert external['ЮТест']['calls'] == 2 and external['ЮТест']['methods'] == 2
    # Переменная = Вычислить("Модуль") — динамический вызов, а не внешняя библиотека.
    assert 'МодульСкидок' not in external and result['dynamic'] >= 1
    assert 'ТипСклада' not in external and 'Ссылка' not in external
    assert 'ЧужойМодуль' in external

    assert pm.broken_calls('p', module_name='СвояПроверка') == {
        'missing': [], 'not_export': [], 'external': [], 'dynamic': result['dynamic']}
    assert len(pm.broken_calls('p', source_id='нет')['missing']) == 0


def test_find_broken_calls_tool_and_page(pm):
    text = execute_tool(pm, 'find_broken_calls', {'project_id': 'p'})
    assert 'метода в модуле нет — 3' in text and 'неэкспортный метод из другого модуля — 1' in text
    assert 'СкидкиСервер.ОформитьЗаказ: метода в модуле нет — с таким именем есть в: ' \
           'ОбщийМодуль.ЗаказыСервер.Модуль @main' in text
    assert 'ЮТест — вызовов 2, методов 2' in text
    page1 = execute_tool(pm, 'find_broken_calls', {'project_id': 'p', 'limit': 2})
    assert '(use offset=2 for more)' in page1
    page2 = execute_tool(pm, 'find_broken_calls', {'project_id': 'p', 'limit': 2, 'offset': 2})
    assert 'ВнутренняяЗаказа' in page2 and 'Модулей нет в выгрузке' not in page2

    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        page = client.get('/projects/p/checks')
        assert page.status_code == 200
        assert 'Метода в модуле нет (3)' in page.text and 'ВнутренняяЗаказа' in page.text
        assert 'ЮТест' in page.text and 'aria-current="page"' in page.text
        filtered = client.get('/projects/p/checks', params={'module': 'Склады'}).text
        assert 'НетВМенеджере' in filtered and 'НетТакогоМетода' not in filtered
    finally:
        app_module.set_pm(previous)
