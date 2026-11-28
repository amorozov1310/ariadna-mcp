"""
Ариадна — точка входа: MCP-сервер (streamable HTTP) и Web UI.
"""

import os
import sys
import logging
import threading
from pathlib import Path

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('ariadna')


class ProactorConnectionResetFilter(logging.Filter):
    """Понижает до DEBUG шум ProactorEventLoop (Windows, запуск без Docker):
    клиент резко закрыл соединение — asyncio пишет ERROR «Exception in
    callback _ProactorBasePipeTransport._call_connection_lost()» с
    трассировкой ConnectionResetError [WinError 10054]. Известное поведение
    CPython, на работу не влияет. Только этот случай: другие ошибки asyncio
    (и ConnectionResetError из других мест) проходят как есть."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if (isinstance(exc, ConnectionResetError)
                and '_call_connection_lost' in record.getMessage()):
            record.levelno, record.levelname = logging.DEBUG, 'DEBUG'
            return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        return True


def _quiet_proactor_connection_reset(platform: str = sys.platform) -> None:
    """Ставит ProactorConnectionResetFilter на логгер asyncio — только на
    Windows (ProactorEventLoop есть только там)."""
    if platform != 'win32':
        return
    asyncio_logger = logging.getLogger('asyncio')
    if not any(isinstance(f, ProactorConnectionResetFilter) for f in asyncio_logger.filters):
        asyncio_logger.addFilter(ProactorConnectionResetFilter())


def _index_dir() -> str | None:
    """Каталог индексов SQLite (INDEX_DIR, в Docker — том вне 9p); None —
    индексы рядом с выгрузками в DATA_DIR/projects, как раньше."""
    return os.environ.get('INDEX_DIR') or None


def _ensure_token_store(pm):
    """Общее хранилище для обоих транспортов; совместимость прямых запусков."""
    from .auth import TokenStore
    tokens = getattr(pm, 'token_store', None)
    if tokens is None:
        tokens = pm.token_store = TokenStore(pm.data_dir)
        tokens.initialize()
    return tokens


def build_shared_state(data_dir: str):
    """Один ProjectManager на процесс — общий для Web UI и MCP: один реестр
    и один пул индексов. Раньше у каждого сервера (Web UI, MCP SSE и HTTP)
    был свой экземпляр, и согласовывать их приходилось заплатками (MCP не видел
    проектов из Web UI, читал индекс удалённого проекта, держал файл,
    который удалял Web UI).

    До запуска любых рабочих потоков (ни одна переиндексация ещё не идёт):
    перенос индексов в INDEX_DIR, затем сброс статуса 'indexing', оставшегося
    от прогона, который не пережил остановку процесса (Этап U0). Порядок
    важен: reset_stale_indexing открывает БД, и на новом месте без переноса
    появился бы пустой индекс. Ошибки обслуживания логируются и не мешают
    запуску."""
    from .core.project_manager import ProjectManager
    from .auth import TokenStore
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    pm = ProjectManager(data_dir, _index_dir())
    pm.token_store = TokenStore(data_dir)
    pm.token_store.initialize()
    try:
        pm.migrate_index_dir()
    except Exception:
        logger.exception("Не удалось перенести индексы в INDEX_DIR")
    try:
        pm.reset_stale_indexing()
    except Exception:
        logger.exception("Не удалось проверить незавершённые индексации")
    pm.sync_platform_help()
    return pm


def main(*, in_docker: bool = False):
    data_dir = os.environ.get('DATA_DIR', '/data')
    mcp_http_port = int(os.environ.get('MCP_HTTP_PORT', '9879'))
    web_port = int(os.environ.get('WEB_PORT', '9878'))

    _quiet_proactor_connection_reset()
    pm = build_shared_state(data_dir)
    _ensure_token_store(pm)

    from .license import LicenseStore
    from .security import LOCAL_BIND_NOTICE, is_loopback_address

    host = os.environ.get('LISTEN_ADDR', '0.0.0.0' if in_docker else '127.0.0.1')
    check_local_interface = not in_docker and not is_loopback_address(host)
    listen_addr_overridden = check_local_interface and not LicenseStore(data_dir).state().valid
    if listen_addr_overridden:
        host = '127.0.0.1'
        logger.warning(LOCAL_BIND_NOTICE)
        check_local_interface = False

    logger.info(f"Data directory: {data_dir}")
    logger.info(f"Index directory: {_index_dir() or data_dir + '/projects'}")
    logger.info(f"MCP streamable HTTP port: {mcp_http_port}")
    logger.info(f"Web UI port: {web_port}")

    # Web UI — в фоновом потоке.
    web_thread = threading.Thread(
        target=_start_web, args=(pm, web_port, host), daemon=True,
        kwargs={'listen_addr_overridden': listen_addr_overridden,
                'check_local_interface': check_local_interface})
    web_thread.start()
    logger.info(f"Web UI starting on :{web_port}")

    # MCP (streamable HTTP) — в основном потоке, блокирующий: это основной
    # интерфейс сервера. Если он упал, процесс завершается и Docker его
    # перезапускает (restart: unless-stopped); упавший в фоне MCP оставил бы
    # «живой» контейнер без MCP — healthcheck проверяет только Web UI.
    logger.info(f"MCP streamable HTTP starting on :{mcp_http_port}")
    _start_mcp_streamable_http(pm, mcp_http_port, host, check_local_interface=check_local_interface)


def _start_web(pm, port: int, host: str = '127.0.0.1', *,
               listen_addr_overridden: bool = False, check_local_interface: bool = False):
    """Start FastAPI Web UI with uvicorn — с общим ProjectManager процесса.
    Объект app, а не строка "src.web.app:app": uvicorn импортировал бы модуль
    сам, и set_pm мог бы попасть не в тот экземпляр приложения."""
    try:
        import uvicorn
        from .web import app as web_app
        _ensure_token_store(pm)
        web_app.set_pm(pm)
        web_app.set_token_store(pm.token_store)
        web_app.app.state.listen_addr_overridden = listen_addr_overridden
        web_app.app.state.check_local_interface = check_local_interface
        uvicorn.run(
            web_app.app,
            host=host,
            port=port,
            log_level="warning",
        )
    except ImportError:
        logger.error("uvicorn not installed — Web UI unavailable")
    except Exception as e:
        logger.error(f"Web UI failed: {e}")


def _idle_forever():
    """Keep a thread/process alive without a real server (MCP SDK missing)."""
    import time
    while True:
        time.sleep(60)


def _start_mcp_streamable_http(pm, port: int, host: str = '127.0.0.1', *,
                              check_local_interface: bool = False):
    """MCP-сервер на транспорте streamable HTTP (/mcp) — блокирующий вызов.

    Защита от DNS rebinding — transport_security SDK: внутри контейнера
    слушаем 0.0.0.0 (проброс портов), и SDK сам её не включает. Без MCP SDK
    процесс не завершается — работает только Web UI."""
    try:
        from .mcp_server.server import create_mcp_server, HAS_MCP
        if not HAS_MCP:
            logger.warning("MCP SDK not installed — running Web UI only")
            _idle_forever()
            return

        import uvicorn
        from .auth import MCPAuthMiddleware
        tokens = _ensure_token_store(pm)
        mcp = create_mcp_server(pm)

        from .security import NetworkLicenseMiddleware, mcp_transport_security
        app = mcp.streamable_http_app(host="0.0.0.0", transport_security=mcp_transport_security())
        if tokens.enabled():
            logger.info("Авторизация по токенам включена (%d токенов, лицензия: %s)",
                        len(tokens.list()), tokens.license.state().message)
        elif os.environ.get('MCP_ALLOWED_HOSTS') and tokens.license.state().valid:
            logger.warning("Ариадна открыта для других машин (MCP_ALLOWED_HOSTS), но токенов нет — "
                           "доступ без авторизации. Создайте токен администратора: "
                           "python -m src.auth create --name <имя> --role admin")
        app = NetworkLicenseMiddleware(MCPAuthMiddleware(app, tokens),
                                       license_valid=lambda: tokens.license.state().valid, mcp=True,
                                       check_local_interface=lambda: check_local_interface)
        uvicorn.run(app, host=host, port=port, log_level="warning")

    except ImportError as e:
        logger.warning(f"MCP dependencies missing ({e}) — running Web UI only")
        _idle_forever()
    except Exception as e:
        logger.error(f"MCP streamable HTTP server failed: {e}")
        raise


if __name__ == '__main__':
    main()
