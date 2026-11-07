"""
Выдача ключей Ариадны — локальная утилита владельца (docs/license.md).

Реестр клиентов и выданных ключей, выпуск нового ключа и продление в пару
кликов, файл ключа и текст письма для отправки. Всё хранится рядом с
закрытым ключом, вне репозитория:

    ~/.ariadna-license/                 (или ARIADNA_LICENSE_HOME)
        ariadna_license_private.pem     закрытый ключ (python -m src.license keygen)
        registry.json                   клиенты и выданные ключи
        keys/<id>.key                   файлы выданных ключей

Запуск: `python -m tools.license_desk` — откроется http://127.0.0.1:9890.
Сервер слушает только этот компьютер. В Docker-образ утилита не попадает.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment, select_autoescape

from src import license as lic
from src.core.fsretry import replace_with_retry

PRIVATE_KEY_NAME = 'ariadna_license_private.pem'
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def default_home() -> Path:
    return Path(os.environ.get('ARIADNA_LICENSE_HOME') or Path.home() / '.ariadna-license')


def add_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:                  # 29 февраля
        return d.replace(year=d.year + 1, day=28)


class Registry:
    """Клиенты и выданные ключи в registry.json (атомарная запись)."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / 'registry.json'
        self.keys_dir = self.home / 'keys'
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            data = {}
        data.setdefault('customers', [])
        data.setdefault('licenses', [])
        return data

    def _save(self, data: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        replace_with_retry(tmp, self.path)

    def private_key_path(self) -> Path:
        return self.home / PRIVATE_KEY_NAME

    def customer(self, data: dict, customer_id: str) -> dict:
        found = next((c for c in data['customers'] if c['id'] == customer_id), None)
        if found is None:
            raise HTTPException(404, 'Клиент не найден')
        return found

    def save_customer(self, fields: dict, customer_id: str | None = None) -> dict:
        fields = {k: (v or '').strip() for k, v in fields.items()}
        if not fields.get('org') and not fields.get('contact'):
            raise ValueError('Укажите организацию или ФИО')
        if fields.get('email') and not _EMAIL.match(fields['email']):
            raise ValueError(f'Похоже, email с ошибкой: {fields["email"]}')
        with self._lock:
            data = self.load()
            if customer_id:
                customer = self.customer(data, customer_id)
                customer.update(fields)
            else:
                customer = {'id': secrets.token_hex(4), 'created': date.today().isoformat(), **fields}
                data['customers'].append(customer)
            self._save(data)
        return customer

    def issue(self, customer_id: str, users: int, updates_until: str, note: str = '') -> dict:
        """Выпустить ключ клиенту: файл в keys/ и запись в реестре."""
        if users < 1:
            raise ValueError('Пользователей — не меньше 1')
        try:
            until = date.fromisoformat(updates_until)
        except ValueError:
            raise ValueError('Дата «обновления до» — в формате ГГГГ-ММ-ДД')
        key_path = self.private_key_path()
        if not key_path.exists():
            raise ValueError(f'Нет закрытого ключа {key_path} — создайте его: '
                             f'python -m src.license keygen --out "{self.home}"')
        private_key = lic._private_key(key_path)
        with self._lock:
            data = self.load()
            customer = self.customer(data, customer_id)
            owner = customer['org'] or customer['contact']
            license_id = secrets.token_hex(6)
            text = lic.issue(private_key, owner, users, until.isoformat(), license_id=license_id)
            lic.parse(text, public_key=private_key.public_key())       # подпись сходится
            self.keys_dir.mkdir(parents=True, exist_ok=True)
            (self.keys_dir / f'{license_id}.key').write_text(text, encoding='utf-8')
            record = {'id': license_id, 'customer_id': customer_id, 'owner': owner,
                      'users': users, 'updates_until': until.isoformat(),
                      'issued': date.today().isoformat(),
                      'issued_at': datetime.now().isoformat(timespec='seconds'), 'note': note.strip()}
            data['licenses'].append(record)
            self._save(data)
        return record

    def key_text(self, license_id: str) -> str:
        if not re.fullmatch(r'[0-9a-f]{6,32}', license_id):
            raise HTTPException(404, 'Ключ не найден')
        try:
            return (self.keys_dir / f'{license_id}.key').read_text(encoding='utf-8')
        except FileNotFoundError:
            raise HTTPException(404, 'Файл ключа не найден')


def letter(customer: dict, record: dict) -> tuple[str, str]:
    """Тема и текст письма клиенту (файл ключа прикладывается вручную)."""
    until = date.fromisoformat(record['updates_until'])
    greeting = f'Здравствуйте, {customer["contact"]}!' if customer.get('contact') else 'Здравствуйте!'
    subject = f'Лицензионный ключ Ариадны — {record["owner"]}'
    body = f'''{greeting}

Во вложении — лицензионный ключ Ариадны (файл ariadna_{record["id"]}.key):
владелец «{record["owner"]}», до {record["users"]} пользователей,
обновления и поддержка до {until:%d.%m.%Y}.

Как установить: Web UI Ариадны → «Лицензия» → выбрать файл ключа →
«Установить ключ». Или на сервере — положить файл в каталог data/ рядом
с docker-compose.yml и выполнить:
  docker compose exec ariadna python -m src.license install /data/ariadna_{record["id"]}.key
Перезапуск не нужен. После установки на странице «Токены» можно выдать
личные токены коллегам и роботу выгрузки.

Ключ бессрочный: он работает со всеми версиями Ариадны, вышедшими до
{until:%d.%m.%Y}. Более новые версии потребуют продления.
'''
    return subject, body


# ---------------------------------------------------------------- страницы

_BASE = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Выдача ключей{% endblock %} — Ариадна</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7280; --line:#e3e6eb;
        --accent:#1f8a70; --accent-fg:#fff; --warn:#a15c00; --warn-bg:#fff4e0; --ok:#1f8a70; --ok-bg:#e3f5ee; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1c2027; --fg:#e6e8eb; --muted:#9aa1ab;
        --line:#2c323b; --accent:#3cb391; --accent-fg:#0d1512; --warn:#f0b35a; --warn-bg:#33270f; --ok:#5fd0a8; --ok-bg:#15302a; } }
* { box-sizing:border-box } body { margin:0; font:14px/1.5 system-ui, "Segoe UI", sans-serif; background:var(--bg); color:var(--fg) }
header { padding:12px 20px; border-bottom:1px solid var(--line); background:var(--card); display:flex; gap:16px; align-items:center }
header a { color:var(--fg); text-decoration:none; font-weight:600 } main { max-width:980px; margin:20px auto; padding:0 16px }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:16px 20px; margin-bottom:16px }
h1 { font-size:18px; margin:0 0 12px } h2 { font-size:15px; margin:0 0 10px }
table { width:100%; border-collapse:collapse } th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top }
th { color:var(--muted); font-weight:500; font-size:12px } a { color:var(--accent) }
.muted { color:var(--muted) } .mono { font-family:ui-monospace, Consolas, monospace; font-size:12px }
.btn { display:inline-block; border:1px solid var(--line); background:var(--card); color:var(--fg); border-radius:6px; padding:6px 12px; cursor:pointer; font:inherit; text-decoration:none }
.btn-primary { background:var(--accent); border-color:var(--accent); color:var(--accent-fg) }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:12px }
label { display:block; font-size:12px; color:var(--muted) } input, textarea { width:100%; margin-top:3px; padding:6px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); font:inherit }
.badge { font-size:12px; padding:1px 8px; border-radius:10px } .ok { background:var(--ok-bg); color:var(--ok) } .old { background:var(--warn-bg); color:var(--warn) }
.notice { padding:10px 14px; border-radius:6px; margin-bottom:12px; background:var(--warn-bg); color:var(--warn) }
.done { background:var(--ok-bg); color:var(--ok) } .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center }
pre { white-space:pre-wrap; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:10px; margin:6px 0 }
</style></head><body>
<header><a href="/">Ариадна · выдача ключей</a><span class="muted" style="font-size:12px">{{ home }}</span></header>
<main>
{% if not has_key %}<div class="notice">Нет закрытого ключа <span class="mono">{{ home }}/ariadna_license_private.pem</span> —
выпускать ключи нельзя. Создайте: <span class="mono">python -m src.license keygen --out "{{ home }}"</span></div>{% endif %}
{% if error %}<div class="notice">{{ error }}</div>{% endif %}
{% block content %}{% endblock %}
</main></body></html>'''

_CUSTOMER_FIELDS = '''<div class="grid">
  <label>Организация<input name="org" value="{{ c.org or '' }}" placeholder="ООО Ромашка"></label>
  <label>Контактное лицо (ФИО)<input name="contact" value="{{ c.contact or '' }}" placeholder="Иван Петров"></label>
  <label>Email<input name="email" type="email" value="{{ c.email or '' }}" placeholder="it@example.com"></label>
  <label>Телефон<input name="phone" value="{{ c.phone or '' }}" placeholder="+7 …"></label>
