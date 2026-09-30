"""Routes: project CRUD, source management, indexing."""

import logging

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from .app import templates, get_pm
from ..core.project_manager import ReindexInProgressError

logger = logging.getLogger('ariadna')

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def projects_list(request: Request):
    pm = get_pm()
    projects = pm.list_projects()
    return templates.TemplateResponse(name="projects_list.html", request=request, context={
        "projects": projects,
    })


@router.get("/projects/new", response_class=HTMLResponse)
def project_create_form(request: Request):
    return templates.TemplateResponse(name="project_create.html", request=request, context={})


@router.post("/api/projects")
async def project_create(
    project_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(''),
):
    pm = get_pm()
    try:
        pm.create_project(project_id, name, description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_dashboard(request: Request, project_id: str):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")

    # Этап U0. Два изменения против прежнего варианта:
    #
    # 1. Числа берутся из реестра, а не из get_stats(): тот делает COUNT(*)
    #    по семи таблицам, включая calls (на БСХП ~1.5 млн строк), и во
    #    время индексации страница открывалась десятки секунд. В реестре
    #    лежат те же числа по итогам последней успешной индексации, и
    #    достаются они бесплатно. Из базы остаётся один дешёвый GROUP BY
    #    по видам.
    # 2. Статистика показывается при любом статусе, а не только 'ready':
    #    во время переиндексации незачем показывать пустой дашборд —
    #    прошлые числа никуда не делись и остаются полезными.
    s = project.index_stats
    stats = {
        'objects': s.total_objects,
        'attributes': s.total_attributes,
        'forms': s.total_forms,
        'modules': s.total_modules,
        'procedures': s.total_procedures,
        'sources': len(project.sources),
    }

    kinds: dict[str, int] = {}
    try:
        kinds = pm.get_db(project_id).get_kind_counts()
    except Exception:
        logger.exception("Не удалось прочитать виды метаданных проекта %s", project_id)

    return templates.TemplateResponse(name="project_dashboard.html", request=request, context={
        "project": project,
        "stats": stats,
        "kinds_breakdown": _kinds_breakdown(kinds),
    })


def _kinds_breakdown(kinds: dict[str, int]) -> list[dict]:
    """Виды метаданных для таблицы-топа с барами (U9).

    Сплошная строка «Справочник: 754 · Документ: 464 · …» на 25-34
    значения нечитаема: сравнить два вида глазом невозможно, а на узком
    экране это два десятка строк подряд. Здесь к каждому виду добавляется
    доля от общего числа объектов и длина бара — относительно САМОГО
    крупного вида, а не общего числа: иначе при топе в 17% все бары
    съёжились бы в левую пятую часть строки и перестали что-либо
    показывать.
    """
    total = sum(kinds.values())
    largest = max(kinds.values()) if kinds else 0

    rows = []
    for kind, count in kinds.items():
        share = (count / total * 100) if total else 0
        rows.append({
            'kind': kind,
            'count': count,
            'share': share,
            'share_str': ('<0,1' if 0 < share < 0.1 else f'{share:.1f}'.replace('.', ',')) + '%',
            # Минимум 1.5% — чтобы у самых редких видов бар оставался
            # видимой чёрточкой, а не исчезал в ноль пикселей.
            'bar': max(1.5, count / largest * 100) if largest else 0,
        })
    return rows


@router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: str):
    """Настройки проекта: имя и описание."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")

    return templates.TemplateResponse(name="project_settings.html", request=request, context={
        "project": project,
    })


@router.post("/api/projects/{project_id}/update")
async def update_project_meta(
    project_id: str,
    name: str = Form(...),
    description: str = Form(''),
):
    pm = get_pm()
    try:
        pm.update_project(project_id, name=name, description=description)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=303)


@router.post("/api/projects/{project_id}/sources")
async def add_source(
    project_id: str,
    source_id: str = Form(...),
    label: str = Form(...),
    source_type: str = Form('main'),
    # Отчёт по конфигурации Web UI больше не предлагает загружать — с
    # этапа 5 он генерируется из XML сам. Приём оставлен для volume-сценария
    # и внешних вызовов: источники, созданные когда-то одним отчётом, должны
    # продолжать работать.
    report_file: UploadFile | None = File(None),
    xml_archive: UploadFile | None = File(None),
):
    pm = get_pm()
    try:
        report_fobj = report_file.file if report_file and report_file.filename else None
        xml_fobj = xml_archive.file if xml_archive and xml_archive.filename else None
        xml_fname = xml_archive.filename if xml_archive else ''

        pm.add_source(
            project_id, source_id, label,
            source_type=source_type,
            report_file=report_fobj,
            report_filename=report_file.filename if report_file else '',
            xml_archive=xml_fobj,
            xml_archive_filename=xml_fname,
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))

    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/api/projects/{project_id}/reindex")
async def reindex_project(request: Request, project_id: str, source_id: str | None = Form(None)):
    """Kicks off reindexing in the background and returns immediately
    (Этап 4/D7) — a large corpus takes minutes, too long to hold this HTTP
    request open. The project dashboard shows status='indexing' (and, once
    polled, progress) until it flips to 'ready'; see get_db().get_stats()."""
    pm = get_pm()
    try:
        pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    result = pm.reindex_async(project_id, source_id=source_id if source_id else None)
    if result['status'] == 'error':
        # Проект могли удалить между проверкой выше и запуском.
        raise HTTPException(404 if project_id not in {p.id for p in pm.list_projects()} else 500,
                            f"Не удалось начать переиндексацию: {result['error']}")

    accept = request.headers.get('accept', '')
    if 'application/json' in accept or request.headers.get('x-requested-with'):
        return JSONResponse(result)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/api/projects/{project_id}/status")
async def project_status(project_id: str):
    """Polled by the dashboard while a reindex_async() run is in flight
    (Этап 4/D7) — status plus progress_current/progress_total/progress_phase.

    Читает только строку index_state (get_index_progress), а не get_stats():
    опрос идёт раз в 1.5 секунды, и COUNT(*) по всем таблицам во время
    активной записи индексатора занимал десятки секунд — см. Этап U0.
    """
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    progress = {}
    try:
        progress = pm.get_db(project_id).get_index_progress()
    except Exception:
        logger.exception("Не удалось прочитать прогресс индексации %s", project_id)

    return JSONResponse({
        "status": project.status,
        "progress_current": progress.get('progress_current', 0),
        "progress_total": progress.get('progress_total', 0),
        "progress_phase": progress.get('progress_phase', ''),
    })


@router.post("/api/projects/{project_id}/sources/{source_id}/update")
async def update_source(
    project_id: str,
    source_id: str,
    label: str | None = Form(None),
    source_type: str | None = Form(None),
    report_file: UploadFile | None = File(None),
    xml_archive: UploadFile | None = File(None),
    auto_reindex: str | None = Form(None),
):
    """Replace files (or just metadata) of an existing source.
    If auto_reindex=on, runs reindex for that source immediately after."""
    pm = get_pm()
    try:
        report_fobj = report_file.file if report_file and report_file.filename else None
        xml_fobj = xml_archive.file if xml_archive and xml_archive.filename else None
        xml_fname = xml_archive.filename if xml_archive else ''

        pm.update_source(
            project_id, source_id,
            label=label or None,
            source_type=source_type or None,
            report_file=report_fobj,
            report_filename=report_file.filename if report_file else '',
            xml_archive=xml_fobj,
            xml_archive_filename=xml_fname,
        )

        if auto_reindex:
            pm.reindex_async(project_id, source_id=source_id)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))

    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/api/projects/{project_id}/sources/{source_id}/delete")
async def remove_source(project_id: str, source_id: str):
    """Remove a source from the project (deletes files + indexed DB rows)."""
    pm = get_pm()
    try:
        pm.remove_source(project_id, source_id)
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Нельзя удалить источник: {e}")
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    pm = get_pm()
    try:
        pm.delete_project(project_id)
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Нельзя удалить проект: {e}")
    except KeyError as e:
        raise HTTPException(404, str(e))
    return JSONResponse({"status": "deleted", "project_id": project_id})


@router.post("/api/projects/{project_id}/delete")
async def delete_project_post(project_id: str):
    pm = get_pm()
    try:
        pm.delete_project(project_id)
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Нельзя удалить проект: {e}")
    except KeyError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse("/", status_code=303)
