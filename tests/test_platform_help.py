"""Поиск, публикация версий и доступ к справке через Web UI и MCP."""

import asyncio
import io
import sqlite3
import struct
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from src.core import platform_help as ph
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool
from src.web import app as web
from tests.fixtures.generate_platform_help import _book, _container, context_book, query_book

OLD, NEW = '8.3.9.1', '8.3.27.1'
UPLOAD_URL = '/api/admin/platform-help'


@pytest.fixture
def store(tmp_path):
    store = ph.PlatformHelpStore(tmp_path / 'справка')
    store.build_from_bytes(NEW, context_book(), query_book())
    return store


@pytest.fixture
def help_client(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path / 'data'))
    monkeypatch.setattr(web, '_pm', pm)
    with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
        yield client, pm
    pm.close_all()


def change_index(path, sql, params=()):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(sql, params)
        conn.commit()


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


def test_streaming_upload_stops_before_oversize_chunk_is_written(help_client, monkeypatch):
    _, pm = help_client
    limit = 256
    monkeypatch.setattr(ph, 'MAX_UPLOAD_BYTES', limit)
    monkeypatch.setattr(MultiPartParser, 'spool_max_size', 32)
    written = []
    write = UploadFile.write

    async def track(upload, data):
        written.append((upload.file, len(data)))
        await write(upload, data)

    monkeypatch.setattr(UploadFile, 'write', track)
    body = (b'--x\r\nContent-Disposition: form-data; name="version"\r\n\r\n8.3.27.1\r\n'
            b'--x\r\nContent-Disposition: form-data; name="context"; filename="test.hbk"\r\n\r\n')

    async def request():
        async def chunks():
            yield body
            for _ in range(6):
                yield b'a' * 64
            yield b'\r\n--x--\r\n'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                     base_url='http://127.0.0.1', follow_redirects=True) as client:
            return await client.post(UPLOAD_URL, content=chunks(),
                                     headers={'Content-Type': 'multipart/form-data; boundary=x'})

    response = asyncio.run(request())
    assert response.status_code == 200 and 'размер файла превышает' in response.text
    assert sum(size for _, size in written) <= limit
    assert written and all(file.closed for file, _ in written)
    assert not pm.platform_help.get(NEW)


def test_upload_rejects_content_length_without_reading_body(help_client, monkeypatch):
    monkeypatch.setattr(ph, 'MAX_UPLOAD_BYTES', 256)

    async def chunks():
        pytest.fail('тело запроса со слишком большим Content-Length начали читать')
        yield b''

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app),
                                     base_url='http://127.0.0.1', follow_redirects=True) as client:
            return await client.post(UPLOAD_URL, content=chunks(), headers={
                'Content-Length': str(1024 * 1024),
                'Content-Type': 'multipart/form-data; boundary=x',
            })

    response = asyncio.run(request())
    assert response.status_code == 200 and 'размер запроса превышает' in response.text


def test_upload_rejects_extra_file_field(help_client, monkeypatch):
    client, pm = help_client
    write = UploadFile.write

    async def forbid_extra(upload, data):
        assert upload.filename != 'extra.hbk'
        await write(upload, data)

    monkeypatch.setattr(UploadFile, 'write', forbid_extra)
    response = client.post(UPLOAD_URL, data={'version': NEW}, files=[
        ('context', ('test.hbk', context_book())),
        ('unexpected', ('extra.hbk', b'extra')),
    ])
    assert response.status_code == 200 and 'неожиданное файловое поле' in response.text
    assert not pm.platform_help.get(NEW)


def test_local_book_size_checked_before_reading(tmp_path, store, monkeypatch):
    monkeypatch.setattr(ph, 'MAX_UPLOAD_BYTES', 256)
    book = tmp_path / ph.CONTEXT_FILE
    book.write_bytes(context_book())
    open_file = Path.open

    def open_checked(path, *args, **kwargs):
        assert path != book, 'слишком большую книгу открыли для чтения'
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', open_checked)
    with pytest.raises(ph.HbkError, match='размер файла превышает'):
        store.build_from_dir(tmp_path, OLD)
    assert not store.get(OLD)


