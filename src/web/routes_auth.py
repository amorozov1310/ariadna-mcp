"""Routes: вход по личному токену и управление токенами (src/auth.py).

Пока токенов нет, авторизация выключена и Web UI работает как раньше. С
первым токеном каждый запрос требует его: заголовок `Authorization: Bearer`
(скрипты, API) или cookie после входа на /login (браузер). Роль `user` не
может создавать и удалять проекты и источники, менять их настройки и
управлять токенами — это `admin`.
"""

import os
import re
import tempfile
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ..auth import ROLES, TokenStore, bearer
from .. import __version__
from ..license import LicenseError, release_date
from .app import app, get_pm, templates

router = APIRouter()

COOKIE = 'ariadna_token'
_NEW_COOKIE = 'ariadna_new_token'       # только что созданный токен — показать один раз
_COOKIE_MAX_AGE = 30 * 24 * 3600

# Текст лицензионного соглашения открыт и до входа: условия видны любому.
_PUBLIC = re.compile(r'^/(health$|login$|logout$|license/agreement$|static/|favicon)')
# Действия администратора: проекты, источники, их настройки, токены.
# Обновление выгрузки существующего источника (…/sources/<id>/refresh — робот
# выгрузки, docs/update.md) — не админское: проекты и источники не меняются.
_ADMIN_POST = re.compile(r'^/api/(projects$|projects/[^/]+/(update|delete|comparison|base)$'
                         r'|projects/[^/]+/sources(/[^/]+/(update|delete))?$|admin/)')
_ADMIN_GET = re.compile(r'^/(projects/new$|(?:api/)?admin(/|$))')

_stores: dict[str, TokenStore] = {}  # резерв для запуска Web UI без main.py
_DELTA_ROUTE = re.compile(r'^/api/projects/[^/]+/sources/[^/]+/delta$')


async def _call_next_with_delta_limit(request: Request, call_next):
    """Ограничить тело разницы до того, как FastAPI начнёт разбирать multipart."""
    if request.method != 'POST' or not _DELTA_ROUTE.match(request.url.path):
        return await call_next(request)
    limit = int(os.environ.get('ARIADNA_DELTA_MAX_BODY', str(34 * 1024**3)))
    try:
        declared = int(request.headers.get('content-length', ''))
    except ValueError:
        return JSONResponse({'detail': 'Для архива разницы нужен Content-Length'}, status_code=411)
    if declared > limit:
        return JSONResponse({'detail': 'Размер запроса превышает лимит'}, status_code=413)
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    size = 0
    try:
        async for block in request.stream():
            size += len(block)
            if size > limit:
                return JSONResponse({'detail': 'Размер запроса превышает лимит'}, status_code=413)
            spool.write(block)
        spool.seek(0)
        remaining = size

        async def replay():
            nonlocal remaining
            if remaining <= 0:
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            block = spool.read(min(64 * 1024, remaining))
            remaining -= len(block)
            return {'type': 'http.request', 'body': block, 'more_body': remaining > 0}

        request._receive = replay
        # BaseHTTPMiddleware's cached receive wrapper emits an empty body after
        # Request.stream() completes, so make it consume the spooled replay.
        request._stream_consumed = False
        return await call_next(request)
    finally:
        spool.close()


def tokens() -> TokenStore:
    # Только путь к data/ — без создания ProjectManager (и каталогов) на
    # каждый запрос: токены читаются из файла, а ProjectManager нужен роутам.
    from . import app as app_module
    pm = app_module._pm
    data_dir = str(pm.data_dir) if pm is not None else os.environ.get('DATA_DIR', '/data')
    if (app_module._token_store is not None
            and app_module._token_store.path.parent.resolve() == Path(data_dir).resolve()):
        return app_module._token_store
    store = _stores.get(data_dir)
    if store is None:
        store = _stores[data_dir] = TokenStore(data_dir)
    return store


def current_user(request: Request):
    """Пользователь запроса или None (авторизация выключена)."""
    return getattr(request.state, 'user', None)


def is_admin(request: Request) -> bool:
    """Админ — или авторизация выключена (локальная установка)."""
    user = current_user(request)
    return user is None or user.role == 'admin'


def _wants_json(request: Request) -> bool:
    return (request.url.path.startswith('/api/')
            or 'application/json' in request.headers.get('accept', ''))


@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    store = tokens()
    path = request.url.path
    if not store.enabled() or _PUBLIC.match(path):
        return await _call_next_with_delta_limit(request, call_next)
    user = store.verify(bearer(request.headers.get('authorization'))
                        or request.cookies.get(COOKIE))
    if user is None:
        if _wants_json(request):
            return JSONResponse({'detail': 'Нужен токен Ариадны: Authorization: Bearer <токен>'},
                                status_code=401, headers={'WWW-Authenticate': 'Bearer'})
        target = path + (f'?{request.url.query}' if request.url.query else '')
        return RedirectResponse(f'/login?next={quote(target)}', status_code=303)
    request.state.user = user
    reason = store.denied(user)
    if reason:
        if _wants_json(request):
            return JSONResponse({'detail': reason}, status_code=403)
        from .app import http_exception_handler
        return await http_exception_handler(request, HTTPException(403, reason))
    admin_only = (_ADMIN_POST.match(path) if request.method == 'POST' else _ADMIN_GET.match(path))
    if admin_only and user.role != 'admin':
        if _wants_json(request):
            return JSONResponse({'detail': 'Только для администратора'}, status_code=403)
        raise_page = HTTPException(403, 'Это действие доступно только администратору.')
        from .app import http_exception_handler
        return await http_exception_handler(request, raise_page)
    return await _call_next_with_delta_limit(request, call_next)


