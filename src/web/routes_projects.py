"""Routes: project CRUD, source management, indexing."""

import logging

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .app import templates, get_pm, project_read_lease
from ..core.project_manager import (PROJECT_GONE, ProjectFilesNotRemovedError,
                                     ReindexInProgressError)
from ..core.source_delta import DeltaBusy

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


def _back(project_id: str, next_url: str = '') -> str:
    """Адрес возврата после действия: next — только страница этого же
    проекта (не открытый редирект), иначе «Обзор»."""
    base = f"/projects/{project_id}"
    if next_url and (next_url == base or next_url.startswith(base + '/') or next_url.startswith(base + '?')):
        return next_url
    return base


@router.post("/api/projects")
async def project_create(
    project_id: str = Form(''),
    name: str = Form(...),
    description: str = Form(''),
):
    pm = get_pm()
    try:
        pm.create_project(project_id, name, description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse,
            dependencies=[Depends(project_read_lease)])
def project_dashboard(request: Request, project_id: str):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")

    # Этап U0. Два изменения против прежнего варианта:
    #
    # 1. Числа берутся из реестра, а не из get_stats(): тот делает COUNT(*)
    #    по семи таблицам, включая calls (на крупной конфигурации ~1.5 млн строк), и во
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
    except PROJECT_GONE:
        raise          # проект удалён во время запроса — 404 (app.py)
    except Exception:
        logger.exception("Не удалось прочитать виды метаданных проекта %s", project_id)

    try:
        counts = pm.overview_counts(project_id)
    except PROJECT_GONE:
        raise
    except Exception:
        counts = {}

    return templates.TemplateResponse(name="project_dashboard.html", request=request, context={
        "project": project,
        "stats": stats,
        "counts": counts,
        "kinds_breakdown": _kinds_breakdown(kinds),
        "index_error": pm.index_error(project_id) if project.status == 'error' else '',
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


def is_admin(request):
    from .routes_auth import is_admin as check
    return check(request)


SETTINGS_TABS = ('data', 'general', 'danger')


@router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: str, tab: str = 'data', edit: str = ''):
    """Настройки проекта: «Данные» (состав проекта и индекс), «Общее»
    (название, описание, сравнение с типовой), «Опасная зона» (удаление)."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")

    return templates.TemplateResponse(name="project_settings.html", request=request, context={
        "project": project,
        # «Опасная зона» — только администратору; участнику по такой ссылке — «Данные».
        "tab": tab if tab in SETTINGS_TABS and (tab != 'danger' or is_admin(request)) else 'data',
        "platform_versions": [v["version"] for v in pm.platform_help.versions()] if tab == "general" else [],
        "edit_source": next((s for s in project.sources if s.id == edit), None),
        "index_error": pm.index_error(project_id) if project.status == 'error' else '',
    })


@router.post("/api/projects/{project_id}/update")
async def update_project_meta(
    request: Request,
    project_id: str,
    name: str = Form(...),
    description: str = Form(''),
    platform_version: str = Form(''),
):
    pm = get_pm()
    try:
        changes = {'name': name, 'description': description}
        if 'platform_version' in await request.form():
            if platform_version and platform_version != pm.get_project(project_id).platform_version:
                if not pm.platform_help.get(platform_version):
                    raise HTTPException(400, 'Справка выбранной версии ещё не загружена')
            changes['platform_version'] = platform_version
        pm.update_project(project_id, **changes)
        if 'platform_version' in changes:
            pm.warm_platform_checks(project_id=project_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")
    return RedirectResponse(f"/projects/{project_id}/settings?tab=general", status_code=303)


@router.post("/api/projects/{project_id}/sources")
async def add_source(
    project_id: str,
    source_id: str = Form(...),
    label: str = Form(...),
    source_type: str = Form('main'),
    xml_archive: UploadFile | None = File(None),
    auto_reindex: str | None = Form(None),
    next: str = Form(''),
):
    """Добавить часть проекта (конфигурацию или расширение). auto_reindex=on —
    сразу обновить индекс: первый запуск «проект → выгрузка → индекс» без
    отдельного нажатия."""
    pm = get_pm()
    try:
        xml_fobj = xml_archive.file if xml_archive and xml_archive.filename else None
        xml_fname = xml_archive.filename if xml_archive else ''

        source = await run_in_threadpool(
            pm.add_source, project_id, source_id, label,
            source_type=source_type,
            xml_archive=xml_fobj,
            xml_archive_filename=xml_fname,
        )
        if auto_reindex and source.xml_path:
            pm.reindex_async(project_id)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))

    return RedirectResponse(_back(project_id, next), status_code=303)


@router.post("/api/projects/{project_id}/reindex")
async def reindex_project(request: Request, project_id: str, source_id: str | None = Form(None),
                         next: str = Form('')):
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
    return RedirectResponse(_back(project_id, next), status_code=303)


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
        with pm.read_lease(project_id) as db:
            progress = db.get_index_progress()
    except PROJECT_GONE:
        raise          # проект удалён во время запроса — 404 (app.py)
    except Exception:
        logger.exception("Не удалось прочитать прогресс индексации %s", project_id)

    return JSONResponse({
        "status": project.status,
        "progress_current": progress.get('progress_current', 0),
        "progress_total": progress.get('progress_total', 0),
        "progress_phase": progress.get('progress_phase', ''),
        "error": pm.index_error(project_id) if project.status == 'error' else '',
    })


@router.post("/api/projects/{project_id}/sources/{source_id}/update")
async def update_source(
    project_id: str,
    source_id: str,
    label: str | None = Form(None),
    source_type: str | None = Form(None),
    xml_archive: UploadFile | None = File(None),
    auto_reindex: str | None = Form(None),
    next: str = Form(''),
):
    """Replace files (or just metadata) of an existing source.
    If auto_reindex=on, runs reindex for that source immediately after."""
    pm = get_pm()
    try:
        xml_fobj = xml_archive.file if xml_archive and xml_archive.filename else None
        xml_fname = xml_archive.filename if xml_archive else ''

        await run_in_threadpool(
            pm.update_source, project_id, source_id,
            label=label or None,
            source_type=source_type or None,
            xml_archive=xml_fobj,
            xml_archive_filename=xml_fname,
        )

        if auto_reindex:
            result = pm.reindex_async(project_id, source_id=source_id)
            if result['status'] == 'already_running':
                raise HTTPException(409, 'Выгрузка обновлена, но переиндексация уже выполняется; проверьте статус проекта')
    except DeltaBusy as e:
        raise HTTPException(409, f'Источник занят другой операцией: {e}') from e
    except ReindexInProgressError as e:
        raise HTTPException(409, f'Выгрузка не изменена: {e}') from e
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))

    return RedirectResponse(_back(project_id, next), status_code=303)


@router.post("/api/projects/{project_id}/sources/{source_id}/delete")
async def remove_source(project_id: str, source_id: str, next: str = Form('')):
    """Remove a source from the project (deletes files + indexed DB rows)."""
    pm = get_pm()
    try:
        await run_in_threadpool(pm.remove_source, project_id, source_id)
    except DeltaBusy as e:
        raise HTTPException(409, f'Источник занят другой операцией: {e}') from e
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Нельзя удалить часть проекта: {e}")
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(_back(project_id, next), status_code=303)


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    pm = get_pm()
    try:
        deleted = await run_in_threadpool(pm.delete_project, project_id)
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Нельзя удалить проект: {e}")
    except ProjectFilesNotRemovedError as e:
        raise HTTPException(500, f"Не удалось удалить проект до конца: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, str(e))
    if not deleted:
        return JSONResponse({"status": "not_found", "project_id": project_id}, status_code=404)
    return JSONResponse({"status": "deleted", "project_id": project_id})


@router.delete("/api/projects/")
async def delete_project_without_id():
    """Не позволять пустому ID пройти через slash redirect до 405."""
    raise HTTPException(400, "Не указан ID проекта для удаления.")


@router.delete("/api/projects//")
async def delete_project_without_id_trailing_slash():
    """Пустой ID с завершающим слэшем: та же ошибка, без 307-редиректа."""
    raise HTTPException(400, "Не указан ID проекта для удаления.")


@router.post("/api/projects/{project_id}/delete")
async def delete_project_post(project_id: str):
    pm = get_pm()
    try:
        await run_in_threadpool(pm.delete_project, project_id)
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Нельзя удалить проект: {e}")
    except ProjectFilesNotRemovedError as e:
        raise HTTPException(500, f"Не удалось удалить проект до конца: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except KeyError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse("/", status_code=303)


@router.post("/api/projects//delete")
async def delete_project_post_without_id():
    """Вернуть понятную ошибку для формы с пустым сегментом ID."""
    raise HTTPException(400, "Не указан ID проекта для удаления.")


@router.post("/api/projects//delete/")
async def delete_project_post_without_id_trailing_slash():
    """Форма с пустым ID и завершающим слэшем: та же ошибка, без 307-редиректа."""
    raise HTTPException(400, "Не указан ID проекта для удаления.")
