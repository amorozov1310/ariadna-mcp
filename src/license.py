"""
Лицензия сетевого доступа и командного режима (docs/license.md).

Локально, для одного человека, Ариадна бесплатна: без ключа можно создать
один токен администратора (закрыть свой сервер). Сетевой доступ и командный
режим (второй и следующие личные токены) требуют ключа.

Ключ — подписанный текстовый блок:

    -----BEGIN ARIADNA LICENSE-----
    <base64 JSON: id, owner, users, updates_until, issued>
    <base64 подписи Ed25519 этих байтов>
    -----END ARIADNA LICENSE-----

Ариадна проверяет подпись встроенным открытым ключом (без интернета) и
сравнивает updates_until с датой выпуска своей версии (src/RELEASE_DATE):
ключ бессрочный, но действует для версий, вышедших до этой даты включительно
(обновления и поддержка — год). Закрытый ключ хранится только у владельца
Ариадны; выпуск ключей — `python -m src.license issue`.

Проверка в коде на Python — защита от случайного нарушения, а не DRM:
основная защита — договор.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .core.fsretry import replace_with_retry

FILENAME = 'license.key'
_BEGIN = '-----BEGIN ARIADNA LICENSE-----'
_END = '-----END ARIADNA LICENSE-----'

# Открытый ключ Ed25519 (32 байта, base64) — пара к закрытому ключу владельца.
PUBLIC_KEY = '9F7erj/sc5hfYj+a6s8Rbo0peIldzwhrUrJ/xZ7Ahc8='

_RELEASE_DATE_FILE = Path(__file__).parent / 'RELEASE_DATE'


def release_date() -> date:
    """Дата выпуска этой версии Ариадны — с ней сравнивается updates_until."""
    return date.fromisoformat(_RELEASE_DATE_FILE.read_text(encoding='utf-8').strip())


@dataclass
class LicenseInfo:
    id: str
    owner: str
    users: int
    updates_until: str      # YYYY-MM-DD
    issued: str             # YYYY-MM-DD


@dataclass
class LicenseState:
    status: str             # none | valid | outdated | invalid
    message: str
    info: LicenseInfo | None = None

    @property
    def valid(self) -> bool:
        return self.status == 'valid'

    @property
    def users(self) -> int:
        """Сколько личных токенов разрешено: без действующего ключа — один."""
        return self.info.users if self.valid and self.info else 1


class LicenseError(ValueError):
    pass


def _public_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY))


def parse(text: str, public_key=None) -> LicenseInfo:
    """Разобрать и проверить подпись; LicenseError — ключ повреждён или чужой."""
    from cryptography.exceptions import InvalidSignature
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    try:
        start, end = lines.index(_BEGIN), lines.index(_END)
        payload_b64, sig_b64 = lines[start + 1:end]
        payload, sig = base64.b64decode(payload_b64, validate=True), base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError):
        raise LicenseError('это не ключ Ариадны: нет блока BEGIN/END ARIADNA LICENSE')
    try:
        (public_key or _public_key()).verify(sig, payload)
    except InvalidSignature:
        raise LicenseError('подпись ключа не сходится — ключ изменён или выпущен не для этой Ариадны')
    try:
        data = json.loads(payload.decode('utf-8'))
        info = LicenseInfo(id=str(data['id']), owner=str(data['owner']), users=int(data['users']),
                           updates_until=str(data['updates_until']), issued=str(data['issued']))
        date.fromisoformat(info.updates_until)
    except (KeyError, ValueError, TypeError):
        raise LicenseError('ключ повреждён: нет нужных полей')
    return info


def check(info: LicenseInfo, released: date | None = None) -> LicenseState:
    released = released or release_date()
    until = date.fromisoformat(info.updates_until)
    if until < released:
        return LicenseState('outdated', (
            f'ключ «{info.owner}» действует для версий, вышедших до {until:%d.%m.%Y}, а эта версия '
            f'вышла {released:%d.%m.%Y}: продлите обновления или верните прежнюю версию'), info)
    return LicenseState('valid', f'ключ «{info.owner}»: до {info.users} пользователей, '
                                 f'обновления до {until:%d.%m.%Y}', info)


class LicenseStore:
    """Ключ в `<data_dir>/license.key`; перечитывается при изменении файла."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / FILENAME
        self._lock = threading.Lock()
        self._mtime = None
        self._state = LicenseState('none', 'ключа нет — командный режим не включён')

    def state(self) -> LicenseState:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                mtime = None
            except OSError as e:
                return LicenseState('invalid', f'ключ не принят: {e}')
            if mtime != self._mtime:
                self._mtime = mtime
                if mtime is None:
                    self._state = LicenseState('none', 'ключа нет — командный режим не включён')
                else:
                    try:
                        self._state = check(parse(self.path.read_text(encoding='utf-8')))
                    except (LicenseError, OSError, UnicodeDecodeError) as e:
                        self._state = LicenseState('invalid', f'ключ не принят: {e}')
            return self._state

    def install(self, text: str) -> LicenseState:
        """Проверить и сохранить ключ; LicenseError — не принят (файл не меняется)."""
        state = check(parse(text))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.key.tmp')
        tmp.write_text(text.strip() + '\n', encoding='utf-8')
        replace_with_retry(tmp, self.path)     # Windows: файл занят антивирусом
        return state


