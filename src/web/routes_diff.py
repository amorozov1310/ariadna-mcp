"""Routes: отличия проекта от базы (src/core/base_diff.py) — то же, что
инструмент MCP diff_from_base, для человека: сводка, доработанные / новые /
удалённые объекты, diff текста процедуры."""

from fastapi import APIRouter, Form, HTTPException, Request
from urllib.parse import quote

from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .app import get_pm, templates

router = APIRouter()

_SECTIONS = {'changed': 'Доработанные', 'added': 'Новые', 'removed': 'Удалённые'}


def _project(project_id: str):
    try:
        return get_pm().get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")


def _scheme(project, release_id: str = '') -> dict:
    """Схема сравнения для шапки (_diff_header.html): база, этот проект, новый
    релиз — id и версия конфигурации из индекса; None — не задан или удалён."""
    pm = get_pm()
    known = {p.id for p in pm.list_projects()}

    def item(project_id: str):
        if not project_id or project_id not in known:
            return None
        return {'id': project_id, 'version': pm.config_version(project_id)}

    base, current = item(project.base_project), item(project.id)
    return {
        'base': base, 'project': current,
        'release': item(release_id or project.release_project),
        'versions_differ': bool(base and base['version'] and current['version']
                                and base['version'] != current['version']),
    }


def _candidates(project_id: str) -> list:
    return [p for p in get_pm().list_projects() if p.id != project_id]


