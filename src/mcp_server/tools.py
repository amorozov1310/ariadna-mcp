"""
MCP tool execution logic — transport-agnostic (no MCP SDK imports here so
it's easy to unit test), called by the typed wrapper functions registered
with the SDK in server.py (Этап 6, IMPROVEMENT_PLAN.md).

All tools except list_projects are project-scoped. project_id is optional
when the registry holds exactly one project (D12: set_active_project was
removed — it never actually persisted anything, so every tool required
project_id anyway; a single-project registry now just defaults to it).
"""

import sqlite3

from ..core.platform_help import KINDS, KIND_ALIASES, format_entry
from ..core.project_manager import ProjectManager, ReindexInProgressError
from ..core.platform_check import LEVELS, ReportIndexChanged, check_calls
from ..core.search import format_search_results, format_object_details
from . import semantic_search


# ============================================
# PROJECT RESOLUTION
# ============================================

def _resolve_project_id(pm: ProjectManager, args: dict) -> tuple[str | None, str | None]:
    """
    Returns (project_id, error). If args has a project_id, validates it
    exists. If not, defaults to the registry's only project when there is
    exactly one (Этап 6/D12) — otherwise returns an error listing the
    available ids.
    """
    project_id = (args.get('project_id') or '').strip()

    if project_id:
        try:
            pm.get_project(project_id)
        except KeyError:
            available = ', '.join(p.id for p in pm.list_projects())
            return None, f"Error: project '{project_id}' not found. Available: {available or 'none'}"
        return project_id, None

    projects = pm.list_projects()
    if len(projects) == 1:
        return projects[0].id, None
    if not projects:
        return None, "Error: no projects found. Create one via Web UI at :19878"
    available = ', '.join(p.id for p in projects)
    return None, f"Error: project_id is required (multiple projects exist). Available: {available}"


def _unknown_source_error(pm: ProjectManager, project_id: str, source_id: str) -> str | None:
    """Ошибка со списком доступных источников, если source_id в проекте нет."""
    ids = [s.id for s in pm.get_project(project_id).sources]
    if source_id in ids:
        return None
    return (f"Error: source '{source_id}' not found in project '{project_id}'. "
            f"Available: {', '.join(ids) or 'none'}")


# ============================================
# PAGINATION (Этап 6 items 4 + 6: limit/offset + "shown X of Y" /
# "use offset=N for more")
# ============================================

def _paginate(fetch, limit: int, offset: int):
    """
    Calls ``fetch(limit=limit+1, offset=offset)`` to peek one row past the
    page — cheaper than a separate COUNT(*) query and enough to tell
    "there's more" from "that's everything" without ever fetching more
    than limit+1 rows. Returns (page, has_more).
    """
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    rows = fetch(limit=limit + 1, offset=offset)
    has_more = len(rows) > limit
    return rows[:limit], has_more


def format_broken_call(i: dict) -> str:
    where = f"{i['caller_module']}.{i['caller_proc']}:{i['line']} @{i['source_id']}"
    target = f"{i['module']}.{i['name']}"
    if i.get('missing_object'):
        return f"  {where}\n    → {target}: объекта метаданных нет в выгрузке"
    if 'defined_in' in i:
        homes = ', '.join(f"{d['module']} @{d['source_id']}" for d in i['defined_in'])
        hint = f" — с таким именем есть в: {homes}" if homes else ''
        return f"  {where}\n    → {target}: метода в модуле нет{hint}"
    return f"  {where}\n    → {target}: метод без «Экспорт», а вызов из другого модуля"


def format_broken_calls(project_id: str, result: dict, offset: int, limit: int) -> str:
    """Выдача find_broken_calls: сначала ошибки (постранично), затем — на первой
    странице — модули, которых нет в выгрузке."""
    missing, not_export = result['missing'], result['not_export']
    items = [('missing', i) for i in missing] + [('not_export', i) for i in not_export]
    shown = items[offset:offset + limit]
    lines = []
    if offset == 0:
        ext_calls = sum(e['calls'] for e in result['external'])
        lines += [
            f"Проверка вызовов «{project_id}»: метода в модуле нет — {len(missing)}, "
            f"неэкспортный метод из другого модуля — {len(not_export)}.",
            f"Модулей нет в выгрузке: {len(result['external'])} ({ext_calls} вызовов) — "
            f"внешние библиотеки или опечатки, список ниже. Через переменную-модуль "
            f"(ОбщийМодуль(\"…\")) к таким методам — {result['dynamic']}: платформа их не "
            f"проверяет, в список не входят.",
        ]
        if not items:
            lines.append("\nВызовов несуществующих и неэкспортных методов не найдено.")
    section = None
    for kind, i in shown:
        if kind != section:
            section = kind
            title = ('Метода в модуле нет' if kind == 'missing'
                     else 'Неэкспортный метод из другого модуля')
            count = len(missing) if kind == 'missing' else len(not_export)
            lines.append(f"\n{title} ({count}):")
        lines.append(format_broken_call(i))
    if shown:
        lines.append(_page_trailer(len(shown), offset, limit, offset + limit < len(items)))
    if offset == 0 and result['external']:
        lines.append("\nМодулей нет в выгрузке (первые 10):")
        for e in result['external'][:10]:
            s = e['sample']
            lines.append(f"  {e['module']} — вызовов {e['calls']}, методов {e['methods']} "
                         f"(например {s['caller_module']}.{s['caller_proc']}:{s['line']} "
                         f"@{s['source_id']})")
    if offset == 0 and items:
        lines.append("\nКод вызывающей процедуры: get_procedure_code(module_name=..., "
                     "procedure_name=..., source_id=...)")
    return '\n'.join(lines).lstrip('\n')


def _page_trailer(page_len: int, offset: int, limit: int, has_more: bool) -> str:
    if has_more:
        return f"(use offset={offset + limit} for more)"
    return f"shown {page_len} of {offset + page_len}"


def _format_page(page: list[dict], offset: int, limit: int, has_more: bool) -> str:
    if not page:
        return "No results found." if offset == 0 else f"No further results at offset={offset}."
    text = format_search_results(page, max_items=limit, start_index=offset + 1)
    return f"{text}\n{_page_trailer(len(page), offset, limit, has_more)}"


