"""Лицензия командного режима (src/license.py): без ключа — один токен,
с ключом — сколько в нём указано, пока версия вышла до конца обновлений."""

import base64
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.license as lic
from src.auth import TokenStore, main as auth_cli
from src.core.project_manager import ProjectManager
from tests.license_helper import install_team_license, make_keypair

HOST = {'Host': '127.0.0.1:19878'}


def test_issue_parse_and_tamper(monkeypatch):
    key = make_keypair(monkeypatch)
    text = lic.issue(key, 'ООО «Ромашка»', 5, '2027-11-06', issued='2026-11-06', license_id='abc')
    info = lic.parse(text)
    assert (info.owner, info.users, info.updates_until, info.id) == ('ООО «Ромашка»', 5, '2027-11-06', 'abc')
    # Подменить число пользователей нельзя: подпись не сойдётся.
    lines = text.splitlines()
    forged = base64.b64encode(base64.b64decode(lines[1]).replace(b'"users":5', b'"users":500')).decode()
    with pytest.raises(lic.LicenseError, match='подпись'):
        lic.parse('\n'.join([lines[0], forged, *lines[2:]]))
    with pytest.raises(lic.LicenseError, match='не ключ'):
        lic.parse('просто текст')
    # Ключ другой пары (чужой издатель) не принимается.
    other = lic.issue(make_keypair(monkeypatch), 'x', 1, '2027-01-01')
    make_keypair(monkeypatch)
    with pytest.raises(lic.LicenseError):
        lic.parse(other)


def test_updates_until_against_release_date(monkeypatch):
    info = lic.parse(lic.issue(make_keypair(monkeypatch), 'Ромашка', 3, '2027-01-31'))
    assert lic.check(info, released=date(2027, 1, 31)).valid           # день в день — можно
    assert lic.check(info, released=date(2026, 6, 1)).users == 3
    late = lic.check(info, released=date(2027, 2, 1))
    assert late.status == 'outdated' and late.users == 1 and 'продлите' in late.message


def test_release_date_file():
    assert date(2026, 1, 1) <= lic.release_date() <= date.today()


def test_owner_public_key_is_embedded():
    # Встроенный ключ — настоящие 32 байта Ed25519, а не заглушка.
    lic._public_key()


def test_without_license_only_one_token(tmp_path):
    store = TokenStore(tmp_path)
    store.create('Владелец', 'admin')
    with pytest.raises(ValueError, match='лицензии'):
        store.create('Коллега', 'user')
    assert auth_cli(['--data-dir', str(tmp_path), 'create', '--name', 'Робот']) == 1


def test_license_limits_and_degrades(tmp_path, monkeypatch):
    key = install_team_license(monkeypatch, tmp_path, users=3)
    store = TokenStore(tmp_path)
    store.create('Разработчик', 'user')                 # самый старый, но не админ
    admin, _ = store.create('Админ', 'admin')
    third, _ = store.create('Робот', 'user')
    with pytest.raises(ValueError, match='на 3 токенов'):
        store.create('Четвёртый', 'user')
    assert all(store.denied(t) is None for t in store.list())

    # Ключ устарел для этой версии (обновление без продления) — работает
    # один токен, и это администратор, а не самый старый токен.
    (tmp_path / lic.FILENAME).write_text(lic.issue(key, 'Команда', 3, '2000-01-01'), encoding='utf-8')
    store = TokenStore(tmp_path)
    assert store.denied(admin) is None
    reason = store.denied(third)
    assert reason and 'Робот' in reason and 'продлите' in reason

    # Ключ удалили — то же самое.
    (tmp_path / lic.FILENAME).unlink()
    assert TokenStore(tmp_path).denied(third)


def test_install_rejects_bad_key_and_keeps_old(tmp_path, monkeypatch):
    install_team_license(monkeypatch, tmp_path, users=4)
    store = lic.LicenseStore(tmp_path)
    with pytest.raises(lic.LicenseError):
        store.install('-----BEGIN ARIADNA LICENSE-----\nAAAA\nAAAA\n-----END ARIADNA LICENSE-----')
    assert store.state().valid and store.state().users == 4


