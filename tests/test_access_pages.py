"""Страницы «Доступ», «Лицензия», «Вход» и ошибки: включение входа — только через
экран с последствиями, новый токен — с копированием и настройкой агента, отзыв —
через окно подтверждения, форма ключа свёрнута при действующей лицензии."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.auth import TokenStore
from src.core.project_manager import ProjectManager

HOST = {'Host': '127.0.0.1:19878'}


@pytest.fixture
def web():
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            yield pm, TestClient(app_module.app, headers=HOST)
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def test_enabling_login_goes_through_consequences_screen(web):
    pm, client = web
    page = client.get('/admin/tokens').text
    assert '<h1 class="page-title">Доступ</h1>' in page
    assert 'Вход не включён' in page and 'href="/admin/tokens?enable=1"' in page
    assert 'action="/api/admin/tokens"' not in page, 'форма первого токена — только на экране подтверждения'
    confirm = client.get('/admin/tokens?enable=1').text
    assert 'Включить вход по токенам' in confirm and 'python -m src.auth create' in confirm
    assert 'action="/api/admin/tokens"' in confirm and 'name="role"' not in confirm

    created = client.post('/api/admin/tokens', data={'name': 'Владелец'})
    assert created.status_code == 200
    text = created.text
    assert 'Токен создан' in text and 'Вход по токенам включён' in text
    assert 'id="new-token"' in text and 'readonly' in text and 'data-copy="new-token"' in text
    assert 'Как подключить агента' in text and 'id="agent-config"' in text
    # Уже включено — форма выдачи по кнопке, таблица со счётчиком.
    assert 'id="token-add"' in text and 'id="token-form-card" hidden' in text
    assert 'Токены · 1 из 1' in text


def test_revoke_uses_confirm_dialog_and_warns_about_own_token(web):
    pm, client = web
    store = TokenStore(pm.data_dir)
    _, admin = store.create('Админ', 'admin')
    page = client.get('/admin/tokens', headers={'Authorization': f'Bearer {admin}'}).text
    assert 'onsubmit="return confirm' not in page
    assert 'data-confirm="Это ваш токен' in page and 'btn btn-danger btn-sm">Отозвать' in page
    assert 'Администратор' in page or 'администратор' in page


def test_license_form_folded_when_key_valid(web, monkeypatch):
    from src import license as lic
    from test_license import install_team_license
    pm, client = web
    page = client.get('/admin/license').text
    assert '<h1 class="page-title">Лицензия</h1>' in page
    assert '<details class="disclosure" open>' in page and 'Установить ключ' in page
    install_team_license(monkeypatch, pm.data_dir, users=5)
    page = client.get('/admin/license').text
    assert 'действует' in page and '5 пользователей' in page
    assert '<details class="disclosure" >' in page and 'Заменить ключ' in page
    assert lic is not None


def test_login_field_has_label(web):
    pm, client = web
    TokenStore(pm.data_dir).create('Админ', 'admin')
    page = client.get('/login').text
    assert '<label for="login-token">Личный токен</label>' in page and 'id="login-token"' in page
    assert 'Authorization: Bearer' not in page


def test_error_page_has_one_primary_action(web):
    pm, client = web
    page = client.get('/projects/нет-такого')
    assert page.status_code == 404
    assert page.text.count('btn btn-primary') == 1 and 'К списку проектов' in page.text
    assert 'Назад' in page.text and 'Все проекты' not in page.text


def test_participant_sees_no_admin_forms(web, monkeypatch):
    """Участник не видит форм, которые сервер ему не даст сохранить (403)."""
    import shutil
    from license_helper import install_team_license
    pm, client = web
    fixtures = Path(__file__).parent / 'fixtures' / 'xml_ru'
    for pid in ('p', 'typ'):
        pm.create_project(pid, pid)
        shutil.copytree(fixtures, pm.projects_dir / pid / 'sources' / 'main' / 'xml')
        pm.add_source(pid, 'main', 'Основная')
    install_team_license(monkeypatch, pm.data_dir, users=5)
    store = TokenStore(pm.data_dir)
    _, admin = store.create('Админ', 'admin')
    _, user = store.create('Участник', 'user')
    as_user = {'Authorization': f'Bearer {user}'}
    as_admin = {'Authorization': f'Bearer {admin}'}
    for path in ('/projects/p/diff', '/projects/p/diff/update'):
        page = client.get(path, headers=as_user).text
        assert '/comparison' not in page and 'Сравнение настраивает администратор' in page
        assert '/comparison' in client.get(path, headers=as_admin).text
    danger = client.get('/projects/p/settings?tab=danger', headers=as_user).text
    assert 'Удалить проект навсегда' not in danger and 'Состав проекта' in danger
