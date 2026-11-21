"""
Допустимые заголовки Host/Origin для MCP и Web UI — защита от DNS rebinding.

Привязка портов к 127.0.0.1 не спасает от страницы в браузере пользователя,
которая шлёт запросы на 127.0.0.1:19879 под своим доменом (DNS rebinding):
браузер отправит Host злоумышленника. SDK MCP включает проверку сам только
для host='127.0.0.1'/'localhost', а серверы внутри контейнера слушают 0.0.0.0
(иначе не работает проброс портов), поэтому настройки передаются явно.

Без действующего ключа Web UI и MCP принимают только loopback Host.
С ключом для доступа из сети нужно также опубликовать порты и перечислить
имена или адреса сервера в MCP_ALLOWED_HOSTS через запятую.
"""

import os
import re
from collections.abc import Callable, Mapping
from ipaddress import ip_address

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
_LOOPBACK_HOST = re.compile(rf"(?:{'|'.join(map(re.escape, LOOPBACK_HOSTS))})(?::[0-9]+)?")
LOCAL_BIND_NOTICE = 'Без ключа сервер слушает только 127.0.0.1; LISTEN_ADDR из окружения не применён.'
_LICENSE_DOC = 'https://github.com/amorozov1310/ariadna-mcp/blob/main/docs/license.md'
_NETWORK_ACCESS_REQUIRED = 'Для доступа к Ариадне из сети нужен действующий лицензионный ключ.'
_LOCAL_ACCESS = ('Откройте Ариадну на сервере через 127.0.0.1. Для локального доступа оставьте '
                 'BIND_ADDR=127.0.0.1 в Docker или LISTEN_ADDR=127.0.0.1 при запуске без Docker.')


def is_loopback_address(address: str) -> bool:
    """Петлевой IP-адрес без разрешения имени через DNS."""
    try:
        ip = ip_address(address)
    except ValueError:
        return False
    return (getattr(ip, 'ipv4_mapped', None) or ip).is_loopback


def _invalid_host_port(host: str) -> bool:
    _, separator, port = host.rpartition(':')
    if not (separator and port.isascii() and port.isdecimal()):
        return False
    port = port.lstrip('0')
    return not (1 <= len(port) <= 5 and int(port) <= 65535)


class NetworkLicenseMiddleware:
    """Проверяет Host до авторизации и маршрутизации (см. ADR 0003)."""

    def __init__(self, app: ASGIApp, license_valid: Callable[[], bool], *, mcp: bool = False,
                 check_local_interface: Callable[[], bool] = lambda: False):
        self.app = app
        self.license_valid = license_valid
        self.mcp = mcp
        self.check_local_interface = check_local_interface

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        hosts = headers.getlist('host')
        host = hosts[0] if len(hosts) == 1 else ''
        normalized_host = host.lower()
        loopback_host = _LOOPBACK_HOST.fullmatch(normalized_host)
        invalid_port = _invalid_host_port(host)

        # После смены ключа открытый нативный сокет обслуживает бесплатный
        # режим только через loopback. ASGI server — локальный адрес сокета.
        server = scope.get('server')
        local_interface = not self.check_local_interface() or (server and is_loopback_address(server[0]))
        if not invalid_port and ((loopback_host and local_interface) or self.license_valid()):
            if host != normalized_host:
                # Starlette и MCP SDK сверяют Host с разрешёнными именами.
                normalized_value = normalized_host.encode('latin-1')
                scope = dict(scope, headers=[(k, normalized_value if k == b'host' else v)
                                             for k, v in scope['headers']])
            return await self.app(scope, receive, send)

        detail = ('Недопустимый порт в заголовке Host.' if invalid_port else
                  f'{_NETWORK_ACCESS_REQUIRED} {_LOCAL_ACCESS}')
        if self.mcp or scope['path'].startswith('/api/') or 'application/json' in headers.get('accept', ''):
            response = JSONResponse({
                'error': 'invalid_host' if invalid_port else 'license_required',
                'detail': detail,
                'docs': _LICENSE_DOC,
            }, status_code=421 if self.mcp else 400)
        else:
            response = HTMLResponse(
                '<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<title>Сетевой доступ — Ариадна</title><h1>Сетевой доступ</h1>'
                f'<p>{detail}</p>'
                f'<p><a href="{_LICENSE_DOC}">Как установить ключ</a></p></html>',
                status_code=400)
        response.headers['Cache-Control'] = 'no-store'
        await response(scope, receive, send)


def _has_port(host: str) -> bool:
    if host.startswith('['):
        return ']:' in host
    return ':' in host


def allowed_hosts(env: Mapping[str, str] | None = None) -> list[str]:
    """Шаблоны Host в формате SDK MCP: «host:*» — любой порт."""
    env = os.environ if env is None else env
    hosts = [*LOOPBACK_HOSTS, *(f'{host}:*' for host in LOOPBACK_HOSTS)]
    for item in (env.get('MCP_ALLOWED_HOSTS') or '').split(','):
        item = item.strip().lower()
        if not item:
            continue
        # Без порта: и сам host (порт по умолчанию, в заголовке его нет),
        # и host с любым портом.
        extra = [item] if _has_port(item) else [item, f'{item}:*']
        hosts.extend(h for h in extra if h not in hosts)
    return hosts


def allowed_origins(hosts: list[str]) -> list[str]:
    return [f'http://{h}' for h in hosts]


def web_allowed_hosts(env: Mapping[str, str] | None = None) -> list[str]:
    """Те же хосты для TrustedHostMiddleware Web UI — он сравнивает имя без порта."""
    names: list[str] = []
    for h in allowed_hosts(env):
        if h.startswith('['):
            name = h[:h.index(']') + 1]
        else:
            name = h.split(':', 1)[0]
        if name not in names:
            names.append(name)
    return names


def mcp_transport_security(env: Mapping[str, str] | None = None):
    """TransportSecuritySettings для streamable_http_app."""
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = allowed_hosts(env)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=allowed_origins(hosts),
    )