def _stale_module_warning(path: str, mod: dict) -> str:
    """Предупреждение, если файл модуля изменился после индексации.

    get_procedure_code режет текущий файл по номерам строк из индекса: после
    «обновить источник» без reindex под заголовком процедуры оказались бы
    чужие строки. Хеш считается так же, как в индексаторе; пустой
    file_hash (индексатор не смог прочитать файл) — не повод для тревоги."""
    from ..core.indexer import form_xml_for_module, module_file_hash
    stored = mod.get('file_hash') or ''
    if not stored:
        return ''
    try:
        current = module_file_hash(path, form_xml_for_module(path, mod.get('module_type') or ''))
    except OSError:
        return ''
    if current == stored:
        return ''
    return ("// ВНИМАНИЕ: файл модуля изменён после индексации — границы процедуры "
            "могут быть неверны, код ниже может быть чужим. Переиндексируйте проект "
            "(reindex).\n")


_GLOSSARY_KIND = {'object': 'объект', 'attribute': 'реквизит', 'procedure': 'процедура'}


def format_glossary_entry(entry, prefix: str = '', with_author: bool = True) -> str:
    """Строка записи словаря: id, цель, термины, примечание, кто и когда.
    with_author=False — подтверждение записи: повторная запись того же даёт
    тот же текст (время в нём менялось бы)."""
    note = f" — {entry.note}" if entry.note else ''
    who = ', '.join(x for x in (entry.author, entry.updated_at.replace('T', ' ')) if x) \
        if with_author else ''
    return (f"{prefix}[{entry.id}] {entry.target} ({_GLOSSARY_KIND.get(entry.target_kind, entry.target_kind)}): "
            f"{', '.join('«' + t + '»' for t in entry.terms)}{note}" + (f"  ({who})" if who else ''))


def _call_list(rows: list[dict], shown: int) -> list[str]:
    out = []
    for r in rows[:shown]:
        where = f"стр. {r['line']}" + (f", мест: {r['sites']}" if r['sites'] > 1 else '')
        out.append(f"  {r['module_name']}.{r['procedure_name']}  ({where})")
    if len(rows) > shown:
        out.append(f"  … и ещё {len(rows) - shown} — get_call_tree")
    return out


def format_procedure_context(ctx: dict) -> str:
    """Текст get_procedure_context: объявление, место, описание, связи.
    Вызывающие — сначала с наибольшим числом мест вызова; «стр.» — строка
    вызова в модуле вызывающего."""
    mod, proc = ctx['module'], ctx['procedure']
    src = f"  @{ctx['source_label']}" if ctx.get('source_label') else ''
    lines = [f"// {mod['name']}.{proc['name']}{src}"]
    decl = proc.get('signature') or f"{proc['kind']} {proc['name']}({proc.get('params') or ''})"
    if proc.get('is_export') and not any(w in decl.casefold() for w in ('экспорт', 'export')):
        decl += ' Экспорт'
    if proc.get('directive'):
        decl = f"{proc['directive']} {decl}"
    lines.append(decl)
    count = proc['end_line'] - proc['start_line'] + 1
    lines.append(f"Строки: {proc['start_line']}-{proc['end_line']} ({count}), файл {mod.get('file_path', '')}")
    if mod.get('object_full_name'):
        obj = mod['object_full_name']
        if mod.get('object_synonym'):
            obj += f" — {mod['object_synonym']}"
        lines.append(f"Объект: {obj}")
    if ctx.get('glossary'):
        g = ctx['glossary']
        note = f" — {g['note']}" if g.get('note') else ''
        lines.append(f"Словарь: {', '.join('«' + t + '»' for t in g['terms'])}{note}")
    if ctx.get('region'):
        lines.append(f"Область: {ctx['region']}")
    if ctx.get('comment'):
        shown, size = [], 0
        for line in ctx['comment']:
            if len(shown) >= CONTEXT_COMMENT_LINES or size + len(line) > CONTEXT_COMMENT_CHARS:
                shown.append('…')
                break
            shown.append(line)
            size += len(line)
        lines.append('Описание:')
        lines += [f'  {line}' for line in shown]
    elif not ctx.get('file_readable'):
        lines.append("Описание: файл модуля недоступен на сервере — область и комментарий не прочитаны")

    callers = ctx['callers']
    if callers:
        sites = sum(r['sites'] for r in callers)
        lines.append(f"\nВызывают ({len(callers)}, мест вызова: {sites}):")
    else:
        lines.append("\nВызывают: никто в проиндексированном коде (точка входа, обработчик "
                     "события или вызов по строке)")
    lines += _call_list(callers, CONTEXT_CALLS_SHOWN)
    if ctx['callees']:
        lines.append(f"\nВызывает ({len(ctx['callees'])}):")
        lines += _call_list(ctx['callees'], CONTEXT_CALLS_SHOWN)
    unresolved = ctx['unresolved']
    if unresolved:
        names = ', '.join(r['name'] for r in unresolved[:CONTEXT_CALLS_SHOWN])
        if len(unresolved) > CONTEXT_CALLS_SHOWN:
            names += f" … и ещё {len(unresolved) - CONTEXT_CALLS_SHOWN}"
        lines.append(f"\nНе найдены в индексе ({len(unresolved)}): {names}")
    lines.append(f"\nКод — get_procedure_code(module_name=\"{mod['name']}\", "
                 f"procedure_name=\"{proc['name']}\")")
    return '\n'.join(lines)


# ============================================
# TOOL EXECUTION
# ============================================

# Потолок limit у постраничных инструментов. Больший limit раздувал ответ и
# контекст модели (limit=100000 — десятки тысяч строк за один вызов). 500 —
# в 5 раз больше самого крупного значения по умолчанию (list_objects, 100):
# ~500 строк результата — это уже десятки тысяч токенов; дальше
# листать через offset.
MAX_LIMIT = 500
_PAGED_TOOLS = ('search_metadata', 'search_attributes', 'find_references', 'list_objects',
                'search_procedures', 'search_code', 'diff_from_base', 'update_conflicts',
                'find_broken_calls', 'knowledge_sources', 'platform_help', 'check_platform_calls')

# Сколько процедур «Missing from index» показывает diagnose_index: на большой
# конфигурации их тысячи, а для диагностики хватает первых.
DIAGNOSE_MISSING_SHOWN = 100

# get_procedure_context: сколько вызывающих и вызываемых показывать (дальше —
# get_call_tree) и предел описания из комментария (строк и символов).
CONTEXT_CALLS_SHOWN = 20
CONTEXT_COMMENT_LINES = 12
CONTEXT_COMMENT_CHARS = 800


