"""Routes: проверка вызовов (src/core/broken_calls.py) — то же, что
инструмент MCP find_broken_calls, для человека: вызовы методов, которых нет в
модуле или которые не экспортные, и модули, которых нет в выгрузке."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from .app import get_pm, templates

router = APIRouter()


@router.get("/projects/{project_id}/checks", response_class=HTMLResponse)
async def checks_page(request: Request, project_id: str, source: str = '', module: str = ''):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")
    result, error = None, ''
    if project.status == 'ready':
        try:
            result = await run_in_threadpool(pm.broken_calls, project_id, source, module)
        except Exception as e:          # индекс удалён/пересоздаётся — показать, а не 500
            error = str(e)
    return templates.TemplateResponse(name="checks.html", request=request, context={
        "project": project,
        "result": result,
        "error": error,
        "source": source,
        "module": module,
        "sources": project.sources,
    })
