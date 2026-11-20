"""Управление общей для проектов справкой платформы в настройках сервера."""

import sqlite3
from urllib.parse import urlencode
from zipfile import BadZipFile

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from .app import get_pm, templates

router = APIRouter()
_PAGE = '/admin/platform-help'
_MAX_UPLOAD = 256 * 1024 * 1024


def _read(upload: UploadFile) -> bytes:
    if upload.size is not None and upload.size > _MAX_UPLOAD:
        raise ValueError('размер файла превышает 256 МБ')
    data = upload.file.read(_MAX_UPLOAD + 1)
    if len(data) > _MAX_UPLOAD:
        raise ValueError('размер файла превышает 256 МБ')
    return data


def _back(**message):
    return RedirectResponse(_PAGE + '?' + urlencode(message), status_code=303)


@router.get(_PAGE)
def platform_settings(request: Request, message: str = '', error: str = ''):
    store = get_pm().platform_help
    return templates.TemplateResponse(name='platform_help.html', request=request, context={
        'versions': store.versions(), 'status': store.status(), 'message': message, 'error': error,
    })


@router.get('/admin/platform-help/status')
def build_status():
    return get_pm().platform_help.status()


@router.post('/api/admin/platform-help')
def upload_help(version: str = Form(''), context: UploadFile | None = File(None),
                query: UploadFile | None = File(None), index: UploadFile | None = File(None)):
    store = get_pm().platform_help
    try:
        if index and not context and not query:
            store.install_index(version.strip(), _read(index))
        elif context and not index:
            # Браузер отправляет невыбранный файл частью multipart с пустым именем.
            query_data = _read(query) if query and query.filename else None
            store.build_from_bytes(version.strip(), _read(context), query_data)
        else:
            raise ValueError('выберите shcntx_ru.hbk или готовый help.db')
    except (ValueError, OSError, BadZipFile, sqlite3.DatabaseError) as e:
        return _back(error=str(e))
    return _back(message='Справка загружена и доступна агентам.')


@router.post('/api/admin/platform-help/{version}/delete')
def remove_help(version: str):
    try:
        get_pm().platform_help.remove(version)
    except (ValueError, OSError) as e:
        return _back(error=str(e))
    return _back(message=f'Справка {version} удалена.')