# ============================================
# DIFF FROM BASE
# ============================================

_DIFF_SECTIONS = {'changed': 'Доработанные объекты типовой (больше изменений — выше)',
                  'added': 'Новые объекты', 'removed': 'Удалённые объекты'}
# Сколько имён процедур/реквизитов перечислять в одной строке сводки.
DIFF_NAMES_SHOWN = 5


def _names(items: list[str], shown: int = DIFF_NAMES_SHOWN) -> str:
    return ', '.join(items[:shown]) + (f' … (+{len(items) - shown})' if len(items) > shown else '')


def _module_short(module_name: str, object_name: str) -> str:
    """«Документ.X.Форма.ФормаДокумента» у объекта Документ.X → «Форма.ФормаДокумента»."""
    prefix = object_name + '.'
    return module_name[len(prefix):] if object_name and module_name.startswith(prefix) else module_name


def _module_counts(m) -> str:
    if m.status == 'added':
        return f"новый модуль, процедур {m.procedures}"
    if m.status == 'removed':
        return f"модуль удалён, процедур было {m.procedures}"
    parts = [f"{label} {len(items)}" for label, items in
             (('изменено', m.changed), ('добавлено', m.added), ('удалено', m.removed)) if items]
    return 'процедуры: ' + ', '.join(parts) if parts else 'правка вне процедур'


def format_base_diff_summary(project_id: str, diff) -> str:
    t = diff.totals()
    versions = f": {diff.base_version or '?'} → {diff.current_version or '?'}" \
        if diff.base_version != diff.current_version else (f" ({diff.base_version})" if diff.base_version else '')
    return '\n'.join([
        f"Отличия «{project_id}» от базы «{diff.base_project}»{versions}",
        f"Код: изменено модулей {t['modules_changed']} из {diff.modules_total}, новых "
        f"{t['modules_added']}, удалено {t['modules_removed']}. Процедуры в изменённых модулях: "
        f"изменено {t['procedures_changed']} (сигнатура — у {t['procedures_signature']}), "
        f"добавлено {t['procedures_added']}, удалено {t['procedures_removed']}; "
        f"в новых модулях — {t['procedures_in_added_modules']}",
        f"Метаданные: новых объектов {t['objects_added']}, удалено {t['objects_removed']}; "
        f"у типовых: реквизитов добавлено {t['attributes_added']}, удалено "
        f"{t['attributes_removed']}, тип изменён у {t['attributes_retyped']}, форм добавлено "
        f"{t['forms_added']}",
        f"Доработано типовых объектов: {t['objects_changed']}",
    ] + ([VERSIONS_DIFFER] if diff.versions_differ else []))


# Проект и база на разных релизах: отличия — уже не только доработки.
VERSIONS_DIFFER = ("Внимание: версии проекта и базы различаются — в отличия попали и изменения "
                   "типовой между этими релизами. Базой должна быть типовая того же релиза, что "
                   "и проект; новый релиз для подготовки к обновлению — update_conflicts.")


def format_object_change_line(o) -> str:
    syn = f" — {o.synonym}" if o.synonym else ''
    parts = []
    if o.attrs_added:
        parts.append(f"реквизиты +{len(o.attrs_added)}")
    if o.attrs_removed:
        parts.append(f"реквизиты −{len(o.attrs_removed)}")
    if o.attrs_retyped:
        parts.append(f"тип изменён у {len(o.attrs_retyped)}")
    if o.forms_added:
        parts.append(f"формы +{len(o.forms_added)}")
    if o.forms_removed:
        parts.append(f"формы −{len(o.forms_removed)}")
    if o.synonym_was is not None:
        parts.append("синоним изменён")
    for m in o.modules:
        parts.append(f"{_module_short(m.name, o.full_name)}: {_module_counts(m)}")
    return f"  {o.full_name}{syn}" + (f": {'; '.join(parts)}" if parts else '')


def format_object_change(o, project_id: str, base_project: str) -> str:
    status = {'changed': 'доработан', 'added': f'новый (в базе «{base_project}» его нет)',
              'removed': f'удалён (есть только в базе «{base_project}»)'}[o.status]
    syn = f" ({o.synonym})" if o.synonym else ''
    lines = [f"{o.full_name}{syn} — {status}"]
    if o.synonym_was is not None:
        lines.append(f"Синоним: «{o.synonym_was}» → «{o.synonym}»")
    for title, items in (('Реквизиты добавлены', o.attrs_added), ('Реквизиты удалены', o.attrs_removed)):
        if items:
            lines.append(f"{title} ({len(items)}):")
            lines += [f"  {n}" + (f": {t}" if t else '') for n, t in items]
    if o.attrs_retyped:
        lines.append(f"Тип изменён ({len(o.attrs_retyped)}):")
        lines += [f"  {n}: {was or '—'} → {now or '—'}" for n, was, now in o.attrs_retyped]
    for title, items in (('Формы добавлены', o.forms_added), ('Формы удалены', o.forms_removed)):
        if items:
            lines.append(f"{title}: {', '.join(items)}")
    if o.modules:
        lines.append("Модули:")
        for m in o.modules:
            lines.append(f"  {m.name} — {_module_counts(m)}")
            for label, items in (('изменены', m.changed), ('добавлены', m.added), ('удалены', m.removed)):
                if items:
                    lines.append(f"    {label}: {', '.join(items)}")
            if m.signature_changed:
                lines.append(f"    сигнатура изменена: {', '.join(m.signature_changed)}")
        lines.append("Текст изменения процедуры: diff_from_base(module_name=..., procedure_name=...)")
    return '\n'.join(lines)


def format_procedure_diff(result: dict) -> str:
    name, base = result['name'], result['base_project']
    match result['status']:
        case 'missing':
            return f"Процедуры {name} нет ни в проекте, ни в базе «{base}». Имя модуля и процедуры — из search_procedures."
        case 'same':
            return f"{name}: текст в проекте и в базе «{base}» совпадает."
        case 'unreadable':
            side = f"базы «{base}»" if result['base_unreadable'] else "проекта"
            return (f"{name}: процедура есть в обоих индексах, но файл выгрузки {side} недоступен на "
                    f"сервере — текст не сравнить (переиндексируйте или проверьте путь к выгрузке).")
    note = {'added': f"процедуры нет в базе «{base}» — добавлена",
            'removed': f"процедура есть только в базе «{base}» — удалена"}.get(result['status'], '')
    return (f"// {note}\n" if note else '') + (result['diff'] or f"{name}: изменение не прочитать.")


