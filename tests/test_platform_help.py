"""Поиск, публикация версий и доступ к справке через Web UI и MCP."""

import sqlite3
import threading
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from src.core import platform_help as ph
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool
from src.web import app as web
from tests.fixtures.generate_platform_help import context_book, query_book

OLD, NEW = '8.3.9.1', '8.3.27.1'


@pytest.fixture
def store(tmp_path):
    store = ph.PlatformHelpStore(tmp_path / 'справка')
    store.build_from_bytes(NEW, context_book(), query_book())
    return store


def test_hbk_pages_and_toc():
    entries = list(ph.parse_hbk(context_book()))
    assert {e.kind for e in entries} == set(ph.KINDS) - {'query'}
    fn = entries[0]
    assert fn.section == 'Учебный раздел'
    assert fn.name_en == 'StrFind' and fn.availability == 'Сервер'
    variant, = fn.variants
    assert variant['syntax'] == 'СтрНайти(<Аргумент>)'
    assert variant['params'] == [dict(name='Аргумент', required=True, types=['Строка'],
                                      description='Учебный параметр.')]
    query, = ph.parse_hbk(query_book(), 'query')
    assert query.name_ru == 'СтрНайти' and query.variants[0]['syntax'] == 'СтрНайти(А, Б)'


def test_exact_precedes_prefix_across_pages(store):
    index = store.get(NEW)
    for offset, expected, more in [(0, ['function'], True), (1, ['query'], False), (2, [], False)]:
        entries, has_more = index.search('сТРнайТИ()', limit=1, offset=offset)
        assert [e.kind for e in entries] == expected and has_more == more
    assert [e.kind for e in index.search('strfind', kind='запрос')[0]] == ['query']
    assert index.search('учебное описание')[0]
    assert index.search('.') == ([], False)
    assert index.search('ФорматироватьСтроку') == ([], False)


@pytest.mark.parametrize('query', ['Новый ТаблицаЗначений', 'NEW ValueTable()'])
def test_constructor_query(store, query):
    entries, more = store.get(NEW).search(query)
    assert len(entries) == 2 and not more
    assert all(e.kind == 'ctor' and e.owner_en == 'ValueTable' for e in entries)


@pytest.mark.parametrize(('owner', 'expected'), [
    ('Справочники.Пример', 'CatalogManager'), ('Catalogs.Demo', 'CatalogManager'),
    ('Документы.Пример', 'DocumentManager'), ('Documents.Demo', 'DocumentManager'),
    ('РегистрыСведений.Пример', 'InformationRegisterManager'),
    ('InformationRegisters.Demo', 'InformationRegisterManager'),
    ('Перечисления.Пример', 'EnumManager'), ('Enums.Demo', 'EnumManager'),
    ('ПланыОбмена.Пример', 'ExchangePlanManager'), ('СправочникОбъект', 'CatalogObject'),
    ('CatalogObject', 'CatalogObject'), ('СправочникОбъект.<Имя объекта>', 'CatalogObject'),
])
def test_owner_resolution(store, owner, expected):
    name = 'НайтиПоКоду' if not owner.isascii() else 'FindByCode'
    entries, more = store.get(NEW).search(owner.swapcase() + '.' + name)
    assert [e.owner_en for e in entries] == [expected + '.<Object name>']
    assert not more


def test_discovery_rebuild_and_failed_replacement(tmp_path, store):
    directory = tmp_path / OLD / 'bin'
    directory.mkdir(parents=True)
    (directory / ph.CONTEXT_FILE).write_bytes(context_book())
    assert store.sync_installed([tmp_path], background=False) == [OLD]
    assert store.sync_installed([tmp_path], background=False) == []
    assert store.newest() == NEW
    with closing(sqlite3.connect(store.path(OLD))) as conn:
        conn.execute("UPDATE meta SET value='old' WHERE key='fingerprint'")
        conn.commit()
    assert store.versions()[1]['stale']
    assert store.sync_installed([tmp_path], background=False) == [OLD]
    with pytest.raises(ph.HbkError):
        store.build_from_bytes(OLD, b'broken')
    assert OLD in store.status()['errors']
    assert store.get(OLD).search('StrFind')[0]
    with pytest.raises(ph.HbkError):
        store.install_index(NEW, store.path(OLD).read_bytes())
    assert store.remove(OLD) and not store.get(OLD)


