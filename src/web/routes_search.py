"""Routes: metadata search, procedure search, code search, object detail, calltree.

Source-aware: every search accepts `source_id` to filter by a specific
data source (main config vs extension), and every result carries its
`source_id` / `source_label` so the UI can show where each match came from.
"""

import time
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse

from ..core.project_manager import PROJECT_GONE
from .app import templates, get_pm

router = APIRouter()


@router.get("/projects/{project_id}/search", response_class=HTMLResponse)
def search_page(request: Request, project_id: str, q: str = '', kind: str = '',
                mode: str = 'metadata', module_filter: str = '', export_only: str = '',
                source_id: str = ''):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    engine = pm.get_search(project_id)
    sources = engine.list_sources()

    # Этап U0/U2: список видов метаданных берётся из индекса проекта, а не
    # из захардкоженного перечня в шаблоне (там было 9 видов из 25+, и
    # отфильтровать по Роли или Константе было невозможно). Порядок — по
    # убыванию количества, рядом с видом показываем счётчик.
    kinds: dict[str, int] = {}
    try:
        kinds = pm.get_db(project_id).get_stats().get('kinds', {}) or {}
    except PROJECT_GONE:
        raise          # проект удалён во время запроса — 404 (app.py)
    except Exception:
        kinds = {}

    results = []
    elapsed_ms = 0
    if q:
        t0 = time.time()
        src = source_id or None

        if mode == 'procedures':
            results = engine.search_procedures(
                q, module_filter=module_filter or None,
                export_only=bool(export_only), source_id=src, limit=50)
        elif mode == 'code':
            results = engine.search_code(q, source_id=src, limit=50)
        else:
            results = engine.search_metadata(q, kind=kind or None, source_id=src)

        elapsed_ms = round((time.time() - t0) * 1000, 1)

    return templates.TemplateResponse(name="search.html", request=request, context={
        "project": project,
        "query": q,
        "kind": kind,
        "mode": mode,
        "module_filter": module_filter,
        "export_only": export_only,
        "source_id": source_id,
        "sources": sources,
        "kinds": kinds,
        "results": results,
        "elapsed_ms": elapsed_ms,
    })


@router.get("/projects/{project_id}/object/{full_name:path}", response_class=HTMLResponse)
def object_detail(request: Request, project_id: str, full_name: str, source_id: str = ''):
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    engine = pm.get_search(project_id)
    details = engine.get_object_details(full_name, source_id=source_id or None)
    if not details:
        raise HTTPException(404, f"Object '{full_name}' not found")

    return templates.TemplateResponse(name="object_detail.html", request=request, context={
        "project": project,
        "details": details,
        "current_source_id": details.get('source_id', ''),
    })