</div>
<label style="margin-top:10px">Заметка<input name="note" value="{{ c.note or '' }}" placeholder="счёт №…, договор…"></label>'''

_ISSUE_FIELDS = '''<div class="grid" style="margin-top:10px">
  <label>Пользователей (токенов)<input name="users" type="number" min="1" value="{{ users }}" required></label>
  <label>Обновления до<input name="updates_until" type="date" value="{{ until }}" required></label>
  <label>Основание (счёт, договор)<input name="license_note" value=""></label>
</div>'''

_INDEX = '''{% extends "base" %}{% block content %}
<div class="card">
  <div class="row" style="justify-content:space-between"><h1>Клиенты</h1>
  <form method="get" class="row"><input name="q" value="{{ q }}" placeholder="поиск: организация, ФИО, email" style="width:260px"><button class="btn">Найти</button></form></div>
  {% if rows %}<table>
    <tr><th>Клиент</th><th>Контакты</th><th>Последний ключ</th><th>Обновления до</th><th></th></tr>
    {% for c, last in rows %}<tr>
      <td><a href="/customers/{{ c.id }}"><b>{{ c.org or c.contact }}</b></a>{% if c.org and c.contact %}<div class="muted">{{ c.contact }}</div>{% endif %}</td>
      <td>{{ c.email }}{% if c.phone %}<div class="muted">{{ c.phone }}</div>{% endif %}</td>
      <td>{% if last %}{{ last.users }} польз., {{ last.issued }}{% else %}<span class="muted">—</span>{% endif %}</td>
      <td>{% if last %}<span class="badge {{ 'ok' if last.updates_until >= today else 'old' }}">{{ last.updates_until }}</span>{% endif %}</td>
      <td><a class="btn" href="/customers/{{ c.id }}#issue">Выдать ключ</a></td>
    </tr>{% endfor %}
  </table>{% else %}<p class="muted">{{ 'Ничего не найдено.' if q else 'Клиентов пока нет — выдайте первый ключ ниже.' }}</p>{% endif %}