def test_readers_see_published_version_during_rebuild(store, monkeypatch):
    started, resume = threading.Event(), threading.Event()
    build = ph.build_index

    def slow(*args):
        started.set()
        assert resume.wait(5)
        return build(*args)

    monkeypatch.setattr(ph, 'build_index', slow)
    reader = store.get(NEW)
    worker = threading.Thread(target=store.build_from_bytes, args=(NEW, context_book('Обновлено.')))
    worker.start()
    try:
        assert started.wait(5) and NEW in store.status()['building']
        assert reader.search('StrFind')[0][0].description == 'Учебное описание.'
        with pytest.raises(ValueError, match='строится'):
            store.remove(NEW)
    finally:
        resume.set()
        worker.join(5)
    assert reader.search('StrFind')[0][0].description == 'Обновлено.'


def test_web_and_mcp_share_versions(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path / 'data'), str(tmp_path / 'index'))
    monkeypatch.setattr(web, '_pm', pm)
    client = TestClient(web.app, headers={'Host': '127.0.0.1'})
    store = pm.platform_help
    assert store.root == tmp_path / 'index' / 'platform-help'
    url = '/api/admin/platform-help'

    def ask(**args):
        return execute_tool(pm, 'platform_help', {'query': 'СтрНайти', **args})

    assert 'Настройки' in ask() and not ask().startswith('Error:')
    for version in (NEW, OLD):
        result = client.post(url, data={'version': version},
                             files={'context': (ph.CONTEXT_FILE, context_book(version)),
                                    'query': (ph.QUERY_FILE, query_book())})
        assert result.status_code == 200 and 'Справка загружена' in result.text
    assert ask().startswith('Справка платформы ' + NEW)
    pm.create_project('demo', 'Демо')
    settings = '/api/projects/demo/update'
    client.post(settings, data={'name': 'Демо', 'platform_version': OLD})
    assert ProjectManager(str(pm.data_dir)).get_project('demo').platform_version == OLD
    assert ask().startswith('Справка платформы ' + OLD)
    assert '<option selected>' + OLD in client.get('/projects/demo/settings?tab=general').text
    pm.create_project('second', 'Второй')
    assert NEW in ask() and OLD in ask(project_id='demo') and NEW in ask(project_id='demo', version=NEW)
    msg = ask(query='ФорматироватьСтроку', project_id='demo')
    assert OLD in msg and 'метод конфигурации' in msg and 'search_code' in msg
    assert 'offset=1' in ask(limit=1) and 'язык запросов' in ask(offset=1)
    missing = ask(version='8.3.1.1')
    assert not missing.startswith('Error:') and 'не загружена' in missing
    # Невыбранная необязательная книга в обычной HTML-форме приходит с filename="".
    multipart = (b'--x\r\nContent-Disposition: form-data; name="version"\r\n\r\n8.3.27.1\r\n'
                 b'--x\r\nContent-Disposition: form-data; name="context"; filename="shcntx_ru.hbk"\r\n\r\n'
                 + context_book() + b'\r\n--x\r\nContent-Disposition: form-data; name="query"; filename=""'
                 b'\r\nContent-Type: application/octet-stream\r\n\r\n\r\n--x--\r\n')
    response = client.post(url, content=multipart, headers={'Content-Type': 'multipart/form-data; boundary=x'})
    assert 'Справка загружена' in response.text and store.get(NEW).search('СтрНайти', 'query') == ([], False)
    data = store.path(OLD).read_bytes()
    assert client.post(f'{url}/{OLD}/delete').status_code == 200
    assert 'не загружена' in ask(project_id='demo')
    assert 'Справка загружена' in client.post(url, files={'index': ('help.db', data)}).text
    assert store.get(OLD)
    failed = client.post(url, data={'version': NEW}, files={'context': ('bad.hbk', b'bad')})
    assert 'слишком короткий' in failed.text and store.get(NEW)
    client.post(settings, data={'name': 'Демо', 'platform_version': ''})
    assert NEW in ask(project_id='demo')
    # Допустимый идентификатор проекта совпадает с именем каталога справки на томе.
    pm.create_project('platform-help', 'Пример')
    pm.get_db('platform-help')
    pm.delete_project('platform-help')
    assert store.get(NEW) and store.get(OLD)
    pm.close_all()