# ---------------------------------------------------------------- выпуск ключей

def _private_key(path: str | Path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(Path(path).read_bytes(), password=None)


def issue(private_key, owner: str, users: int, updates_until: str,
          issued: str | None = None, license_id: str | None = None) -> str:
    """Текст ключа (функция пригодится и сервису автоматической выдачи)."""
    if users < 1:
        raise ValueError('users — не меньше 1')
    date.fromisoformat(updates_until)
    payload = json.dumps({'id': license_id or secrets.token_hex(6), 'owner': owner.strip(),
                          'users': int(users), 'updates_until': updates_until,
                          'issued': issued or date.today().isoformat()},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    sig = private_key.sign(payload)
    return '\n'.join([_BEGIN, base64.b64encode(payload).decode(), base64.b64encode(sig).decode(), _END]) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m src.license', description='Лицензии Ариадны')
    sub = parser.add_subparsers(dest='cmd', required=True)
    k = sub.add_parser('keygen', help='создать пару ключей владельца (один раз)')
    k.add_argument('--out', required=True, help='каталог для закрытого ключа (вне репозитория!)')
    i = sub.add_parser('issue', help='выпустить ключ клиенту')
    i.add_argument('--private-key', required=True)
    i.add_argument('--owner', required=True, help='клиент: название организации')
    i.add_argument('--users', type=int, required=True, help='сколько личных токенов')
    i.add_argument('--updates-until', required=True, help='обновления до, YYYY-MM-DD')
    i.add_argument('--out', help='файл ключа (по умолчанию — вывод на экран)')
    s = sub.add_parser('show', help='проверить ключ: файл или установленный в data/')
    s.add_argument('file', nargs='?')
    s.add_argument('--data-dir', default=os.environ.get('DATA_DIR', 'data'))
    a = sub.add_parser('install', help='установить ключ в data/ (то же, что в Web UI)')
    a.add_argument('file')
    a.add_argument('--data-dir', default=os.environ.get('DATA_DIR', 'data'))
    args = parser.parse_args(argv)

    if args.cmd == 'keygen':
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        target = out / 'ariadna_license_private.pem'
        if target.exists():
            print(f'{target} уже есть — второй ключ не создаю', file=sys.stderr)
            return 1
        key = Ed25519PrivateKey.generate()
        target.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print(f'Закрытый ключ: {target} — храните в надёжном месте, сделайте резервную копию.')
        print(f'Открытый ключ (PUBLIC_KEY в src/license.py): {base64.b64encode(pub).decode()}')
        return 0
    if args.cmd == 'issue':
        text = issue(_private_key(args.private_key), args.owner, args.users, args.updates_until)
        if args.out:
            Path(args.out).write_text(text, encoding='utf-8')
            print(f'Ключ для «{args.owner}» записан в {args.out}')
        else:
            print(text, end='')
        return 0
    if args.cmd == 'show':
        try:
            if args.file:
                state = check(parse(Path(args.file).read_text(encoding='utf-8')))
            else:
                state = LicenseStore(args.data_dir).state()
        except LicenseError as e:
            print(f'Ключ не принят: {e}')
            return 1
        print(f'{state.status}: {state.message}')
        if state.info:
            for k_, v in asdict(state.info).items():
                print(f'  {k_}: {v}')
        print(f'Эта версия вышла: {release_date():%d.%m.%Y}')
        return 0 if state.valid else 1
    try:
        state = LicenseStore(args.data_dir).install(Path(args.file).read_text(encoding='utf-8'))
    except LicenseError as e:
        print(f'Ключ не принят: {e}')
        return 1
    print(f'Установлен. {state.message}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