</div>
<div class="card" id="new"><h2>Новый клиент и ключ</h2>
  <form method="post" action="/issue">''' + _CUSTOMER_FIELDS + _ISSUE_FIELDS + '''
  <div style="margin-top:12px"><button class="btn btn-primary" {{ 'disabled' if not has_key }}>Выпустить ключ</button></div></form>
</div>{% endblock %}'''

_CUSTOMER = '''{% extends "base" %}{% block title %}{{ c.org or c.contact }}{% endblock %}{% block content %}
{% if issued %}<div class="card"><div class="notice done">Ключ выпущен: {{ issued.users }} польз., обновления до {{ issued.updates_until }}.</div>
  <div class="row"><a class="btn btn-primary" href="/keys/{{ issued.id }}">Скачать файл ключа</a>
  {% if c.email %}<a class="btn" href="mailto:{{ c.email }}?subject={{ subject|urlencode }}&body={{ body|urlencode }}">Письмо в почтовой программе</a>{% endif %}
  <button class="btn" type="button" onclick="navigator.clipboard.writeText(document.getElementById('letter').innerText)">Скопировать текст письма</button></div>
  <p class="muted" style="margin:8px 0 0">Файл ключа приложите к письму вручную — ссылка mailto вложений не передаёт.</p>
  <pre id="letter">{{ body }}</pre></div>{% endif %}
<div class="card"><h1>{{ c.org or c.contact }}</h1>
  <form method="post" action="/customers/{{ c.id }}">''' + _CUSTOMER_FIELDS + '''
  <div style="margin-top:12px"><button class="btn">Сохранить данные клиента</button> <span class="muted">клиент с {{ c.created }}</span></div></form>
</div>
<div class="card"><h2>Выданные ключи</h2>
  {% if licenses %}<table><tr><th>Выдан</th><th>Пользователей</th><th>Обновления до</th><th>Номер</th><th>Основание</th><th></th></tr>
  {% for l in licenses %}<tr><td>{{ l.issued }}</td><td>{{ l.users }}</td>
    <td><span class="badge {{ 'ok' if l.updates_until >= today else 'old' }}">{{ l.updates_until }}</span></td>
    <td class="mono">{{ l.id }}</td><td>{{ l.note }}</td>
    <td><a href="/keys/{{ l.id }}">файл</a> · <a href="/customers/{{ c.id }}?issued={{ l.id }}">письмо</a></td></tr>{% endfor %}
  </table>{% else %}<p class="muted">Ключей ещё нет.</p>{% endif %}
</div>
<div class="card" id="issue"><h2>{{ 'Продлить или изменить — новый ключ' if licenses else 'Выдать ключ' }}</h2>
  <form method="post" action="/customers/{{ c.id }}/issue">''' + _ISSUE_FIELDS + '''
  <div style="margin-top:12px"><button class="btn btn-primary" {{ 'disabled' if not has_key }}>Выпустить ключ</button></div></form>
  <p class="muted" style="margin:8px 0 0">Новый ключ заменяет прежний: клиент устанавливает его поверх.</p>
