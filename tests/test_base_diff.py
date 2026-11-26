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
        assert 'Сравнение с типовой не настроено' in client.get('/projects/typical/diff').text
        # Без базы — не ошибка, а подсказка: база и релиз необязательны.
        no_base = client.get('/projects/typical/diff/update').text
        assert 'Для этого раздела нужны база' in no_base and 'notice-warning' not in no_base
        assert 'необязательно' in client.get('/projects/typical/diff').text
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
        for text in ('Сравнение:', 'ДемоКонфигурация 1.2.3.4', 'Что доработано',
                     'Обновление на новый релиз', 'Как это работает'):
            assert text in page
        # Выбор базы и релиза — только у строки сравнения, один раз на странице;
        # в настройках проекта — текущие значения и ссылка сюда.
        assert 'Изменить сравнение' in page and page.count('name="release_project"') == 1
        settings = client.get('/projects/custom/settings?tab=general').text
        assert 'Сравнение с типовой' in settings and 'name="base_project"' not in settings
        assert 'href="/projects/custom/diff#comparison"' in settings and 'href="/projects/typical"' in settings
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


# ============================================
# Перехваты в расширениях × изменения релиза
# ============================================

def _extension(pm: ProjectManager, project_id: str) -> None:
    """Расширение проекта: &Вместо над процедурой, которую релиз изменил, и
    &После над процедурой модуля, который релиз тоже изменил."""
    xml = pm.projects_dir / project_id / 'sources' / 'ext' / 'xml'
    modules = {
        'ЦеныСервер': '&Вместо("РассчитатьЦену")\nФункция Расш_РассчитатьЦену(Товар)\n'
                      '\tВозврат ПродолжитьВызов(Товар) + 1;\nКонецФункции\n',
        'СкидкиСервер': '&После("РассчитатьЦену")\nПроцедура Расш_ПослеРасчета(Товар)\n'
                        'КонецПроцедуры\n',
    }
    for name, text in modules.items():
        path = xml / 'CommonModules' / name / 'Ext' / 'Module.bsl'
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding='utf-8-sig')
    pm.add_source(project_id, 'ext', 'Расширение', source_type='extension')
    pm.reindex(project_id)


def test_intercepts_changed_by_release(release_pm):
    pm = release_pm
    _extension(pm, 'custom')
    found = {(i.module, i.procedure, i.keyword) for i in pm.intercepts('custom')}
    assert ('ОбщийМодуль.ЦеныСервер.Модуль', 'РассчитатьЦену', 'Вместо') in found
    assert ('ОбщийМодуль.СкидкиСервер.Модуль', 'РассчитатьЦену', 'После') in found

    result = pm.get_update_conflicts('custom', 'release')
    intercepted = {c.target: c for c in result.items if c.kind == 'intercepted_changed'}
    replaced = intercepted['ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену']
    assert 'ext: Расш_РассчитатьЦену' in replaced.note and 'заменена целиком' in replaced.note
    after = intercepted['ОбщийМодуль.СкидкиСервер.Модуль.РассчитатьЦену']
    assert 'заменена целиком' not in after.note

    detail = execute_tool(pm, 'update_conflicts', {
        'project_id': 'custom', 'release_project': 'release',
        'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'})
    assert '== Перехваты в расширениях' in detail and 'Расш_РассчитатьЦену' in detail

    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        page = client.get('/projects/custom/diff/conflict', params={
            'release': 'release', 'module': 'ОбщийМодуль.ЦеныСервер.Модуль', 'name': 'РассчитатьЦену'}).text
        assert 'Перехват в расширении «ext»' in page and 'ПродолжитьВызов' in page
    finally:
        app_module.set_pm(previous)


