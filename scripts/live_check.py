"""
Проверка работающего сервера Ариадны «вживую» — по сети, как его видят
агент и браузер.

Быстрые тесты гоняют код в одном процессе; часть ошибок видна только
против запущенного сервера: гонки удаления под нагрузкой, статус сразу
после reindex, запись реестра в Docker, открытые файлы на Windows. Скрипт
прогоняет эти проверки против уже запущенного сервера:

    python scripts/live_check.py [--web http://127.0.0.1:19878]
                                 [--mcp http://127.0.0.1:19879/mcp]
                                 [--project ID] [--quick]
                                 [--docker-container ariadna]

Только HTTP-запросы к серверу (MCP — клиентом streamable HTTP из пакета
mcp, Web UI — httpx2, который ставится вместе с mcp); к файлам сервера
скрипт не обращается, кроме проверок через `docker exec`/`docker logs` при
--docker-container. Работает на своём временном проекте zz_live_<суффикс>,
созданном через Web UI из zip с tests/fixtures/xml_ru, и всегда удаляет
его (и проекты стресс-проверки zz_live_s*) в конце, даже при падении.
Существующие проекты не трогает; --project ID — только чтение.

Код выхода: 0 — все проверки PASS, 1 — есть FAIL, 2 — сервер недоступен
или проверку не удалось начать.

Функции-проверки (check_*) принимают LiveContext и возвращают список
результатов — их же вызывают тесты (tests/test_live_check.py).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import re
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx2
from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_XML = ROOT / 'tests' / 'fixtures' / 'xml_ru'
# Отчёт по конфигурации к той же выгрузке: без него метаданные строятся из
# XML только при установленном extra xml-report, а скрипт не должен от него
# зависеть.
FIXTURE_REPORT = ROOT / 'tests' / 'fixtures' / 'report_ru.txt'
EXPECTED_TOOLS = 16
PREFIX = 'zz_live_'
STRESS_PREFIX = 'zz_live_s'

# Аргументы инструментов для сверки playground и MCP на временном проекте
# (фикстура xml_ru). project_id подставляется сам.
PARITY_CASES = {
    'list_projects': {},
    'list_sources': {},
    'get_index_status': {},
    'search_metadata': {'query': 'Виды', 'limit': 3},
    'get_object_details': {'full_name': 'Справочник.ВидыУдобрений', 'detail': 'full'},
    'search_attributes': {'query': 'Основное'},
    'find_references': {'object_name': 'ВидыУдобрений'},
    'list_objects': {'limit': 20, 'offset': 5},
    'search_procedures': {'query': 'Рассчитать'},
    'get_procedure_code': {'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'},
    'get_module_outline': {'module_name': 'ЦеныСервер'},
    'get_call_tree': {'procedure_name': 'РассчитатьЦену', 'direction': 'up', 'depth': 2},
    'search_code': {'query': 'Возврат'},
    'diagnose_index': {},
    'reindex': {},
    'remove_source': {'source_id': 'main'},          # без confirm — только предпросмотр
}

# Что клиент вправе увидеть, читая проект, который в этот момент удаляют.
GONE_MARKERS = ('not found', 'удалён')


# ============================================
# Результаты
# ============================================

@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ''
    info: bool = False          # только сведения (время и т.п.), не влияет на итог

    def line(self) -> str:
        tag = 'INFO' if self.info else ('PASS' if self.ok else 'FAIL')
        return f"[{tag}] {self.name}" + (f" — {self.detail}" if self.detail else '')


def passed(name: str, detail: str = '') -> Result:
    return Result(name, True, detail)


def failed(name: str, detail: str) -> Result:
    return Result(name, False, detail)


def check(name: str, condition: bool, detail: str = '', fail_detail: str = '') -> Result:
    return Result(name, bool(condition), detail if condition else (fail_detail or detail))


# ============================================
# Контекст: адреса, клиенты
# ============================================

@dataclass
class LiveContext:
    web: str = 'http://127.0.0.1:19878'
    mcp: str = 'http://127.0.0.1:19879/mcp'
    quick: bool = False
    docker_container: str | None = None
    project: str | None = None
    temp_id: str = field(default_factory=lambda: PREFIX + secrets.token_hex(3))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    log: callable = print

    # ── Web UI ──

    def http(self) -> httpx2.Client:
        return httpx2.Client(base_url=self.web.rstrip('/'), timeout=60, follow_redirects=False)

    def web_get(self, path: str, **kw) -> httpx2.Response:
        with self.http() as c:
            return c.get(path, **kw)

    def create_project(self, project_id: str, with_source: bool = True) -> None:
        with self.http() as c:
            r = c.post('/api/projects', data={'project_id': project_id, 'name': project_id})
            if r.status_code != 303:
                raise RuntimeError(f"создание проекта {project_id}: HTTP {r.status_code} {r.text[:200]}")
            if with_source:
                r = c.post(f'/api/projects/{project_id}/sources',
                           data={'source_id': 'main', 'label': 'Main'},
                           files={'xml_archive': ('xml_ru.zip', fixture_zip(), 'application/zip'),
                                  'report_file': ('report.txt', FIXTURE_REPORT.read_bytes(), 'text/plain')})
                if r.status_code != 303:
                    raise RuntimeError(f"источник проекта {project_id}: HTTP {r.status_code} {r.text[:200]}")

    def delete_project(self, project_id: str) -> int:
        with self.http() as c:
            return c.delete(f'/api/projects/{project_id}').status_code

    def project_status(self, project_id: str) -> str | None:
        r = self.web_get(f'/api/projects/{project_id}/status')
        return r.json().get('status') if r.status_code == 200 else None

    def reindex_web(self, project_id: str) -> dict:
        with self.http() as c:
            r = c.post(f'/api/projects/{project_id}/reindex', headers={'Accept': 'application/json'})
            r.raise_for_status()
            return r.json()

    def wait_ready(self, project_id: str, timeout: float = 120) -> str | None:
        deadline = time.time() + timeout
        status = None
        while time.time() < deadline:
            status = self.project_status(project_id)
            if status in ('ready', 'error', None):
                return status
            time.sleep(0.2)
        return status

    def playground(self, project_id: str, tool: str, params: dict) -> dict:
        with self.http() as c:
            r = c.post(f'/api/projects/{project_id}/playground',
                       json={'tool': tool, 'params': params},
                       headers={'Accept': 'application/json'})
            r.raise_for_status()
            return r.json()

    def project_ids(self) -> list[str]:
        """id проектов по ответу MCP list_projects."""
        text, _ = self.call('list_projects', {})
        return re.findall(r'^\s+(\S+) — ', text, flags=re.M)

    # ── MCP ──

    def call(self, tool: str, args: dict) -> tuple[str, bool]:
        """Один вызов инструмента в своём сеансе MCP: (текст, isError)."""
        async def go():
            async with Client(self.mcp) as client:
                return await call_tool(client, tool, args)
        return asyncio.run(go())


async def call_tool(client: Client, tool: str, args: dict) -> tuple[str, bool]:
    res = await client.call_tool(tool, args)
    text = '\n'.join(c.text for c in res.content if getattr(c, 'text', None))
    return text, bool(res.is_error)


def fixture_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in FIXTURE_XML.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURE_XML).as_posix())
    return buf.getvalue()


def expected_version() -> str:
    return (ROOT / 'src' / 'VERSION').read_text(encoding='utf-8').strip()


def mcp_text_of_error(text: str, tool: str) -> str:
    """Текст ошибки MCP без префикса SDK «Error executing tool X: » — для
    сравнения с ответом playground «Error: …»."""
    return text.removeprefix(f'Error executing tool {tool}: ')


# ============================================
# 1. Регрессия
# ============================================

def check_regression(ctx: LiveContext) -> list[Result]:
    out = []

    async def server_side():
        async with Client(ctx.mcp) as client:
            tools = await client.list_tools()
            missing = await call_tool(client, 'get_index_status', {'project_id': PREFIX + 'нет_такого'})
            return client.server_info, tools.tools, missing
    info, tools, (missing_text, missing_err) = asyncio.run(server_side())

    out.append(check('serverInfo.version == src/VERSION', info.version == expected_version(),
                     info.version, f"сервер {info.version}, src/VERSION {expected_version()}"))
    names = {t.name for t in tools}
    out.append(check(f'{EXPECTED_TOOLS} инструментов', len(names) == EXPECTED_TOOLS,
                     ', '.join(sorted(names)), f"{len(names)}: {', '.join(sorted(names))}"))
    schemas = {t.name: t.input_schema.get('properties', {}) for t in tools}
    module_ok = all('module_name' in schemas.get(t, {}) and 'module_path' not in schemas.get(t, {})
                    for t in ('get_procedure_code', 'get_module_outline', 'get_call_tree'))
    out.append(check('параметр модуля — module_name', module_ok,
                     fail_detail=str({t: sorted(schemas.get(t, {})) for t in
                                      ('get_procedure_code', 'get_module_outline', 'get_call_tree')})))
    out.append(check('несуществующий project_id → isError «not found»',
                     missing_err and 'not found' in missing_text, missing_text[:120]))

    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                       'clientInfo': {'name': 'live_check', 'version': '1'}}}
    mcp_headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    port = httpx2.URL(ctx.mcp).port
    web_port = httpx2.URL(ctx.web).port
    with httpx2.Client(timeout=30) as c:
        def mcp_post(**headers):
            return c.post(ctx.mcp, json=init, headers={**mcp_headers, **headers}).status_code

        def web_health(host):
            return c.get(ctx.web.rstrip('/') + '/health', headers={'Host': host}).status_code

        cases = [
            ('Host evil.example на /mcp → 421', mcp_post(Host=f'evil.example:{port}'), 421),
            ('Origin evil.example на /mcp → 403',
             mcp_post(Host=f'127.0.0.1:{port}', Origin='http://evil.example'), 403),
            ('Host 127.0.0.1 на /mcp проходит', mcp_post(Host=f'127.0.0.1:{port}'), 200),
            ('Host localhost на /mcp проходит', mcp_post(Host=f'localhost:{port}'), 200),
            ('Host evil.example на Web UI → 400', web_health(f'evil.example:{web_port}'), 400),
            ('Host 127.0.0.1 на Web UI проходит', web_health(f'127.0.0.1:{web_port}'), 200),
            ('Host localhost на Web UI проходит', web_health(f'localhost:{web_port}'), 200),
        ]
    for name, got, want in cases:
        out.append(check(name, got == want, f"HTTP {got}", f"HTTP {got}, ожидалось {want}"))
    return out


# ============================================
# 2. Индексация временного проекта
# ============================================

def check_indexing(ctx: LiveContext) -> list[Result]:
    """reindex → сразу после ответа статус indexing (он записывается до
    ответа «started»: раньше get_index_status ещё отдавал прежний статус) →
    затем ready и процедуры в индексе."""
    pid = ctx.temp_id

    async def go():
        async with Client(ctx.mcp) as client:
            started = await call_tool(client, 'reindex', {'project_id': pid})
            status = await call_tool(client, 'get_index_status', {'project_id': pid})
            return started, status
    (text, is_error), (status_now, _) = asyncio.run(go())
    out = [check('reindex отвечает «started»', not is_error and 'started' in text, text[:120])]
    first = status_now.splitlines()[0] if status_now else ''
    out.append(check('сразу после ответа статус indexing', first == 'Status: indexing', first))
    final = ctx.wait_ready(pid)
    stats, _ = ctx.call('get_index_status', {'project_id': pid})
    procs = int(m.group(1)) if (m := re.search(r'^Procedures: (\d+)', stats, re.M)) else 0
    out.append(check('затем ready, процедур > 0', final == 'ready' and procs > 0,
                     f"status={final}, procedures={procs}"))
    return out


# ============================================
# 3. playground == MCP
# ============================================

def check_playground_parity(ctx: LiveContext) -> list[Result]:
    pid = ctx.temp_id
    out = []

    async def mcp_answers(cases):
        answers = {}
        async with Client(ctx.mcp) as client:
            for tool, args in cases:
                answers[tool] = await call_tool(client, tool, {'project_id': pid, **args}
                                                if tool != 'list_projects' else args)
        return answers

    for tool, args in PARITY_CASES.items():
        play = ctx.playground(pid, tool, args)
        if tool == 'reindex':
            ctx.wait_ready(pid)
        text, is_error = asyncio.run(mcp_answers([(tool, args)]))[tool]
        if tool == 'reindex':
            ctx.wait_ready(pid)
        mcp_text = ('Error: ' + mcp_text_of_error(text, tool)) if is_error else text
        same = play['result'] == mcp_text and bool(play['is_error']) == is_error
        # Ответ содержательный: сверка двух пустых ответов ничего не доказывает.
        empty = any(m in text for m in ('No results', 'not found', 'No procedures'))
        out.append(check(f'playground == MCP: {tool}', same and not is_error and not empty,
                         f"{len(text)} символов",
                         f"playground: {play['result'][:150]!r}\n        MCP: {mcp_text[:150]!r}"))

    play = ctx.playground(pid, 'list_objects', {'limit': 1000})
    text, _ = ctx.call('list_objects', {'project_id': pid, 'limit': 1000})
    capped = 'урезан до 500' in play['result'] and 'урезан до 500' in text
    out.append(check('limit=1000 → оговорка об урезании в playground и MCP',
                     capped and play['result'] == text,
                     fail_detail=f"playground: …{play['result'][-120:]!r}, MCP: …{text[-120:]!r}"))
    return out


# ============================================
# 4. Пересоздание
# ============================================

def check_recreate(ctx: LiveContext) -> list[Result]:
    pid = ctx.temp_id
    code = ctx.delete_project(pid)
    out = [check('удаление временного проекта → 200', code == 200, f"HTTP {code}")]
    ctx.create_project(pid, with_source=False)
    stats, err = ctx.call('get_index_status', {'project_id': pid})
    out.append(check('пересозданный без источников: 0 процедур',
                     not err and re.search(r'^Procedures: 0$', stats, re.M) is not None,
                     fail_detail=stats[:200]))
    found, err = ctx.call('search_procedures', {'project_id': pid, 'query': 'Рассчитать'})
    out.append(check('пересозданный без источников: поиск пуст',
                     not err and 'No procedures found' in found, fail_detail=found[:200]))
    # Вернуть данные для следующих проверок.
    ctx.delete_project(pid)
    ctx.create_project(pid)
    ctx.reindex_web(pid)
    ctx.wait_ready(pid)
    return out


# ============================================
# 5. Удаление под нагрузкой
# ============================================

def _docker_path_exists(ctx: LiveContext, path: str) -> bool:
    r = subprocess.run(['docker', 'exec', ctx.docker_container, 'test', '-e', path],
                       capture_output=True, timeout=30)
    return r.returncode == 0


def _mcp_reader(ctx: LiveContext, pid: str, stop: threading.Event, warmed: threading.Event,
                bad: list, calls: list) -> None:
    async def loop():
        async with Client(ctx.mcp) as client:
            while not stop.is_set():
                for tool, args in (('search_procedures', {'query': 'Рассчитать'}),
                                   ('get_index_status', {})):
                    text, is_error = await call_tool(client, tool, {'project_id': pid, **args})
                    calls.append(1)
                    if is_error and not any(m in text for m in GONE_MARKERS):
                        bad.append(f"MCP {tool}: {text[:150]}")
                    if not is_error:
                        warmed.set()
    try:
        asyncio.run(loop())
    except Exception as e:          # обрыв сеанса — тоже находка
        bad.append(f"MCP-сеанс упал: {e!r}")
        warmed.set()


def _web_reader(ctx: LiveContext, pid: str, stop: threading.Event, bad: list, calls: list) -> None:
    with ctx.http() as c:
        while not stop.is_set():
            for path in (f'/projects/{pid}', f'/api/projects/{pid}/status'):
                code = c.get(path).status_code
                calls.append(1)
                if code not in (200, 404):
                    bad.append(f"Web UI GET {path}: HTTP {code}")


def check_delete_under_load(ctx: LiveContext) -> list[Result]:
    runs = 1 if ctx.quick else 5
    out = []
    for run in range(1, runs + 1):
        pid = f'{ctx.temp_id}_d{run}'
        ctx.create_project(pid)
        ctx.reindex_web(pid)
        ctx.wait_ready(pid)
        stop, warmed = threading.Event(), threading.Event()
        bad, calls = [], []
        threads = [threading.Thread(target=_mcp_reader, args=(ctx, pid, stop, warmed, bad, calls))
                   for _ in range(4)]
        threads.append(threading.Thread(target=_web_reader, args=(ctx, pid, stop, bad, calls)))
        for t in threads:
            t.start()
        try:
            warmed.wait(30)
            time.sleep(0.3)
            code = ctx.delete_project(pid)
            time.sleep(0.5)                 # читатели застают удалённый проект
        finally:
            stop.set()
            for t in threads:
                t.join(60)
        out.append(check(f'удаление под нагрузкой, прогон {run}: DELETE → 200', code == 200,
                         f"HTTP {code}, {len(calls)} запросов"))
        out.append(check(f'удаление под нагрузкой, прогон {run}: только «not found»/«удалён» и 200/404',
                         not bad, fail_detail='; '.join(bad[:5])))
        if ctx.docker_container:
            out.append(check(f'удаление под нагрузкой, прогон {run}: нет /index/{pid}',
                             not _docker_path_exists(ctx, f'/index/{pid}')))
    return out


# ============================================
# 6. Стресс: создание/удаление проектов рядом с чтением реестра
# ============================================

def check_stress(ctx: LiveContext) -> list[Result]:
    seconds = 5 if ctx.quick else 30
    pid = ctx.temp_id
    stop = threading.Event()
    bad, reads, cycles = [], [], [0]

    def churn():
        i = 0
        while not stop.is_set():
            sid = f'{STRESS_PREFIX}{ctx.temp_id[len(PREFIX):]}_{i}'
            try:
                ctx.create_project(sid, with_source=False)
                code = ctx.delete_project(sid)
                if code != 200:
                    bad.append(f"Web UI: удаление {sid} → HTTP {code}")
            except Exception as e:
                bad.append(f"Web UI: {e}")
            cycles[0] += 1
            i += 1

    def reader():
        async def loop():
            async with Client(ctx.mcp) as client:
                while not stop.is_set():
                    text, err = await call_tool(client, 'list_projects', {})
                    reads.append(1)
                    if err or pid not in text:
                        bad.append(f"list_projects без {pid}: {text[:150]}")
                    text, err = await call_tool(client, 'get_index_status', {'project_id': pid})
                    reads.append(1)
                    if err:
                        bad.append(f"get_index_status {pid}: {text[:150]}")
        try:
            asyncio.run(loop())
        except Exception as e:
            bad.append(f"MCP-сеанс упал: {e!r}")

    threads = [threading.Thread(target=churn)] + [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(60)
    return [check(f'стресс {seconds} с: реестр не пустеет, проект не теряется', not bad,
                  f"{cycles[0]} циклов создания/удаления, {len(reads)} чтений",
                  f"{len(bad)} сбоев: " + '; '.join(bad[:5]))]


# ============================================
# 7. Лог контейнера
# ============================================

# Строка уровня ERROR: формат логгера сервера «время имя ERROR сообщение» и
# uvicorn «ERROR:    …».
_ERROR_LINE = re.compile(r'(^|\s)ERROR(:|\s)')


def check_docker_log(ctx: LiveContext) -> list[Result]:
    since = ctx.started_at.strftime('%Y-%m-%dT%H:%M:%SZ')
    r = subprocess.run(['docker', 'logs', '--since', since, ctx.docker_container],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    log = r.stdout + r.stderr
    bad = [line for line in log.splitlines() if 'Traceback' in line or _ERROR_LINE.search(line)]
    return [check('лог контейнера: ни одной Traceback и строки ERROR', r.returncode == 0 and not bad,
                  f"{len(log.splitlines())} строк лога",
                  f"docker logs → {r.returncode}; " + ' | '.join(bad[:5]))]


# ============================================
# 8. Существующий проект владельца — только чтение
# ============================================

def check_owner_project(ctx: LiveContext) -> list[Result]:
    pid = ctx.project
    out = []

    async def go():
        async with Client(ctx.mcp) as client:
            t0 = time.perf_counter()
            procs = await call_tool(client, 'search_procedures', {'project_id': pid, 'query': 'Получить',
                                                                  'limit': 5})
            elapsed = (time.perf_counter() - t0) * 1000
            meta = await call_tool(client, 'search_metadata', {'project_id': pid, 'query': 'Номенклатура',
                                                               'limit': 3})
            full_name = m.group(1) if (m := re.search(r'^\d+\. (\S+)', meta[0], re.M)) else 'Справочник.Номенклатура'
            details = await call_tool(client, 'get_object_details', {'project_id': pid, 'full_name': full_name})
            proc = m.group(1) if (m := re.search(r'\.(\w+)\(\)', procs[0])) else 'Получить'
            tree = await call_tool(client, 'get_call_tree', {'project_id': pid, 'procedure_name': proc,
                                                             'depth': 1})
            return procs, elapsed, meta, details, tree
    procs, elapsed, meta, details, tree = asyncio.run(go())
    for name, (text, err) in (('search_procedures', procs), ('search_metadata', meta),
                              ('get_object_details', details), ('get_call_tree', tree)):
        out.append(check(f'проект {pid}: {name} без ошибки', not err, text.splitlines()[0][:100] if text else '',
                         text[:200]))
    out.append(Result(f'проект {pid}: время search_procedures', True, f"{elapsed:.0f} мс", info=True))
    return out


# ============================================
# Прогон
# ============================================

def cleanup(ctx: LiveContext) -> list[str]:
    """Удаляет временные проекты этого прогона (zz_live_<суффикс>*,
    zz_live_s<суффикс>_*). Возвращает id, которые удалить не удалось."""
    suffix = ctx.temp_id[len(PREFIX):]
    left = []
    for _ in range(3):
        try:
            ids = [p for p in ctx.project_ids()
                   if p.startswith(ctx.temp_id) or p.startswith(f'{STRESS_PREFIX}{suffix}_')]
        except Exception:
            ids = [ctx.temp_id]
        left = []
        for pid in ids:
            ctx.wait_ready(pid, timeout=60)
            if ctx.delete_project(pid) not in (200, 404):
                left.append(pid)
        if not left:
            break
        time.sleep(1)
    return left


def run(ctx: LiveContext) -> list[Result]:
    results: list[Result] = []

    def section(title: str, fn) -> None:
        ctx.log(f"\n== {title}")
        try:
            got = fn(ctx)
        except Exception as e:
            got = [failed(title, f"проверка упала: {e!r}")]
        for r in got:
            ctx.log('  ' + r.line())
        results.extend(got)

    if ctx.temp_id in ctx.project_ids():
        raise SystemExit(f"Проект {ctx.temp_id} уже есть — live_check его не тронет. Повторите запуск.")
    ctx.log(f"Временный проект: {ctx.temp_id}")
    try:
        section('1. Регрессия', check_regression)
        ctx.create_project(ctx.temp_id)
        section('2. Индексация временного проекта', check_indexing)
        section('3. Playground == MCP', check_playground_parity)
        if ctx.project:
            section(f'8. Проект {ctx.project} (только чтение)', check_owner_project)
        section('4. Пересоздание проекта', check_recreate)
        section('5. Удаление под нагрузкой', check_delete_under_load)
        section('6. Стресс реестра', check_stress)
    finally:
        left = cleanup(ctx)
        results.append(check('временные проекты удалены', not left,
                             fail_detail=f"остались: {', '.join(left)}"))
        ctx.log('  ' + results[-1].line())
    if ctx.docker_container:
        section('7. Лог контейнера', check_docker_log)
    return results


def summary(results: list[Result]) -> str:
    fails = [r for r in results if not r.ok and not r.info]
    checks = [r for r in results if not r.info]
    lines = [f"\nИтого: {len(checks) - len(fails)} PASS, {len(fails)} FAIL"]
    lines += ['  ' + r.line() for r in fails]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='live_check',
                                     description='Проверка работающего сервера Ариадны по сети.')
    parser.add_argument('--web', default='http://127.0.0.1:19878', help='адрес Web UI')
    parser.add_argument('--mcp', default='http://127.0.0.1:19879/mcp', help='адрес MCP (streamable HTTP)')
    parser.add_argument('--project', help='существующий проект: проверки только на чтение')
    parser.add_argument('--quick', action='store_true', help='короткий режим: 1 прогон удаления, стресс 5 с')
    parser.add_argument('--docker-container', help='имя контейнера: проверки через docker exec/logs')
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Консоль Windows в кодировке cp1252 не уронит вывод на кириллице.
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    ctx = LiveContext(web=args.web, mcp=args.mcp, quick=args.quick,
                      docker_container=args.docker_container, project=args.project)
    try:
        ctx.web_get('/health').raise_for_status()
    except Exception as e:
        print(f"Web UI {ctx.web} недоступен: {e}", file=sys.stderr)
        return 2
    results = run(ctx)
    print(summary(results))
    return 1 if any(not r.ok and not r.info for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
