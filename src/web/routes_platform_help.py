"""Управление общей для проектов справкой платформы в настройках сервера."""

import logging
import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from zipfile import BadZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from ..core import platform_help as ph
from .app import get_pm, templates

router = APIRouter()
logger = logging.getLogger('ariadna')
_PAGE = '/admin/platform-help'
_MULTIPART_OVERHEAD = 64 * 1024
_REQUEST_SIZE_ERROR = 'размер запроса превышает допустимый предел'


async def _request_stream(request: Request, limit: int):
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise MultiPartException(_REQUEST_SIZE_ERROR)
        yield chunk


class _HelpMultipartParser(MultiPartParser):
    def __init__(self, request: Request, limit: int):
        super().__init__(request.headers, _request_stream(request, limit),
                         max_files=3, max_fields=1, max_part_size=1024)
        self._file_fields: set[str] = set()
        self._file_size = 0

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        part = self._current_part
        if part.file is None:
            if part.field_name != 'version':
                raise MultiPartException(f'неожиданное поле формы: {part.field_name}')
            return
        name = part.field_name
        if name not in {'context', 'query', 'index'}:
            raise MultiPartException(f'неожиданное файловое поле: {name}')
        if name in self._file_fields:
            raise MultiPartException(f'повторяющееся файловое поле: {name}')
        if self._file_fields and (name == 'index' or 'index' in self._file_fields):
            raise MultiPartException('загружайте готовый индекс отдельно от книг справки')
        self._file_fields.add(name)
        self._file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._file_size += end - start
            if self._file_size > ph.MAX_UPLOAD_BYTES:
                raise MultiPartException(ph.UPLOAD_SIZE_ERROR)
        super().on_part_data(data, start, end)


def _back(**message):
    return RedirectResponse(_PAGE + '?' + urlencode(message), status_code=303)


@router.get(_PAGE)
def platform_settings(request: Request, message: str = '', error: str = ''):
    store = get_pm().platform_help
    versions, listing_error = store.version_listing()
    return templates.TemplateResponse(name='platform_help.html', request=request, context={
        'versions': versions, 'status': store.status(), 'message': message, 'error': error,
        'listing_error': listing_error,
    })


@router.get('/admin/platform-help/status')
def build_status():
    return get_pm().platform_help.status()


def _install_upload(form: FormData) -> None:
    pm = get_pm()
    store = pm.platform_help
    version = form.get('version', '').strip()
    context, query, index = (form.get(field) for field in ('context', 'query', 'index'))
    if isinstance(index, UploadFile) and index.filename:
        version = store.install_index(version, index.file.read())['version']
    elif isinstance(context, UploadFile) and context.filename:
        # Браузер отправляет невыбранный файл частью multipart с пустым именем.
        query_data = query.file.read() if isinstance(query, UploadFile) and query.filename else None
        store.build_from_bytes(version, context.file.read(), query_data)
    else:
        raise ValueError('выберите shcntx_ru.hbk или готовый help.db')
    pm.warm_platform_checks(version)


@router.post('/api/admin/platform-help', openapi_extra={
    'requestBody': {'content': {'multipart/form-data': {'schema': {
        'type': 'object',
        'properties': {
            'version': {'type': 'string', 'default': ''},
            'context': {'type': 'string', 'format': 'binary'},
            'query': {'type': 'string', 'format': 'binary'},
            'index': {'type': 'string', 'format': 'binary'},
        },
    }}}},
})
async def upload_help(request: Request):
    try:
        # В одной форме могут быть две книги по 256 МБ и служебные поля multipart.
        limit = 2 * ph.MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD
        if int(request.headers.get('content-length', '0')) > limit:
            return _back(error=_REQUEST_SIZE_ERROR)
        media_type = request.headers.get('content-type', '').partition(';')[0].strip().lower()
        if media_type != 'multipart/form-data':
            return _back(error='выберите shcntx_ru.hbk или готовый help.db')
        form = await _HelpMultipartParser(request, limit).parse()
        try:
            await run_in_threadpool(_install_upload, form)
        finally:
            await form.close()
    except ClientDisconnect:
        # MultiPartParser закрывает временные файлы при исключении из потока.
        message = 'Загрузка справки прервана клиентом.'
        logger.info(message)
        raise HTTPException(status_code=400, detail=message) from None
    except (MultiPartException, ValueError, OSError, BadZipFile, sqlite3.DatabaseError) as e:
        return _back(error=str(e))
    return _back(message='Справка загружена и доступна агентам.')


@router.get('/api/admin/platform-help/{version}/download')
def download_help(version: str):
    store = get_pm().platform_help
    missing = 'Версия справки платформы не найдена.'
    try:
        source = store.path(version).resolve()
    except ValueError:
        raise HTTPException(status_code=404, detail=missing) from None
    if not source.is_relative_to(store.root.resolve()) or not any(
            item['version'] == version for item in store.versions()):
        raise HTTPException(status_code=404, detail=missing)

    # Хранилище публикует готовый файл атомарной заменой и больше его не пишет.
    # Копия позволяет завершить скачивание, даже если версию заменят или удалят.
    directory = TemporaryDirectory(prefix='ariadna-platform-help-')
    snapshot = Path(directory.name) / ph.INDEX_FILE
    try:
        shutil.copyfile(source, snapshot)
    except FileNotFoundError:
        directory.cleanup()
        raise HTTPException(status_code=404, detail=missing) from None
    except OSError:
        directory.cleanup()
        raise
    return FileResponse(snapshot, media_type='application/octet-stream', filename=f'help-{version}.db',
                        background=BackgroundTask(directory.cleanup))


@router.post('/api/admin/platform-help/{version}/delete')
def remove_help(version: str):
    pm = get_pm()
    store = pm.platform_help
    try:
        from ..core.platform_check import forget_report_version
        result = forget_report_version(
            version, allowed_root=pm.index_dir or pm.projects_dir,
            protected_roots=pm._protected_project_roots())
        if not result:
            raise OSError(result.reason or 'не удалось удалить архивы справки')
        removed = store.remove(version)
        if removed:
            pm.warm_platform_checks()
    except (ValueError, OSError) as e:
        return _back(error=str(e))
    return _back(message=f'Справка {version} удалена.')