def test_cli_keygen_issue_show(tmp_path, capsys, monkeypatch):
    assert lic.main(['keygen', '--out', str(tmp_path / 'keys')]) == 0
    pub = capsys.readouterr().out.strip().splitlines()[-1].split(': ')[-1]
    monkeypatch.setattr(lic, 'PUBLIC_KEY', pub)
    assert lic.main(['keygen', '--out', str(tmp_path / 'keys')]) == 1    # не перезаписывает
    out = tmp_path / 'client.key'
    assert lic.main(['issue', '--private-key', str(tmp_path / 'keys' / 'ariadna_license_private.pem'),
                     '--owner', 'Ромашка', '--users', '7', '--updates-until', '2099-01-01',
                     '--out', str(out)]) == 0
    assert lic.main(['install', str(out), '--data-dir', str(tmp_path / 'data')]) == 0
    capsys.readouterr()
    assert lic.main(['show', '--data-dir', str(tmp_path / 'data')]) == 0
    assert 'users: 7' in capsys.readouterr().out


# ---------------------------------------------------------------- Web UI

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


def test_web_license_agreement(web):
    _, client = web
    assert 'href="/license/agreement"' in client.get('/admin/license').text

    agreement = client.get('/license/agreement')
    assert agreement.status_code == 200
    assert agreement.headers['content-type'] == 'text/plain; charset=utf-8'
    assert agreement.content == (Path(__file__).resolve().parents[1] / 'LICENSE').read_bytes()


def test_web_license_page_and_install(web, monkeypatch):
    pm, client = web
    key = make_keypair(monkeypatch)
    page = client.get('/admin/license')
    assert page.status_code == 200 and 'нет ключа' in page.text
    # Второй токен без ключа — отказ с объяснением.
    client.post('/api/admin/tokens', data={'name': 'Владелец'})
    resp = client.post('/api/admin/tokens', data={'name': 'Коллега'})
    assert 'Без лицензии' in resp.text
    bad = client.post('/api/admin/license', data={'key': 'мусор'}, headers={'Accept': 'application/json'})
    assert bad.status_code == 400
    text = lic.issue(key, 'Ромашка', 5, '2099-01-01')
    ok = client.post('/api/admin/license', files={'key_file': ('r.key', text.encode(), 'text/plain')})
    assert ok.status_code == 200 and 'Ромашка' in ok.text and 'действует' in ok.text
    resp = client.post('/api/admin/tokens', data={'name': 'Коллега'})
    assert 'Коллега' in resp.text and 'ar_' in resp.text
    assert len(TokenStore(pm.data_dir).list()) == 2


def test_token_over_limit_gets_403(web, monkeypatch):
    pm, client = web
    key = install_team_license(monkeypatch, pm.data_dir, users=2)
    store = TokenStore(pm.data_dir)
    _, admin = store.create('Админ', 'admin')
    _, user = store.create('Разработчик', 'user')
    as_user = {'Authorization': f'Bearer {user}'}
    assert client.get('/', headers=as_user).status_code == 200
    (Path(pm.data_dir) / lic.FILENAME).write_text(lic.issue(key, 'x', 2, '2000-01-01'), encoding='utf-8')
    denied = client.get('/', headers={**as_user, 'Accept': 'application/json'})
    assert denied.status_code == 403 and 'Разработчик' in denied.json()['detail']
    page = client.get('/', headers=as_user)
    assert page.status_code == 403 and 'Лицензия' in page.text
    assert client.get('/admin/license', headers={'Authorization': f'Bearer {admin}'}).status_code == 200
    # Пользователь не устанавливает ключ.
    store.license.install(lic.issue(key, 'x', 2, '2099-01-01'))
    assert client.post('/api/admin/license', headers=as_user, data={'key': 'x'}).status_code == 403
