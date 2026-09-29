"""
Call Analyzer: builds call trees from indexed procedures and calls.
Supports up/down traversal with depth limiting.

Source-aware: each node tracks its source_id/source_label so calls across
data sources (main config ↔ extensions) are visible in the output.
"""

import sqlite3
from dataclasses import dataclass, field
from .db import Database


@dataclass
class CallTreeNode:
    module_name: str = ''
    procedure_name: str = ''
    full_name: str = ''
    line: int = 0
    directive: str = ''
    source_id: str = ''
    source_label: str = ''
    # Whether this edge was found in calls_resolved at all (False = the
    # call couldn't be matched to any indexed procedure — likely platform
    # or external code; shown as a leaf, never expanded further).
    resolved: bool = True
    # exact | name_only | override — from calls_resolved.confidence.
    # Meaningless when resolved=False.
    confidence: str = 'exact'
    children: list['CallTreeNode'] = field(default_factory=list)
    # True only on the tree actually returned by build_call_tree() (root or
    # virtual_root) if max_nodes was hit while expanding it — Этап 6 token
    # budget (a hot procedure's fan-out can otherwise blow up the response).
    truncated: bool = False


class CallAnalyzer:
    """Analyzes call relationships between procedures."""

    def __init__(self, db: Database):
        self.db = db

    def build_call_tree(
        self,
        procedure_name: str,
        module_name: str | None = None,
        direction: str = 'down',
        depth: int = 3,
        source_id: str | None = None,
        max_nodes: int = 200,
    ) -> CallTreeNode | None:
        """
        Build call tree.
        direction='down': who does this procedure call?
        direction='up': who calls this procedure?
        source_id: optional — restrict root to procedures from one source.
        max_nodes: hard cap (Этап 6 token budget) on total nodes across the
            whole returned tree (all matches combined, if several) — a hot
            procedure's fan-out can otherwise produce an unbounded response.
            The returned root's ``truncated`` flag is set if the cap was hit.

        If multiple procedures match, builds trees for all of them.
        """
        # Этап 4/D7: per-thread reader — doesn't contend with a background
        # reindex's writer connection.
        conn = self.db.read_conn()

        # Find ALL matching procedures (with source info)
        conds = ["p.name_cf = ?"]
        params: list = [procedure_name.strip().casefold()]
        if module_name:
            from .search import resolve_module_ids
            module_ids = resolve_module_ids(conn, module_name, source_id)
            if not module_ids:
                return None
            conds.append(f"m.id IN ({','.join('?' * len(module_ids))})")
            params.extend(module_ids)
        if source_id:
            conds.append("m.source_id = ?")
            params.append(source_id)
        where = ' AND '.join(conds)

        rows = conn.execute(
            f"""SELECT p.id, p.name, p.directive,
                      m.name as module_name, m.source_id,
                      s.label as source_label
               FROM procedures p
               JOIN modules m ON m.id = p.module_id
               LEFT JOIN sources s ON s.id = m.source_id
               WHERE {where}
               ORDER BY m.source_id, m.name""", params).fetchall()

        if not rows:
            return None

        # Shared node-count budget (Этап 6) across every match's subtree —
        # a virtual root with several matches must not let each one
        # independently blow past max_nodes.
        budget = {'count': 1, 'limit': max_nodes}

        # Single match — return tree directly
        if len(rows) == 1:
            row = rows[0]
            root = CallTreeNode(
                module_name=row['module_name'],
                procedure_name=row['name'],
                full_name=f"{row['module_name']}.{row['name']}",
                directive=row['directive'] or '',
                source_id=row['source_id'] or '',
                source_label=row['source_label'] or row['source_id'] or '',
            )
            visited = set()
            if direction == 'down':
                self._build_down(conn, row['id'], root, depth, visited, budget)
            else:
                self._build_up(conn, row['id'], root, depth, visited, budget)
            root.truncated = budget['count'] >= budget['limit']
            return root

        # Multiple matches — build virtual root with each match as child tree
        virtual_root = CallTreeNode(
            full_name=f'{procedure_name} ({len(rows)} совпадений)',
            procedure_name=procedure_name,
        )

        for row in rows:
            child = CallTreeNode(
                module_name=row['module_name'],
                procedure_name=row['name'],
                full_name=f"{row['module_name']}.{row['name']}",
                directive=row['directive'] or '',
                source_id=row['source_id'] or '',
                source_label=row['source_label'] or row['source_id'] or '',
            )
            visited = set()
            budget['count'] += 1
            if direction == 'down':
                self._build_down(conn, row['id'], child, depth, visited, budget)
            else:
                self._build_up(conn, row['id'], child, depth, visited, budget)
            virtual_root.children.append(child)

        virtual_root.truncated = budget['count'] >= budget['limit']
        return virtual_root

    def _build_down(self, conn, proc_id: int, node: CallTreeNode,
                     depth: int, visited: set, budget: dict):
        """Find procedures called by this procedure (deduplicated).

        Resolution now goes through calls_resolved (populated by
        Indexer.resolve_calls) instead of a same-name LIKE lookup (D4):
        an unresolved call means the target genuinely isn't in the index
        (platform/external code) rather than "whatever else matched the
        name", so it's shown as a leaf instead of guessed at.
        """
        if depth <= 0 or proc_id in visited or budget['count'] >= budget['limit']:
            return
        visited.add(proc_id)

        rows = conn.execute(
            """SELECT c.id, c.callee_proc, c.callee_module, c.line,
                      cr.callee_id, cr.confidence
               FROM calls c
               LEFT JOIN calls_resolved cr ON cr.call_id = c.id
               WHERE c.caller_id = ?
               ORDER BY c.line""",
            (proc_id,)).fetchall()

        # Dedup by (callee_proc, callee_module), preferring a resolved row
        # over an unresolved one when the same target appears on several lines.
        best: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            key = (row['callee_proc'], row['callee_module'])
            prev = best.get(key)
            if prev is None or (row['callee_id'] is not None and prev['callee_id'] is None):
                best[key] = row

        for (callee_proc, callee_module), row in best.items():
            if budget['count'] >= budget['limit']:
                break
            if row['callee_id'] is not None:
                target = conn.execute(
                    """SELECT p.id, p.name, p.directive,
                              m.name as module_name, m.source_id,
                              s.label as source_label
                       FROM procedures p
                       JOIN modules m ON m.id = p.module_id
                       LEFT JOIN sources s ON s.id = m.source_id
                       WHERE p.id = ?""",
                    (row['callee_id'],)).fetchone()
                child = CallTreeNode(
                    module_name=target['module_name'],
                    procedure_name=target['name'],
                    full_name=f"{target['module_name']}.{target['name']}",
                    line=row['line'],
                    directive=target['directive'] or '',
                    source_id=target['source_id'] or '',
                    source_label=target['source_label'] or target['source_id'] or '',
                    resolved=True,
                    confidence=row['confidence'] or 'exact',
                )
                node.children.append(child)
                budget['count'] += 1
                if target['id'] not in visited:
                    self._build_down(conn, target['id'], child, depth - 1, visited, budget)
            else:
                full_name = f"{callee_module}.{callee_proc}" if callee_module else callee_proc
                child = CallTreeNode(
                    procedure_name=callee_proc,
                    full_name=full_name,
                    line=row['line'],
                    resolved=False,
                    confidence='none',
                )
                node.children.append(child)
                budget['count'] += 1

    def _build_up(self, conn, proc_id: int, node: CallTreeNode,
                   depth: int, visited: set, budget: dict):
        """Find procedures that call this procedure, via calls_resolved
        (D3): matched by the specific procedure's id, not by name — a
        same-named procedure in an unrelated module never shows up here."""
        if depth <= 0 or proc_id in visited or budget['count'] >= budget['limit']:
            return
        visited.add(proc_id)

        rows = conn.execute(
            """SELECT p.id, p.name, p.directive,
                      m.name as module_name, m.source_id,
                      s.label as source_label,
                      cr.confidence,
                      MIN(c.line) as line
               FROM calls_resolved cr
               JOIN calls c ON c.id = cr.call_id
               JOIN procedures p ON p.id = c.caller_id
               JOIN modules m ON m.id = p.module_id
               LEFT JOIN sources s ON s.id = m.source_id
               WHERE cr.callee_id = ?
               GROUP BY p.id, p.name, p.directive, m.name, m.source_id, s.label, cr.confidence
               ORDER BY m.source_id, m.name, p.name""",
            (proc_id,)).fetchall()

        for row in rows:
            if budget['count'] >= budget['limit']:
                break
            child = CallTreeNode(
                module_name=row['module_name'],
                procedure_name=row['name'],
                full_name=f"{row['module_name']}.{row['name']}",
                line=row['line'],
                directive=row['directive'] or '',
                source_id=row['source_id'] or '',
                source_label=row['source_label'] or row['source_id'] or '',
                resolved=True,
                confidence=row['confidence'] or 'exact',
            )
            node.children.append(child)
            budget['count'] += 1
            if row['id'] not in visited:
                self._build_up(conn, row['id'], child, depth - 1, visited, budget)

    def format_tree_simple(self, node: CallTreeNode | None, max_depth: int = 5) -> str:
        """Текстовое дерево вызовов.

        Модуль и источник печатаются ТОЛЬКО когда меняются относительно
        родителя. Подавляющее большинство вызовов — локальные, внутри того
        же модуля, и раньше каждая строка начиналась с одинаковых
        «ОбщийМодуль.CommonServerCall.Модуль.» и заканчивалась одинаковым
        «@idm2»: полезное (имя процедуры и форма дерева) тонуло между двумя
        стенами повторов. Переход в другой модуль — наоборот, самое важное
        событие в дереве, и теперь он единственное, что выделяется.
        """
        if not node:
            return "Procedure not found."
        lines = []
        self._format_node(node, lines, '', True, 0, max_depth,
                          inherited_module='', inherited_source='')
        if node.truncated:
            lines.append(
                "... (truncated: hit max_nodes — narrow with direction/depth "
                "or raise max_nodes for more)")
        return '\n'.join(lines)

    @staticmethod
    def _node_label(node: CallTreeNode, inherited_module: str) -> str:
        """Имя узла: короткое, если модуль тот же, что у родителя, и полное
        с модулем — если вызов уходит в другой модуль."""
        name = node.procedure_name or node.full_name
        if node.module_name and node.module_name != inherited_module:
            return f'{node.module_name}.{name}'
        return name

    def _format_node(self, node: CallTreeNode, lines: list, prefix: str,
                      is_root: bool, depth: int, max_depth: int,
                      inherited_module: str = '', inherited_source: str = ''):
        if is_root:
            dir_str = f' {node.directive}' if node.directive else ''
            src_str = f'  @{node.source_label}' if node.source_label else ''
            suffix = f'(){dir_str}' if node.module_name else ''
            lines.append(f'{node.full_name}{suffix}{src_str}')
            inherited_module = node.module_name
            inherited_source = node.source_label

        if depth >= max_depth and node.children:
            lines.append(f'{prefix}    ... ({len(node.children)} more)')
            return

        for i, child in enumerate(node.children):
            is_last = (i == len(node.children) - 1)
            connector = '└── ' if is_last else '├── '
            new_prefix = prefix + ('    ' if is_last else '│   ')

            label = self._node_label(child, inherited_module)
            c_dir = f' {child.directive}' if child.directive else ''
            c_line = f' [L{child.line}]' if child.line else ''
            # Источник — только при переходе в другой (конфигурация ↔
            # расширение): это действительно значимо, а повтор одного и
            # того же на каждой строке — нет.
            c_src = (f'  @{child.source_label}'
                     if child.source_label and child.source_label != inherited_source else '')
            # Confidence marker (1.3): a call with no calls_resolved row at
            # all is likely platform/external code (~ВозможноПлатформа); a
            # resolved-but-lower-confidence edge (name_only/override) gets
            # a lighter '?' so the agent can see the trust level.
            if not child.resolved:
                c_conf = '  ~ВозможноПлатформа'
            elif child.confidence and child.confidence != 'exact':
                c_conf = '  ?'
            else:
                c_conf = ''
            lines.append(f'{prefix}{connector}{label}(){c_dir}{c_line}{c_src}{c_conf}')
            if child.children:
                self._format_node(
                    child, lines, new_prefix, False, depth + 1, max_depth,
                    inherited_module=child.module_name or inherited_module,
                    inherited_source=child.source_label or inherited_source)
