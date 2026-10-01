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


def _listen_addr() -> str:
    """Адрес, на котором слушают Web UI и MCP. Авторизации нет, поэтому по
    умолчанию только эта машина (127.0.0.1) — как и порты Docker (BIND_ADDR).
    В контейнере Dockerfile задаёт LISTEN_ADDR=0.0.0.0: иначе не работает
    проброс портов, а наружу их по-прежнему открывает только BIND_ADDR."""
    return os.environ.get('LISTEN_ADDR', '127.0.0.1')


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
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    pm = ProjectManager(data_dir, _index_dir())
    try:
        pm.migrate_index_dir()
    except Exception:
        logger.exception("Не удалось перенести индексы в INDEX_DIR")
    try:
        pm.reset_stale_indexing()
    except Exception:
        logger.exception("Не удалось проверить незавершённые индексации")
    return pm


def main():
    data_dir = os.environ.get('DATA_DIR', '/data')
    mcp_http_port = int(os.environ.get('MCP_HTTP_PORT', '9879'))
    web_port = int(os.environ.get('WEB_PORT', '9878'))

    _quiet_proactor_connection_reset()
    pm = build_shared_state(data_dir)

    logger.info(f"Data directory: {data_dir}")
    logger.info(f"Index directory: {_index_dir() or data_dir + '/projects'}")
    logger.info(f"MCP streamable HTTP port: {mcp_http_port}")
    logger.info(f"Web UI port: {web_port}")

    # Web UI — в фоновом потоке.
    web_thread = threading.Thread(target=_start_web, args=(pm, web_port), daemon=True)
    web_thread.start()
    logger.info(f"Web UI starting on :{web_port}")

    # MCP (streamable HTTP) — в основном потоке, блокирующий: это основной
    # интерфейс сервера. Если он упал, процесс завершается и Docker его
    # перезапускает (restart: unless-stopped); упавший в фоне MCP оставил бы
    # «живой» контейнер без MCP — healthcheck проверяет только Web UI.
    logger.info(f"MCP streamable HTTP starting on :{mcp_http_port}")
    _start_mcp_streamable_http(pm, mcp_http_port)


def _start_web(pm, port: int):
    """Start FastAPI Web UI with uvicorn — с общим ProjectManager процесса.
    Объект app, а не строка "src.web.app:app": uvicorn импортировал бы модуль
    сам, и set_pm мог бы попасть не в тот экземпляр приложения."""
    try:
        import uvicorn
        from .web import app as web_app
        web_app.set_pm(pm)
        uvicorn.run(
            web_app.app,
            host=_listen_addr(),
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


def _start_mcp_streamable_http(pm, port: int):
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
        mcp = create_mcp_server(pm)

        from .security import mcp_transport_security
        app = mcp.streamable_http_app(host="0.0.0.0", transport_security=mcp_transport_security())
        uvicorn.run(app, host=_listen_addr(), port=port, log_level="warning")

    except ImportError as e:
        logger.warning(f"MCP dependencies missing ({e}) — running Web UI only")
        _idle_forever()
    except Exception as e:
        logger.error(f"MCP streamable HTTP server failed: {e}")
        raise


if __name__ == '__main__':
    main()