def test_intercept_of_procedure_removed_in_release():
    from src.core.base_diff import BaseDiff, Intercept, ModuleChange, ObjectChange, compute_update_conflicts
    ours = BaseDiff('typical', 'main')
    release = BaseDiff('typical', 'main')
    release.objects['ОбщийМодуль.X'] = ObjectChange('ОбщийМодуль.X', 'ОбщийМодуль', modules=[
        ModuleChange('ОбщийМодуль.X.Модуль', 'ОбщийМодуль.X', 'changed', removed=['Старая'], changed=['Новая'],
                     signature_changed=['Новая'])])
    result = compute_update_conflicts(ours, release, 'release', [
        Intercept('ОбщийМодуль.X.Модуль', 'Старая', 'ext', 'Расш_Старая', '&Перед:Старая'),
        Intercept('ОбщийМодуль.X.Модуль', 'Новая', 'ext', 'Расш_Новая', '&После:Новая'),
        Intercept('ОбщийМодуль.Y.Модуль', 'Любая', 'ext', 'Расш_Любая', '&Вместо:Любая'),
    ])
    kinds = {c.target: (c.kind, c.note) for c in result.items}
    assert kinds['ОбщийМодуль.X.Модуль.Старая'][0] == 'intercepted_removed'
    assert kinds['ОбщийМодуль.X.Модуль.Новая'][0] == 'intercepted_changed'
    assert 'сигнатура' in kinds['ОбщийМодуль.X.Модуль.Новая'][1]
    assert 'ОбщийМодуль.Y.Модуль.Любая' not in kinds        # релиз модуль не трогал


# ============================================
# Отчёты файлом
# ============================================

