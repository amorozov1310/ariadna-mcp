"""
Личные токены доступа для общего (командного) сервера.

Пока не создан ни один токен, Ариадна работает как раньше — без авторизации:
локальная установка ничего не замечает. Как только создан первый токен,
каждый запрос к MCP и к Web UI должен его предъявить: `Authorization: Bearer
<токен>` (агенты, скрипты) или cookie после входа в Web UI.

Роли:
  admin — всё: проекты, источники, удаление, токены;
  user  — поиск и все читающие инструменты, переиндексация, словарь,
          сравнение с типовой; без создания и удаления проектов и источников.

Токен показывается один раз при создании; на диске (`data/tokens.json`)
хранится только SHA-256 — токен случайный (256 бит), соль не нужна.

Первый токен администратора — из командной строки (в контейнере —
`docker compose exec ariadna python -m src.auth create --name Имя --role admin`),
дальше — в Web UI («Токены»).

Без лицензии (src/license.py) — один токен: свой сервер можно закрыть
бесплатно. Второй и следующие токены — командный режим, их число задаёт ключ.
Если ключ пропал или не подходит к этой версии, работают первые разрешённые
токены (администраторы — в первую очередь), остальные получают 403.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .core.fsretry import replace_with_retry
from .license import LicenseStore

logger = logging.getLogger('ariadna')

ROLES = ('admin', 'user')
FILENAME = 'tokens.json'
TOKEN_PREFIX = 'ar_'
# last_used пишется на диск не чаще раза в столько секунд на токен.
_LAST_USED_PERSIST_SEC = 300


@dataclass
class TokenInfo:
    id: str
    name: str
    role: str
    hash: str
    created_at: str
    last_used: str = ''


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class TokenStore:
    """Токены в `<data_dir>/tokens.json`. Файл перечитывается при изменении на
    диске (второй процесс — CLI в контейнере — создал токен)."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / FILENAME
        self._lock = threading.Lock()
        self._mtime = None
        self._tokens: dict[str, TokenInfo] = {}       # id -> info
        self._by_hash: dict[str, TokenInfo] = {}
        self._last_persist: dict[str, float] = {}
        self.license = LicenseStore(data_dir)

    # ------------------------------------------------------------ чтение
    def _reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime == self._mtime:
            return
        tokens: dict[str, TokenInfo] = {}
        if mtime is not None:
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8') or '[]')
                for item in raw:
                    info = TokenInfo(**{k: item.get(k, '') for k in TokenInfo.__dataclass_fields__})
                    if info.role in ROLES and info.hash:
                        tokens[info.id] = info
            except (OSError, ValueError, TypeError) as e:
                # Повреждённый файл не должен открыть сервер: токены есть, но
                # не читаются — авторизация остаётся включённой, вход только
                # после исправления файла.
                logger.error('Токены: не прочитан %s (%s) — доступ закрыт', self.path, e)
                self._tokens, self._by_hash, self._mtime = {}, {}, mtime
                self._broken = True
                return
        self._broken = False
        self._tokens = tokens
        self._by_hash = {t.hash: t for t in tokens.values()}
        self._mtime = mtime

    def enabled(self) -> bool:
        """Авторизация включена: есть хотя бы один токен (или файл токенов
        повреждён — тогда закрыто всё)."""
        with self._lock:
            self._reload()
            return bool(self._tokens) or getattr(self, '_broken', False)

    def list(self) -> list[TokenInfo]:
        with self._lock:
            self._reload()
            return sorted(self._tokens.values(), key=lambda t: (t.created_at, t.name))

    def limit(self) -> int:
        """Сколько токенов разрешает лицензия (без неё — один)."""
        return self.license.state().users

    def _allowed_ids(self) -> set[str]:
        ordered = sorted(self._tokens.values(),
                         key=lambda t: (t.role != 'admin', t.created_at, t.id))
        return {t.id for t in ordered[:self.limit()]}

    def denied(self, info: TokenInfo) -> str | None:
        """Причина отказа токену сверх лицензии или None — токен работает."""
        with self._lock:
            self._reload()
            if info.id in self._allowed_ids():
                return None
            state = self.license.state()
        reason = (state.message if state.status != 'none'
                  else 'лицензии командного режима нет')
        return (f'Токен «{info.name}» не действует: {reason}. Без лицензии работает один '
                f'токен (администратора), с ключом — столько, сколько в нём указано. '
                f'Ключ устанавливает администратор на странице «Лицензия».')

    def verify(self, token: str | None) -> TokenInfo | None:
        if not token:
            return None
        with self._lock:
            self._reload()
            info = self._by_hash.get(_hash(token.strip()))
            if info is None:
                return None
            now = datetime.now()
            info.last_used = now.isoformat(timespec='seconds')
            ts = now.timestamp()
            if ts - self._last_persist.get(info.id, 0) > _LAST_USED_PERSIST_SEC:
                self._last_persist[info.id] = ts
                self._save()
            return info

    # ------------------------------------------------------------ запись
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps([asdict(t) for t in self._tokens.values()],
                                  ensure_ascii=False, indent=2), encoding='utf-8')
        replace_with_retry(tmp, self.path)     # Windows: файл занят антивирусом
        try:
            self._mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._mtime = None

    def create(self, name: str, role: str = 'user') -> tuple[TokenInfo, str]:
        """Новый токен: (запись, сам токен — показать один раз)."""
        name = (name or '').strip()
        if not name:
            raise ValueError('Укажите имя владельца токена')
        if role not in ROLES:
            raise ValueError(f'Роль — одна из: {", ".join(ROLES)}')
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._reload()
            if getattr(self, '_broken', False):
                raise ValueError(f'Файл токенов {self.path} повреждён — исправьте или удалите его')
            limit = self.limit()
            if len(self._tokens) >= limit:
                state = self.license.state()
                raise ValueError(
                    'Без лицензии командного режима — один токен (закрыть свой сервер). '
                    'Чтобы выдать токены команде, установите ключ на странице «Лицензия».'
                    if not state.valid else
                    f'Лицензия «{state.info.owner}» — на {limit} токенов, все заняты: '
                    f'отзовите ненужный или расширьте лицензию.')
            info = TokenInfo(id=secrets.token_hex(4), name=name, role=role, hash=_hash(token),
                             created_at=datetime.now().isoformat(timespec='seconds'))
            self._tokens[info.id] = info
            self._by_hash[info.hash] = info
            self._save()
        return info, token

    def revoke(self, token_id: str) -> TokenInfo:
        with self._lock:
            self._reload()
            info = self._tokens.pop(token_id, None)
            if info is None:
                raise KeyError(token_id)
            self._by_hash.pop(info.hash, None)
            self._save()
        return info


