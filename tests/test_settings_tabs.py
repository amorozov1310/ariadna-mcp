"""«Обзор» без форм и удаления; «Настройки проекта»: Данные (состав и индекс),
Общее, Опасная зона; права участника; возврат после действий."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore
from src.core.project_manager import ProjectManager
from tests.license_helper import install_team_license

FIXTURES = Path(__file__).parent / 'fixtures'
HOST = {'Host': '127.0.0.1:19878'}


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'Первый')
        pm.add_source('p1', 'main', 'Основная', xml_archive=_zip(FIXTURES / 'xml_ru'),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def _main(html: str) -> str:
    """Разметка <main> без встроенных скриптов (в них тексты кнопок и пути)."""
    import re
    main = html[html.index('<main'):html.index('</main>')]
    return re.sub(r'<script>.*?</script>', '', main, flags=re.S)


def test_overview_has_no_forms_or_delete(web):
    _, client = web
    main = _main(client.get('/projects/p1').text)
    assert 'Удалить' not in main and '/delete' not in main and 'type="file"' not in main
    assert 'action="/projects/p1/search"' in main                 # поиск на «Обзоре»
    assert 'href="/projects/p1/settings?tab=data"' in main
    assert 'объект' in main and 'конфигурация' in main             # сводка одной строкой


def test_settings_data_tab(web):
    _, client = web
    main = _main(client.get('/projects/p1/settings').text)        # по умолчанию — «Данные»
    assert 'Состав проекта' in main and 'parts-table' in main
    assert 'data-confirm="Удалить «Основная» из проекта' in main
    assert 'name="next" value="/projects/p1/settings?tab=data"' in main
    edit = _main(client.get('/projects/p1/settings?tab=data&edit=main').text)
    assert 'action="/api/projects/p1/sources/main/update"' in edit
    assert 'name="auto_reindex"' in edit


def test_actions_return_to_settings(web):
    pm, client = web
    resp = client.post('/api/projects/p1/sources/main/update',
                       data={'label': 'Основная конфигурация', 'next': '/projects/p1/settings?tab=data'},
                       follow_redirects=False)
    assert resp.headers['location'] == '/projects/p1/settings?tab=data'
    resp = client.post('/api/projects/p1/sources/main/update',
                       data={'label': 'x', 'next': 'https://example.com/'}, follow_redirects=False)
    assert resp.headers['location'] == '/projects/p1'           # не открытый редирект
    resp = client.post('/api/projects/p1/update', data={'name': 'Первый', 'description': ''},
                       follow_redirects=False)
    assert resp.headers['location'] == '/projects/p1/settings?tab=general'


def test_general_and_danger_tabs(web):
    _, client = web
    general = _main(client.get('/projects/p1/settings?tab=general').text)
    assert 'action="/api/projects/p1/update"' in general and 'name="base_project"' in general
    assert 'disabled>' not in general.split('Код проекта')[1][:200]    # код — текст, не поле
    danger = _main(client.get('/projects/p1/settings?tab=danger').text)
    assert 'action="/api/projects/p1/delete"' in danger and 'data-expect="p1"' in danger
    assert 'Удалить проект навсегда' in danger


def test_empty_project_settings_offers_first_upload(web):
    pm, client = web
    pm.create_project('p2', 'Второй')
    main = _main(client.get('/projects/p2/settings').text)
    assert 'Добавьте выгрузку конфигурации' in main
    assert 'name="next" value="/projects/p2/settings?tab=data"' in main


def test_member_sees_no_changing_actions(web, monkeypatch):
    pm, client = web
    install_team_license(monkeypatch, pm.data_dir)
    store = TokenStore(pm.data_dir)
    store.create('Админ', 'admin')
    _, member = store.create('Участник', 'user')
    h = {'Authorization': f'Bearer {member}'}
    data = _main(client.get('/projects/p1/settings', headers=h).text)
    assert 'Обновить индекс' in data                              # участнику можно
    assert '/delete' not in data and 'Добавить расширение' not in data and 'edit=main' not in data
    general = _main(client.get('/projects/p1/settings?tab=general', headers=h).text)
    assert 'action="/api/projects/p1/update"' not in general and 'name="base_project"' not in general
    page = client.get('/projects/p1/settings?tab=danger', headers=h).text
    assert 'Опасная зона' not in page and '/api/projects/p1/delete' not in page


def test_overview_kinds_section_is_visible(web):
    _, client = web
    main = _main(client.get('/projects/p1').text)
    assert 'Состав конфигурации</h2>' in main and 'щелчок по строке' in main
    assert main.count('class="kind-row"') >= 1 and '/search?kind=' in main
