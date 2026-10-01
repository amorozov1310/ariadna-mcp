"""
Web UI: FastAPI + Jinja2 for managing 1C projects.
Routes split into routes_projects, routes_search, routes_playground.
"""

import logging
import os
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..core.db import IndexRemovedError
from ..core.project_manager import ProjectManager, ProjectNotFoundError
from ..security import web_allowed_hosts

logger = logging.getLogger('ariadna')

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / 'templates'
STATIC_DIR = WEB_DIR / 'static'

app = FastAPI(title="Ариадна")
# Защита от DNS rebinding: чужой Host (страница в браузере, резолвящая свой
# домен на 127.0.0.1) получает 400. Список общий с MCP, расширяется через
# MCP_ALLOWED_HOSTS (src/security.py). Healthcheck ходит на localhost — он в списке.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=web_allowed_hosts())

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ProjectManager процесса. src/main.py задаёт общий для Web UI и MCP через
# set_pm() до запуска сервера. Без него (режим разработки
# `uvicorn src.web.app:app --reload`, Web UI один в процессе) get_pm создаёт
# свой из DATA_DIR/INDEX_DIR.
_pm: ProjectManager | None = None


def set_pm(pm: ProjectManager) -> None:
    global _pm
    _pm = pm


def get_pm() -> ProjectManager:
    global _pm
    if _pm is None:
        data_dir = os.environ.get('DATA_DIR', '/data')
        _pm = ProjectManager(data_dir, os.environ.get('INDEX_DIR') or None)
    return _pm


# ============================================
# Template globals (Этап U0)
# ============================================

def _all_projects() -> list[dict]:
    """Backs the project switcher in the shared shell (U8) — every page has
    it, so it's a template global rather than something each route has to
    remember to put in its context."""
    try:
        return [{'id': p.id, 'name': p.name, 'status': p.status}
                for p in get_pm().list_projects()]
    except Exception:
        logger.exception("Could not list projects for the header switcher")
        return []


def _fmt_int(value) -> str:
    """534157 → '534 157' (U10). Non-numbers pass through untouched."""
    try:
        return f'{int(value):,}'.replace(',', ' ')
    except (TypeError, ValueError):
        return '' if value is None else str(value)


def _asset_version() -> str:
    """Метка версии для ?v= у статики: стилей, спрайта иконок и favicon.

    Без неё браузер, у которого закэширована прошлая таблица стилей,
    показывает страницу с чужими токенами: старый :root ещё определяет
    --accent, но правил новой оболочки в нём нет, и логотип из шапки
    (инлайновый SVG) растягивается во весь экран. Считается один раз при
    старте по mtime статики — образ пересобирается вместе с ней."""
    stamp = 0.0
    for name in ('style.css', 'fonts.css', 'icons.svg', 'favicon.svg'):
        try:
            stamp = max(stamp, (STATIC_DIR / name).stat().st_mtime)
        except OSError:
            pass
    return str(int(stamp))


templates.env.globals['all_projects'] = _all_projects
templates.env.globals['asset_v'] = _asset_version()
templates.env.filters['num'] = _fmt_int


# ============================================
# Error pages (Этап U0/U13)
# ============================================

def _wants_json(request: Request) -> bool:
    """API callers (and the dashboard's own fetch() polling) must keep
    getting JSON — only page navigation gets the rendered error page."""
    return (request.url.path.startswith('/api/')
            or 'application/json' in request.headers.get('accept', ''))


def _error_context(request: Request, status: int, title: str, message: str,
                    detail: str = '') -> dict:
    """The error page renders inside the normal shell where possible, so a
    404 still has navigation instead of dead-ending the user."""
    project = None
    parts = [p for p in request.url.path.split('/') if p]
    if len(parts) >= 2 and parts[0] == 'projects':
        try:
            project = get_pm().get_project(parts[1])
        except Exception:
            project = None
    return {'status': status, 'title': title, 'message': message,
            'detail': detail, 'project': project}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)

    titles = {
        404: ('Страница не найдена', 'Проверьте адрес — возможно, проект или объект был удалён.'),
        403: ('Доступ запрещён', 'У вас нет прав на этот раздел.'),
        405: ('Метод не поддерживается', 'Этот адрес не отвечает на такой запрос.'),
    }
    title, message = titles.get(
        exc.status_code, ('Ошибка', 'Запрос не удалось выполнить.'))
    detail = str(exc.detail) if exc.detail and str(exc.detail) != message else ''

    return templates.TemplateResponse(
        name="error.html", request=request, status_code=exc.status_code,
        context=_error_context(request, exc.status_code, title, message, detail))


async def _project_gone_response(request: Request):
    """Проект удалили, пока шёл запрос (delete_project под нагрузкой): 404
    «проект удалён», одна строка в лог без трассировки и без пути индекса."""
    logger.info("Проект удалён во время запроса %s", request.url.path)
    exc = StarletteHTTPException(404, 'Проект удалён.')
    return await http_exception_handler(request, exc)


@app.exception_handler(IndexRemovedError)
async def index_removed_handler(request: Request, exc: IndexRemovedError):
    return await _project_gone_response(request)


@app.exception_handler(ProjectNotFoundError)
async def project_not_found_handler(request: Request, exc: ProjectNotFoundError):
    return await _project_gone_response(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Anything that escaped a route (U13: the audit hit a bare
    'Internal Server Error' with no way back). Starlette re-raises after
    this so the traceback still reaches the server log."""
    logger.exception("Unhandled error serving %s", request.url.path)
    if _wants_json(request):
        return JSONResponse({'detail': f'{type(exc).__name__}: {exc}'}, status_code=500)

    return templates.TemplateResponse(
        name="error.html", request=request, status_code=500,
        context=_error_context(
            request, 500, 'Внутренняя ошибка',
            'Сервер не смог обработать запрос. Часто помогает просто повторить — '
            'часть таких ошибок разовые.',
            detail=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))))


# Health check
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Браузеры просят /favicon.ico независимо от <link rel="icon">; без
    этого маршрута каждый такой запрос рендерил бы страницу 404."""
    # no-cache: этот адрес браузер запрашивает сам, без ?v=, и без
    # явного указания держал бы прежнюю иконку после ребрендинга.
    return FileResponse(STATIC_DIR / 'favicon.svg', media_type='image/svg+xml',
                        headers={'Cache-Control': 'no-cache'})


# Include routers
from .routes_projects import router as projects_router
from .routes_search import router as search_router
from .routes_playground import router as playground_router

app.include_router(projects_router)
app.include_router(search_router)
app.include_router(playground_router)
