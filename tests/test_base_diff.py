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