def bearer(header: str | None) -> str | None:
    """Токен из заголовка Authorization: Bearer …"""
    if not header:
        return None
    scheme, _, value = header.partition(' ')
    return value.strip() if scheme.lower() == 'bearer' and value.strip() else None


# ---------------------------------------------------------------- MCP

class MCPAuthMiddleware:
    """ASGI-прослойка перед MCP (streamable HTTP): при включённой авторизации
    без верного токена — 401; пользователь кладётся в scope['ariadna_user']
    (его читают инструменты, которым нужна роль admin)."""

    def __init__(self, app, store: TokenStore):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not self.store.enabled():
            return await self.app(scope, receive, send)
        headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
        user = self.store.verify(bearer(headers.get('authorization')))
        if user is None:
            body = json.dumps({'error': 'unauthorized',
                               'detail': 'Нужен токен Ариадны: заголовок Authorization: Bearer <токен>'},
                              ensure_ascii=False).encode('utf-8')
            await send({'type': 'http.response.start', 'status': 401,
                        'headers': [(b'content-type', b'application/json; charset=utf-8'),
                                    (b'www-authenticate', b'Bearer realm="ariadna"')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        reason = self.store.denied(user)
        if reason:
            body = json.dumps({'error': 'license', 'detail': reason}, ensure_ascii=False).encode('utf-8')
            await send({'type': 'http.response.start', 'status': 403,
                        'headers': [(b'content-type', b'application/json; charset=utf-8')]})
            await send({'type': 'http.response.body', 'body': body})
            return
        scope['ariadna_user'] = user
        return await self.app(scope, receive, send)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m src.auth',
                                     description='Токены доступа Ариадны (командный сервер)')
    parser.add_argument('--data-dir', default=os.environ.get('DATA_DIR', 'data'))
    sub = parser.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('create', help='создать токен (показывается один раз)')
    c.add_argument('--name', required=True, help='владелец: имя человека или робота')
    c.add_argument('--role', default='user', choices=ROLES)
    sub.add_parser('list', help='список токенов')
    r = sub.add_parser('revoke', help='отозвать токен по id')
    r.add_argument('id')
    args = parser.parse_args(argv)
    store = TokenStore(args.data_dir)
    if args.cmd == 'create':
        try:
            info, token = store.create(args.name, args.role)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f'Токен для «{info.name}» ({info.role}), id {info.id}:')
        print(token)
        print('Сохраните его сейчас — повторно он не показывается.')
    elif args.cmd == 'list':
        for t in store.list():
            print(f'{t.id}  {t.role:5}  {t.name}  создан {t.created_at}'
                  + (f', последний вход {t.last_used}' if t.last_used else ''))
    else:
        info = store.revoke(args.id)
        print(f'Токен {info.id} («{info.name}») отозван.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
