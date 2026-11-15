"""Routes: проверка вызовов (src/core/broken_calls.py) — то же, что
инструмент MCP find_broken_calls, для человека: вызовы методов, которых нет в
модуле или которые не экспортные, и модули, которых нет в выгрузке."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from .app import get_pm, templates

router = APIRouter()

# Вкладки страницы — ключи результата broken_calls.
VIEWS = ('missing', 'not_export', 'external')
# Сколько строк вкладки показывать сразу; «Показать ещё» добавляет столько же.
EXTERNAL_PAGE = 100


@router.get("/projects/{project_id}/checks", response_class=HTMLResponse)
async def checks_page(request: Request, project_id: str, source: str = '', module: str = '',
                      view: str = '', n: int = EXTERNAL_PAGE):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")
    result, error, pending = None, '', False
    if project.status == 'ready':
        try:
            # Не посчитано — страница открывается сразу, проверка идёт в фоне.
            pending = not await run_in_threadpool(pm.warm_broken_calls, project_id)
            if not pending:
                result = await run_in_threadpool(pm.broken_calls, project_id, source, module)
        except Exception as e:          # индекс удалён/пересоздаётся — показать, а не 500
            error = str(e)
    # Вкладка: по умолчанию — первая непустая из ошибок, затем «Не выгружено».
    if result is not None and view not in VIEWS:
        view = next((v for v in VIEWS if result[v]), VIEWS[0])
    return templates.TemplateResponse(name="checks.html", request=request, context={
        "project": project,
        "result": result,
        "view": view,
        "n": max(EXTERNAL_PAGE, n),
        "page": EXTERNAL_PAGE,
        "pending": pending,
        "error": error,
        "source": source,
        "module": module,
        "sources": project.sources,
    })


@router.get("/api/projects/{project_id}/checks/warming")
def checks_warming(project_id: str):
    """Опрос страницы «Проверка вызовов», пока проверка считается в фоне."""
    pm = get_pm()
    try:
        pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)
    return {'warming': pm.checks_warming(project_id), 'ready': pm.broken_calls_ready(project_id)}
