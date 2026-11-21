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

from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
_LOOPBACK_HOST = re.compile(rf"(?:{'|'.join(map(re.escape, LOOPBACK_HOSTS))})(?::[0-9]+)?")
_LICENSE_DOC = 'https://github.com/amorozov1310/ariadna-mcp/blob/main/docs/license.md'
_NETWORK_ACCESS_REQUIRED = 'Для доступа к Ариадне из сети нужен действующий лицензионный ключ.'
_LOCAL_ACCESS = ('Откройте Ариадну на сервере через 127.0.0.1. Для локального доступа оставьте '
                 'BIND_ADDR=127.0.0.1 в Docker или LISTEN_ADDR=127.0.0.1 при запуске без Docker.')


class NetworkLicenseMiddleware:
    """Проверяет Host до авторизации и маршрутизации (см. ADR 0003)."""

    def __init__(self, app: ASGIApp, license_valid: Callable[[], bool], *, mcp: bool = False):
        self.app = app
        self.license_valid = license_valid
        self.mcp = mcp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        hosts = headers.getlist('host')
        if (len(hosts) == 1 and _LOOPBACK_HOST.fullmatch(hosts[0])) or self.license_valid():
            return await self.app(scope, receive, send)

        if self.mcp or scope['path'].startswith('/api/') or 'application/json' in headers.get('accept', ''):
            response = JSONResponse({
                'error': 'license_required',
                'detail': f'{_NETWORK_ACCESS_REQUIRED} {_LOCAL_ACCESS}',
                'docs': _LICENSE_DOC,
            }, status_code=421 if self.mcp else 400)
        else:
            response = HTMLResponse(
                '<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<title>Сетевой доступ — Ариадна</title><h1>Сетевой доступ</h1>'
                f'<p>{_NETWORK_ACCESS_REQUIRED}</p><p>{_LOCAL_ACCESS}</p>'
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
        item = item.strip()
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