@router.get("/projects/{project_id}/calltree", response_class=HTMLResponse)
def calltree_page(
    request: Request, project_id: str,
    procedure: str = '', module: str = '', direction: str = 'down', depth: int = 3,
    source_id: str = '',
):
    """Text-based call tree (works without client-side JS graphics)."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    engine = pm.get_search(project_id)
    sources = engine.list_sources()

    tree_text = None
    tree_node = None
    not_ready = False
    error = None

    if procedure:
        from ..core.call_analyzer import CallAnalyzer
        db = pm.get_db(project_id)
        ca = CallAnalyzer(db)
        try:
            depth_int = max(1, min(int(depth or 3), 10))
        except (ValueError, TypeError):
            depth_int = 3
        tree_node = ca.build_call_tree(
            procedure,
            module_name=module or None,
            direction=direction or 'down',
            depth=depth_int,
            source_id=source_id or None,
        )
        if tree_node is None:
            error = f"Процедура «{procedure}» не найдена в индексе" + \
                (f" (источник: {source_id})" if source_id else '')
        else:
            tree_text = ca.format_tree_simple(tree_node)

    return templates.TemplateResponse(name="calltree.html", request=request, context={
        "project": project,
        "procedure": procedure,
        "module": module,
        "direction": direction or 'down',
        "depth": depth,
        "source_id": source_id,
        "sources": sources,
        "tree": tree_text,
        "tree_node": tree_node,
        "not_ready": not_ready,
        "error": error,
    })


@router.get("/projects/{project_id}/graph", response_class=HTMLResponse)
def graph_page(
    request: Request, project_id: str,
    procedure: str = '', module: str = '', direction: str = 'down', depth: int = 3,
    source_id: str = '',
):
    """Interactive visual graph of a call tree (vis.js network)."""
    pm = get_pm()
    try:
        project = pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    engine = pm.get_search(project_id)
    sources = engine.list_sources()

    return templates.TemplateResponse(name="graph.html", request=request, context={
        "project": project,
        "procedure": procedure,
        "module": module,
        "direction": direction or 'down',
        "depth": depth,
        "source_id": source_id,
        "sources": sources,
    })


@router.get("/api/projects/{project_id}/graph/calltree")
def api_calltree_graph(
    project_id: str,
    procedure: str = '', module: str = '', direction: str = 'down', depth: int = 3,
    source_id: str = '',
):
    """Return call tree as vis.js-compatible {nodes, edges} JSON."""
    from fastapi.responses import JSONResponse
    from ..core.call_analyzer import CallAnalyzer

    pm = get_pm()
    try:
        pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    if not procedure:
        return JSONResponse({'nodes': [], 'edges': [], 'error': 'procedure required'})

    db = pm.get_db(project_id)
    ca = CallAnalyzer(db)
    try:
        depth_int = max(1, min(int(depth or 3), 10))
    except (ValueError, TypeError):
        depth_int = 3

    root = ca.build_call_tree(
        procedure,
        module_name=module or None,
        direction=direction or 'down',
        depth=depth_int,
        source_id=source_id or None,
    )
    if root is None:
        return JSONResponse({
            'nodes': [], 'edges': [],
            'error': f"Процедура '{procedure}' не найдена"
        })

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    next_id = [0]

    # Colour palette per source_id (stable across requests via simple hash)
    def _color_for_source(sid: str) -> str:
        palette = ['#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6',
                   '#06b6d4', '#ef4444', '#84cc16', '#6366f1', '#f97316']
        if not sid:
            return '#94a3b8'
        return palette[hash(sid) % len(palette)]

    def _walk(node, parent_id: int | None, level: int):
        # Build a stable node key
        key = f"{node.full_name}|{node.source_id}"
        if key in seen:
            # Already drawn — just add an edge from parent
            existing_id = next((n['id'] for n in nodes if n.get('_key') == key), None)
            if existing_id is not None and parent_id is not None:
                if direction == 'up':
                    edges.append({'from': existing_id, 'to': parent_id, 'arrows': 'to'})
                else:
                    edges.append({'from': parent_id, 'to': existing_id, 'arrows': 'to'})
            return
        seen.add(key)
        nid = next_id[0]
        next_id[0] += 1

        label = node.procedure_name or node.full_name
        title_parts = [f"{node.full_name}"]
        if node.directive:
            title_parts.append(f"Директива: {node.directive}")
        if node.source_label:
            title_parts.append(f"Источник: {node.source_label}")
        if node.line:
            title_parts.append(f"Строка: {node.line}")

        nodes.append({
            'id': nid,
            '_key': key,
            'label': label,
            'title': '\n'.join(title_parts),   # tooltip
            'group': node.source_id or 'unknown',
            'color': _color_for_source(node.source_id),
            'level': level,
            'source_label': node.source_label,
            'module': node.module_name,
        })

        if parent_id is not None:
            if direction == 'up':
                # up-mode: child is a CALLER of parent, so edge points from child -> parent
                edges.append({'from': nid, 'to': parent_id, 'arrows': 'to'})
            else:
                # down-mode: parent calls child
                edges.append({'from': parent_id, 'to': nid, 'arrows': 'to'})

        for ch in node.children:
            _walk(ch, nid, level + 1)

    _walk(root, None, 0)

    # Strip internal _key before sending
    for n in nodes:
        n.pop('_key', None)

    # Build legend: unique sources present
    legend = {}
    for n in nodes:
        sid = n.get('group')
        if sid and sid not in legend:
            legend[sid] = {
                'source_id': sid,
                'source_label': n.get('source_label') or sid,
                'color': n.get('color'),
            }

    return JSONResponse({
        'nodes': nodes,
        'edges': edges,
        'root_procedure': procedure,
        'direction': direction,
        'depth': depth_int,
        'total_nodes': len(nodes),
        'total_edges': len(edges),
        'legend': list(legend.values()),
    })


@router.get("/api/projects/{project_id}/graph/references")
def api_references_graph(
    project_id: str,
    object_name: str = '',
    depth: int = 1,
    source_id: str = '',
):
    """Return object-reference graph: for a given object, which objects reference it
    (via attribute types like СправочникСсылка.X) and which objects it references."""
    from fastapi.responses import JSONResponse

    pm = get_pm()
    try:
        pm.get_project(project_id)
    except KeyError:
        raise HTTPException(404)

    if not object_name:
        return JSONResponse({'nodes': [], 'edges': [], 'error': 'object_name required'})

    engine = pm.get_search(project_id)
    db = pm.get_db(project_id)
    conn = db.read_conn()  # read-only (Этап 4/D7)

    try:
        depth_int = max(1, min(int(depth or 1), 3))
    except (ValueError, TypeError):
        depth_int = 1

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    id_by_key: dict[str, int] = {}
    next_id = [0]

    palette = ['#3b82f6', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6',
               '#06b6d4', '#ef4444', '#84cc16', '#6366f1', '#f97316']

    def _color(sid: str) -> str:
        return palette[hash(sid or '') % len(palette)] if sid else '#94a3b8'

    def _add_node(full_name: str, kind: str, src_id: str, src_label: str,
                   is_root: bool = False) -> int:
        key = f"{full_name}|{src_id}"
        if key in id_by_key:
            return id_by_key[key]
        nid = next_id[0]
        next_id[0] += 1
        id_by_key[key] = nid
        nodes.append({
            'id': nid,
            'label': full_name.split('.')[-1] if '.' in full_name else full_name,
            'title': f"{full_name}\nТип: {kind}\nИсточник: {src_label}",
            'group': src_id or 'unknown',
            'color': _color(src_id),
            'shape': 'diamond' if is_root else ('box' if kind in ('Справочник', 'Документ') else 'dot'),
            'size': 30 if is_root else 18,
            'full_name': full_name,
            'kind': kind,
            'source_label': src_label,
            'source_id': src_id,
        })
        return nid

    # Root: find the object itself (prefer exact match in all sources)
    root_conds = ["(m.full_name = ? OR m.name = ?)"]
    root_params: list = [object_name, object_name]
    if source_id:
        root_conds.append("m.source_id = ?")
        root_params.append(source_id)
    root_rows = conn.execute(
        f"""SELECT m.full_name, m.kind, m.source_id, s.label as source_label
            FROM metadata_objects m LEFT JOIN sources s ON s.id = m.source_id
            WHERE {' AND '.join(root_conds)}""",
        root_params).fetchall()

    if not root_rows:
        return JSONResponse({
            'nodes': [], 'edges': [],
            'error': f"Объект '{object_name}' не найден"
        })

    root_ids = []
    for r in root_rows:
        rid = _add_node(r['full_name'], r['kind'], r['source_id'] or '',
                        r['source_label'] or r['source_id'] or '', is_root=True)
        root_ids.append((rid, r['full_name'], r['source_id']))

    # For each root, walk references
    def _walk_refs(root_id: int, root_full_name: str, root_src: str, level: int):
        if level <= 0:
            return

        # Inbound: who points to this object (via attribute types)
        short_name = root_full_name.split('.')[-1]
        ref_conds = ["a.type_desc LIKE ?"]
        ref_params: list = [f'%{short_name}%']
        if source_id:
            ref_conds.append("m.source_id = ?")
            ref_params.append(source_id)
        inbound = conn.execute(
            f"""SELECT DISTINCT m.full_name, m.kind, m.source_id, s.label as source_label,
                       a.name as attr_name, a.type_desc
                FROM attributes a JOIN metadata_objects m ON m.id = a.object_id
                LEFT JOIN sources s ON s.id = m.source_id
                WHERE {' AND '.join(ref_conds)}
                LIMIT 100""",
            ref_params).fetchall()

        for r in inbound:
            if r['full_name'] == root_full_name:
                continue   # skip self-refs for clarity
            nid = _add_node(r['full_name'], r['kind'], r['source_id'] or '',
                            r['source_label'] or r['source_id'] or '')
            edges.append({
                'from': nid, 'to': root_id,
                'arrows': 'to',
                'label': r['attr_name'],
                'font': {'size': 9, 'color': '#64748b'},
            })
            if level > 1:
                _walk_refs(nid, r['full_name'], r['source_id'], level - 1)

    for rid, rname, rsrc in root_ids:
        _walk_refs(rid, rname, rsrc, depth_int)

    legend = {}
    for n in nodes:
        sid = n.get('group')
        if sid and sid not in legend:
            legend[sid] = {
                'source_id': sid,
                'source_label': n.get('source_label') or sid,
                'color': n.get('color'),
            }

    return JSONResponse({
        'nodes': nodes,
        'edges': edges,
        'root_object': object_name,
        'depth': depth_int,
        'total_nodes': len(nodes),
        'total_edges': len(edges),
        'legend': list(legend.values()),
    })
