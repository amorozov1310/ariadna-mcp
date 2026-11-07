"""Утилита выдачи ключей (tools/license_desk): реестр клиентов, выпуск и
продление ключа, файл ключа проверяется Ариадной."""

import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.license as lic
from tools.license_desk.app import add_year, create_app


@pytest.fixture
def desk(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    assert lic.main(['keygen', '--out', str(tmp_path)]) == 0
    from cryptography.hazmat.primitives import serialization
    import base64
    key = lic._private_key(tmp_path / 'ariadna_license_private.pem')
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(lic, 'PUBLIC_KEY', base64.b64encode(pub).decode())
    return tmp_path, TestClient(create_app(tmp_path))


def _issued_id(resp) -> str:
    return re.search(r'issued=([0-9a-f]+)', str(resp.url)).group(1)


def test_issue_new_customer_and_download(desk, capsys):
    home, client = desk
    assert 'Клиентов пока нет' in client.get('/').text
    resp = client.post('/issue', data={'org': 'ООО Ромашка', 'contact': 'Иван Петров',
                                       'email': 'it@example.com', 'phone': '+7 900 000-00-00',
                                       'users': '10', 'updates_until': '2027-11-07',
                                       'license_note': 'счёт 15'})
    assert resp.status_code == 200 and 'Ключ выпущен' in resp.text
    assert 'Здравствуйте, Иван Петров!' in resp.text and 'mailto:it@example.com' in resp.text
    license_id = _issued_id(resp)
    key = client.get(f'/keys/{license_id}')
    assert 'attachment' in key.headers['content-disposition']
    info = lic.parse(key.text)                         # Ариадна принимает ключ
    assert (info.owner, info.users, info.updates_until) == ('ООО Ромашка', 10, '2027-11-07')

    registry = json.loads((home / 'registry.json').read_text(encoding='utf-8'))
    assert registry['customers'][0]['email'] == 'it@example.com'
    assert registry['licenses'][0]['note'] == 'счёт 15'
    page = client.get('/?q=ромашка')
    assert 'ООО Ромашка' in page.text and '2027-11-07' in page.text
    assert 'Ничего не найдено' in client.get('/?q=лютик').text


def test_renew_prefills_next_year_and_keeps_history(desk):
    _, client = desk
    first = client.post('/issue', data={'contact': 'Пётр Сидоров', 'users': '3',
                                        'updates_until': '2099-01-31'})
    customer_url = str(first.url).split('?')[0]
    page = client.get(customer_url)
    assert 'value="2100-01-31"' in page.text and 'value="3"' in page.text   # продление на год
    customer_id = customer_url.rsplit('/', 1)[-1]
    second = client.post(f'/customers/{customer_id}/issue', data={'users': '5', 'updates_until': '2100-01-31'})
    assert lic.parse(client.get(f'/keys/{_issued_id(second)}').text).users == 5
    assert second.text.count('class="mono"') >= 2                           # оба ключа в истории
    # Данные клиента правятся без выпуска ключа.
    client.post(f'/customers/{customer_id}', data={'contact': 'Пётр Сидоров', 'email': 'p@example.com'})
    assert 'p@example.com' in client.get(customer_url).text


def test_validation_and_missing_private_key(desk, tmp_path):
    home, client = desk
    bad = client.post('/issue', data={'org': '', 'contact': '', 'users': '1', 'updates_until': '2027-01-01'})
    assert 'Укажите организацию или ФИО' in bad.text
    bad = client.post('/issue', data={'org': 'X', 'email': 'нет-почты', 'users': '1',
                                      'updates_until': '2027-01-01'})
    assert 'email' in bad.text
    assert client.get('/keys/../../registry').status_code == 404
    (home / 'ariadna_license_private.pem').unlink()
    assert 'Нет закрытого ключа' in client.get('/').text


def test_add_year_leap_day():
    assert add_year(date(2028, 2, 29)) == date(2029, 2, 28)