def format_update_conflicts_summary(project_id: str, result) -> str:
    from ..core.base_diff import CONFLICT_KINDS
    t = result.release_totals
    counts = result.by_kind()
    lines = [
        f"Обновление «{project_id}»: база «{result.base_project}» ({result.base_version or '?'}) → "
        f"релиз «{result.release_project}» ({result.release_version or '?'})",
        f"Релиз изменил: модулей {t['modules_changed']}, новых {t['modules_added']}, удалено "
        f"{t['modules_removed']}; процедур изменено {t['procedures_changed']}, добавлено "
        f"{t['procedures_added']}, удалено {t['procedures_removed']}; новых объектов {t['objects_added']}",
        f"Пересечения с доработками проекта: {len(result.items)}",
    ]
    lines += [f"  {title}: {counts[kind]}" for kind, title in CONFLICT_KINDS.items() if counts[kind]]
    return '\n'.join(lines)


def format_conflict_line(c) -> str:
    return f"  [{c.title}] {c.target}" + (f" — {c.note}" if c.note else '')


def is_error_text(text: str) -> bool:
    """Ответ execute_tool — ожидаемая ошибка («Error: …»). Одно правило для
    MCP (server.py: такой ответ уходит клиенту с isError=true) и playground
    Web UI (показывается как ошибка)."""
    return text.startswith('Error')


def semantic_search_payload(pm: ProjectManager, args: dict) -> dict | str:
    """Результат semantic_search_code как данные (structuredContent в server.py)
    или текст ошибки «Error: …» — с той же проверкой проекта и источника."""
    project_id, error = _resolve_project_id(pm, args)
    if error:
        return error
    source_id = (args.get('source_id') or '').strip() or None
    if source_id:
        error = _unknown_source_error(pm, project_id, source_id)
        if error:
            return error
    return semantic_search.search(pm.get_search(project_id), project_id, args.get('query', ''),
                                  source_id, args.get('limit'), args.get('offset', 0))


def execute_tool(pm: ProjectManager, tool: str, args: dict) -> str:
    """Execute a tool and return text result."""
    requested = args.get('limit')
    try:
        capped = tool in _PAGED_TOOLS and requested is not None and int(requested) > MAX_LIMIT
    except (TypeError, ValueError):
        capped = False
    if not capped:
        return _execute_tool(pm, tool, args)
    text = _execute_tool(pm, tool, {**args, 'limit': MAX_LIMIT})
    if is_error_text(text):
        return text
    return (f"{text}\n(limit={requested} урезан до {MAX_LIMIT} — больше за один вызов не "
            f"выдаётся; остальное — через offset)")


def _project_help(pm: ProjectManager, project, version: str = ''):
    store = pm.platform_help
    version = version or (project.platform_version if project else '') or store.newest()
    try:
        help_index = store.get(version) if version else None
    except ValueError as e:
        return f'Error: {e}'
    if not help_index:
        label = f' {version}' if version else ''
        return (f'Справка платформы{label} не загружена. Добавьте её в Web UI → Настройки → '
                'Справка платформы. При запуске Ариадна сама находит установленную платформу.')
    return help_index


def _platform_help(pm: ProjectManager, args: dict) -> str:
    project_id = (args.get('project_id') or '').strip()
    requested_version = (args.get('version') or '').strip()
    if project_id:
        try:
            project = pm.get_project(project_id)
        except KeyError:
            if not requested_version:
                return f'Error: проект «{project_id}» не найден'
            project = None
    else:
        projects = pm.list_projects()
        project = projects[0] if len(projects) == 1 else None
    help_index = _project_help(pm, project, requested_version)
    if isinstance(help_index, str):
        return help_index
    version = help_index.version
    query = args.get('query', '').strip()
    if not query:
        return 'Error: укажите имя или слова для поиска в справке платформы'
    kind = (args.get('kind') or '').strip().casefold()
    if kind and kind not in KIND_ALIASES:
        return 'Error: неизвестный вид записи. Допустимые значения: ' + ', '.join(KINDS)
    limit = max(1, int(args.get('limit', 10)))
    offset = max(0, int(args.get('offset', 0)))
    try:
        entries, more = help_index.search(query, kind, limit, offset)
    except sqlite3.OperationalError:
        if help_index.path.exists():
            raise
        return f'Справка {version} удалена во время поиска.'
    if not entries:
        if offset:
            return f'Справка {version}: при offset={offset} записей больше нет.'
        msg = f'В справке платформы {version} нет записи «{query}».'
        if all(part.isidentifier() for part in query.removesuffix('()').split('.')):
            msg += ' Вероятно, это метод конфигурации. Ищите через search_procedures или search_code.'
        return msg
    mode = pm.platform_mode(project.id) if project else ''
    parts = [f'Справка платформы {version}', *(format_entry(e, mode=mode) for e in entries)]
    if more:
        parts.append(f'Следующая страница: offset={offset + limit}.')
    return '\n\n'.join(parts)