def test_duplicate_zip_names_use_one_page_and_one_budget(store, monkeypatch):
    name = 'objects/Global context/methods/Demo.html'
    first, last = '<h1>Первая</h1>', '<h1>Пример (Demo)</h1>' + ' ' * 1024
    with pytest.warns(UserWarning, match='Duplicate name'):
        book = _book([(name, 'Пример', first), (name, 'Пример', last)])
    monkeypatch.setattr(ph, '_MAX_UNPACKED_BYTES', len(last.encode()))
    assert store.build_from_bytes(OLD, book)['entries'] == 1
    entries, more = store.get(OLD).search('Demo')
    assert [e.name_ru for e in entries] == ['Пример'] and not more
    monkeypatch.setattr(ph, '_MAX_UNPACKED_BYTES', len(last.encode()) - 1)
    with pytest.raises(ph.HbkError, match='объём распакованной справки'):
        store.build_from_bytes(OLD, book)


def test_html_text_with_unclosed_tags_is_linear():
    assert ph.html_text('А<br/>Б</P><ul><LI>В</li></ul>&lt;') == 'А\nБ\n\n• В\n<'
    assert ph.html_text('а < б<br>в') == 'а < б\nв'
    tail = '<' * (2 * 1024 * 1024)
    # Проверка меньшего фрагмента быстро выявляет квадратичность при регрессии.
    for fragment, budget in [(tail[:64 * 1024], 0.8), (tail, 1.0)]:
        started = time.monotonic()
        assert ph.html_text(fragment) == fragment
        assert time.monotonic() - started < budget


def test_return_type_survives_index_and_reaches_agent(help_client):
    _, pm = help_client
    store = pm.platform_help
    store.build_from_bytes(NEW, context_book())
    entry = store.get(NEW).search('StrFind')[0][0]
    assert entry.returns_types == 'Число'
    result = execute_tool(pm, 'platform_help', {'query': 'StrFind', 'version': NEW})
    assert 'Возвращает: Число — Учебный результат.' in result


def test_old_schema_is_stale_and_cannot_be_uploaded(tmp_path, store):
    path = store.path(NEW)
    if ph.SCHEMA_VERSION != '1':
        change_index(path, 'ALTER TABLE entries DROP COLUMN returns_types')
    change_index(path, "UPDATE meta SET value='1' WHERE key='schema'")
    assert store.versions()[0]['stale']
    assert store.get(NEW).search('StrFind')[0][0].returns_types == ''
    other = ph.PlatformHelpStore(tmp_path / 'импорт')
    with pytest.raises(ph.HbkError, match='совместимой версией'):
        other.install_index(NEW, path.read_bytes())
    assert not other.get(NEW)


@pytest.mark.parametrize('preexisting', [False, True])
def test_auto_build_rechecks_version_after_listing(tmp_path, store, monkeypatch, preexisting):
    directory = tmp_path / OLD / 'bin'
    directory.mkdir(parents=True)
    (directory / ph.CONTEXT_FILE).write_bytes(context_book('Локальная книга.'))
    donor = ph.PlatformHelpStore(tmp_path / 'донор')
    donor.build_from_bytes(OLD, context_book('Загруженная справка.'))
    if preexisting:
        store.build_from_bytes(OLD, context_book('Ранее загруженная справка.'))
        change_index(store.path(OLD), "UPDATE meta SET value='old' WHERE key='fingerprint'")
        change_index(donor.path(OLD), "UPDATE meta SET value='old' WHERE key='fingerprint'")
    versions = store.versions

    def stale_snapshot():
        snapshot = versions()
        store.install_index(OLD, donor.path(OLD).read_bytes())
        return snapshot

    monkeypatch.setattr(store, 'versions', stale_snapshot)
    store.sync_installed([directory], background=False)
    assert store.get(OLD).search('StrFind')[0][0].description == 'Загруженная справка.'


