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
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
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
# Защита от совпадения всех полей stat: читаем маленький файл не чаще раза в 100 мс.
_TOKEN_HASH_CHECK_NS = 100_000_000
_TOKEN_LOCK_TIMEOUT_SEC = 2.0
_TOKEN_LOCK_POLL_SEC = 0.025


@contextmanager
def _token_file_lock(path: Path, *, wait: bool = True):
    """Межпроцессная блокировка рядом с tokens.json (Windows и POSIX)."""
    lock_path = path.with_name(path.name + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            deadline = time.monotonic() + _TOKEN_LOCK_TIMEOUT_SEC
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if not wait or time.monotonic() >= deadline:
                        raise TimeoutError(f'не удалось заблокировать файл токенов за '
                                            f'{_TOKEN_LOCK_TIMEOUT_SEC:g} с')
                    time.sleep(_TOKEN_LOCK_POLL_SEC)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            deadline = time.monotonic() + _TOKEN_LOCK_TIMEOUT_SEC
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait or time.monotonic() >= deadline:
                        raise TimeoutError(f'не удалось заблокировать файл токенов за '
                                            f'{_TOKEN_LOCK_TIMEOUT_SEC:g} с')
                    time.sleep(_TOKEN_LOCK_POLL_SEC)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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
        self._file_signature = None
        self._content_hash = None
        self._last_hash_check_ns = 0
        self._tokens: dict[str, TokenInfo] = {}       # id -> info
        self._by_hash: dict[str, TokenInfo] = {}
        self._last_persist: dict[str, float] = {}
        self._broken = False
        self._has_seen_file = False
        self._ever_enabled = False
        self.license = LicenseStore(data_dir)

    def initialize(self) -> None:
        """Проверить файл при старте процесса, до запуска серверов."""
        with self._lock:
            self._reload(force=True)

    # ------------------------------------------------------------ чтение
    def _reload(self, *, force: bool = False) -> None:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino,
                         getattr(stat, 'st_ctime_ns', None))
        except FileNotFoundError:
            if self._has_seen_file or self._ever_enabled:
                self._fail_closed('файл токенов исчез после запуска')
            else:
                self._tokens, self._by_hash = {}, {}
                self._broken = False
                self._remember_file(None, None)
            return
        except OSError as e:
            self._fail_closed(f'не удалось проверить файл токенов: {e}')
            return

        now_ns = time.monotonic_ns()
        if (not force and signature == self._file_signature
                and now_ns - self._last_hash_check_ns < _TOKEN_HASH_CHECK_NS):
            return

        # Сверяем подпись до и после чтения: если файл сменился посреди чтения,
        # повторяем попытку, чтобы хэш и кэш относились к одному содержимому.
        for _ in range(3):
            try:
                content = self.path.read_bytes()
                after = self.path.stat()
            except OSError as e:
                self._fail_closed(f'не удалось прочитать файл токенов: {e}')
                return
            after_signature = (after.st_mtime_ns, after.st_size, after.st_ino,
                               getattr(after, 'st_ctime_ns', None))
            if after_signature == signature:
                break
            signature = after_signature
        else:
            self._fail_closed('файл токенов постоянно меняется при чтении')
            return

        content_hash = hashlib.sha256(content).hexdigest()
        self._last_hash_check_ns = now_ns
        if signature == self._file_signature and content_hash == self._content_hash:
            return
        try:
            if not content:
                raise ValueError('пустой файл')
            raw = json.loads(content.decode('utf-8'))
            if not isinstance(raw, list):
                raise ValueError('ожидался список токенов')
            tokens: dict[str, TokenInfo] = {}
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError('запись токена должна быть объектом')
                values = {k: item.get(k, '') for k in TokenInfo.__dataclass_fields__}
                if (not all(isinstance(v, str) for v in values.values())
                        or not values['id'] or values['role'] not in ROLES
                        or len(values['hash']) != 64
                        or any(c not in '0123456789abcdef' for c in values['hash'])):
                    raise ValueError('некорректная запись токена')
                info = TokenInfo(**values)
                if info.id in tokens or info.hash in {t.hash for t in tokens.values()}:
                    raise ValueError('повторяется id или хэш токена')
                tokens[info.id] = info
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as e:
            self._fail_closed(f'повреждён файл токенов: {e}', signature, content_hash)
            return

        self._has_seen_file = True
        self._ever_enabled = self._ever_enabled or bool(tokens)
        self._broken = False
        self._tokens = tokens
        self._by_hash = {t.hash: t for t in tokens.values()}
        self._last_persist = {token_id: ts for token_id, ts in self._last_persist.items()
                              if token_id in tokens}
        self._remember_file(signature, content_hash)

    def _fail_closed(self, reason: str, signature=None, content_hash=None) -> None:
        logger.warning('Токены: %s (%s) — доступ закрыт', self.path, reason)
        self._tokens, self._by_hash = {}, {}
        self._last_persist.clear()
        self._broken = True
        self._has_seen_file = True
        self._remember_file(signature, content_hash)

    def _remember_file(self, signature, content_hash) -> None:
        self._file_signature = signature
        self._content_hash = content_hash
        self._last_hash_check_ns = time.monotonic_ns()

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
            ts = time.time()
            if ts - self._last_persist.get(info.id, 0) <= _LAST_USED_PERSIST_SEC:
                return info
            token_id = info.id
        try:
            # Обновление last_used — best effort. Не задерживаем запрос, если
            # другой процесс сейчас меняет токены.
            with _token_file_lock(self.path, wait=False):
                with self._lock:
                    self._reload(force=True)
                    info = self._by_hash.get(_hash(token.strip()))
                    if info is None:
                        return None
                    now = datetime.now()
                    candidate = {key: replace(value) for key, value in self._tokens.items()}
                    updated = replace(candidate[info.id],
                                      last_used=now.isoformat(timespec='seconds'))
                    candidate[info.id] = updated
                    self._save(candidate)
                    self._publish(candidate)
                    self._last_persist[info.id] = now.timestamp()
                    return updated
        except TimeoutError as e:
            # Даже если запись пропущена из-за занятой блокировки, решение
            # принимается по повторно сверенной версии файла.
            logger.debug('Токены: пропущено обновление last_used для %s (%s)', token_id, e)
        except OSError as e:
            # last_used — только статистика; ошибка записи не отменяет вход.
            logger.warning('Токены: не удалось обновить last_used для %s (%s)', token_id, e)
        with self._lock:
            self._reload(force=True)
            return self._by_hash.get(_hash(token.strip()))

    # ------------------------------------------------------------ запись
    def _save(self, tokens: dict[str, TokenInfo] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tokens = self._tokens if tokens is None else tokens
        content = json.dumps([asdict(t) for t in tokens.values()],
                             ensure_ascii=False, indent=2).encode('utf-8')
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp',
                                        dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
            replace_with_retry(tmp_name, self.path)  # повтор при краткой блокировке Windows
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        stat = self.path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino,
                     getattr(stat, 'st_ctime_ns', None))
        # Хэш соответствует именно сериализованным в памяти токенам. Если
        # внешний писатель успел заменить файл, следующая сверка обнаружит
        # несовпадение его содержимого с этим хэшем.
        content_hash = hashlib.sha256(content).hexdigest()
        self._remember_file(signature, content_hash)
        self._has_seen_file = True
        self._ever_enabled = self._ever_enabled or bool(tokens)

    def _publish(self, tokens: dict[str, TokenInfo]) -> None:
        """Опубликовать только после успешной атомарной записи файла."""
        self._tokens = tokens
        self._by_hash = {t.hash: t for t in tokens.values()}
        self._last_persist = {token_id: ts for token_id, ts in self._last_persist.items()
                              if token_id in tokens}

    def create(self, name: str, role: str = 'user') -> tuple[TokenInfo, str]:
        """Новый токен: (запись, сам токен — показать один раз)."""
        name = (name or '').strip()
        if not name:
            raise ValueError('Укажите имя владельца токена')
        if role not in ROLES:
            raise ValueError(f'Роль — одна из: {", ".join(ROLES)}')
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with _token_file_lock(self.path):
            with self._lock:
                self._reload(force=True)
                if self._broken:
                    raise ValueError(f'Файл токенов {self.path} повреждён — исправьте его')
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
                candidate = {key: replace(value) for key, value in self._tokens.items()}
                candidate[info.id] = info
                self._save(candidate)
                self._publish(candidate)
        return info, token

    def revoke(self, token_id: str) -> TokenInfo:
        with _token_file_lock(self.path):
            with self._lock:
                self._reload(force=True)
                if self._broken:
                    raise ValueError(f'Файл токенов {self.path} повреждён — исправьте его')
                info = self._tokens.get(token_id)
                if info is None:
                    raise KeyError(token_id)
                candidate = {key: replace(value) for key, value in self._tokens.items()
                             if key != token_id}
                self._save(candidate)
                self._publish(candidate)
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
