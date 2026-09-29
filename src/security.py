"""
Допустимые заголовки Host/Origin для MCP и Web UI — защита от DNS rebinding.

Привязка портов к 127.0.0.1 не спасает от страницы в браузере пользователя,
которая шлёт запросы на 127.0.0.1:19879 под своим доменом (DNS rebinding):
браузер отправит Host злоумышленника. SDK MCP включает проверку сам только
для host='127.0.0.1'/'localhost', а серверы внутри контейнера слушают 0.0.0.0
(иначе не работает проброс портов), поэтому настройки передаются явно.

По умолчанию разрешены только адреса самой машины. Для доступа с других
машин (BIND_ADDR=0.0.0.0) их имена или адреса перечисляются в
MCP_ALLOWED_HOSTS через запятую: «host» — любой порт, «host:port» — только
этот порт. Список общий для MCP и Web UI.
"""

import os
from collections.abc import Mapping

DEFAULT_ALLOWED_HOSTS = ('127.0.0.1:*', 'localhost:*', '[::1]:*')


def _has_port(host: str) -> bool:
    if host.startswith('['):
        return ']:' in host
    return ':' in host


def allowed_hosts(env: Mapping[str, str] | None = None) -> list[str]:
    """Шаблоны Host в формате SDK MCP: «host:*» — любой порт."""
    env = os.environ if env is None else env
    hosts = list(DEFAULT_ALLOWED_HOSTS)
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
    """TransportSecuritySettings для sse_app/streamable_http_app."""
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = allowed_hosts(env)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=allowed_origins(hosts),
    )


class HostOriginGuard:
    """ASGI-обёртка: проверка Host и Origin до приложения SDK.

    Нужна для SSE: mcp SDK (2.2) в connect_sse, отправив 421/403, бросает
    ValueError("Request validation failed"), и uvicorn пишет полную
    трассировку «Exception in ASGI application» на каждый отклонённый запрос.
    Здесь такой запрос получает тот же ответ, а в лог попадает одна строка
    WARNING (её пишет сама проверка SDK) — до SDK он не доходит. Проверка —
    тот же TransportSecurityMiddleware SDK с теми же списками, поэтому
    разрешённый здесь запрос SDK не отклонит. Content-Type POST-запросов
    по-прежнему проверяет SDK.
    """

    def __init__(self, app, settings):
        from mcp.server.transport_security import TransportSecurityMiddleware
        self.app = app
        self._security = TransportSecurityMiddleware(settings)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            from starlette.requests import Request
            error = await self._security.validate_request(Request(scope, receive), is_post=False)
            if error is not None:
                await error(scope, receive, send)
                return
        await self.app(scope, receive, send)
