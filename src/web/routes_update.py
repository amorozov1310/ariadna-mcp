"""Routes: обновление источника для робота выгрузки (docs/update.md,
scripts/update_from_infobase.ps1) — одним запросом: при необходимости
заменить выгрузку архивом, переиндексировать и дождаться результата.

Выгрузку можно не передавать: робот сам положил обновлённые файлы в папку
источника (общая папка, robocopy) — тогда это «переиндексировать и дождаться».
Роль user достаточно: заменяются файлы существующего источника, проекты и
источники не создаются и не удаляются.
"""

import asyncio
import threading
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..core.project_manager import ReindexInProgressError
from ..core.source_manifest import (MANIFEST_NAME, UnsafeArchiveError, apply_delta,
                                    build_manifest, read_or_create_manifest, write_manifest)
from .app import get_pm

router = APIRouter()

_POLL_SEC = 2.0
_delta_locks: dict[str, threading.Lock] = {}
_delta_locks_guard = threading.Lock()


def _source_paths(pm, project_id: str, source_id: str):
    root = pm.get_source_xml_path(project_id, source_id)
    if root is None:
        root = pm.projects_dir / project_id / 'sources' / source_id / 'xml'
    return root, root / MANIFEST_NAME


def _delta_lock(key: str) -> threading.Lock:
    with _delta_locks_guard:
        return _delta_locks.setdefault(key, threading.Lock())


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
    refresh_manifest: str = Form('0'),
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
        lock = _delta_lock(f'{project_id}/{source_id}')
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Для источника уже применяется другое обновление')
        # Пока индексация читает файлы источника, заменять их нельзя.
        try:
            if not await _wait_idle(pm, project_id, deadline):
                return JSONResponse({'status': 'busy', 'project_id': project_id, 'source_id': source_id,
                                     'detail': 'Проект всё ещё индексируется — выгрузка не заменена'},
                                    status_code=409)
            await run_in_threadpool(pm.update_source, project_id, source_id,
                                    xml_archive=xml_archive.file,
                                    xml_archive_filename=xml_archive.filename)
            xml_root, sidecar = _source_paths(pm, project_id, source_id)
            def refresh_saved_manifest():
                write_manifest(sidecar, build_manifest(xml_root))
            await run_in_threadpool(refresh_saved_manifest)
        except ReindexInProgressError as e:
            raise HTTPException(409, f'Выгрузка не заменена: {e}')
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f'Выгрузка не заменена: {e}')
        finally:
            lock.release()
    elif refresh_manifest.lower() in ('1', 'true', 'yes'):
        # Общая папка/robocopy может обновить источник в обход API. Клиент
        # явно просит зафиксировать новые хеши после такого зеркалирования.
        xml_root, sidecar = _source_paths(pm, project_id, source_id)
        try:
            def rebuild_saved_manifest():
                with _delta_lock(f'{project_id}/{source_id}'):
                    write_manifest(sidecar, build_manifest(xml_root))
            await run_in_threadpool(rebuild_saved_manifest)
        except (OSError, UnsafeArchiveError) as e:
            raise HTTPException(400, f'Не удалось обновить манифест источника: {e}') from e

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
    """Версия и хеши файлов, сохранённые после последнего обновления источника."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f'Проект «{project_id}» не найден')
    if not any(s.id == source_id for s in project.sources):
        raise HTTPException(404, f'Источник «{source_id}» не найден в проекте «{project_id}»')
    root, sidecar = _source_paths(pm, project_id, source_id)
    try:
        def load_saved_manifest():
            with _delta_lock(f'{project_id}/{source_id}'):
                return read_or_create_manifest(root, sidecar)
        value = await run_in_threadpool(load_saved_manifest)
    except (OSError, UnsafeArchiveError) as e:
        raise HTTPException(409, f'Не удалось получить манифест источника: {e}')
    return JSONResponse(value)


@router.post('/api/projects/{project_id}/sources/{source_id}/delta')
async def update_source_delta(
    project_id: str,
    source_id: str,
    base_version: str = Form(...),
    delta_archive: UploadFile = File(...),
    wait: str = Form('1'),
    timeout: int = Form(3600),
):
    """Атомарно применить архив разницы и запустить обычную инкрементальную индексацию."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404, f'Проект «{project_id}» не найден')
    if not any(s.id == source_id for s in project.sources):
        raise HTTPException(404, f'Источник «{source_id}» не найден в проекте «{project_id}»')
    if not delta_archive.filename:
        raise HTTPException(400, 'Не передан архив разницы')
    lock = _delta_lock(f'{project_id}/{source_id}')
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Для источника уже применяется другая разница')
    started = time.monotonic()
    deadline = started + max(1, timeout)
    root, sidecar = _source_paths(pm, project_id, source_id)

    def apply_if_reserved() -> bool:
        result = apply_delta(root, sidecar, delta_archive.file,
                             delta_archive.filename, base_version)
        return result['changed']

    try:
        while True:
            try:
                result = await run_in_threadpool(pm.reindex_async, project_id, source_id,
                                                 apply_if_reserved)
            except UnsafeArchiveError as e:
                raise HTTPException(400, f'Разница отклонена: {e}') from e
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            except OSError as e:
                raise HTTPException(400, f'Разница отклонена: {e}') from e
            if result['status'] != 'already_running':
                break
            if not await _wait_idle(pm, project_id, deadline):
                return JSONResponse({'status': 'busy',
                                     'detail': 'Проект индексируется — разница не применена; повторите запрос позже'},
                                    status_code=409)
        if result['status'] == 'error':
            raise HTTPException(500, f"Не удалось начать переиндексацию: {result.get('error', '')}")
    finally:
        lock.release()
    body = {'project_id': project_id, 'source_id': source_id}
    if result['status'] == 'unchanged':
        project = pm.get_project(project_id)
        body.update(status=project.status, duration_sec=round(time.monotonic() - started, 1))
        if project.index_stats is not None:
            stats = project.index_stats
            body['index'] = {k: getattr(stats, k, 0) for k in
                             ('total_objects', 'total_modules', 'total_procedures', 'total_calls')}
        return JSONResponse(body, status_code=200 if project.status == 'ready' else 500)
    if wait in ('0', 'false', 'no', ''):
        return JSONResponse({**body, 'status': 'started'}, status_code=202)
    if not await _wait_idle(pm, project_id, deadline):
        return JSONResponse({**body, 'status': 'running'}, status_code=202)
    project = pm.get_project(project_id)
    body.update(status=project.status, duration_sec=round(time.monotonic() - started, 1))
    if project.status != 'ready':
        body['detail'] = 'Индексация завершилась с ошибкой — подробности в журнале сервера'
        return JSONResponse(body, status_code=500)
    stats = project.index_stats
    if stats is not None:
        body['index'] = {k: getattr(stats, k, 0) for k in
                         ('total_objects', 'total_modules', 'total_procedures', 'total_calls')}
    return JSONResponse(body)