def test_reports_export(release_pm):
    import csv
    import io
    from src.core import diff_report

    diff = release_pm.get_base_diff('custom')
    md = diff_report.diff_report('custom', diff, 'md').decode('utf-8')
    assert md.startswith('# Что доработано: «custom»') and 'База — «typical»' in md
    assert '### ОбщийМодуль.ЦеныСервер' in md and 'РассчитатьЦену' in md
    assert 'НашПроизводитель' in md
    raw = diff_report.diff_report('custom', diff, 'csv')
    assert raw.startswith(b'\xef\xbb\xbf')              # BOM — Excel узнаёт UTF-8
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';'))
    assert rows[0][:3] == ['Объект', 'Синоним', 'Статус объекта']
    assert any(r[3] == 'процедура' and r[4].endswith('.РассчитатьЦену') and r[5] == 'изменена'
               for r in rows[1:])
    assert any(r[3] == 'реквизит' and r[4] == 'НашПроизводитель' and r[5] == 'добавлен' for r in rows)

    result = release_pm.get_update_conflicts('custom', 'release')
    md = diff_report.update_report('custom', result, 'md').decode('utf-8')
    assert '- [ ] `ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену`' in md
    assert f'Всего: {len(result.items)}.' in md
    rows = list(csv.reader(io.StringIO(diff_report.update_report('custom', result, 'csv')
                                       .decode('utf-8-sig')), delimiter=';'))
    assert rows[0][-2:] == ['Решение', 'Ответственный'] and len(rows) == len(result.items) + 1
    assert rows[1][1] == 'правили и вы, и релиз'

    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(release_pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        resp = client.get('/projects/custom/diff/update/export',
                          params={'release': 'release', 'format': 'csv'})
        assert resp.status_code == 200 and resp.headers['content-type'].startswith('text/csv')
        assert 'attachment' in resp.headers['content-disposition']
        assert 'obnovlenie_custom_' in resp.headers['content-disposition']
        resp = client.get('/projects/custom/diff/export', params={'format': 'md'})
        assert resp.status_code == 200 and 'Что доработано' in resp.text
        assert client.get('/projects/custom/diff/export', params={'format': 'xlsx'}).status_code == 400
        assert client.get('/projects/typical/diff/update/export').status_code == 400   # нет базы
        page = client.get('/projects/custom/diff/update', params={'release': 'release'}).text
        assert '/diff/update/export?release=release&format=csv' in page
    finally:
        app_module.set_pm(previous)



FORM = 'Catalogs/Склады/Forms/ФормаЭлемента/Ext'


def _with_form_events(form_ext: Path, events: str) -> None:
    form = form_ext / 'Form.xml'
    text = form.read_text(encoding='utf-8-sig')
    form.write_text(text.replace('<Attributes>', f'<Events>{events}</Events>\n\t<Attributes>', 1),
                    encoding='utf-8-sig')


def test_form_event_intercepts(release_pm, monkeypatch):
    """Перехват события заимствованной формы — callType в Form.xml расширения,
    без аннотации в коде: релиз изменил обработчик основной формы."""
    pm = release_pm
    main = pm.projects_dir / 'custom' / 'sources' / 'main' / 'xml'
    _with_form_events(main / FORM, '<Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>'
                                   '<Event name="AfterWrite">ПослеЗаписи</Event>')
    ext = pm.projects_dir / 'custom' / 'sources' / 'ext' / 'xml'
    shutil.copytree(main / FORM, ext / FORM)
    _with_form_events(ext / FORM, '<Event name="OnCreateAtServer" callType="After">'
                                  'Расш_ПриСозданииПосле</Event>')
    (ext / FORM / 'Form' / 'Module.bsl').write_text(
        '&НаСервере\nПроцедура Расш_ПриСозданииПосле(Отказ, СтандартнаяОбработка)\n'
        '\tКонтроль = 42;\nКонецПроцедуры\n', encoding='utf-8-sig')
    pm.add_source('custom', 'ext', 'Расширение', source_type='extension')

    release_form = pm.projects_dir / 'release' / 'sources' / 'main' / 'xml' / FORM / 'Form' / 'Module.bsl'
    release_form.write_text(release_form.read_text(encoding='utf-8-sig').replace(
        'Объект.ТипСклада = Перечисления.ТипыСкладов.Оптовый;',
        'Объект.ТипСклада = Перечисления.ТипыСкладов.Розничный;'), encoding='utf-8-sig')
    pm.reindex('release')
    pm.reindex('custom')

    module = 'Справочник.Склады.Форма.ФормаЭлемента'
    found = [i for i in pm.intercepts('custom') if i.module == module]
    assert [(i.procedure, i.ext_procedure, i.keyword, i.form_event) for i in found] == [
        ('ПриСозданииНаСервере', 'Расш_ПриСозданииПосле', 'После', True)]
    assert found[0].label == '&После("ПриСозданииНаСервере"), событие формы'
    assert pm.intercepts('custom', module_name=module, procedure_name='ПослеЗаписи') == []

    result = pm.get_update_conflicts('custom', 'release')
    target = f'{module}.ПриСозданииНаСервере'
    conflict = next(c for c in result.items if c.target == target)
    assert conflict.kind == 'intercepted_changed' and 'событие формы' in conflict.note

    from fastapi.testclient import TestClient
    from src.web import app as app_module

    monkeypatch.setattr(app_module, '_pm', pm)
    client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
    url = '/projects/custom/diff/conflict'
    params = {'module': module, 'name': found[0].procedure, 'release': 'release'}
    detail = client.get(url, params=params)
    assert detail.status_code == 200 and 'Контроль = 42;' in detail.text

    for export in (main, ext, pm.get_source_xml_path('release', 'main'),
                   pm.get_source_xml_path('typical', 'main')):
        export.rename(export.with_name('xml-moved'))
    assert pm.intercepts('custom', module_name=module) == found
    assert pm.get_update_conflicts('custom', 'release') == result
    assert client.get(url, params=params).content == detail.content


def test_old_base_index_is_rebuilt_and_cannot_seed_new_index(pm, monkeypatch):
    with pm.get_db('typical').conn as conn:
        conn.execute('DROP TABLE module_text')

    calls = {}
    real_reindex = pm.reindex

    def counted_reindex(project_id, source_id=None):
        calls[project_id] = calls.get(project_id, 0) + 1
        return real_reindex(project_id, source_id=source_id)

    monkeypatch.setattr(pm, 'reindex', counted_reindex)
    pm.reindex('custom')
    assert calls == {'custom': 1}
    assert 'Возврат 120;' in execute_tool(pm, 'get_procedure_code', {
        'project_id': 'custom', 'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'})

    pm.close_all()
    pm.reindex('typical')
    pm._schema_reindex_queue.join()
    assert calls == {'custom': 1, 'typical': 1}
    assert 'Возврат 100;' in execute_tool(pm, 'get_procedure_code', {
        'project_id': 'typical', 'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'})


def test_main_source_with_any_id(release_pm):
    """Основная конфигурация проекта не обязана называться «main»: у проекта
    источник «conf», у базы и релиза — «main»; сравнение — по основным."""
    pm = release_pm
    pm.create_project('named', 'named')
    shutil.copytree(pm.projects_dir / 'custom' / 'sources' / 'main' / 'xml',
                    pm.projects_dir / 'named' / 'sources' / 'conf' / 'xml')
    pm.add_source('named', 'conf', 'Конфигурация')
    pm.set_base_project('named', 'typical')
    pm.reindex('named')
    assert pm.main_source('named') == 'conf' and pm.main_source('typical') == 'main'
    assert pm.config_version('named') == pm.config_version('typical') != ''

    ours = pm.get_base_diff('custom').totals()
    assert pm.get_base_diff('named').totals() == ours and ours['procedures_changed'] > 0
    found = {(c.kind, c.target) for c in pm.get_update_conflicts('named', 'release').items}
    assert found == {(c.kind, c.target) for c in pm.get_update_conflicts('custom', 'release').items}
    assert ('both_changed', 'ОбщийМодуль.ЦеныСервер.Модуль.РассчитатьЦену') in found
    detail = pm.get_procedure_diff('named', 'ЦеныСервер', 'РассчитатьЦену')
    assert detail['status'] == 'changed' and '+\tВозврат 120;' in detail['diff']
    text = execute_tool(pm, 'update_conflicts', {'project_id': 'named', 'release_project': 'release',
                                                 'module_name': 'ЦеныСервер',
                                                 'procedure_name': 'РассчитатьЦену'})
    assert '+\tВозврат 120; // наценка' in text and '+\tВозврат 110;' in text


def test_no_diff_while_a_project_is_indexing(pm):
    """Проект или его база индексируется — сравнение не запускается (оно
    устарело бы сразу); после индексации его пересчитает _warm_related."""
    pm.reindex('custom')
    pm.close_all()                                  # дождаться прогрева после reindex
    pm._base_diffs.clear()
    pm._warming.clear()
    for busy in ('custom', 'typical'):
        pm.get_project(busy).status = 'indexing'
        with pytest.raises(ValueError, match=f'«{busy}»'):
            pm.warm_diffs('custom')
        pm._warm_related('typical')                 # окончание индексации базы
        assert 'custom' not in pm._warming
        pm.get_project(busy).status = 'ready'
    pm._warm_related('custom')
    pm._warming['custom'].join(10)
    assert pm.warm_diffs('custom') is True


def test_diff_pages_layout(release_pm):
    """Шаг 10 плана UI: строка сравнения вместо схемы, таблица объектов без
    бейджей, категории обновления по тяжести, две колонки на пересечении."""
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(release_pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        release_pm.set_comparison('custom', 'typical', 'release')
        release_pm.get_base_diff('custom')
        release_pm.get_update_conflicts('custom')
        page = client.get('/projects/custom/diff').text
        main = page[page.index('<main'):page.index('</main>')]
        assert 'diff-compare-line' in main and 'diff-chip' not in main
        assert 'class="diff-grid diff-grid-head"' in main and 'data-sort="procs"' in main
        assert main.count('class="badge') == 0                     # числа в колонках, не бейджи
        assert 'Таблица для Excel (CSV)' in main and 'Все цифры' in main
        update = client.get('/projects/custom/diff/update').text
        assert 'conflict-cat' in update and 'Слить вручную' in update
        assert 'id="conflict-filter"' in update and 'Нет пересечений:' in update
        conflict = client.get('/projects/custom/diff/conflict', params={
            'release': 'release', 'module': 'ОбщийМодуль.ЦеныСервер.Модуль', 'name': 'РассчитатьЦену'}).text
        assert 'class="conflict-sides"' in conflict and 'Открыть процедуру в проекте' in conflict
        proc = client.get('/projects/custom/diff/procedure', params={
            'module': 'ОбщийМодуль.ЦеныСервер.Модуль', 'name': 'РассчитатьЦену'}).text
        assert '&tab=callers' in proc
        # Без типовой — мастер из двух шагов.
        assert 'Создайте проект с типовой того же релиза' in client.get('/projects/typical/diff').text
    finally:
        app_module.set_pm(previous)


def test_overview_cards_show_only_computed_numbers(pm):
    """Карточки «Обзора» — числа только из уже посчитанного: заход на страницу
    проверку вызовов и сравнение не запускает (сравнение после индексации
    считается в фоне само — _warm_related)."""
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    previous = app_module._pm
    app_module.set_pm(pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        pm.reindex('custom')
        thread = pm._warming.get('custom')
        if thread is not None:
            thread.join(30)
        client.get('/projects/custom')
        assert 'broken' not in pm.overview_counts('custom'), 'страница не запускает проверку вызовов'
        assert not pm.broken_calls_ready('custom')
        totals = pm.get_base_diff('custom').totals()
        pm.broken_calls('custom')
        counts = pm.overview_counts('custom')
        assert counts['diff'] == totals and 'broken' in counts and counts['glossary'] == 0
        page = client.get('/projects/custom').text
        assert 'overview-num' in page and 'изменен' in page
    finally:
        app_module.set_pm(previous)
