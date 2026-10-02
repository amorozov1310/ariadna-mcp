"""Routes: playground MCP-инструментов — тот же код и тот же ответ, что у агента.

Раньше здесь была отдельная реализация: свой _execute_tool поверх
SearchEngine, свой список из 11 инструментов (без list_projects,
list_sources, get_procedure_code, reindex, remove_source), без offset и
потолка limit, со своими умолчаниями и своим форматом ответа. Исправления
формата ответов MCP в playground не попадали.

Теперь:
- список инструментов, параметры, их типы, умолчания, обязательность и
  описания — из схемы MCP-сервера (MCPServer.list_tools()), той же, что
  агент получает при подключении. Отдельного описания нет: новый
  инструмент или параметр MCP появляется в форме сам;
- выполнение — src/mcp_server/tools.py:execute_tool с общим
  ProjectManager; аргументы дополняются умолчаниями схемы, как их
  подставляет SDK, поэтому текст ответа совпадает с ответом MCP;
- project_id в форме нет — подставляется проект страницы.
"""

import logging
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from ..core.project_manager import PROJECT_GONE
from ..mcp_server.tools import execute_tool, is_error_text
from .app import templates, get_pm

logger = logging.getLogger('ariadna')

router = APIRouter()

# Параметр, который playground подставляет сам (проект страницы).
_PAGE_PARAM = 'project_id'

_tool_schemas: list[dict] | None = None


async def mcp_tool_schemas() -> list[dict]:
    """Инструменты MCP-сервера в том виде, в каком их видит агент:
    [{name, description, input_schema}]. Схема от ProjectManager не зависит,
    поэтому строится один раз."""
    global _tool_schemas
    if _tool_schemas is None:
        from ..mcp_server.server import create_mcp_server
        mcp = create_mcp_server(get_pm())
        if mcp is None:          # нет MCP SDK — нечего и показывать
            return []
        _tool_schemas = [{'name': t.name, 'description': t.description or '',
                          'input_schema': t.input_schema}
                         for t in await mcp.list_tools()]
    return _tool_schemas


def _json_type(prop: dict) -> str:
    """Тип параметра из JSON-схемы: «integer», «string»… Для Optional
    (anyOf с null) — тип непустого варианта."""
    if 'type' in prop:
        return prop['type']
    for variant in prop.get('anyOf', []):
        if variant.get('type') != 'null':
            return variant.get('type', 'string')
    return 'string'


def tool_forms(schemas: list[dict], suggestions: dict[str, list[str]] | None = None) -> list[dict]:
    """Описание форм playground из схемы MCP: по полю на параметр, кроме
    project_id. suggestions — подсказки значений из проекта (source_id,
    kind): поле остаётся свободным вводом, как в MCP."""
    suggestions = suggestions or {}
    forms = []
    for tool in schemas:
        schema = tool['input_schema']
        required = set(schema.get('required', []))
        params = []
        for name, prop in schema.get('properties', {}).items():
            if name == _PAGE_PARAM:
                continue
            json_type = _json_type(prop)
            if 'enum' in prop:
                kind = 'select'
            elif json_type == 'boolean':
                kind = 'checkbox'
            elif json_type == 'integer':
                kind = 'number'
            else:
                kind = 'text'
            params.append({
                'name': name,
                'type': kind,
                'required': name in required,
                'default': prop.get('default'),
                'description': prop.get('description', ''),
                'options': prop.get('enum', []),
                'suggestions': suggestions.get(name, []),
            })
        forms.append({'name': tool['name'], 'description': tool['description'], 'params': params})
    return forms


def build_args(schema: dict, params: dict, project_id: str) -> tuple[dict, str | None]:
    """Аргументы для execute_tool из полей формы — как их собирает SDK:
    значения приводятся к типам схемы, незаполненные получают умолчания
    схемы, неизвестные отбрасываются. (args, None) или (None, «Error: …»)."""
    args = {}
    required = set(schema.get('required', []))
    for name, prop in schema.get('properties', {}).items():
        if name == _PAGE_PARAM:
            continue
        value = params.get(name)
        if isinstance(value, str):
            value = value.strip()
        if value is None or value == '':
            if name in required:
                return None, f"Error: не задан обязательный параметр {name}"
            args[name] = prop.get('default')
            continue
        json_type = _json_type(prop)
        if json_type == 'integer':
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None, f"Error: {name}: ожидается целое число, получено {value!r}"
        elif json_type == 'boolean':
            value = value if isinstance(value, bool) else str(value).lower() in ('true', '1', 'on', 'yes')
        if 'enum' in prop and value not in prop['enum']:
            return None, f"Error: {name}: допустимо {', '.join(map(str, prop['enum']))}"
        args[name] = value
    if _PAGE_PARAM in schema.get('properties', {}):
        args[_PAGE_PARAM] = project_id
    return args, None


@router.get("/projects/{project_id}/playground", response_class=HTMLResponse)
async def playground_page(request: Request, project_id: str):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    def project_values():
        sources = [s['id'] for s in pm.get_search(project_id).list_sources()]
        # Этап U0/U2: виды метаданных — из индекса проекта, а не из
        # захардкоженного перечня.
        try:
            kinds = list((pm.get_db(project_id).get_stats().get('kinds') or {}).keys())
        except PROJECT_GONE:
            raise          # проект удалён во время запроса — 404 (app.py)
        except Exception:
            kinds = []
        return {'source_id': sources, 'kind': kinds}

    suggestions = await run_in_threadpool(project_values)
    return templates.TemplateResponse(name="playground.html", request=request, context={
        "project": project,
        "tools_meta": tool_forms(await mcp_tool_schemas(), suggestions),
    })


@router.post("/api/projects/{project_id}/playground")
async def playground_execute(request: Request, project_id: str):
    pm = get_pm()
    try:
        pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    body = await request.json()
    tool_name = body.get('tool', '')
    params = body.get('params') or {}
    schemas = {t['name']: t['input_schema'] for t in await mcp_tool_schemas()}

    t0 = time.time()
    if tool_name not in schemas:
        result_text = f"Error: unknown tool '{tool_name}'"
    else:
        args, result_text = build_args(schemas[tool_name], params, project_id)
        if args is not None:
            try:
                result_text = await run_in_threadpool(execute_tool, pm, tool_name, args)
            except PROJECT_GONE:
                raise          # проект удалён во время запроса — 404 (app.py)
            except Exception as e:
                # Как в MCP (server.py): непредвиденная ошибка — в лог с
                # трассировкой, пользователю — её текст.
                logger.exception("Playground: инструмент %s упал", tool_name)
                result_text = f"Error: {e}"
    elapsed_ms = round((time.time() - t0) * 1000, 1)

    return JSONResponse({
        "result": result_text,
        "is_error": is_error_text(result_text),
        "elapsed_ms": elapsed_ms,
        "tool": tool_name,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    })
