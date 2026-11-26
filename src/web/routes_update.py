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

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..core.project_manager import ReindexInProgressError
from ..core.source_manifest import (MANIFEST_NAME, MAX_UPLOAD_BYTES, UnsafeArchiveError, apply_delta,
                                    build_manifest, read_or_create_manifest, write_manifest)
from .app import get_pm

router = APIRouter()

_POLL_SEC = 2.0


def _source_paths(pm, project_id: str, source_id: str):
    root = pm.get_source_xml_path(project_id, source_id)
    if root is None:
        root = pm.projects_dir / project_id / 'sources' / source_id / 'xml'
    return root, root / MANIFEST_NAME


def _status(pm, project_id: str) -> str:
    try:
        return pm.get_project(project_id).status
    except KeyError:
        raise HTTPException(404, f"Проект «{project_id}» не найден")


@router.post("/api/projects/{project_id}/sources/{source_id}/refresh")
async def refresh_source(
    project_id: str,
    source_id: str,
    xml_archive: UploadFile | None = File(None),
    wait: str = Form('1'),
    timeout: int = Form(3600),
    refresh_manifest: str = Form('0'),
):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)
    if not any(s.id == source_id for s in project.sources):
        raise HTTPException(404)

    def publish_and_queue():
        with pm.source_operation_lock(project_id, source_id):
            if xml_archive is not None and xml_archive.filename:
                pm.update_source(project_id, source_id, xml_archive=xml_archive.file,
                                 xml_archive_filename=xml_archive.filename)
            root = pm.get_source_xml_path(project_id, source_id)
            if root is None:
                pm.register_source_xml_path(project_id, source_id)
                root = pm.get_source_xml_path(project_id, source_id)
            # Full refresh is an explicit request to reconcile externally changed files.
            if refresh_manifest.lower() in ('1', 'true', 'yes') or not (xml_archive and xml_archive.filename):
                manifest = build_manifest(root)
                write_manifest(root / MANIFEST_NAME, manifest)
            else:
                manifest = read_or_create_manifest(root, root / MANIFEST_NAME)
            pm.record_source_manifest(project_id, source_id, manifest)
            pm.enqueue_reindex(project_id, source_id)
            return manifest

    try:
        manifest = await run_in_threadpool(publish_and_queue)
    except ReindexInProgressError as e:
        raise HTTPException(409, str(e)) from e
    except (ValueError, KeyError, OSError, UnsafeArchiveError) as e:
        raise HTTPException(400, str(e)) from e
    return await _respond_for_version(pm, project_id, source_id, manifest['version'], wait, timeout)


@router.get('/api/projects/{project_id}/sources/{source_id}/manifest')
async def source_manifest(project_id: str, source_id: str):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
        source = next(s for s in project.sources if s.id == source_id)
    except (KeyError, StopIteration):
        raise HTTPException(404)
    root = pm.get_source_xml_path(project_id, source_id)
    if root is None:
        root = pm.projects_dir / project_id / 'sources' / source_id / 'xml'
    try:
        def load():
            with pm.source_operation_lock(project_id, source_id):
                return read_or_create_manifest(root, root / MANIFEST_NAME)
        value = await run_in_threadpool(load)
    except (OSError, UnsafeArchiveError) as e:
        raise HTTPException(409, f'Could not read source manifest: {e}') from e
    indexed_version = source.indexed_version or ''
    value.update(indexed_version=indexed_version,
                 indexed=(indexed_version == value['version']),
                 index_status=project.status)
    if indexed_version != value['version'] and project.status != 'indexing':
        pm.enqueue_reindex(project_id, source_id)
    return JSONResponse(value)


