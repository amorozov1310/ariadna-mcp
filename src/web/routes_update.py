"""Routes: обновление источника для робота выгрузки (docs/update.md,
scripts/update_from_infobase.ps1) — одним запросом: при необходимости
заменить выгрузку архивом, переиндексировать и дождаться результата.

Выгрузку можно не передавать: робот сам положил обновлённые файлы в папку
источника (общая папка, robocopy) — тогда это «переиндексировать и дождаться».
Роль user достаточно: заменяются файлы существующего источника, проекты и
источники не создаются и не удаляются.
"""

import asyncio
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..core.project_manager import ReindexInProgressError
from .app import get_pm

router = APIRouter()

_POLL_SEC = 2.0


def _status(pm, project_id: str) -> str:
    try:
        return pm.get_project(project_id).status
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")


async def _wait_idle(pm, project_id: str, deadline: float) -> bool:
    """Дождаться, пока проект не индексируется; False — истёк срок."""
    while _status(pm, project_id) == 'indexing':
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(_POLL_SEC)
    return True


@router.post("/api/projects/{project_id}/sources/{source_id}/refresh")
async def refresh_source(
    project_id: str,
    source_id: str,
    xml_archive: UploadFile | None = File(None),
    wait: str = Form('1'),
    timeout: int = Form(3600),
):
    """Обновить источник и переиндексировать.

    xml_archive — новая выгрузка архивом (zip/7z/…; старая папка заменяется
    целиком); без него — индексируются файлы, уже лежащие в папке источника.
    wait=1 (по умолчанию) — ответ после окончания индексации: status ready /
    error, или running (HTTP 202), если не уложились в timeout секунд;
    wait=0 — ответ сразу после запуска."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")
    if not any(s.id == source_id for s in project.sources):
        raise HTTPException(404, f"Источник «{source_id}» не найден в проекте «{project_id}»")
    started = time.monotonic()
    deadline = started + max(1, timeout)

    if xml_archive is not None and xml_archive.filename:
        # Пока индексация читает файлы источника, заменять их нельзя.
        if not await _wait_idle(pm, project_id, deadline):
            return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                 'detail': 'Проект всё ещё индексируется — выгрузка не заменена'},
                                status_code=409)
        try:
            await run_in_threadpool(pm.update_source, project_id, source_id,
                                    xml_archive=xml_archive.file,
                                    xml_archive_filename=xml_archive.filename)
        except ReindexInProgressError as e:
            raise HTTPException(409, f'Выгрузка не заменена: {e}')
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f'Выгрузка не заменена: {e}')

    # Идёт другая индексация — дождаться и запустить свою: файлы могли
    # измениться уже после её начала.
    if not await _wait_idle(pm, project_id, deadline):
        return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                             'detail': 'Проект всё ещё индексируется'}, status_code=409)
    result = pm.reindex_async(project_id, source_id=source_id)
    if result['status'] == 'already_running':
        if not await _wait_idle(pm, project_id, deadline):
            return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                 'detail': 'Проект всё ещё индексируется'}, status_code=409)
        result = pm.reindex_async(project_id, source_id=source_id)
    if result['status'] == 'error':
        raise HTTPException(500, f"Не удалось начать переиндексацию: {result.get('error', '')}")

    body = {'project_id': project_id, 'source_id': source_id}
    if wait in ('0', 'false', 'no', ''):
        return JSONResponse({**body, 'status': 'started'}, status_code=202)
    if not await _wait_idle(pm, project_id, deadline):
        return JSONResponse({**body, 'status': 'running',
                             'detail': f'Индексация идёт дольше {timeout} с — проверьте статус позже: '
                                       f'GET /api/projects/{project_id}/status'}, status_code=202)
    project = pm.get_project(project_id)
    body.update(status=project.status, duration_sec=round(time.monotonic() - started, 1))
    stats = project.index_stats
    if stats is not None:
        body['index'] = {k: getattr(stats, k, 0) for k in
                         ('total_objects', 'total_modules', 'total_procedures', 'total_calls')}
    if project.status != 'ready':
        body['detail'] = 'Индексация завершилась с ошибкой — подробности в журнале сервера'
        return JSONResponse(body, status_code=500)
    return JSONResponse(body)
