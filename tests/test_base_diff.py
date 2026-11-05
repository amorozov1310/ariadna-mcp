"""
Отличия проекта от базы (src/core/base_diff.py, ProjectManager.get_base_diff):
база — проект с исходной выгрузкой фикстуры, проект — её копия с правками в
коде и метаданных. Плюс засев первого индекса из базы и инструмент
diff_from_base.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'
PRICES = 'CommonModules/ЦеныСервер/Ext/Module.bsl'
CATALOG = 'Catalogs/Номенклатура.xml'


def _project(pm: ProjectManager, project_id: str, xml: Path, base: str = '') -> None:
    pm.create_project(project_id, project_id)
    shutil.copytree(xml, pm.projects_dir / project_id / 'sources' / 'main' / 'xml')
    pm.add_source(project_id, 'main', 'Основная')
    if base:
        pm.set_base_project(project_id, base)


def _modify(xml: Path) -> None:
    """Доработки «внедренца»: правка и новая процедура в типовом модуле, новый
    модуль, удалённый модуль, переименованный реквизит справочника."""
    prices = xml / PRICES
    text = prices.read_text(encoding='utf-8-sig')
    text = text.replace('Возврат 100;', 'Возврат 120; // наценка')
    text += '\nФункция НаценкаКлиента(Клиент) Экспорт\n\tВозврат 20;\nКонецФункции\n'
    prices.write_text(text, encoding='utf-8-sig')
    (xml / 'CommonModules/СкидкиСервер/Ext/Module.bsl').unlink()
    catalog = xml / CATALOG
    catalog.write_text(catalog.read_text(encoding='utf-8-sig').replace(
        '<Name>Производитель</Name>', '<Name>НашПроизводитель</Name>'), encoding='utf-8-sig')


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        _project(manager, 'typical', FIXTURES / 'xml_ru')
        manager.reindex('typical')
        modified = Path(tmpdir) / 'modified'
        shutil.copytree(FIXTURES / 'xml_ru', modified)
        _modify(modified)
        _project(manager, 'custom', modified, base='typical')
        try:
            yield manager
        finally:
            manager.close_all()


def test_base_validation(pm):
    with pytest.raises(ValueError):
        pm.set_base_project('custom', 'custom')
    with pytest.raises(KeyError):
        pm.set_base_project('custom', 'нет')
    with pytest.raises(ValueError, match='не задана база'):
        pm.get_base_diff('typical')
    assert pm.get_project('custom').base_project == 'typical'


def test_first_index_is_seeded_from_base(pm):
    pm.reindex('custom')
    conn = pm.get_db('custom').conn
    # Засеянный индекс: неизменённые модули не разбирались — их строки (и id)
    # пришли из базы, а не вставлены заново.
    base_ids = {r[0]: r[1] for r in pm.get_db('typical').conn.execute("SELECT name, id FROM modules")}
    ids = {r[0]: r[1] for r in conn.execute("SELECT name, id FROM modules")}
    same = [n for n in ids if n in base_ids and ids[n] == base_ids[n]]
    assert len(same) >= len(base_ids) - 2
    assert 'ОбщийМодуль.СкидкиСервер.Модуль' not in {
        r[0] for r in conn.execute("SELECT name FROM modules WHERE file_hash != ''")}
    assert pm.get_project('custom').status == 'ready'


def test_diff_summary_and_object(pm):
    pm.reindex('custom')
    diff = pm.get_base_diff('custom')
    prices = diff.find_object('ОбщийМодуль.ЦеныСервер')
    assert prices.status == 'changed'
    module = prices.modules[0]
    assert module.changed == ['РассчитатьЦену'] and module.added == ['НаценкаКлиента']
    assert [m.status for m in diff.find_object('ОбщийМодуль.СкидкиСервер').modules] == ['removed']

    catalog = diff.find_object('Справочник.Номенклатура')
    assert [n for n, _ in catalog.attrs_added] == ['НашПроизводитель']
    assert [n for n, _ in catalog.attrs_removed] == ['Производитель']

    totals = diff.totals()
    assert totals['procedures_changed'] == 1 and totals['procedures_added'] == 1
    assert totals['modules_removed'] == 1 and totals['objects_added'] == 0
    assert pm.get_base_diff('custom') is diff           # кэш до переиндексации


def test_procedure_diff(pm):
    pm.reindex('custom')
    changed = pm.get_procedure_diff('custom', 'ЦеныСервер', 'РассчитатьЦену')
    assert changed['status'] == 'changed'
    assert '-\tВозврат 100;' in changed['diff'] and '+\tВозврат 120; // наценка' in changed['diff']
    assert pm.get_procedure_diff('custom', 'ЦеныСервер', 'НаценкаКлиента')['status'] == 'added'
    assert pm.get_procedure_diff('custom', 'ЗаказыСервер', 'Нет')['status'] == 'missing'


def test_diff_from_base_tool(pm):
    pm.reindex('custom')

    def tool(**args):
        return execute_tool(pm, 'diff_from_base', {'project_id': 'custom', **args})

    summary = tool()
    assert 'Отличия «custom» от базы «typical»' in summary
    assert 'ОбщийМодуль.ЦеныСервер' in summary and 'Справочник.Номенклатура' in summary
    detail = tool(object_name='Справочник.Номенклатура')
    assert 'Реквизиты добавлены (1)' in detail and 'НашПроизводитель' in detail
    assert 'изменены: РассчитатьЦену' in tool(object_name='ОбщийМодуль.ЦеныСервер.Модуль')
    assert '+\tВозврат 120;' in tool(module_name='ЦеныСервер', procedure_name='РассчитатьЦену')
    assert tool(procedure_name='X').startswith('Error:')
    assert 'Новые объекты (0):\n  нет' in tool(section='added')
    assert execute_tool(pm, 'diff_from_base', {'project_id': 'typical'}).startswith('Error:')


def test_web_diff_pages(pm):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    pm.reindex('custom')
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        pm.get_base_diff('custom')              # посчитано — страница без ожидания
        page = client.get('/projects/custom/diff')
        assert page.status_code == 200 and 'ОбщийМодуль.ЦеныСервер' in page.text
        proc = client.get('/projects/custom/diff/procedure',
                          params={'module': 'ОбщийМодуль.ЦеныСервер.Модуль', 'name': 'РассчитатьЦену'})
        assert proc.status_code == 200 and 'diff-line-add' in proc.text
        assert 'База — проект с типовой' in client.get('/projects/typical/diff').text
        resp = client.post('/api/projects/typical/base', data={'base_project': 'custom'},
                           follow_redirects=False)
        assert resp.status_code == 303 and pm.get_project('typical').base_project == 'custom'
    finally:
        app_module.set_pm(previous)


# ============================================
# Конфликты обновления: проект × новый релиз от общей базы
# ============================================

def _release(xml: Path) -> None:
    """«Новый релиз» той же типовой: правит процедуру, которую правил проект;
    добавляет процедуру и реквизит с теми же именами, что добавил проект;
    меняет модуль, который проект удалил; меняет модуль, которого проект не
    трогал (не конфликт)."""
    prices = xml / PRICES
    text = prices.read_text(encoding='utf-8-sig').replace('Возврат 100;', 'Возврат 110;')
    text += '\nФункция НаценкаКлиента(Клиент, Дата) Экспорт\n\tВозврат 5;\nКонецФункции\n'
    prices.write_text(text, encoding='utf-8-sig')
    discounts = xml / 'CommonModules/СкидкиСервер/Ext/Module.bsl'
    discounts.write_text(discounts.read_text(encoding='utf-8-sig').replace('Возврат 10;', 'Возврат 15;'),
                         encoding='utf-8-sig')
    orders = xml / 'CommonModules/ЗаказыСервер/Ext/Module.bsl'
    orders.write_text(orders.read_text(encoding='utf-8-sig') + '\nПроцедура Новая() Экспорт\nКонецПроцедуры\n',
                      encoding='utf-8-sig')
    catalog = xml / CATALOG
    text = catalog.read_text(encoding='utf-8-sig')
    start = text.index('<Attribute', text.index('<Name>Производитель</Name>') - 400)
    end = text.index('</Attribute>', start) + len('</Attribute>')
    clone = text[start:end].replace('<Name>Производитель</Name>', '<Name>НашПроизводитель</Name>')
    catalog.write_text(text[:end] + clone + text[end:], encoding='utf-8-sig')


@pytest.fixture
def release_pm(pm):
    release = pm.projects_dir.parent / 'release'
    shutil.copytree(FIXTURES / 'xml_ru', release)
    _release(release)
    _project(pm, 'release', release)
    pm.reindex('release')
    pm.reindex('custom')
    return pm


def test_update_conflicts(release_pm):
    result = release_pm.get_update_conflicts('custom', 'release')
    found = {(c.kind, c.target) for c in result.items}
    assert ('both_changed', 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену') in found
    assert ('name_clash', 'ОбщийМодуль.ЦеныСервер.Модуль.НаценкаКлиента') in found
    assert ('removed_by_you', 'ОбщийМодуль.СкидкиСервер.Модуль') in found
    assert ('name_clash', 'Справочник.Номенклатура.НашПроизводитель') in found
    assert not any('ЗаказыСервер' in c.target for c in result.items)
    assert result.release_totals['procedures_added'] == 2
    with pytest.raises(ValueError):
        release_pm.get_update_conflicts('custom', 'typical')


def test_update_conflicts_tool_and_pages(release_pm):
    def tool(**args):
        return execute_tool(release_pm, 'update_conflicts', {'project_id': 'custom', 'release_project': 'release', **args})

    text = tool()
    assert 'Обновление «custom»: база «typical»' in text
    assert '[правили и вы, и релиз] ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену' in text
    both = tool(module_name='ЦеныСервер', procedure_name='РассчитатьЦену')
    assert '+\tВозврат 120; // наценка' in both and '+\tВозврат 110;' in both
    assert 'Справочник.Номенклатура.НашПроизводитель' in tool(object_name='Справочник.Номенклатура')
    assert 'ЦеныСервер' not in tool(object_name='Справочник.Номенклатура').split('Пересечения по')[1]
    assert execute_tool(release_pm, 'update_conflicts',
                        {'project_id': 'custom', 'release_project': 'нет'}).startswith('Error:')

    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(release_pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        release_pm.get_update_conflicts('custom', 'release')
        page = client.get('/projects/custom/diff/update', params={'release': 'release'})
        assert page.status_code == 200 and 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену' in page.text
        conflict = client.get('/projects/custom/diff/conflict', params={
            'release': 'release', 'module': 'ОбщийМодуль.ЦеныСервер.Модуль', 'name': 'РассчитатьЦену'})
        assert conflict.status_code == 200 and conflict.text.count('diff-line-add') >= 2
    finally:
        app_module.set_pm(previous)


def test_saved_release_and_comparison_settings(release_pm):
    pm = release_pm
    with pytest.raises(ValueError, match='разные проекты'):
        pm.set_comparison('custom', 'typical', 'typical')
    with pytest.raises(ValueError, match='не задан новый релиз'):
        pm.get_update_conflicts('custom')
    pm.set_comparison('custom', 'typical', 'release')
    assert pm.get_update_conflicts('custom').release_project == 'release'
    text = execute_tool(pm, 'update_conflicts', {'project_id': 'custom'})
    assert 'релиз «release»' in text
    assert '+\tВозврат 110;' in execute_tool(pm, 'update_conflicts', {
        'project_id': 'custom', 'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'})
    # Проект обновлён на новый релиз: релиз становится базой, поле релиза сбрасывается.
    pm.set_base_project('custom', 'release')
    assert (pm.get_project('custom').base_project, pm.get_project('custom').release_project) == ('release', '')


def test_web_scheme_and_comparison_form(release_pm):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(release_pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        r = client.post('/api/projects/custom/comparison', data={
            'base_project': 'typical', 'release_project': 'release', 'next': '/projects/custom/diff/update'},
            follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/projects/custom/diff/update'
        release_pm.get_update_conflicts('custom')
        page = client.get('/projects/custom/diff').text
        for text in ('База — типовая текущего релиза', 'ДемоКонфигурация 1.2.3.4', '1. Что доработано',
                     '2. Обновление на новый релиз', 'Как пользоваться'):
            assert text in page
        # Выбор базы и релиза — у схемы, один раз на странице; и в настройках проекта.
        assert 'Изменить сравнение' in page and page.count('name="release_project"') == 1
        settings = client.get('/projects/custom/settings').text
        assert 'Сравнение с типовой' in settings and 'name="base_project"' in settings
        update = client.get('/projects/custom/diff/update').text
        assert 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену' in update
        assert client.post('/api/projects/custom/comparison', data={
            'base_project': 'typical', 'release_project': 'typical'}).status_code == 400
    finally:
        app_module.set_pm(previous)


def test_pending_page_while_diff_is_computed(pm, monkeypatch):
    """Сравнение не посчитано — страница отвечает сразу «идёт сравнение»,
    расчёт идёт в фоне; когда он закончен, страница показывает результат."""
    import threading
    from fastapi.testclient import TestClient
    from src.core import project_manager as pm_module
    from src.web import app as app_module
    pm.reindex('custom')
    pm.close_all()                                  # дождаться прогрева после reindex
    pm._base_diffs.clear()
    gate = threading.Event()
    real = pm_module.ProjectManager._diff_between

    def slow(self, *args, **kwargs):
        gate.wait(10)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pm_module.ProjectManager, '_diff_between', slow)
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        page = client.get('/projects/custom/diff').text
        assert 'Идёт сравнение' in page and 'ОбщийМодуль.ЦеныСервер' not in page
        assert client.get('/api/projects/custom/diff/warming').json() == {'warming': True}
        gate.set()
        pm._warming['custom'].join(10)
        assert client.get('/api/projects/custom/diff/warming').json() == {'warming': False}
        assert 'ОбщийМодуль.ЦеныСервер' in client.get('/projects/custom/diff').text
    finally:
        gate.set()
        app_module.set_pm(previous)
