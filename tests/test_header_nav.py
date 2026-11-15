"""Шапка Web UI: пять разделов проекта и «Ещё», меню сервера / пользователя,
что видно локально, администратору и участнику."""

import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore
from src.core.project_manager import ProjectManager
from tests.license_helper import install_team_license

HOST = {'Host': '127.0.0.1:19878'}


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'Первый')
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def _header(html: str) -> str:
    return html[html.index('<header'):html.index('</header>')]


def _main_sections(header: str) -> list[str]:
    nav = header[header.index('project-nav'):]
    nav = nav[:nav.index('<details')]                     # до «Ещё»
    return [re.sub(r'<[^>]+>', '', a).strip() for a in re.findall(r'<a class="navlink".*?</a>', nav, re.S)]


def test_project_header_local_mode(web):
    _, client = web
    header = _header(client.get('/projects/p1').text)
    assert _main_sections(header) == ['Обзор', 'Поиск', 'Вызовы', 'Доработки', 'Проверка вызовов']
    for path in ('/projects/p1/glossary', '/projects/p1/playground', '/projects/p1/settings'):
        assert f'href="{path}"' in header                 # в «Ещё» и в «☰»
    # Локально — меню сервера: доступ, лицензия, тема; выхода нет.
    assert 'href="/admin/tokens"' in header and 'href="/admin/license"' in header
    assert 'href="/logout"' not in header
    assert 'Создать проект' in header                     # в списке проектов переключателя


def test_more_highlighted_and_search_field(web):
    _, client = web
    header = _header(client.get('/projects/p1/glossary').text)
    assert re.search(r'<summary class="navlink" aria-current="page"', header)   # «Ещё» подсвечено
    assert 'class="appsearch"' in header
    assert 'class="appsearch"' not in _header(client.get('/projects/p1/search').text)


def test_projects_page_create_button_in_page_header(web):
    _, client = web
    page = client.get('/').text
    assert 'Создать проект' not in _header(page)
    body = page[page.index('</header>'):]
    assert 'class="page-header"' in body and 'href="/projects/new"' in body


def test_admin_and_member_menus(web, monkeypatch):
    pm, client = web
    install_team_license(monkeypatch, pm.data_dir)
    store = TokenStore(pm.data_dir)
    _, admin = store.create('Анна Админ', 'admin')
    _, member = store.create('Пётр Участник', 'user')

    header = _header(client.get('/projects/p1', headers={'Authorization': f'Bearer {admin}'}).text)
    assert 'Анна Админ' in header and 'администратор' in header
    assert 'href="/admin/tokens"' in header and 'href="/logout"' in header

    as_member = {'Authorization': f'Bearer {member}'}
    header = _header(client.get('/projects/p1', headers=as_member).text)
    assert 'Пётр Участник' in header and 'участник' in header and 'href="/logout"' in header
    assert 'href="/admin/tokens"' not in header and 'href="/admin/license"' not in header
    assert 'Создать проект' not in header
    page = client.get('/', headers=as_member).text
    assert 'href="/projects/new"' not in page


def test_text_filters():
    from datetime import datetime, timedelta
    from src.web.app import _dt, _plural
    assert [_plural(n, 'часть', 'части', 'частей') for n in (1, 2, 5, 11, 21, 104)] == \
        ['1 часть', '2 части', '5 частей', '11 частей', '21 часть', '104 части']
    now = datetime.now().replace(microsecond=0)
    assert _dt(now.isoformat()) == f'сегодня {now:%H:%M}'
    assert _dt((now - timedelta(days=1)).isoformat()).startswith('вчера ')
    assert _dt('2025-03-07T22:50:07.123') == '07.03.2025 22:50'
    assert _dt('') == '' and _dt('не дата') == 'не дата'


def test_theme_menu_has_three_choices(web):
    """Тема — «Системная / Светлая / Тёмная», текущая отмечается скриптом (Q10)."""
    _, client = web
    header = _header(client.get('/projects/p1').text)
    for mode, label in (('system', 'Системная'), ('light', 'Светлая'), ('dark', 'Тёмная')):
        assert f'data-theme-set="{mode}"' in header and label in header
    assert 'Сменить тему' not in header


def test_switcher_shows_state_and_filters_long_lists(web):
    """Переключатель проекта: точка состояния у каждого, фильтр — от 11 проектов (A7)."""
    pm, client = web
    header = _header(client.get('/projects/p1').text)
    assert 'status-dot status-dot-empty' in header and 'switcher-filter' not in header
    for i in range(10):
        pm.create_project(f'x{i}', f'Проект {i}')
    header = _header(client.get('/projects/p1').text)
    assert 'switcher-filter' in header and header.count('class="switcher-item"') == 11
