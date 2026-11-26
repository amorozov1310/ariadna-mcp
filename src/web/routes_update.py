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
from ..core.source_delta import DeltaConflict, DeltaError
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
    request: Request,
    project_id: str,
    source_id: str,
    xml_archive: UploadFile | None = File(None),
    delta_archive: UploadFile | None = File(None),
    base_version: str = Form(''),
    target_manifest: str = Form(''),
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
    max_body = int(os.environ.get('ARIADNA_DELTA_MAX_BODY', str(34 * 1024**3)))
    content_length = request.headers.get('content-length')
    if delta_archive is not None:
        try:
            request_size = int(content_length or '')
        except ValueError:
            raise HTTPException(411, 'Для архива разницы нужен Content-Length') from None
        if request_size > max_body:
            raise HTTPException(413, 'Размер запроса превышает лимит')
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")
    if not any(s.id == source_id for s in project.sources):
        raise HTTPException(404, f"Источник «{source_id}» не найден в проекте «{project_id}»")
    started = time.monotonic()
    deadline = started + max(1, timeout)

    if delta_archive is not None and delta_archive.filename:
        if xml_archive is not None and xml_archive.filename:
            raise HTTPException(400, 'xml_archive и delta_archive нельзя передавать одновременно')
        if not base_version or not target_manifest:
            raise HTTPException(400, 'Для разницы нужны base_version и target_manifest')
        if not await _wait_idle(pm, project_id, deadline):
            return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                 'detail': 'Проект всё ещё индексируется — разница не применена'},
                                status_code=409)
        try:
            parsed_target = json.loads(target_manifest)
            delta_archive.file.seek(0, 2)
            archive_size = delta_archive.file.tell()
            delta_archive.file.seek(0)
            if archive_size > max_body:
                raise DeltaError('Размер архива превышает лимит')
            await run_in_threadpool(pm.apply_source_delta, project_id, source_id,
                                    delta_archive.file, base_version, parsed_target)
        except DeltaConflict as e:
            raise HTTPException(409, str(e)) from e
        except ReindexInProgressError as e:
            raise HTTPException(409, f'Разница не применена: {e}') from e
        except (DeltaError, ValueError, KeyError, json.JSONDecodeError) as e:
            raise HTTPException(400, f'Разница не применена: {e}') from e
    elif xml_archive is not None and xml_archive.filename:
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


@router.get('/api/projects/{project_id}/sources/{source_id}/manifest')
async def source_manifest(project_id: str, source_id: str):
    """Версия и хеши файлов выгрузки; вычисление не запускает переиндексацию."""
    try:
        return await run_in_threadpool(get_pm().source_file_manifest, project_id, source_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
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
    try:
        timeout = int(form.get('timeout', 3600))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Некорректный timeout') from None
    return await refresh_source(
        request, project_id, source_id, xml_archive=None, delta_archive=archive,
        base_version=str(form.get('base_version', '')),
        target_manifest=str(form.get('target_manifest', '')),
        wait=str(form.get('wait', '1')), timeout=timeout)
