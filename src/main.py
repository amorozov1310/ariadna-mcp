"""
Ариадна — точка входа: MCP-сервер (SSE + streamable HTTP) и Web UI.
"""

import os
import logging
import threading
from pathlib import Path

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('ariadna')


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


def _prepare_data(data_dir: str) -> None:
    """Обслуживание до запуска любых рабочих потоков (ни одна переиндексация
    ещё не идёт): перенос индексов в INDEX_DIR, затем сброс статуса
    'indexing', оставшегося от прогона, который не пережил остановку
    процесса (Этап U0). Порядок важен: reset_stale_indexing открывает БД, и
    на новом месте без переноса появился бы пустой индекс."""
    try:
        from .core.project_manager import ProjectManager
        pm = ProjectManager(data_dir, _index_dir())
        try:
            try:
                pm.migrate_index_dir()
            except Exception:
                logger.exception("Не удалось перенести индексы в INDEX_DIR")
            pm.reset_stale_indexing()
        finally:
            pm.close_all()
    except Exception:
        logger.exception("Не удалось проверить незавершённые индексации")


def main():
    data_dir = os.environ.get('DATA_DIR', '/data')
    mcp_port = int(os.environ.get('MCP_PORT', '9877'))
    mcp_http_port = int(os.environ.get('MCP_HTTP_PORT', '9879'))
    web_port = int(os.environ.get('WEB_PORT', '9878'))

    # Ensure data directory exists
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    _prepare_data(data_dir)

    logger.info(f"Data directory: {data_dir}")
    logger.info(f"Index directory: {_index_dir() or data_dir + '/projects'}")
    logger.info(f"MCP SSE port: {mcp_port}")
    logger.info(f"MCP streamable HTTP port: {mcp_http_port}")
    logger.info(f"Web UI port: {web_port}")

    # Start Web UI in background thread
    web_thread = threading.Thread(target=_start_web, args=(data_dir, web_port), daemon=True)
    web_thread.start()
    logger.info(f"Web UI starting on :{web_port}")

    # Streamable HTTP (Этап 6): the current MCP transport — start alongside
    # SSE, which stays on its existing port for backward compatibility with
    # clients that haven't migrated yet (SSE is deprecated in the MCP spec).
    http_thread = threading.Thread(target=_start_mcp_streamable_http, args=(data_dir, mcp_http_port), daemon=True)
    http_thread.start()
    logger.info(f"MCP streamable HTTP starting on :{mcp_http_port}")

    # Start MCP SSE server in main thread (blocking)
    logger.info(f"MCP SSE server starting on :{mcp_port}")
    _start_mcp_sse(data_dir, mcp_port)


def _start_web(data_dir: str, port: int):
    """Start FastAPI Web UI with uvicorn."""
    try:
        import uvicorn
        os.environ['DATA_DIR'] = data_dir
        uvicorn.run(
            "src.web.app:app",
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


def _start_mcp_sse(data_dir: str, port: int):
    """Start the MCP server on the (deprecated but still widely used) SSE
    transport — kept on its existing port for backward compatibility
    (Этап 6). mcp.server.mcpserver.MCPServer.sse_app() builds this itself
    now (SDK 2.x); no more hand-rolled SseServerTransport wiring, which
    also means the old modelcontextprotocol/python-sdk#1099 Response()
    workaround is gone with it — the SDK's own app already returns one."""
    try:
        from .mcp_server.server import create_mcp_server, HAS_MCP
        if not HAS_MCP:
            logger.warning("MCP SDK not installed — running Web UI only")
            _idle_forever()
            return

        import uvicorn
        from .core.project_manager import ProjectManager
        pm = ProjectManager(data_dir, _index_dir())
        mcp = create_mcp_server(pm)

        uvicorn.run(_sse_app(mcp), host=_listen_addr(), port=port, log_level="warning")

    except ImportError as e:
        logger.warning(f"MCP dependencies missing ({e}) — running Web UI only")
        _idle_forever()
    except Exception as e:
        logger.error(f"MCP SSE server failed: {e}")
        raise


def _sse_app(mcp):
    """SSE-приложение SDK за проверкой Host/Origin (HostOriginGuard).

    Внутри контейнера слушаем 0.0.0.0 (проброс портов) — SDK тогда сам не
    включает защиту от DNS rebinding, передаём её явно. Обёртка отвечает
    421/403 до SDK: иначе SDK после ответа бросает исключение, и каждый
    отклонённый запрос оставляет в логе трассировку. Streamable HTTP в
    обёртке не нуждается — там SDK просто возвращает ответ."""
    from .security import HostOriginGuard, mcp_transport_security
    settings = mcp_transport_security()
    return HostOriginGuard(mcp.sse_app(host="0.0.0.0", transport_security=settings), settings)


def _start_mcp_streamable_http(data_dir: str, port: int):
    """Start the MCP server on the streamable HTTP transport (Этап 6) —
    the current (non-deprecated) transport in the MCP spec."""
    try:
        from .mcp_server.server import create_mcp_server, HAS_MCP
        if not HAS_MCP:
            return  # already logged/idled by _start_mcp_sse in the main thread

        import uvicorn
        from .core.project_manager import ProjectManager
        pm = ProjectManager(data_dir, _index_dir())
        mcp = create_mcp_server(pm)

        from .security import mcp_transport_security
        app = mcp.streamable_http_app(host="0.0.0.0", transport_security=mcp_transport_security())
        uvicorn.run(app, host=_listen_addr(), port=port, log_level="warning")

    except ImportError as e:
        logger.warning(f"MCP dependencies missing ({e}) — streamable HTTP transport unavailable")
    except Exception as e:
        logger.error(f"MCP streamable HTTP server failed: {e}")


if __name__ == '__main__':
    main()
