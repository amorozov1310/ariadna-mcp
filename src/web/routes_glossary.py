"""Routes: словарь проекта (src/core/glossary.py) — слова пользователей →
объекты, реквизиты, процедуры. То же, что инструменты MCP add_glossary_term,
list_glossary, remove_glossary_term, но для человека: таблица, правка,
удаление."""

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ..core.glossary import split_terms
from .app import get_pm, templates

router = APIRouter()

_KIND_LABEL = {'object': 'объект', 'attribute': 'реквизит', 'procedure': 'процедура'}


def _project(project_id: str):
    try:
        return get_pm().get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")


@router.get("/projects/{project_id}/glossary", response_class=HTMLResponse)
def glossary_page(request: Request, project_id: str, error: str = '', saved: str = '',
                  target: str = ''):
    project = _project(project_id)
    entries = get_pm().get_glossary(project_id).entries()
    return templates.TemplateResponse(name="glossary.html", request=request, context={
        "project": project,
        "entries": entries,
        "kind_label": _KIND_LABEL,
        "error": error,
        "saved": saved,
        "target": target,
    })


@router.post("/api/projects/{project_id}/glossary")
async def glossary_save(
    project_id: str,
    target: str = Form(...),
    terms: str = Form(...),
    note: str = Form(''),
    entry_id: str = Form(''),
):
    """Новая запись или правка существующей (entry_id): термины и
    примечание заменяются, а не дописываются, как у агента."""
    _project(project_id)
    pm = get_pm()

    def save():
        resolved = pm.get_search(project_id).resolve_glossary_target(target)
        if resolved is None:
            return None, f"«{target.strip()}» нет в индексе проекта — укажите полное имя объекта, реквизита или процедуры"
        if not split_terms(terms):
            return None, "Нужен хотя бы один термин"
        glossary = pm.get_glossary(project_id)
        old = glossary.get(entry_id) if entry_id else None
        if old is not None and old.target.casefold() != resolved[0].casefold():
            glossary.remove(old.id)
        entry = glossary.save(resolved[0], resolved[1], split_terms(terms), note=note.strip(),
                              author='Web UI', merge=False)
        return entry, ''

    entry, error = await run_in_threadpool(save)
    if error:
        return RedirectResponse(f"/projects/{project_id}/glossary?error={quote(error)}", status_code=303)
    return RedirectResponse(f"/projects/{project_id}/glossary?saved={quote(entry.target)}", status_code=303)


@router.post("/api/projects/{project_id}/glossary/{entry_id}/delete")
def glossary_delete(project_id: str, entry_id: str):
    _project(project_id)
    get_pm().get_glossary(project_id).remove(entry_id)
    return RedirectResponse(f"/projects/{project_id}/glossary", status_code=303)
