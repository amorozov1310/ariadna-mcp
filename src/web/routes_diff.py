"""Routes: отличия проекта от базы (src/core/base_diff.py) — то же, что
инструмент MCP diff_from_base, для человека: сводка, доработанные / новые /
удалённые объекты, diff текста процедуры."""

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .app import get_pm, templates

router = APIRouter()

_SECTIONS = {'changed': 'Доработанные', 'added': 'Новые', 'removed': 'Удалённые'}


def _project(project_id: str):
    try:
        return get_pm().get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")


@router.get("/projects/{project_id}/diff", response_class=HTMLResponse)
async def diff_page(request: Request, project_id: str, section: str = 'changed'):
    project = _project(project_id)
    pm = get_pm()
    section = section if section in _SECTIONS else 'changed'
    diff, error = None, ''
    if project.base_project:
        try:
            diff = await run_in_threadpool(pm.get_base_diff, project_id)
        except ValueError as e:
            error = str(e)
    return templates.TemplateResponse(name="diff.html", request=request, context={
        "project": project,
        "diff": diff,
        "totals": diff.totals() if diff else {},
        "items": diff.by_status(section) if diff else [],
        "counts": {s: len(diff.by_status(s)) for s in _SECTIONS} if diff else {},
        "section": section,
        "sections": _SECTIONS,
        "error": error,
        "candidates": [p for p in pm.list_projects() if p.id != project_id],
    })


@router.get("/projects/{project_id}/diff/procedure", response_class=HTMLResponse)
async def procedure_diff_page(request: Request, project_id: str, module: str, name: str):
    project = _project(project_id)
    try:
        result = await run_in_threadpool(get_pm().get_procedure_diff, project_id, module, name)
    except ValueError as e:
        result = {'status': 'error', 'error': str(e), 'name': f"{module}.{name}", 'diff': ''}
    return templates.TemplateResponse(name="diff_procedure.html", request=request, context={
        "project": project, "result": result, "lines": _diff_lines(result), "module": module,
    })


def _diff_lines(result: dict) -> list[tuple[str, str]]:
    """Строки унифицированного diff с видом для подсветки, без заголовков ---/+++."""
    lines = []
    for line in (result.get('diff') or '').split('\n')[2:]:
        kind = ('hunk' if line.startswith('@@') else 'add' if line.startswith('+')
                else 'del' if line.startswith('-') else 'ctx')
        lines.append((kind, line))
    return lines


@router.get("/projects/{project_id}/diff/update", response_class=HTMLResponse)
async def update_page(request: Request, project_id: str, release: str = ''):
    """Подготовка к обновлению: пересечение доработок с изменениями релиза."""
    from ..core.base_diff import CONFLICT_KINDS
    project = _project(project_id)
    pm = get_pm()
    result, error = None, ''
    if release:
        try:
            result = await run_in_threadpool(pm.get_update_conflicts, project_id, release)
        except ValueError as e:
            error = str(e)
    groups: dict[str, list] = {}
    for c in (result.items if result else []):
        groups.setdefault(c.kind, []).append(c)
    return templates.TemplateResponse(name="diff_update.html", request=request, context={
        "project": project,
        "result": result,
        "groups": groups,
        "kinds": CONFLICT_KINDS,
        "release": release,
        "error": error,
        "candidates": [p for p in pm.list_projects()
                       if p.id not in (project_id, project.base_project)],
    })


@router.get("/projects/{project_id}/diff/conflict", response_class=HTMLResponse)
async def conflict_page(request: Request, project_id: str, release: str, module: str, name: str):
    """Обе правки процедуры — проекта и релиза, каждая от общей базы."""
    project = _project(project_id)
    pm = get_pm()

    def both():
        base_id = pm.get_update_conflicts(project_id, release).base_project
        return (base_id, pm.get_procedure_diff(project_id, module, name),
                pm.get_procedure_diff(release, module, name, base_id=base_id))

    try:
        base_id, ours, theirs = await run_in_threadpool(both)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return templates.TemplateResponse(name="diff_conflict.html", request=request, context={
        "project": project, "release": release, "name": ours['name'],
        "sides": [(f"Ваша правка: {base_id} → {project_id}", ours, _diff_lines(ours), project_id),
                  (f"Изменение релиза: {base_id} → {release}", theirs, _diff_lines(theirs), release)],
    })


@router.post("/api/projects/{project_id}/base")
async def set_base(project_id: str, base_project: str = Form('')):
    _project(project_id)
    try:
        get_pm().set_base_project(project_id, base_project)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    return RedirectResponse(f"/projects/{project_id}/diff", status_code=303)