</div>{% endblock %}'''


def create_app(home: Path | None = None) -> FastAPI:
    registry = Registry(home or default_home())
    env = Environment(loader=DictLoader({'base': _BASE, 'index': _INDEX, 'customer': _CUSTOMER}),
                      autoescape=select_autoescape(default=True))
    env.filters['urlencode'] = lambda s: quote(str(s), safe='')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def render(name: str, **ctx) -> HTMLResponse:
        ctx.setdefault('error', '')
        return HTMLResponse(env.get_template(name).render(
            home=registry.home.as_posix(), has_key=registry.private_key_path().exists(),
            today=date.today().isoformat(), **ctx))

    def latest(data: dict, customer_id: str) -> dict | None:
        own = [l for l in data['licenses'] if l['customer_id'] == customer_id]
        return max(own, key=lambda l: l['issued_at']) if own else None

    def defaults(last: dict | None) -> dict:
        if last:     # продление: год от конца прежних обновлений, если они ещё идут
            base = max(date.fromisoformat(last['updates_until']), date.today())
            return {'users': last['users'], 'until': add_year(base).isoformat()}
        return {'users': 5, 'until': add_year(date.today()).isoformat()}

    def customer_fields(org, contact, email, phone, note):
        return {'org': org, 'contact': contact, 'email': email, 'phone': phone, 'note': note}

    def customer_rows(q: str) -> list:
        data = registry.load()
        needle = q.strip().casefold()
        rows = [(c, latest(data, c['id'])) for c in data['customers']
                if not needle or any(needle in (c.get(f) or '').casefold()
                                     for f in ('org', 'contact', 'email', 'phone', 'note'))]
        rows.sort(key=lambda r: (r[1]['issued_at'] if r[1] else r[0]['created']), reverse=True)
        return rows

    @app.get('/', response_class=HTMLResponse)
    def index(q: str = ''):
        return render('index', rows=customer_rows(q), q=q, c={}, **defaults(None))

    @app.post('/issue')
    def issue_new(org: str = Form(''), contact: str = Form(''), email: str = Form(''),
                  phone: str = Form(''), note: str = Form(''), users: int = Form(...),
                  updates_until: str = Form(...), license_note: str = Form('')):
        fields = customer_fields(org, contact, email, phone, note)
        try:
            if not registry.private_key_path().exists():
                raise ValueError('Нет закрытого ключа')
            customer = registry.save_customer(fields)
        except ValueError as e:
            return render('index', rows=customer_rows(''), q='', c=fields, users=users, until=updates_until, error=str(e))
        return _issue(customer['id'], users, updates_until, license_note)

    def _issue(customer_id: str, users: int, updates_until: str, note: str):
        try:
            record = registry.issue(customer_id, users, updates_until, note)
        except ValueError as e:
            return RedirectResponse(f'/customers/{customer_id}?error={quote(str(e))}', status_code=303)
        return RedirectResponse(f'/customers/{customer_id}?issued={record["id"]}', status_code=303)

    @app.get('/customers/{customer_id}', response_class=HTMLResponse)
    def customer_page(customer_id: str, issued: str = '', error: str = ''):
        data = registry.load()
        c = registry.customer(data, customer_id)
        licenses = sorted((l for l in data['licenses'] if l['customer_id'] == customer_id),
                          key=lambda l: l['issued_at'], reverse=True)
        record = next((l for l in licenses if l['id'] == issued), None)
        subject, body = letter(c, record) if record else ('', '')
        return render('customer', c=c, licenses=licenses, issued=record, subject=subject, body=body,
                      error=error, **defaults(licenses[0] if licenses else None))

    @app.post('/customers/{customer_id}')
    def update_customer(customer_id: str, org: str = Form(''), contact: str = Form(''),
                        email: str = Form(''), phone: str = Form(''), note: str = Form('')):
        try:
            registry.save_customer(customer_fields(org, contact, email, phone, note), customer_id)
        except ValueError as e:
            return RedirectResponse(f'/customers/{customer_id}?error={quote(str(e))}', status_code=303)
        return RedirectResponse(f'/customers/{customer_id}', status_code=303)

    @app.post('/customers/{customer_id}/issue')
    def issue_existing(customer_id: str, users: int = Form(...), updates_until: str = Form(...),
                       license_note: str = Form('')):
        registry.customer(registry.load(), customer_id)
        return _issue(customer_id, users, updates_until, license_note)

    @app.get('/keys/{license_id}')
    def download(license_id: str):
        return Response(registry.key_text(license_id), media_type='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename="ariadna_{license_id}.key"'})

    @app.exception_handler(HTTPException)
    def not_found(_request, exc: HTTPException):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    return app
