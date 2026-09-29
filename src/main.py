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


def main():
    data_dir = os.environ.get('DATA_DIR', '/data')
    mcp_port = int(os.environ.get('MCP_PORT', '9877'))
    mcp_http_port = int(os.environ.get('MCP_HTTP_PORT', '9879'))
    web_port = int(os.environ.get('WEB_PORT', '9878'))

    # Ensure data directory exists
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    # Этап U0: до запуска любых рабочих потоков — снять статус 'indexing',
    # оставшийся от прогона, который не пережил прошлую остановку процесса.
    # Именно здесь это безопасно: ни одна переиндексация ещё не идёт.
    try:
        from .core.project_manager import ProjectManager
        ProjectManager(data_dir, _index_dir()).reset_stale_indexing()
    except Exception:
        logger.exception("Не удалось проверить незавершённые индексации")

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
            host="0.0.0.0",
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

        app = mcp.sse_app(host="0.0.0.0")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

    except ImportError as e:
        logger.warning(f"MCP dependencies missing ({e}) — running Web UI only")
        _idle_forever()
    except Exception as e:
        logger.error(f"MCP SSE server failed: {e}")
        raise


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

        app = mcp.streamable_http_app(host="0.0.0.0")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

    except ImportError as e:
        logger.warning(f"MCP dependencies missing ({e}) — streamable HTTP transport unavailable")
    except Exception as e:
        logger.error(f"MCP streamable HTTP server failed: {e}")


if __name__ == '__main__':
    main()