async def _respond_for_version(pm, project_id: str, source_id: str, version: str,
                               wait: str, timeout: int):
    body = {'project_id': project_id, 'source_id': source_id, 'version': version}
    if wait.lower() in ('0', 'false', 'no', ''):
        return JSONResponse({**body, 'status': 'queued'}, status_code=202)
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        try:
            project = pm.get_project(project_id)
            source = next(s for s in project.sources if s.id == source_id)
        except (KeyError, StopIteration):
            raise HTTPException(404)
        if source.published_version and source.published_version != version:
            return JSONResponse({**body, 'status': 'superseded',
                                 'current_version': source.published_version}, status_code=409)
        if source.indexed_version == version and project.status == 'ready':
            result = {**body, 'status': 'ready'}
            stats = project.index_stats
            if stats is not None:
                result['index'] = {k: getattr(stats, k, 0) for k in
                                   ('total_objects', 'total_modules', 'total_procedures', 'total_calls')}
            return JSONResponse(result)
        if project.status == 'error':
            return JSONResponse({**body, 'status': 'error',
                                 'detail': pm.index_error(project_id)}, status_code=500)
        await asyncio.sleep(min(.2, max(0, deadline-time.monotonic())))
    return JSONResponse({**body, 'status': 'queued'}, status_code=202)


@router.post('/api/projects/{project_id}/sources/{source_id}/delta')
async def update_source_delta(project_id: str, source_id: str, request: Request):
    # Count ASGI body bytes before Starlette parses/spools multipart fields.
    content_length = request.headers.get('content-length')
    request_limit = MAX_UPLOAD_BYTES + 1024 * 1024
    if content_length:
        try:
            if int(content_length) > request_limit:
                raise HTTPException(413, 'Delta upload exceeds the request size limit')
        except ValueError:
            raise HTTPException(400, 'Invalid Content-Length')
    receive = request._receive
    received = 0
    async def limited_receive():
        nonlocal received
        message = await receive()
        if message['type'] == 'http.request':
            received += len(message.get('body', b''))
            if received > request_limit:
                raise HTTPException(413, 'Delta upload exceeds the request size limit')
        return message
    request._receive = limited_receive
    try:
        form = await request.form(max_files=1, max_fields=3)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f'Invalid multipart request: {e}') from e
    base_version = str(form.get('base_version', ''))
    delta_archive = form.get('delta_archive')
    wait = str(form.get('wait', '1'))
    try:
        timeout = int(form.get('timeout', 3600))
    except (TypeError, ValueError):
        raise HTTPException(400, 'Invalid timeout')
    if not hasattr(delta_archive, 'file') or not hasattr(delta_archive, 'filename'):
        await form.close()
        raise HTTPException(400, 'Delta archive is missing')
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
        if not any(s.id == source_id for s in project.sources):
            await form.close()
            raise HTTPException(404)
    except KeyError:
        await form.close()
        raise HTTPException(404)
    if not delta_archive.filename:
        await form.close()
        raise HTTPException(400, 'Delta archive is missing')

    def apply_and_queue():
        with pm.source_operation_lock(project_id, source_id):
            root = pm.get_source_xml_path(project_id, source_id)
            if root is None:
                root = pm.projects_dir / project_id / 'sources' / source_id / 'xml'
            result = apply_delta(root, root / MANIFEST_NAME, delta_archive.file,
                                 delta_archive.filename, base_version)
            pm.register_source_xml_path(project_id, source_id)
            pm.record_source_manifest(project_id, source_id, result['manifest'])
            source = next(s for s in pm.get_project(project_id).sources if s.id == source_id)
            # An identical delta is still a repair request if the index trails the dump.
            if result['changed'] or source.indexed_version != result['manifest']['version']:
                pm.enqueue_reindex(project_id, source_id)
            return result['manifest']

    try:
        manifest = await run_in_threadpool(apply_and_queue)
    except UnsafeArchiveError as e:
        raise HTTPException(400, f'Delta rejected: {e}') from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except OSError as e:
        raise HTTPException(400, f'Delta rejected: {e}') from e
    finally:
        await form.close()
    return await _respond_for_version(pm, project_id, source_id, manifest['version'], wait, timeout)
