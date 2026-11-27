"""Routes: обновление источника для робота выгрузки (docs/update.md,
scripts/update_from_infobase.ps1) — одним запросом: при необходимости
заменить выгрузку архивом, переиндексировать и дождаться результата.

Выгрузку можно не передавать: робот сам положил обновлённые файлы в папку
источника (общая папка, robocopy) — тогда это «переиндексировать и дождаться».
Роль user достаточно: заменяются файлы существующего источника, проекты и
источники не создаются и не удаляются.
"""

import asyncio
import json
import os
import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..core.project_manager import ReindexInProgressError
from ..core.source_delta import DeltaBusy, DeltaConflict, DeltaError, parse_manifest_json
from .app import get_pm

router = APIRouter()

_POLL_SEC = 2.0
_MAX_MANIFEST_TEXT = int(os.environ.get('ARIADNA_DELTA_MAX_MANIFEST', str(16 * 1024**2)))


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


async def _reindex_source(pm, project_id: str, source_id: str, wait: str,
                          timeout: int, started: float, deadline: float):
    """Запускает штатную переиндексацию источника."""
    no_wait = wait in ('0', 'false', 'no', '')
    while True:
        if not no_wait and not await _wait_idle(pm, project_id, deadline):
            return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                 'detail': 'Проект всё ещё индексируется'}, status_code=409)
        ensure = getattr(pm, 'ensure_source_xml_path', None)
        if ensure is not None:
            try:
                ensure(project_id, source_id)
            except DeltaError as e:
                raise HTTPException(409, f'Выгрузка недоступна для переиндексации: {e}') from e
        result = pm.reindex_async(project_id, source_id=source_id)
        if result['status'] != 'already_running':
            break
        if no_wait or time.monotonic() >= deadline:
            return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                 'detail': 'Переиндексация уже выполняется'}, status_code=409)
        await asyncio.sleep(min(_POLL_SEC, max(0, deadline - time.monotonic())))
    if result['status'] == 'error':
        raise HTTPException(500, f"Не удалось начать переиндексацию: {result.get('error', '')}")
    body = {'project_id': project_id, 'source_id': source_id}
    if no_wait:
        return JSONResponse({**body, 'status': 'started'}, status_code=202)
    if not await _wait_idle(pm, project_id, deadline):
        return JSONResponse({**body, 'status': 'running',
                             'detail': f'Индексация ещё идёт после {timeout} с; проверьте состояние: '
                                       f'GET /api/projects/{project_id}/status'}, status_code=202)
    project = pm.get_project(project_id)
    body.update(status=project.status, duration_sec=round(time.monotonic() - started, 1))
    stats = project.index_stats
    if stats is not None:
        body['index'] = {k: getattr(stats, k, 0) for k in
                         ('total_objects', 'total_modules', 'total_procedures', 'total_calls')}
    if project.status != 'ready':
        body['detail'] = 'Переиндексация завершилась с ошибкой; подробности доступны в статусе проекта'
        return JSONResponse(body, status_code=500)
    return JSONResponse(body)


@router.post("/api/projects/{project_id}/sources/{source_id}/refresh")
async def refresh_source(
    project_id: str,
    source_id: str,
    xml_archive: UploadFile | None = File(None),
    wait: str = Form('1'),
    timeout: int = Form(3600),
):
    """Обновить выгрузку при необходимости и запустить штатную переиндексацию."""
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
        if not await _wait_idle(pm, project_id, deadline):
            return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                 'detail': 'Проект всё ещё индексируется — выгрузка не заменена'}, status_code=409)
        try:
            await run_in_threadpool(pm.update_source, project_id, source_id,
                                    xml_archive=xml_archive.file,
                                    xml_archive_filename=xml_archive.filename)
        except DeltaBusy as e:
            raise HTTPException(409, f'Выгрузка не заменена: {e}') from e
        except ReindexInProgressError as e:
            raise HTTPException(409, f'Выгрузка не заменена: {e}') from e
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f'Выгрузка не заменена: {e}') from e
    elif wait in ('0', 'false', 'no', '') and not await _wait_idle(pm, project_id, deadline):
        return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                             'detail': 'Проект всё ещё индексируется'}, status_code=409)
    return await _reindex_source(pm, project_id, source_id, wait, timeout, started, deadline)


@router.get('/api/projects/{project_id}/sources/{source_id}/manifest')
async def source_manifest(project_id: str, source_id: str):
    """Версия и хеши файлов выгрузки; вычисление не запускает переиндексацию."""
    try:
        return await run_in_threadpool(get_pm().source_file_manifest, project_id, source_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ReindexInProgressError as e:
        raise HTTPException(409, f"Манифест временно недоступен: {e}") from e
    except DeltaError as e:
        raise HTTPException(409, str(e)) from e


@router.post('/api/projects/{project_id}/sources/{source_id}/delta')
async def refresh_source_delta(request: Request, project_id: str, source_id: str):
    """Принимает ограниченный multipart и передаёт разницу штатному refresh."""
    max_body = int(os.environ.get('ARIADNA_DELTA_MAX_BODY', str(34 * 1024**3)))
    try:
        length = int(request.headers.get('content-length', ''))
    except ValueError:
        raise HTTPException(411, 'Для архива разницы нужен Content-Length') from None
    if length > max_body:
        raise HTTPException(413, 'Размер запроса превышает лимит')
    try:
        form = await request.form(max_files=1, max_fields=5, max_part_size=max_body)
    except Exception as e:
        raise HTTPException(400, f'Некорректная форма разницы: {e}') from e
    archive = form.get('delta_archive')
    if archive is None or not hasattr(archive, 'file') or not hasattr(archive, 'filename'):
        raise HTTPException(400, 'Не передан delta_archive')
    manifest_text = form.get('target_manifest', '')
    if not isinstance(manifest_text, str) or len(manifest_text.encode('utf-8')) > _MAX_MANIFEST_TEXT:
        raise HTTPException(413, 'Целевой манифест превышает лимит размера')
    try:
        timeout = int(form.get('timeout', 3600))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Некорректный timeout') from None
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден") from None
    if not any(s.id == source_id for s in project.sources):
        raise HTTPException(404, f"Источник «{source_id}» не найден в проекте «{project_id}»")
    started = time.monotonic()
    deadline = started + max(1, timeout)
    if str(form.get('wait', '1')) not in ('0', 'false', 'no', '') and not await _wait_idle(pm, project_id, deadline):
        return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                             'detail': 'Проект всё ещё индексируется — разница не применена'}, status_code=409)
    try:
        target = parse_manifest_json(str(manifest_text))
        archive.file.seek(0, 2)
        if archive.file.tell() > max_body:
            raise DeltaError('Размер архива превышает лимит')
        archive.file.seek(0)
        await run_in_threadpool(pm.apply_source_delta, project_id, source_id, archive.file,
                                str(form.get('base_version', '')), target)
    except DeltaConflict as e:
        raise HTTPException(409, str(e)) from e
    except DeltaBusy as e:
        raise HTTPException(409, str(e)) from e
    except ReindexInProgressError as e:
        raise HTTPException(409, f'Разница не применена: {e}') from e
    except (DeltaError, ValueError, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(400, f'Разница не применена: {e}') from e
    return await _reindex_source(pm, project_id, source_id, str(form.get('wait', '1')),
                                 timeout, started, deadline)