def _execute_tool(pm: ProjectManager, tool: str, args: dict) -> str:
    if tool == 'platform_help':
        return _platform_help(pm, args)
    if tool == 'list_projects':
        projects = pm.list_projects()
        if not projects:
            return "No projects found. Create one via Web UI at :19878"
        lines = [f"Projects ({len(projects)}):"]
        for p in projects:
            lines.append(f"  {p.id} — {p.name} [{p.status}] {p.index_stats.total_objects} objects")
        return '\n'.join(lines)

    project_id, error = _resolve_project_id(pm, args)
    if error:
        return error

    engine = pm.get_search(project_id)
    source_id = (args.get('source_id') or '').strip() or None
    limit_arg = args.get('limit')
    offset = int(args.get('offset', 0) or 0)

    match tool:
        case 'search_metadata':
            limit = int(limit_arg or 20)
            page, has_more = _paginate(
                lambda limit, offset: engine.search_metadata(
                    args.get('query', ''), kind=args.get('kind') or None,
                    source_id=source_id, limit=limit, offset=offset),
                limit, offset)
            return _format_page(page, offset, limit, has_more)

        case 'get_object_details':
            details = engine.get_object_details(
                args.get('full_name', ''),
                source_id=source_id,
            )
            detail = (args.get('detail') or 'brief').strip().lower()
            if detail not in ('brief', 'full'):
                detail = 'brief'
            return format_object_details(details, detail=detail)

        case 'search_attributes':
            limit = int(limit_arg or 30)
            page, has_more = _paginate(
                lambda limit, offset: engine.search_attributes(
                    args.get('query', ''), type_filter=args.get('type_filter') or None,
                    source_id=source_id, limit=limit, offset=offset),
                limit, offset)
            return _format_page(page, offset, limit, has_more)

        case 'find_references':
            limit = int(limit_arg or 50)
            page, has_more = _paginate(
                lambda limit, offset: engine.find_references(
                    args.get('object_name', ''), source_id=source_id,
                    limit=limit, offset=offset),
                limit, offset)
            return _format_page(page, offset, limit, has_more)

        case 'list_objects':
            limit = int(limit_arg or 100)
            page, has_more = _paginate(
                lambda limit, offset: engine.list_objects(
                    kind=args.get('kind') or None, source_id=source_id,
                    limit=limit, offset=offset),
                limit, offset)
            return _format_page(page, offset, limit, has_more)

        case 'knowledge_sources':
            from ..core.knowledge_sources import format_sources
            full_name = args.get('full_name') or None
            include_query = bool(args.get('include_query'))
            limit = int(limit_arg or (1 if full_name else 50))
            page, has_more = _paginate(
                lambda limit, offset: engine.knowledge_sources(
                    kind=args.get('kind') or None, full_name=full_name, source_id=source_id,
                    include_query=include_query, limit=limit, offset=offset),
                limit, offset)
            if not page:
                return "No results found." if offset == 0 else f"No further results at offset={offset}."
            return f"{format_sources(page, start_index=offset + 1)}\n{_page_trailer(len(page), offset, limit, has_more)}"

        case 'list_sources':
            sources = engine.list_sources()
            if not sources:
                return "No sources indexed yet."
            lines = [f"Sources ({len(sources)}):"]
            for s in sources:
                cfg = f"{s.get('config_name','')} {s.get('config_version','')}".strip()
                lines.append(f"  {s['id']} — {s['label']} [{s['source_type']}]" + (f" {cfg}" if cfg else ''))
            return '\n'.join(lines)

        case 'get_index_status':
            stats = engine.get_index_status()
            lines = [f"Status: {stats.get('status', 'unknown')}"]
            if stats.get('status') == 'indexing' and stats.get('progress_total'):
                lines.append(
                    f"Progress: {stats.get('progress_current', 0)}/{stats['progress_total']} "
                    f"({stats.get('progress_phase', '')})"
                )
                if stats.get('progress_phase') == 'bsl_full':
                    lines.append("Полный перепарс BSL: изменилась версия парсера "
                                 "(файлы выгрузки не менялись)")
            if stats.get('updated_at'):
                lines.append(f"Updated: {stats['updated_at'][:19]}")
            lines.append(f"Objects: {stats.get('metadata_objects', 0)}")
            lines.append(f"Attributes: {stats.get('attributes', 0)}")
            lines.append(f"Forms: {stats.get('forms', 0)}")
            lines.append(f"Modules: {stats.get('modules', 0)}")
            lines.append(f"Procedures: {stats.get('procedures', 0)}")
            if stats.get('kinds'):
                lines.append("By kind:")
                for k, v in stats['kinds'].items():
                    lines.append(f"  {k}: {v}")

            return '\n'.join(lines)

        case 'reindex':
            # Этап 4/D7: never blocks — starts in the background and
            # returns immediately, so a large corpus can't time out
            # the MCP call. Poll get_index_status for progress/completion.
            # Проверка до старта: иначе фоновый reindex() упал бы с KeyError уже
            # после ответа «started» и оставил бы проекту status='error'.
            if source_id:
                error = _unknown_source_error(pm, project_id, source_id)
                if error:
                    return error
            result = pm.reindex_async(project_id, source_id=source_id)
            if result['status'] == 'error':
                return f"Error: не удалось начать переиндексацию '{project_id}': {result['error']}"
            if result['status'] == 'already_running':
                return (
                    f"Reindex already in progress for '{project_id}'. "
                    f"Track it with get_index_status."
                )
            return (
                f"Reindex started for '{project_id}'. "
                f"Track progress via get_index_status."
            )

        case 'add_glossary_term':
            from ..core.glossary import split_terms
            target = (args.get('target') or '').strip()
            terms = split_terms(args.get('terms') or '')
            if not target or not terms:
                return "Error: нужны target (полное имя объекта, реквизита или процедуры) и terms (слова через запятую)"
            resolved = engine.resolve_glossary_target(target)
            if resolved is None:
                return (f"Error: «{target}» нет в индексе проекта. Укажите полное имя: объект — "
                        f"Справочник.X, реквизит — РегистрНакопления.X.Сумма, процедура — "
                        f"ОбщийМодуль.X.Модуль.Процедура (найти: search_metadata, "
                        f"search_attributes, search_procedures)")
            full_name, kind = resolved
            entry = engine.glossary.save(full_name, kind, terms, note=(args.get('note') or '').strip(),
                                         author='агент')
            return format_glossary_entry(entry, prefix='Записано в словарь: ', with_author=False)

        case 'list_glossary':
            query = (args.get('query') or '').strip()
            glossary = engine.glossary
            entries = [e for e, _ in glossary.match(query)] if query else glossary.entries()
            if not entries:
                return ("Словарь пуст." if not query else f"В словаре нет записей для «{query}».")
            limit = int(limit_arg or 50)
            shown = entries[offset:offset + limit]
            text = '\n'.join(format_glossary_entry(e) for e in shown)
            return f"{text}\n{_page_trailer(len(shown), offset, limit, offset + limit < len(entries))}"

        case 'remove_glossary_term':
            entry_id = (args.get('entry_id') or '').strip()
            entry = engine.glossary.remove(entry_id) if entry_id else None
            if entry is None:
                return f"Error: записи «{entry_id}» в словаре нет — id есть в list_glossary"
            return format_glossary_entry(entry, prefix='Удалено из словаря: ', with_author=False)

        case 'remove_source':
            target = args.get('source_id', '')
            if not target:
                return "Error: source_id is required"
            confirm = bool(args.get('confirm', False))
            if not confirm:
                try:
                    preview = pm.preview_remove_source(project_id, target)
                except KeyError as e:
                    return f"Error: {e}"
                return (
                    f"Will delete source '{target}' ({preview['label']}, {preview['source_type']}):\n"
                    f"  Metadata objects: {preview['objects']}\n"
                    f"  Attributes: {preview['attributes']}\n"
                    f"  Forms: {preview['forms']}\n"
                    f"  Modules: {preview['modules']}\n"
                    f"  Procedures: {preview['procedures']}\n"
                    f"  Calls: {preview['calls']}\n"
                    f"  Files on disk: {'yes' if preview['files_exist'] else 'no'}\n"
                    f"This is irreversible. Repeat with confirm=true to actually delete."
                )
            # pm.remove_source молча выходит на неизвестном id (так его зовёт
            # Web UI) — без проверки здесь инструмент рапортовал бы об удалении.
            error = _unknown_source_error(pm, project_id, target)
            if error:
                return error
            try:
                pm.remove_source(project_id, target)
                return f"Source '{target}' removed (files + indexed data wiped)."
            except (ReindexInProgressError, ValueError, KeyError) as e:
                return f"Error: {e}"

        # Phase 2: BSL code analysis tools
        case 'search_procedures':
            limit = int(limit_arg or 30)
            page, has_more = _paginate(
                lambda limit, offset: engine.search_procedures(
                    args.get('query', ''), module_filter=args.get('module_filter') or None,
                    export_only=args.get('export_only', False), source_id=source_id,
                    limit=limit, offset=offset),
                limit, offset)
            if not page:
                return "No procedures found." if offset == 0 else f"No further results at offset={offset}."
            lines = [f"Found {len(page)} procedure(s):"]
            for r in page:
                exp = ' Экспорт' if r.get('is_export') else ''
                src = f"  @{r.get('source_label','')}" if r.get('source_label') else ''
                lines.append(
                    f"  {r['module_name']}.{r['name']}(){exp} "
                    f"{r.get('directive', '')} [{r['start_line']}-{r['end_line']}]{src}"
                )
            lines.append(_page_trailer(len(page), offset, limit, has_more))
            return '\n'.join(lines)

        case 'get_procedure_code':
            from ..core.bsl_parser import read_bsl_text
            module_name = args.get('module_name', '')
            proc_name = (args.get('procedure_name') or '').strip()
            outline = engine.get_module_outline(module_name, source_id=source_id)
            if not outline:
                return (f"Module '{module_name}' not found. Find the module with "
                        f"search_procedures(query=\"{proc_name}\") — its output starts with the module name.")
            mod = outline['module']
            proc = next((p for p in outline['procedures']
                         if p['name'].casefold() == proc_name.casefold()), None)
            if not proc:
                return (f"Procedure '{proc_name}' not found in module {mod['name']}. "
                        f"List its procedures with get_module_outline(module_name=\"{mod['name']}\").")
            path = engine.module_file_path(mod)
            if not path:
                return (f"{mod['name']}.{proc['name']}: lines {proc['start_line']}-{proc['end_line']} — "
                        f"module file is not readable on the server (XML dump moved or deleted? reindex the project).")
            content = read_bsl_text(path)
            if content is None:
                return f"{mod['name']}.{proc['name']}: could not read {mod.get('file_path', '')}."
            lines = content.split('\n')
            code = '\n'.join(lines[proc['start_line'] - 1:proc['end_line']])
            src = f"  @{outline['source_label']}" if outline.get('source_label') else ''
            return (_stale_module_warning(path, mod)
                    + f"// {mod['name']}.{proc['name']}{src}\n"
                    f"// Lines {proc['start_line']}-{proc['end_line']}\n\n{code}")

        case 'get_procedure_context':
            ctx = engine.get_procedure_context(
                args.get('module_name', ''), args.get('procedure_name', ''), source_id=source_id)
            return ctx if isinstance(ctx, str) else format_procedure_context(ctx)

        case 'get_call_tree':
            from ..core.call_analyzer import CallAnalyzer
            # Любое значение, кроме 'down', раньше молча работало как 'up'.
            direction = (args.get('direction') or 'down').strip().lower()
            if direction not in ('down', 'up'):
                return "Error: direction must be 'down' or 'up'"
            db = pm.get_db(project_id)
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree(
                args.get('procedure_name', ''),
                module_name=args.get('module_name') or None,
                direction=direction,
                depth=int(args.get('depth', 3)),
                source_id=source_id,
                max_nodes=int(args.get('max_nodes', 200)),
            )
            return ca.format_tree_simple(tree)

        case 'search_code':
            from ..core import search as search_mod
            limit = int(limit_arg or 30)
            found = {'last': None}

            def fetch_code(limit, offset):
                found['last'] = engine.search_code_limited(
                    args.get('query', ''), file_pattern=args.get('file_pattern') or None,
                    source_id=source_id, limit=limit, offset=offset)
                return found['last'].results

            page, has_more = _paginate(fetch_code, limit, offset)
            last = found['last']
            truncated = bool(last and last.truncated)
            unreadable = last.unreadable if last else 0
            readable = last.readable if last else 0

            # Модули читаются с диска: если выгрузки там нет, пустой ответ —
            # не «ничего не найдено», а невозможность искать.
            if not page and unreadable and not readable:
                return (f"Error: файлы выгрузки недоступны на сервере — не прочитан ни один из "
                        f"{unreadable} модулей-кандидатов. Переиндексируйте проект или "
                        f"проверьте путь к выгрузке.")

            # Поиск просматривает ограниченное число модулей и вхождений на
            # модуль; если предел сработал, «показано всё» было бы неправдой.
            notes = []
            if truncated:
                # Называем только тот предел, который действительно сработал.
                limits = []
                if last.modules_cap:
                    limits.append(f"просмотрено не больше {last.modules_cap} модулей")
                if last.per_module_capped:
                    limits.append(f"не больше {search_mod.SEARCH_CODE_MAX_PER_MODULE} "
                                  f"строк с вхождениями в модуле")
                notes.append(f"выдача ограничена: {' и '.join(limits)} — "
                             f"сузьте поиск через file_pattern или source_id")
            if unreadable:
                notes.append(f"не прочитано модулей: {unreadable} (файлы выгрузки недоступны "
                             f"на сервере) — выдача неполная")

            if not page:
                if offset == 0:
                    return f"No code matches found ({'; '.join(notes)})." if notes \
                        else "No code matches found."
                return f"No further results at offset={offset}."
            lines = [f"Found {len(page)} match(es):"]
            for r in page:
                src = f"  @{r.get('source_label','')}" if r.get('source_label') else ''
                lines.append(f"  {r['module_name']}:{r['line']}{src}")
                for ctx_line in r['context'].splitlines():
                    lines.append(f"    {ctx_line}")
            if has_more:
                lines.append(_page_trailer(len(page), offset, limit, has_more))
                if unreadable:
                    lines.append(f"({notes[-1]})")
            elif notes:
                lines.append(f"(shown {len(page)}; {'; '.join(notes)})")
            else:
                lines.append(_page_trailer(len(page), offset, limit, has_more))
            return '\n'.join(lines)

        case 'get_module_outline':
            outline = engine.get_module_outline(
                args.get('module_name', ''),
                source_id=source_id,
            )
            if not outline:
                return "Module not found."
            mod = outline['module']
            src_suffix = f"  @{outline.get('source_label','')}" if outline.get('source_label') else ''
            lines = [
                f"Module: {mod['name']}{src_suffix}",
                f"Type: {mod['module_type']}",
                f"Lines: {mod['line_count']}",
                f"File: {mod.get('file_path', '')}",
                f"\nProcedures ({len(outline['procedures'])}):",
            ]
            for p in outline['procedures']:
                exp = ' Экспорт' if p.get('is_export') else ''
                dir_str = f' {p["directive"]}' if p.get('directive') else ''
                lines.append(
                    f"  {p['kind']} {p['name']}({p.get('params', '')[:40]}){exp}{dir_str} "
                    f"[{p['start_line']}-{p['end_line']}]"
                )
            return '\n'.join(lines)

        case 'diff_from_base':
            module_name = (args.get('module_name') or '').strip()
            proc_name = (args.get('procedure_name') or '').strip()
            object_name = (args.get('object_name') or '').strip()
            try:
                if proc_name:
                    if not module_name:
                        return "Error: для текста процедуры нужен и module_name (из search_procedures или сводки)"
                    return format_procedure_diff(pm.get_procedure_diff(
                        project_id, module_name, proc_name, source_id=source_id or None))
                diff = pm.get_base_diff(project_id, source_id=source_id or None)
            except ValueError as e:
                return f"Error: {e}"
            if object_name or module_name:
                o = diff.find_object(object_name or module_name)
                if o is None:
                    return (f"«{object_name or module_name}» не отличается от базы «{diff.base_project}» "
                            f"(или такого объекта нет — полное имя из search_metadata).")
                return format_object_change(o, project_id, diff.base_project)
            section = (args.get('section') or 'changed').strip().lower()
            if section not in _DIFF_SECTIONS:
                return f"Error: section — одно из {', '.join(_DIFF_SECTIONS)}"
            limit = max(1, int(limit_arg or 30))
            items = diff.by_status(section)
            shown = items[offset:offset + limit]
            lines = [format_base_diff_summary(project_id, diff), ''] if offset == 0 else []
            lines.append(f"{_DIFF_SECTIONS[section]} ({len(items)}):")
            lines += [format_object_change_line(o) for o in shown] or ["  нет"]
            if shown:
                lines.append(_page_trailer(len(shown), offset, limit, offset + limit < len(items)))
            if offset == 0:
                lines.append("Подробно по объекту: diff_from_base(object_name=...); текст процедуры: "
                             "diff_from_base(module_name=..., procedure_name=...); другие разделы: "
                             "section=added | removed")
            return '\n'.join(lines)

        case 'update_conflicts':
            release_id = (args.get('release_project') or '').strip()
            module_name = (args.get('module_name') or '').strip()
            proc_name = (args.get('procedure_name') or '').strip()
            object_name = (args.get('object_name') or '').strip()
            src = source_id or None           # None — основная конфигурация проекта
            try:
                result = pm.get_update_conflicts(project_id, release_id, source_id=src)
                if proc_name:
                    if not module_name:
                        return "Error: для текста процедуры нужен и module_name (из списка конфликтов)"
                    ours = pm.get_procedure_diff(project_id, module_name, proc_name, source_id=src)
                    theirs = pm.get_procedure_diff(result.release_project, module_name, proc_name,
                                                   source_id=src, base_id=result.base_project)
            except ValueError as e:
                return f"Error: {e}"
            if proc_name:
                text = (f"== Ваша правка (база «{result.base_project}» → «{project_id}»):\n"
                        f"{format_procedure_diff(ours)}\n\n"
                        f"== Изменение релиза (база «{result.base_project}» → «{result.release_project}»):\n"
                        f"{format_procedure_diff(theirs)}")
                full_module = (ours['name'] or theirs['name']).rsplit('.', 1)[0]
                found = pm.intercepts(project_id, src, full_module, proc_name)
                if found:
                    text += "\n\n== Перехваты в расширениях:\n" + '\n'.join(
                        f"  {it.source_id}: {it.module}.{it.ext_procedure}  {it.label}" for it in found)
                    text += ("\nКод перехвата: get_procedure_code(module_name=..., procedure_name=..., "
                             "source_id=<расширение>)")
                return text
            items = result.items
            if object_name:
                key = object_name.casefold()
                items = [c for c in items if c.object_name.casefold() == key
                         or c.target.casefold().startswith(key + '.') or c.target.casefold() == key]
            limit = max(1, int(limit_arg or 50))
            shown = items[offset:offset + limit]
            lines = [format_update_conflicts_summary(project_id, result), ''] if offset == 0 else []
            lines.append(f"Пересечения{f' по {object_name}' if object_name else ''} ({len(items)}):")
            lines += [format_conflict_line(c) for c in shown] or ["  нет — доработки не пересекаются с изменениями релиза"]
            if shown:
                lines.append(_page_trailer(len(shown), offset, limit, offset + limit < len(items)))
            if offset == 0 and items:
                lines.append("Обе правки процедуры (ваша и релиза): update_conflicts(release_project=..., "
                             "module_name=..., procedure_name=...)")
            return '\n'.join(lines)

        case 'semantic_search_code':
            data = semantic_search_payload(pm, args)
            return data if isinstance(data, str) else semantic_search.format_text(data)

        case 'find_broken_calls':
            result = pm.broken_calls(project_id, source_id or '',
                                     (args.get('module_name') or '').strip())
            return format_broken_calls(project_id, result, offset, max(1, int(limit_arg or 50)))

        case 'check_platform_calls':
            if source_id and (error := _unknown_source_error(pm, project_id, source_id)):
                return error
            levels = args.get('level')
            if levels is not None:
                if isinstance(levels, str):
                    levels = [levels]
                if not isinstance(levels, list) or any(
                        not isinstance(level, str) or level not in LEVELS for level in levels):
                    return 'Error: level принимает error, warning, unchecked или список этих значений.'
                levels = frozenset(levels)
            where = " AND source_id=?" if source_id else ""
            modules = engine.db.read_conn().execute(
                "SELECT 1 FROM modules WHERE file_path != ''" + where + " LIMIT 1",
                (source_id,) if source_id else ())
            if not modules.fetchone():
                if source_id:
                    return (f'В источнике нет модулей для проверки (source_id={source_id}). '
                            'Добавьте выгрузку с модулями и выполните reindex.')
                return 'В индексе нет модулей для проверки. Добавьте выгрузку с модулями и выполните reindex.'
            index = _project_help(pm, pm.get_project(project_id))
            if isinstance(index, str):
                return index
            mode = pm.platform_mode(project_id)
            limit = max(1, int(args.get('limit', 50)))
            offset = max(0, offset)
            try:
                page, more, counts, modules = check_calls(engine, index, mode, source_id,
                                                          limit, offset, levels)
            except ReportIndexChanged as e:
                return str(e)
            except (FileNotFoundError, sqlite3.OperationalError):
                if index.path.exists():
                    raise
                return f'Справка {index.version} удалена во время проверки.'
            heading = f'Проверка «{project_id}» по справке {index.version}. '
            heading += f'Режим совместимости {mode}.' if mode else 'Числовой режим совместимости не задан.'
            summary = (f"Модулей — {modules}. Ошибки — {counts['error']}, "
                       f"предупреждения — {counts['warning']}, не проверено — {counts['unchecked']}.")
            if not page:
                if offset:
                    page.append(f'При offset={offset} записей больше нет.')
                else:
                    page.append('Замечаний нет для выбранных уровней.' if levels is not None
                                else 'Замечаний нет.')
            if more:
                page.append(f'Следующая страница: offset={offset + limit}.')
            return '\n'.join([heading, summary, *page])

        case 'diagnose_index':
            from ..core.search import diagnose_index
            result = diagnose_index(pm, project_id)
            lines = [
                f"BSL files scanned: {result['total_in_files']} procedures",
                f"Indexed: {result['total_indexed']} procedures",
                f"Missing from index: {result['missing_count']}",
            ]
            if result['missing']:
                lines.append("")
                shown = result['missing'][:DIAGNOSE_MISSING_SHOWN]
                for m in shown:
                    lines.append(f"  ❌ {m['module']}:{m['line']}  {m['signature'][:120]}")
                if len(shown) < result['missing_count']:
                    lines.append(f"  … показано {len(shown)} из {result['missing_count']}")
            if result['extra']:
                lines.append(f"\nExtra in index (not in files): {result['extra_count']}")
                for e in result['extra'][:10]:
                    lines.append(f"  ⚠ {e['module']}.{e['name']}")
            if not result['missing'] and not result['extra']:
                lines.append("\n✅ Index is complete — all procedures accounted for.")

            ps = result.get('parser')
            if ps:
                lines.append(f"\n--- Parser fingerprint ---")
                lines.append(f"Current: {ps['current'] or '(не вычислен — нет исходников)'}")
                for sid, st in ps['sources'].items():
                    if st['up_to_date']:
                        mark = 'актуален'
                    elif st['fingerprint']:
                        mark = 'устарел — следующий reindex переразберёт BSL полностью'
                    else:
                        mark = 'нет отпечатка — следующий reindex переразберёт BSL полностью'
                    lines.append(f"  {sid}: {st['fingerprint'] or '—'} ({mark})")
                    if st['last_full_reparse_reason']:
                        lines.append(f"    последний полный перепарс {st['last_full_reparse_at'][:19]}: "
                                     f"{st['last_full_reparse_reason']}")

            # Этап 7: call-graph resolution quality
            g = result['graph']
            lines.append(f"\n--- Call graph resolution ---")
            lines.append(f"Resolved edges: {g['resolved_calls']}/{g['total_calls']} ({g['resolved_pct']}%)")
            if g['resolved_by_kind']:
                lines.append("By kind:")
                for kind, s in g['resolved_by_kind'].items():
                    lines.append(f"  {kind}: {s['resolved']}/{s['total']} ({s['pct']}%)")
            lines.append(f"Procedures with &Вместо/&Around-style intercepts: {g['override_procedures_count']}")
            if g['worst_modules']:
                lines.append(f"\nWorst-resolving modules (>= 5 calls):")
                for m in g['worst_modules']:
                    src = f" @{m['source_id']}" if m['source_id'] else ''
                    lines.append(f"  {m['module']}{src}: {m['resolved']}/{m['total']} ({m['pct']}%)")
            if g['top_unresolved']:
                lines.append(f"\nTop unresolved callee names (candidates for platform stoplist or resolver bug):")
                for u in g['top_unresolved']:
                    mod = f"{u['module']}." if u['module'] else ''
                    lines.append(f"  {mod}{u['name']}  x{u['freq']}")
            b = g.get('unresolved_breakdown')
            if b and b['total']:
                lines.append(f"\nUnresolved common_module/manager calls by cause: {b['total']}")
                titles = {
                    'module_missing': 'модуль отсутствует в выгрузке',
                    'method_missing': 'модуль есть, метода в нём нет',
                    'other': 'прочее (кандидаты на баг парсера/резолвера)',
                }
                for group, title in titles.items():
                    grp = b[group]
                    lines.append(f"  [{group}] {title}: {grp['count']} ({grp['pct']}%)")
                    for u in grp['top']:
                        note = ''
                        if u.get('defined_in'):
                            homes = ', '.join(
                                d['module'] + (f" @{d['source_id']}" if d['source_id'] else '')
                                for d in u['defined_in'])
                            note = f"  — определена в {homes}"
                        elif u.get('reason'):
                            note = f"  [{u['reason']}]"
                        lines.append(f"    {u['module']}.{u['name']}  x{u['freq']}{note}")
            return '\n'.join(lines)

        case _:
            return f"Unknown tool: {tool}"