@pytest.mark.parametrize(('sql', 'params'), [
    ('UPDATE entries SET variants=? WHERE id=2', ('{',)),
    ('UPDATE entries SET variants=? WHERE id=2', ('[{"params":[{}]}]',)),
    ('DELETE FROM entries_fts WHERE rowid=2', ()),
    ("UPDATE entries_fts SET names='Другой текст' WHERE rowid=2", ()),
    ('DELETE FROM entries_fts_data WHERE id=(SELECT MAX(id) FROM entries_fts_data)', ()),
    ('ALTER TABLE entries DROP COLUMN returns_types', ()),
])
def test_import_validates_all_entries_and_fts(tmp_path, store, sql, params):
    original = store.path(NEW).read_bytes()
    damaged = tmp_path / 'повреждённый.db'
    damaged.write_bytes(original)
    change_index(damaged, sql, params)
    with pytest.raises(ph.HbkError, match='повреждён индекс справки'):
        store.install_index(NEW, damaged.read_bytes())
    assert store.path(NEW).read_bytes() == original


def test_corrupt_counts_do_not_hide_working_version(help_client):
    client, pm = help_client
    store = pm.platform_help
    for version in (OLD, NEW):
        store.build_from_bytes(version, context_book())
    change_index(store.path(NEW), "UPDATE meta SET value='{' WHERE key='counts'")
    response = client.get('/admin/platform-help')
    assert response.status_code == 200
    assert NEW in response.text and 'Индекс повреждён' in response.text
    assert [item['corrupt'] for item in store.versions()] == [True, False]
    assert execute_tool(pm, 'platform_help', {'query': 'StrFind'}).startswith('Справка платформы ' + OLD)


def test_unreadable_version_directory_has_friendly_error(help_client, monkeypatch):
    client, pm = help_client
    root = pm.platform_help.root
    root.mkdir()
    iterdir = Path.iterdir

    def denied(path):
        if path == root:
            raise PermissionError('нет прав')
        return iterdir(path)

    monkeypatch.setattr(Path, 'iterdir', denied)
    response = client.get('/admin/platform-help')
    assert response.status_code == 200
    assert 'Не удалось прочитать каталог справки платформы' in response.text
    assert pm.platform_help.versions() == []


@pytest.mark.parametrize('member', ['FileStorage', 'PackBlock'])
def test_bad_deflate_is_reported_without_replacing_index(help_client, member):
    client, pm = help_client
    store = pm.platform_help
    store.build_from_bytes(NEW, context_book())
    original = store.path(NEW).read_bytes()
    items = ph.read_container(context_book())
    data = bytearray(items[member])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        offset = archive.infolist()[0].header_offset
    name_len, extra_len = struct.unpack_from('<HH', data, offset + 26)
    data[offset + 30 + name_len + extra_len] = 0xff  # Зарезервированный тип блока DEFLATE.
    items[member] = bytes(data)
    response = client.post(UPLOAD_URL, data={'version': NEW},
                           files={'context': ('test.hbk', _container(items))})
    assert response.status_code == 200 and 'не удалось прочитать' in response.text
    assert store.path(NEW).read_bytes() == original


def test_documented_docker_mount_is_discoverable(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    operations = (root / 'docs' / 'operations.md').read_text(encoding='utf-8')
    snippet = operations.partition('```yaml')[2].partition('```')[0]
    lines = snippet.splitlines()
    target, = (line.split(': ', 1)[1].strip() for line in lines if 'target: /platform/' in line)
    env, = (line.split(': ', 1)[1].strip() for line in lines if 'ARIADNA_PLATFORM:' in line)
    assert 'C:\\Program Files\\1cv8\\' in snippet and 'read_only: true' in snippet
    directory = tmp_path / target.removeprefix('/')
    directory.mkdir(parents=True)
    (directory / ph.CONTEXT_FILE).write_bytes(context_book())
    monkeypatch.setenv('ARIADNA_PLATFORM', str(tmp_path / env.removeprefix('/')))
    assert ph.find_installed() == {directory.parent.name: directory}