def _safe_next(target: str) -> str:
    """Только относительный адрес этого же сервера — не открытый редирект."""
    return target if target.startswith('/') and not target.startswith('//') else '/'


@router.get('/login', response_class=HTMLResponse)
def login_page(request: Request, next: str = '/', error: str = ''):
    return templates.TemplateResponse(name='login.html', request=request, context={
        'next': _safe_next(next), 'error': error, 'enabled': tokens().enabled()})


@router.post('/login')
def login(token: str = Form(''), next: str = Form('/')):
    target = _safe_next(next)
    if tokens().verify(token) is None:
        return RedirectResponse(f'/login?next={quote(target)}&error=1', status_code=303)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE, token.strip(), max_age=_COOKIE_MAX_AGE, httponly=True,
                    samesite='strict')
    return resp


@router.get('/logout')
def logout():
    resp = RedirectResponse('/login', status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get('/admin/tokens', response_class=HTMLResponse)
def tokens_page(request: Request, created: str = '', first: str = '', error: str = '',
                enable: str = ''):
    store = tokens()
    new_token = request.cookies.get(_NEW_COOKIE, '') if created else ''
    new_info = next((t for t in store.list() if t.id == created), None) if new_token else None
    resp = templates.TemplateResponse(name='tokens.html', request=request, context={
        'tokens': store.list(), 'roles': ROLES, 'error': error,
        'limit': store.limit(), 'license': store.license.state(),
        'enabled': store.enabled(), 'me': current_user(request),
        'new_token': new_token if new_info else '', 'new_info': new_info, 'first': bool(first),
        # Шаг «Включить вход по токенам» — экран с последствиями перед первым токеном.
        'enable': bool(enable)})
    if new_token:
        resp.delete_cookie(_NEW_COOKIE, path='/admin/tokens')
    return resp


@router.post('/api/admin/tokens', response_class=HTMLResponse)
def create_token(request: Request, name: str = Form(''), role: str = Form('user')):
    store = tokens()
    first = not store.enabled()
    try:
        # Первый токен включает авторизацию — он всегда администраторский,
        # иначе сервер закрылся бы от собственного владельца.
        info, token = store.create(name, 'admin' if first else role)
    except ValueError as e:
        return RedirectResponse(f'/admin/tokens?error={quote(str(e))}', status_code=303)
    # Токен показывается один раз: переход на страницу токенов (обновление
    # страницы не создаст второй токен), сам токен — в cookie на одну минуту,
    # страница её читает и сразу удаляет.
    resp = RedirectResponse(f'/admin/tokens?created={info.id}' + ('&first=1' if first else ''),
                            status_code=303)
    resp.set_cookie(_NEW_COOKIE, token, max_age=60, httponly=True, samesite='strict',
                    path='/admin/tokens')
    if first:
        resp.set_cookie(COOKIE, token, max_age=_COOKIE_MAX_AGE, httponly=True, samesite='strict')
    return resp


@router.post('/api/admin/tokens/{token_id}/revoke')
def revoke_token(request: Request, token_id: str):
    store = tokens()
    me = current_user(request)
    others_admins = [t for t in store.list() if t.role == 'admin' and t.id != token_id]
    target = next((t for t in store.list() if t.id == token_id), None)
    if target is None:
        raise HTTPException(404, 'Токен не найден')
    if target.role == 'admin' and not others_admins and len(store.list()) > 1:
        return RedirectResponse('/admin/tokens?error=' + quote(
            'Нельзя отозвать последний токен администратора, пока есть другие токены'),
            status_code=303)
    store.revoke(token_id)
    if me is not None and me.id == token_id:
        return RedirectResponse('/logout', status_code=303)
    return RedirectResponse('/admin/tokens', status_code=303)


# ---------------------------------------------------------------- лицензия

@router.get('/license/agreement', response_class=PlainTextResponse)
def license_agreement():
    path = Path(__file__).resolve().parents[2] / 'LICENSE'
    if path.is_file():
        return FileResponse(path, media_type='text/plain')

    # В установленном колесе LICENSE лежит в каталоге .dist-info/licenses.
    try:
        package = distribution('ariadna')
    except PackageNotFoundError:
        raise HTTPException(404, 'Файл лицензионного соглашения не найден') from None
    for file in package.files or ():
        if file.parts[-2:] == ('licenses', 'LICENSE'):
            path = Path(package.locate_file(file))
            if path.is_file():
                return FileResponse(path, media_type='text/plain')
    raise HTTPException(404, 'Файл лицензионного соглашения не найден')


@router.get('/admin/license', response_class=HTMLResponse)
def license_page(request: Request, error: str = '', installed: str = ''):
    store = tokens()
    return templates.TemplateResponse(name='license.html', request=request, context={
        'state': store.license.state(), 'used': len(store.list()), 'limit': store.limit(),
        'released': release_date(), 'version': __version__, 'error': error, 'installed': bool(installed)})


@router.post('/api/admin/license')
async def install_license(request: Request, key: str = Form(''),
                          key_file: UploadFile | None = File(None)):
    # Форма страницы — редирект, скрипт (Accept: application/json) — JSON.
    json_reply = 'application/json' in request.headers.get('accept', '')
    text = key
    if key_file is not None and key_file.filename:
        text = (await key_file.read(64 * 1024)).decode('utf-8', errors='replace')
    try:
        state = tokens().license.install(text)
    except LicenseError as e:
        if json_reply:
            return JSONResponse({'detail': f'Ключ не принят: {e}'}, status_code=400)
        return RedirectResponse(f'/admin/license?error={quote(str(e))}', status_code=303)
    if json_reply:
        return JSONResponse({'status': state.status, 'detail': state.message})
    return RedirectResponse('/admin/license?installed=1', status_code=303)