@router.get("/projects/{project_id}/diff", response_class=HTMLResponse)
async def diff_page(request: Request, project_id: str, section: str = 'changed'):
    project = _project(project_id)
    pm = get_pm()
    section = section if section in _SECTIONS else 'changed'
    diff, error, pending = None, '', False
    if project.base_project:
        try:
            pending = not await run_in_threadpool(pm.warm_diffs, project_id)
            if not pending:
                diff = await run_in_threadpool(pm.get_base_diff, project_id)
        except ValueError as e:
            error = str(e)
    return templates.TemplateResponse(name="diff.html", request=request, context={
        "project": project,
        "diff": diff,
        "pending": pending,
        "totals": diff.totals() if diff else {},
        "items": diff.by_status(section) if diff else [],
        "counts": {s: len(diff.by_status(s)) for s in _SECTIONS} if diff else {},
        "section": section,
        "sections": _SECTIONS,
        "error": error,
        "scheme": _scheme(project),
        "candidates": _candidates(project_id),
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
    result, error, hint, pending = None, '', '', False
    release = release or project.release_project
    # Не ошибка: база и новый релиз необязательны — подсказка, что для раздела нужно.
    if not project.base_project:
        hint = ("Для этого раздела нужны база — типовая того же релиза, что и этот проект, — и "
                "новый релиз, на который обновляетесь («Выбрать базу и новый релиз» у схемы выше).")
    elif not release:
        hint = ("Для этого раздела нужен новый релиз — проект с выгрузкой типовой, на которую "
                "обновляетесь («Изменить сравнение» у схемы выше).")
    else:
        try:
            pending = not await run_in_threadpool(pm.warm_diffs, project_id, release)
            if not pending:
                result = await run_in_threadpool(pm.get_update_conflicts, project_id, release)
        except ValueError as e:
            error = str(e)
    groups: dict[str, list] = {}
    for c in (result.items if result else []):
        groups.setdefault(c.kind, []).append(c)
    return templates.TemplateResponse(name="diff_update.html", request=request, context={
        "project": project,
        "result": result,
        "pending": pending,
        "groups": groups,
        "kinds": CONFLICT_KINDS,
        "error": error,
        "hint": hint,
        "scheme": _scheme(project, release),
        "candidates": _candidates(project_id),
    })


def _download(body: bytes, fmt: str, name: str) -> Response:
    from ..core.diff_report import FORMATS
    ascii_name = name.encode('ascii', 'replace').decode().replace('?', '_')
    return Response(body, media_type=FORMATS[fmt], headers={
        'Content-Disposition': f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})


def _format(fmt: str) -> str:
    from ..core.diff_report import FORMATS
    if fmt not in FORMATS:
        raise HTTPException(400, f"Формат «{fmt}» не поддерживается: {', '.join(FORMATS)}")
    return fmt


@router.get("/projects/{project_id}/diff/export")
async def diff_export(project_id: str, format: str = 'md'):
    """«Что доработано» файлом. Если сравнение ещё не посчитано — считает
    (до полуминуты на больших конфигурациях), страница при этом не ждёт."""
    from ..core import diff_report
    fmt = _format(format)
    _project(project_id)
    try:
        diff = await run_in_threadpool(get_pm().get_base_diff, project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    body = await run_in_threadpool(diff_report.diff_report, project_id, diff, fmt)
    return _download(body, fmt, diff_report.filename('dorabotki', project_id, fmt))


@router.get("/projects/{project_id}/diff/update/export")
async def update_export(project_id: str, release: str = '', format: str = 'md'):
    """«Обновление на новый релиз» файлом: Markdown — чек-лист пересечений,
    CSV — таблица для Excel с пустыми колонками «Решение» и «Ответственный»."""
    from ..core import diff_report
    fmt = _format(format)
    project = _project(project_id)
    release = release or project.release_project
    try:
        result = await run_in_threadpool(get_pm().get_update_conflicts, project_id, release)
    except ValueError as e:
        raise HTTPException(400, str(e))
    body = await run_in_threadpool(diff_report.update_report, project_id, result, fmt)
    return _download(body, fmt, diff_report.filename('obnovlenie', project_id, fmt))


@router.get("/projects/{project_id}/diff/conflict", response_class=HTMLResponse)
async def conflict_page(request: Request, project_id: str, release: str, module: str, name: str):
    """Обе правки процедуры — проекта и релиза, каждая от общей базы."""
    project = _project(project_id)
    pm = get_pm()

    def both():
        # Только тексты процедуры: сравнение целиком (секунды) здесь не нужно.
        base_id = project.base_project
        ours = pm.get_procedure_diff(project_id, module, name)
        theirs = pm.get_procedure_diff(release, module, name, base_id=base_id)
        full_module = (ours['name'] or theirs['name']).rsplit('.', 1)[0]
        return base_id, ours, theirs, _intercept_code(pm, project_id, full_module, name)

    try:
        base_id, ours, theirs, intercepts = await run_in_threadpool(both)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return templates.TemplateResponse(name="diff_conflict.html", request=request, context={
        "project": project, "release": release, "name": ours['name'], "intercepts": intercepts,
        "sides": [(f"Ваша правка: {base_id} → {project_id}", ours, _diff_lines(ours), project_id),
                  (f"Изменение релиза: {base_id} → {release}", theirs, _diff_lines(theirs), release)],
    })


def _intercept_code(pm, project_id: str, module: str, procedure: str) -> list[dict]:
    """Перехваты процедуры в расширениях проекта с текстом процедур расширений."""
    from ..core.bsl_parser import read_bsl_text
    engine = pm.get_search(project_id)
    found = []
    for it in pm.intercepts(project_id, None, module, procedure):
        outline = engine.get_module_outline(it.module, source_id=it.source_id)
        proc = next((p for p in (outline or {}).get('procedures', [])
                     if p['name'].casefold() == it.ext_procedure.casefold()), None)
        path = engine.module_file_path(outline['module']) if outline and proc else None
        text = read_bsl_text(path) if path else None
        lines = text.split('\n')[proc['start_line'] - 1:proc['end_line']] if text else []
        found.append({'intercept': it, 'lines': lines})
    return found


@router.get("/api/projects/{project_id}/diff/warming")
def diff_warming(project_id: str):
    """Идёт ли фоновый расчёт сравнений — страница «идёт сравнение» ждёт false."""
    _project(project_id)
    return {"warming": get_pm().diff_warming(project_id)}


@router.post("/api/projects/{project_id}/comparison")
async def set_comparison(project_id: str, base_project: str = Form(''), release_project: str = Form(''),
                         next: str = Form('')):
    _project(project_id)
    try:
        get_pm().set_comparison(project_id, base_project, release_project)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    target = next if next.startswith(f"/projects/{project_id}/") else f"/projects/{project_id}/diff"
    return RedirectResponse(target, status_code=303)


@router.post("/api/projects/{project_id}/base")
async def set_base(project_id: str, base_project: str = Form('')):
    _project(project_id)
    try:
        get_pm().set_base_project(project_id, base_project)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    return RedirectResponse(f"/projects/{project_id}/diff", status_code=303)
