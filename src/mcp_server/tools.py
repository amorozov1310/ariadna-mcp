"""
MCP tool execution logic — transport-agnostic (no MCP SDK imports here so
it's easy to unit test), called by the typed wrapper functions registered
with the SDK in server.py (Этап 6, IMPROVEMENT_PLAN.md).

All tools except list_projects are project-scoped. project_id is optional
when the registry holds exactly one project (D12: set_active_project was
removed — it never actually persisted anything, so every tool required
project_id anyway; a single-project registry now just defaults to it).
"""

from ..core.project_manager import ProjectManager
from ..core.search import format_search_results, format_object_details


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


def _page_trailer(page_len: int, offset: int, limit: int, has_more: bool) -> str:
    if has_more:
        return f"(use offset={offset + limit} for more)"
    return f"shown {page_len} of {offset + page_len}"


def _format_page(page: list[dict], offset: int, limit: int, has_more: bool) -> str:
    if not page:
        return "No results found." if offset == 0 else f"No further results at offset={offset}."
    text = format_search_results(page, max_items=limit, start_index=offset + 1)
    return f"{text}\n{_page_trailer(len(page), offset, limit, has_more)}"


# ============================================
# TOOL EXECUTION
# ============================================

def execute_tool(pm: ProjectManager, tool: str, args: dict) -> str:
    """Execute a tool and return text result."""

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
            # returns immediately, so an ERP-scale corpus can't time out
            # the MCP call. Poll get_index_status for progress/completion.
            # Проверка до старта: иначе фоновый reindex() упал бы с KeyError уже
            # после ответа «started» и оставил бы проекту status='error'.
            if source_id:
                error = _unknown_source_error(pm, project_id, source_id)
                if error:
                    return error
            result = pm.reindex_async(project_id, source_id=source_id)
            if result['status'] == 'already_running':
                return (
                    f"Reindex already in progress for '{project_id}'. "
                    f"Track it with get_index_status."
                )
            return (
                f"Reindex started for '{project_id}'. "
                f"Track progress via get_index_status."
            )

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
            except (ValueError, KeyError) as e:
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
            module_name = args.get('module_path', '') or args.get('module_name', '')
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
                        f"List its procedures with get_module_outline(module_path=\"{mod['name']}\").")
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
            return (f"// {mod['name']}.{proc['name']}{src}\n"
                    f"// Lines {proc['start_line']}-{proc['end_line']}\n\n{code}")

        case 'get_call_tree':
            from ..core.call_analyzer import CallAnalyzer
            db = pm.get_db(project_id)
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree(
                args.get('procedure_name', ''),
                module_name=args.get('module_name') or None,
                direction=args.get('direction', 'down'),
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
                notes.append(
                    f"выдача ограничена: просмотрено не больше {search_mod.SEARCH_CODE_FTS_MODULES} "
                    f"модулей и не больше {search_mod.SEARCH_CODE_MAX_PER_MODULE} вхождений в модуле — "
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
                args.get('module_path', ''),
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
                for m in result['missing']:
                    lines.append(f"  ❌ {m['module']}:{m['line']}  {m['signature'][:120]}")
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
