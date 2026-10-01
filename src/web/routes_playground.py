"""Routes: MCP tools playground — returns both text and structured data for rich UI.

Source-aware: every tool that looks at metadata/code accepts an optional
``source_id`` parameter. Results include ``source_id`` + ``source_label``
so the user sees which data source each match came from.
"""

import time
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from ..core.project_manager import PROJECT_GONE
from .app import templates, get_pm

router = APIRouter()


@router.get("/projects/{project_id}/playground", response_class=HTMLResponse)
def playground_page(request: Request, project_id: str):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    engine = pm.get_search(project_id)
    sources = engine.list_sources()

    # Этап U0/U2: виды метаданных для выпадающих списков берём из индекса
    # проекта, а не из захардкоженного перечня (в шаблонах их быть не должно).
    try:
        kinds = list((pm.get_db(project_id).get_stats().get('kinds') or {}).keys())
    except PROJECT_GONE:
        raise          # проект удалён во время запроса — 404 (app.py)
    except Exception:
        kinds = []

    return templates.TemplateResponse(name="playground.html", request=request, context={
        "project": project,
        "tools_meta": get_tools_meta(sources, kinds),
        "sources": sources,
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
    params = body.get('params', {})
    params['_project_id'] = project_id

    engine = pm.get_search(project_id)

    t0 = time.time()
    try:
        result_text, result_data = _execute_tool(engine, tool_name, params)
    except PROJECT_GONE:
        raise          # проект удалён во время запроса — 404 (app.py)
    except Exception as e:
        result_text = f"Error: {e}"
        result_data = None
    elapsed_ms = round((time.time() - t0) * 1000, 1)

    return JSONResponse({
        "result": result_text,
        "result_data": result_data,
        "elapsed_ms": elapsed_ms,
        "tool": tool_name,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    })


def _execute_tool(engine, tool: str, params: dict) -> tuple[str, dict | None]:
    """Execute tool. Returns (text_result, structured_data_or_None)."""
    from ..core.search import format_search_results, format_object_details

    limit = int(params.get('limit', 20))
    source_id = (params.get('source_id') or '').strip() or None

    match tool:
        case 'search_metadata':
            results = engine.search_metadata(
                params.get('query', ''),
                kind=params.get('kind') or None,
                source_id=source_id,
                limit=limit,
            )
            return format_search_results(results), _list_data(results, [
                'full_name', 'kind', 'synonym', 'source_label', 'comment',
            ])

        case 'get_object_details':
            details = engine.get_object_details(
                params.get('full_name', ''),
                source_id=source_id,
            )
            if not details:
                return "Object not found.", None
            return format_object_details(details), _details_data(details)

        case 'search_attributes':
            results = engine.search_attributes(
                params.get('query', ''),
                type_filter=params.get('type_filter') or None,
                source_id=source_id,
                limit=limit,
            )
            return format_search_results(results), _list_data(results, [
                'object_full_name', 'name', 'kind', 'type_desc', 'synonym', 'source_label',
            ])

        case 'find_references':
            results = engine.find_references(
                params.get('object_name', ''),
                source_id=source_id,
                limit=limit,
            )
            return format_search_results(results), _list_data(results, [
                'object_full_name', 'attr_name', 'kind', 'type_desc', 'source_label',
            ])

        case 'list_objects':
            results = engine.list_objects(
                kind=params.get('kind') or None,
                source_id=source_id,
                limit=limit,
            )
            return format_search_results(results), _list_data(results, [
                'full_name', 'kind', 'synonym', 'source_label',
            ])

        case 'get_index_status':
            stats = engine.get_index_status()
            sources = engine.list_sources()
            text_lines = [f"Status: {stats.get('status', 'unknown')}"]
            if stats.get('updated_at'):
                text_lines.append(f"Updated: {stats['updated_at'][:19]}")
            text_lines.append(f"Sources: {len(sources)}")
            for s in sources:
                text_lines.append(f"  {s['label']} ({s['source_type']})")
            for k, v in stats.get('kinds', {}).items():
                text_lines.append(f"  {k}: {v}")
            text_lines.append(f"Total objects: {stats.get('metadata_objects', 0)}")
            text_lines.append(f"Total attributes: {stats.get('attributes', 0)}")
            text = '\n'.join(text_lines)

            sections = [
                {'title': 'Статус', 'text': f"Status: {stats.get('status')}\nUpdated: {stats.get('updated_at', '')[:19]}"},
                {'title': f"Источники данных ({len(sources)})", 'items': [
                    {'ID': s['id'], 'Label': s['label'], 'Type': s['source_type'],
                     'Config': f"{s.get('config_name','')} {s.get('config_version','')}".strip()}
                    for s in sources
                ]},
                {'title': 'По типам объектов', 'items': [
                    {'Тип': k, 'Количество': v} for k, v in stats.get('kinds', {}).items()
                ]},
                {'title': 'Итого', 'items': [
                    {'Показатель': 'Объектов', 'Значение': stats.get('metadata_objects', 0)},
                    {'Показатель': 'Реквизитов', 'Значение': stats.get('attributes', 0)},
                    {'Показатель': 'Форм', 'Значение': stats.get('forms', 0)},
                    {'Показатель': 'Модулей', 'Значение': stats.get('modules', 0)},
                    {'Показатель': 'Процедур', 'Значение': stats.get('procedures', 0)},
                ]},
            ]
            return text, {'sections': sections}

        # ── Phase 2: BSL code analysis ──

        case 'search_procedures':
            results = engine.search_procedures(
                params.get('query', ''),
                module_filter=params.get('module_filter') or None,
                export_only=bool(params.get('export_only')),
                source_id=source_id,
                limit=limit,
            )
            if not results:
                return "No procedures found.", None
            text_lines = [f"Found {len(results)} procedure(s):"]
            for r in results:
                exp = ' Экспорт' if r.get('is_export') else ''
                src = f"  @{r.get('source_label','')}" if r.get('source_label') else ''
                text_lines.append(f"  {r['module_name']}.{r['name']}(){exp} {r.get('directive', '')}{src}")
            return '\n'.join(text_lines), _list_data(results, [
                'module_name', 'name', 'kind', 'directive', 'is_export',
                'start_line', 'end_line', 'source_label',
            ])

        case 'get_call_tree':
            from ..core.call_analyzer import CallAnalyzer
            pm = get_pm()
            ca = CallAnalyzer(pm.get_db(params.get('_project_id', '')))
            tree = ca.build_call_tree(
                params.get('procedure_name', ''),
                module_name=params.get('module_name') or None,
                direction=params.get('direction', 'down'),
                depth=int(params.get('depth', 3)),
                source_id=source_id,
            )
            text = ca.format_tree_simple(tree)
            return text, None

        case 'get_module_outline':
            outline = engine.get_module_outline(
                params.get('module_path', ''),
                source_id=source_id,
            )
            if not outline:
                return "Module not found.", None
            mod = outline['module']
            src_suffix = f"  @{outline.get('source_label','')}" if outline.get('source_label') else ''
            text_lines = [
                f"Module: {mod['name']}{src_suffix}",
                f"Type: {mod['module_type']}",
                f"Lines: {mod['line_count']}",
                f"\nProcedures ({len(outline['procedures'])}):",
            ]
            for p in outline['procedures']:
                exp = ' Экспорт' if p.get('is_export') else ''
                d = f' {p["directive"]}' if p.get('directive') else ''
                text_lines.append(f"  {p['kind']} {p['name']}(){exp}{d} [{p['start_line']}-{p['end_line']}]")
            text = '\n'.join(text_lines)

            title = f"{mod['name']} ({mod['module_type']}, {mod['line_count']} строк)"
            if outline.get('source_label'):
                title += f" @ {outline['source_label']}"
            sections = [
                {'title': title, 'items': []},
                {'title': f"Процедуры ({len(outline['procedures'])})", 'items': [
                    {'Имя': p['name'], 'Вид': p['kind'],
                     'Директива': p.get('directive', ''),
                     'Экспорт': '✓' if p.get('is_export') else '',
                     'Строки': f"{p['start_line']}-{p['end_line']}"}
                    for p in outline['procedures']
                ]},
            ]
            return text, {'sections': sections}

        case 'search_code':
            results = engine.search_code(
                params.get('query', ''),
                file_pattern=params.get('file_pattern') or None,
                source_id=source_id,
                limit=limit,
            )
            if not results:
                return "No code matches found.", None
            text_lines = [f"Found {len(results)} match(es):"]
            for r in results:
                src = f"  @{r.get('source_label','')}" if r.get('source_label') else ''
                text_lines.append(f"  {r['module_name']}:{r['line']} {r['context'][:100]}{src}")
            return '\n'.join(text_lines), _list_data(results, [
                'module_name', 'line', 'callee_name', 'context', 'source_label',
            ])

        case 'diagnose_index':
            from ..core.search import diagnose_index
            pm = get_pm()
            result = diagnose_index(pm, params.get('_project_id', ''))
            text_lines = [
                f"BSL файлов просканировано: {result['total_in_files']} процедур",
                f"В индексе: {result['total_indexed']} процедур",
                f"Пропущено: {result['missing_count']}",
            ]
            if result['missing']:
                text_lines.append("")
                for m in result['missing']:
                    text_lines.append(f"  ❌ {m['module']}:{m['line']}  {m['signature'][:120]}")
            if result['extra']:
                text_lines.append(f"\nЛишние в индексе: {result['extra_count']}")
                for e in result['extra'][:10]:
                    text_lines.append(f"  ⚠ {e['module']}.{e['name']}")
            if not result['missing'] and not result['extra']:
                text_lines.append("\n✅ Индекс полный — все процедуры учтены.")

            data = None
            if result['missing']:
                data = _list_data(result['missing'], ['module', 'name', 'line', 'signature'])
            return '\n'.join(text_lines), data

        case _:
            return f"Unknown tool: {tool}", None


def _list_data(results: list[dict], columns: list[str]) -> dict:
    items = []
    for r in results:
        item = {}
        for col in columns:
            val = r.get(col, '')
            if val:
                item[col] = val
        if item:
            items.append(item)
    return {'count': len(results), 'items': items}


def _details_data(details: dict) -> dict:
    obj = details['object']
    sections = []

    title = f"{obj['full_name']} ({obj.get('synonym', '')})"
    if details.get('source_label'):
        title += f"  @{details['source_label']}"
    header = [
        {'Свойство': 'Тип', 'Значение': obj.get('kind', '')},
        {'Свойство': 'Синоним', 'Значение': obj.get('synonym', '')},
        {'Свойство': 'Комментарий', 'Значение': obj.get('comment', '')},
        {'Свойство': 'Источник', 'Значение': details.get('source_label') or details.get('source_id') or ''},
    ]
    sections.append({'title': title, 'items': header})

    if details.get('matches') and len(details['matches']) > 1:
        other = [m['source_label'] for m in details['matches'] if m['source_id'] != details.get('source_id')]
        if other:
            sections.append({
                'title': 'Этот объект также есть в источниках',
                'text': ', '.join(other),
            })

    if details.get('module'):
        m = details['module']
        flags = []
        if m.get('is_server'): flags.append('Сервер')
        if m.get('is_client'): flags.append('Клиент')
        if m.get('is_global'): flags.append('Глобальный')
        if m.get('is_privileged'): flags.append('Привилегированный')
        if m.get('server_call'): flags.append('ВызовСервера')
        sections.append({'title': 'Флаги модуля', 'text': ', '.join(flags) if flags else 'нет'})

    attrs = details.get('attributes', [])
    if attrs:
        sections.append({
            'title': f"Реквизиты ({len(attrs)})",
            'items': [{'Имя': a['name'], 'Вид': a['kind'], 'Тип': a.get('type_desc', '')} for a in attrs],
        })

    ts = details.get('tabular_sections', {})
    for ts_name, ts_attrs in ts.items():
        if ts_attrs:
            sections.append({
                'title': f"ТЧ: {ts_name} ({len(ts_attrs)})",
                'items': [{'Имя': a['name'].replace(f'ТЧ.{ts_name}.', ''), 'Тип': a.get('type_desc', '')} for a in ts_attrs],
            })

    forms = details.get('forms', [])
    if forms:
        sections.append({'title': f"Формы ({len(forms)})", 'items': [{'Имя': f['name']} for f in forms]})

    evs = details.get('enum_values', [])
    if evs:
        sections.append({
            'title': f"Значения перечисления ({len(evs)})",
            'items': [{'Имя': e['name'], 'Синоним': e.get('synonym', '')} for e in evs],
        })

    return {'sections': sections}


def get_tools_meta(sources: list[dict] | None = None,
                    kinds: list[str] | None = None) -> list[dict]:
    """Returns metadata for each tool. Includes source picker if project has
    >1 source, and fills every "вид метаданных" dropdown from the project's
    actual index (Этап U0/U2) rather than a hardcoded nine-item list."""
    sources = sources or []
    kind_options = [''] + list(kinds or [])
    # Source dropdown only makes sense when there's more than one source
    source_param = None
    if len(sources) > 1:
        source_param = {
            'name': 'source_id', 'type': 'select', 'required': False,
            'hint': 'Источник данных (пусто = все)',
            'options': [''] + [s['id'] for s in sources],
            'option_labels': {s['id']: (s['label'] + (' (ext)' if s['source_type'] == 'extension' else ''))
                              for s in sources},
        }

    def _with_source(params: list) -> list:
        if source_param:
            return params + [source_param]
        return params

    return [
        {
            'name': 'search_metadata',
            'description': 'Полнотекстовый поиск по объектам конфигурации',
            'params': _with_source([
                {'name': 'query', 'type': 'text', 'required': True, 'hint': 'Номенклатура, Контрагент...'},
                {'name': 'kind', 'type': 'select', 'required': False, 'hint': 'Фильтр по типу',
                 'options': kind_options},
                {'name': 'limit', 'type': 'number', 'required': False, 'hint': 'Макс. результатов', 'default': 20},
            ]),
        },
        {
            'name': 'get_object_details',
            'description': 'Полная карточка объекта: реквизиты с типами, табличные части, формы',
            'params': _with_source([
                {'name': 'full_name', 'type': 'text', 'required': True, 'hint': 'Справочник.Номенклатура'},
            ]),
        },
        {
            'name': 'search_attributes',
            'description': 'Поиск реквизитов по имени или типу данных',
            'params': _with_source([
                {'name': 'query', 'type': 'text', 'required': True, 'hint': 'Артикул, Сумма...'},
                {'name': 'type_filter', 'type': 'text', 'required': False, 'hint': 'СправочникСсылка.Контрагенты'},
                {'name': 'limit', 'type': 'number', 'required': False, 'hint': 'Макс. результатов', 'default': 30},
            ]),
        },
        {
            'name': 'find_references',
            'description': 'Где объект используется в типах реквизитов других объектов',
            'params': _with_source([
                {'name': 'object_name', 'type': 'text', 'required': True, 'hint': 'Номенклатура, Контрагенты...'},
                {'name': 'limit', 'type': 'number', 'required': False, 'hint': 'Макс. результатов', 'default': 50},
            ]),
        },
        {
            'name': 'list_objects',
            'description': 'Список всех объектов конфигурации с фильтром по типу',
            'params': _with_source([
                {'name': 'kind', 'type': 'select', 'required': False, 'hint': 'Фильтр по типу',
                 'options': kind_options},
                {'name': 'limit', 'type': 'number', 'required': False, 'hint': 'Макс. результатов', 'default': 50},
            ]),
        },
        {
            'name': 'get_index_status',
            'description': 'Статистика индекса: количество объектов по типам, статус, дата, источники',
            'params': [],
        },
        # ── Phase 2: BSL code analysis ──
        {
            'name': 'search_procedures',
            'description': 'Поиск процедур и функций BSL по имени',
            'params': _with_source([
                {'name': 'query', 'type': 'text', 'required': True, 'hint': 'Расчет, Заполнить...'},
                {'name': 'module_filter', 'type': 'text', 'required': False, 'hint': 'РасчетныйМодуль...'},
                {'name': 'export_only', 'type': 'select', 'required': False, 'hint': 'Только экспортные',
                 'options': ['', 'true']},
                {'name': 'limit', 'type': 'number', 'required': False, 'hint': 'Макс. результатов', 'default': 30},
            ]),
        },
        {
            'name': 'get_call_tree',
            'description': 'Дерево вызовов. down = кого вызывает; up = откуда вызывается',
            'params': _with_source([
                {'name': 'procedure_name', 'type': 'text', 'required': True, 'hint': 'ПолучитьРасчетПоПолю'},
                {'name': 'module_name', 'type': 'text', 'required': False, 'hint': 'РасчетныйМодуль (опц.)'},
                {'name': 'direction', 'type': 'select', 'required': False, 'hint': 'Направление',
                 'options': ['down', 'up'],
                 'option_labels': {'down': 'down — кого вызывает', 'up': 'up — кто вызывает'}},
                {'name': 'depth', 'type': 'number', 'required': False, 'hint': 'Глубина', 'default': 3},
            ]),
        },
        {
            'name': 'get_module_outline',
            'description': 'Структура модуля: все процедуры с директивами, экспортом, строками',
            'params': _with_source([
                {'name': 'module_path', 'type': 'text', 'required': True, 'hint': 'РасчетныйМодуль...'},
            ]),
        },
        {
            'name': 'search_code',
            'description': 'Поиск по коду BSL (grep по контексту вызовов)',
            'params': _with_source([
                {'name': 'query', 'type': 'text', 'required': True, 'hint': 'ЗначенияРеквизитовОбъекта...'},
                {'name': 'file_pattern', 'type': 'text', 'required': False, 'hint': 'Фильтр по файлу'},
                {'name': 'limit', 'type': 'number', 'required': False, 'hint': 'Макс. результатов', 'default': 30},
            ]),
        },
        # ── Diagnostics ──
        {
            'name': 'diagnose_index',
            'description': 'Диагностика: сравнить BSL-файлы с индексом, найти пропущенные процедуры',
            'params': [],
        },
    ]
