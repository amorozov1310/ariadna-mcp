"""
Search engine: FTS5 queries + LIKE fallback for Cyrillic.
Works against a per-project SQLite database.

Source awareness
----------------
All search methods accept an optional ``source_id`` filter and include
``source_id`` + ``source_label`` in every result row, so the UI/CLI can
distinguish objects with identical ``full_name`` coming from different
sources (main config vs extensions).
"""

import os
import re
from pathlib import Path

from .db import Database
from .report_parser import canonicalize_kind


# Пределы search_code: каждый модуль-кандидат перечитывается с диска (FTS
# без хранимого текста), поэтому без них один частый запрос читал бы тысячи
# файлов. Сработавший предел поиск сообщает (search_code_limited), а
# инструмент MCP — пользователю, вместо «показано всё».
SEARCH_CODE_FTS_MODULES = 50         # модулей-кандидатов из FTS
SEARCH_CODE_FALLBACK_MODULES = 300   # модулей в резервном просмотре без FTS
SEARCH_CODE_MAX_PER_MODULE = 5       # вхождений из одного модуля


def resolve_module_ids(conn, module_name: str, source_id: str | None = None) -> list[int]:
    """Ids of the modules that best match a user-typed module name.

    Ranked, and only the best-ranked group is returned: exact name
    ("ОбщийМодуль.ОбщегоНазначения.Модуль", or a bare common-module name
    stored from report.txt) → a common module's short name
    ("ОбщегоНазначения") → a whole dotted segment ("Номенклатура.МодульМенеджера")
    → plain substring, shortest name first. A bare substring match alone
    would let "ОбщегоНазначения" resolve to "МеждународныйУчетОбщегоНазначения".
    """
    q = (module_name or '').strip().replace('\\', '/').casefold()
    if not q:
        return []
    conds = ["name_cf LIKE '%' || :q || '%'"]
    params: dict = {'q': q}
    if source_id:
        conds.append("source_id = :sid")
        params['sid'] = source_id
    rows = conn.execute(
        f"""SELECT id, CASE
                WHEN name_cf = :q THEN 0
                WHEN name_cf = 'общиймодуль.' || :q || '.модуль' THEN 1
                WHEN name_cf LIKE :q || '.%' OR name_cf LIKE '%.' || :q
                     OR name_cf LIKE '%.' || :q || '.%' THEN 2
                ELSE 3 END AS rnk
            FROM modules WHERE {' AND '.join(conds)}
            ORDER BY rnk, length(name), name""", params).fetchall()
    if not rows:
        return []
    best = rows[0]['rnk']
    return [r['id'] for r in rows if r['rnk'] == best]


class SearchEngine:
    """Search over indexed 1C configuration metadata."""

    def __init__(self, db: Database, source_roots: dict[str, str] | None = None):
        self.db = db
        # {source_id: XML dump root on THIS machine} — module files are
        # located from here, not from modules.abs_path: that column holds
        # the path as seen by whichever process built the index, which is
        # wrong once the same data/ is served from a container (a
        # natively-built index stores D:\... paths).
        self.source_roots = source_roots or {}
        # Этап 4/D7: no longer forces the writer connection open — every
        # query here uses db.read_conn() (a per-thread reader), which
        # bootstraps the schema itself on a brand-new db_path if needed.

    def module_file_path(self, module_row) -> str | None:
        """Path of a module's .bsl file readable from this process, or None."""
        row = dict(module_row)
        rel = (row.get('file_path') or '').replace('\\', '/')
        root = self.source_roots.get(row.get('source_id') or '')
        if root and rel:
            candidate = Path(root) / rel
            if candidate.exists():
                return str(candidate)
        abs_path = row.get('abs_path') or ''
        if abs_path and os.path.exists(abs_path):
            return abs_path
        return None

    # ============================================
    # SOURCE HELPERS
    # ============================================

    def list_sources(self) -> list[dict]:
        """Return all sources registered in this project's index."""
        conn = self.db.read_conn()
        rows = conn.execute(
            """SELECT id, label, source_type, config_name, config_version, indexed_at
               FROM sources ORDER BY source_type DESC, label"""
        ).fetchall()
        return [dict(r) for r in rows]

    def _source_labels(self) -> dict[str, str]:
        """{source_id: label} lookup — used to decorate result rows."""
        conn = self.db.read_conn()
        rows = conn.execute("SELECT id, label FROM sources").fetchall()
        return {r['id']: (r['label'] or r['id']) for r in rows}

    def _attach_source_labels(self, rows: list[dict]) -> list[dict]:
        """Add source_label to each row using source_id."""
        if not rows:
            return rows
        labels = self._source_labels()
        for r in rows:
            sid = r.get('source_id')
            if sid and 'source_label' not in r:
                r['source_label'] = labels.get(sid, sid)
        return rows

    # ============================================
    # SEARCH METHODS
    # ============================================

    def search_metadata(self, query: str, kind: str | None = None,
                         source_id: str | None = None, limit: int = 20,
                         offset: int = 0) -> list[dict]:
        """Full-text search over metadata objects. Filter by source_id optionally.

        ``kind`` accepts either the internal RU canonical name or its EN
        alias, any case (D11/Этап 2) — e.g. "Catalog" == "справочник" == "Справочник".
        """
        conn = self.db.read_conn()
        kind = canonicalize_kind(kind) if kind else kind

        # Try FTS5 first
        fts_query = self._fts_query(query)
        conditions = ["metadata_fts MATCH ?"]
        params: list = [fts_query]
        if kind:
            conditions.append("m.kind = ?")
            params.append(kind)
        if source_id:
            conditions.append("m.source_id = ?")
            params.append(source_id)
        where = ' AND '.join(conditions)
        params.extend([limit, offset])

        rows = conn.execute(
            f"""SELECT m.full_name, m.kind, m.name, m.synonym, m.comment, m.source_id
                FROM metadata_fts f JOIN metadata_objects m ON m.id = f.rowid
                WHERE {where}
                ORDER BY rank LIMIT ? OFFSET ?""",
            params).fetchall()

        # LIKE fallback: progressively shorter prefixes (helps with inflection,
        # primarily Russian endings, but works language-agnostically). D5:
        # matched against the case-folded columns so Cyrillic (and EN) case
        # doesn't matter — SQLite's built-in NOCASE only folds ASCII.
        if not rows:
            for q in self._like_variants(query):
                conds = ["(name_cf LIKE '%' || casefold(?) || '%' OR "
                         "full_name_cf LIKE '%' || casefold(?) || '%' OR "
                         "synonym_cf LIKE '%' || casefold(?) || '%')"]
                p: list = [q, q, q]
                if kind:
                    conds.append("kind = ?")
                    p.append(kind)
                if source_id:
                    conds.append("source_id = ?")
                    p.append(source_id)
                w = ' AND '.join(conds)
                p.extend([limit, offset])
                rows = conn.execute(
                    f"""SELECT full_name, kind, name, synonym, comment, source_id
                        FROM metadata_objects WHERE {w}
                        ORDER BY name LIMIT ? OFFSET ?""", p).fetchall()
                if rows:
                    break

        return self._attach_source_labels([dict(r) for r in rows])

    def get_object_details(self, full_name: str,
                            source_id: str | None = None) -> dict | None:
        """
        Get full details of an object by full_name.

        If ``source_id`` is given, returns that exact source's version.
        If not given and multiple sources hold this object, returns the
        first one (main > extension) AND includes ``matches`` list so
        callers can offer a source picker.
        """
        conn = self.db.read_conn()

        # Find all matching rows across sources (to detect ambiguity)
        matches = conn.execute(
            """SELECT m.*, s.label as source_label, s.source_type
               FROM metadata_objects m
               LEFT JOIN sources s ON s.id = m.source_id
               WHERE m.full_name = ?
               ORDER BY CASE s.source_type WHEN 'main' THEN 0 ELSE 1 END,
                        s.label""",
            (full_name,)).fetchall()

        # LIKE fallback if no exact match (D5: case-insensitive)
        if not matches:
            matches = conn.execute(
                """SELECT m.*, s.label as source_label, s.source_type
                   FROM metadata_objects m
                   LEFT JOIN sources s ON s.id = m.source_id
                   WHERE m.full_name_cf LIKE '%' || casefold(?) || '%'
                   ORDER BY CASE s.source_type WHEN 'main' THEN 0 ELSE 1 END,
                            s.label""",
                (full_name,)).fetchall()

        if not matches:
            return None

        # Pick the requested source, or the first match
        if source_id:
            obj = next((m for m in matches if m['source_id'] == source_id), None)
            if obj is None:
                return None
        else:
            obj = matches[0]

        obj_id = obj['id']

        attrs = conn.execute(
            """SELECT name, kind, type_desc, synonym, comment FROM attributes
               WHERE object_id = ? AND kind NOT IN ('ТабличнаяЧасть', 'ЗначениеПеречисления')
               ORDER BY kind, name""", (obj_id,)).fetchall()

        ts_rows = conn.execute(
            "SELECT name FROM attributes WHERE object_id = ? AND kind = 'ТабличнаяЧасть' ORDER BY name",
            (obj_id,)).fetchall()

        tabular_sections = {}
        for ts in ts_rows:
            ts_name = ts['name']
            ts_attrs = conn.execute(
                """SELECT name, kind, type_desc, synonym FROM attributes
                   WHERE object_id = ? AND name LIKE ? AND kind != 'ТабличнаяЧасть'
                   ORDER BY name""",
                (obj_id, f'ТЧ.{ts_name}.%')).fetchall()
            tabular_sections[ts_name] = [dict(a) for a in ts_attrs]

        forms = conn.execute(
            "SELECT name FROM forms WHERE object_id = ? ORDER BY name", (obj_id,)).fetchall()

        enum_values = conn.execute(
            """SELECT name, synonym FROM attributes
               WHERE object_id = ? AND kind = 'ЗначениеПеречисления' ORDER BY name""",
            (obj_id,)).fetchall()

        module = conn.execute(
            """SELECT name, is_server, is_client, is_global, is_privileged, server_call
               FROM modules WHERE object_id = ?""", (obj_id,)).fetchone()

        obj_dict = dict(obj)
        # Ambiguity info (multiple sources have this full_name)
        other_matches = [
            {'source_id': m['source_id'], 'source_label': m['source_label'] or m['source_id'],
             'source_type': m['source_type']}
            for m in matches
        ]

        return {
            'object': obj_dict,
            'attributes': [dict(a) for a in attrs],
            'tabular_sections': tabular_sections,
            'forms': [dict(f) for f in forms],
            'enum_values': [dict(e) for e in enum_values],
            'module': dict(module) if module else None,
            'source_id': obj['source_id'],
            'source_label': obj['source_label'] or obj['source_id'],
            'matches': other_matches,       # all sources that have this full_name
        }

    def search_attributes(self, query: str, type_filter: str | None = None,
                          source_id: str | None = None, limit: int = 30,
                          offset: int = 0) -> list[dict]:
        """Search attributes by name or type."""
        conn = self.db.read_conn()
        fts_query = self._fts_query(query)

        conds = ["attributes_fts MATCH ?"]
        params: list = [fts_query]
        if source_id:
            conds.append("m.source_id = ?")
            params.append(source_id)
        where = ' AND '.join(conds)
        params.extend([limit, offset])

        rows = conn.execute(
            f"""SELECT a.name, a.kind, a.type_desc, a.synonym,
                      m.full_name as object_full_name, m.source_id
               FROM attributes_fts f
               JOIN attributes a ON a.id = f.rowid
               JOIN metadata_objects m ON m.id = a.object_id
               WHERE {where}
               ORDER BY rank LIMIT ? OFFSET ?""", params).fetchall()

        # LIKE fallback (D5: case-insensitive)
        if not rows:
            conds = ["(a.name_cf LIKE '%' || casefold(?) || '%' OR "
                     "casefold(a.synonym) LIKE '%' || casefold(?) || '%' OR "
                     "casefold(a.type_desc) LIKE '%' || casefold(?) || '%')"]
            p: list = [query, query, query]
            if source_id:
                conds.append("m.source_id = ?")
                p.append(source_id)
            w = ' AND '.join(conds)
            p.extend([limit, offset])
            rows = conn.execute(
                f"""SELECT a.name, a.kind, a.type_desc, a.synonym,
                          m.full_name as object_full_name, m.source_id
                   FROM attributes a
                   JOIN metadata_objects m ON m.id = a.object_id
                   WHERE {w}
                   ORDER BY m.full_name, a.name LIMIT ? OFFSET ?""", p).fetchall()

        results = [dict(r) for r in rows]
        if type_filter:
            results = [r for r in results if type_filter.lower() in (r.get('type_desc') or '').lower()]
        return self._attach_source_labels(results)

    def find_references(self, object_name: str, source_id: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[dict]:
        """Find where an object type is referenced in attribute types."""
        conn = self.db.read_conn()
        # D5: case-insensitive — type_desc isn't materialized into a _cf
        # column (only name/full_name/synonym per Этап 2), so casefold()
        # is applied to the source column directly at query time.
        conds = ["casefold(a.type_desc) LIKE '%' || casefold(?) || '%'"]
        params: list = [object_name]
        if source_id:
            conds.append("m.source_id = ?")
            params.append(source_id)
        w = ' AND '.join(conds)
        # The LIKE above is a substring match, so "Номенклатура" also hits
        # СправочникСсылка.НоменклатураПоставщиков — types naming exactly
        # this object sort first.
        params.extend([object_name, object_name, limit, offset])

        rows = conn.execute(
            f"""SELECT a.name as attr_name, a.kind, a.type_desc,
                      m.full_name as object_full_name, m.source_id
               FROM attributes a JOIN metadata_objects m ON m.id = a.object_id
               WHERE {w}
               ORDER BY CASE WHEN casefold(a.type_desc) LIKE '%.' || casefold(?)
                              OR casefold(a.type_desc) LIKE '%.' || casefold(?) || ',%'
                         THEN 0 ELSE 1 END,
                        m.full_name, a.name LIMIT ? OFFSET ?""", params).fetchall()
        return self._attach_source_labels([dict(r) for r in rows])

    def list_objects(self, kind: str | None = None,
                      source_id: str | None = None, limit: int = 100,
                      offset: int = 0) -> list[dict]:
        """List objects, optionally filtered by kind and/or source.

        ``kind`` accepts the RU canonical name or its EN alias, any case.
        """
        conn = self.db.read_conn()
        conds = []
        params: list = []
        if kind:
            conds.append("kind = ?")
            params.append(canonicalize_kind(kind))
        if source_id:
            conds.append("source_id = ?")
            params.append(source_id)
        where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
        params.extend([limit, offset])
        rows = conn.execute(
            f"""SELECT full_name, kind, name, synonym, comment, source_id
                FROM metadata_objects {where}
                ORDER BY kind, name LIMIT ? OFFSET ?""", params).fetchall()
        return self._attach_source_labels([dict(r) for r in rows])

    def get_index_status(self) -> dict:
        return self.db.get_stats()

    # ============================================
    # CODE SEARCH (Phase 2)
    # ============================================

    def search_procedures(self, query: str, module_filter: str | None = None,
                          export_only: bool = False, source_id: str | None = None,
                          limit: int = 30, offset: int = 0) -> list[dict]:
        """Search procedures/functions by name (D5: case-insensitive)."""
        conn = self.db.read_conn()
        conditions = ["p.name_cf LIKE '%' || casefold(?) || '%'"]
        params: list = [query]

        if module_filter:
            conditions.append("m.name_cf LIKE '%' || casefold(?) || '%'")
            params.append(module_filter)
        if export_only:
            conditions.append("p.is_export = 1")
        if source_id:
            conditions.append("m.source_id = ?")
            params.append(source_id)

        where = ' AND '.join(conditions)
        params.extend([query, limit, offset])

        rows = conn.execute(f"""
            SELECT p.name, p.kind, p.is_export, p.directive, p.start_line, p.end_line,
                   p.params, p.signature, m.name as module_name, m.module_type,
                   m.source_id
            FROM procedures p JOIN modules m ON m.id = p.module_id
            WHERE {where}
            ORDER BY CASE WHEN p.name_cf = casefold(?) THEN 0 ELSE 1 END,
                     m.name, p.name LIMIT ? OFFSET ?""", params).fetchall()
        return self._attach_source_labels([dict(r) for r in rows])

    def get_module_outline(self, module_name: str,
                            source_id: str | None = None) -> dict | None:
        """Get module structure: procedures with directives and line numbers
        (D5: case-insensitive)."""
        conn = self.db.read_conn()
        ids = resolve_module_ids(conn, module_name, source_id)
        if not ids:
            return None
        mod = conn.execute(
            """SELECT m.*, s.label as source_label
               FROM modules m LEFT JOIN sources s ON s.id = m.source_id
               WHERE m.id = ?""", (ids[0],)).fetchone()
        if not mod:
            return None

        procs = conn.execute("""
            SELECT name, kind, is_export, directive, start_line, end_line, params
            FROM procedures WHERE module_id = ? ORDER BY start_line""",
            (mod['id'],)).fetchall()

        return {
            'module': dict(mod),
            'procedures': [dict(p) for p in procs],
            'source_id': mod['source_id'],
            'source_label': mod['source_label'] or mod['source_id'],
        }

    def search_code(self, query: str, file_pattern: str | None = None,
                    source_id: str | None = None, limit: int = 30,
                    offset: int = 0) -> list[dict]:
        """Вхождения query в тексте модулей — см. search_code_limited(); здесь
        только результаты, без признака сработавшего предела (Web UI)."""
        return self.search_code_limited(query, file_pattern=file_pattern, source_id=source_id,
                                        limit=limit, offset=offset)[0]

    def search_code_limited(self, query: str, file_pattern: str | None = None,
                            source_id: str | None = None, limit: int = 30,
                            offset: int = 0) -> tuple[list[dict], bool]:
        """
        Full-text search over the raw source of every indexed BSL module
        (Этап 3 / D6) — finds assignments, query text (SELECT ...),
        comments, anything, not just recognized call sites the way the old
        implementation (grep over calls.context) did.

        Each result is one occurrence: module, matched line number, and
        ±1 line of context, computed from where the match actually sits in
        the module's text (module_text_fts stores the whole file).

        Второй элемент — сработал ли один из пределов SEARCH_CODE_*: тогда
        вхождения за пределами просмотренных модулей (или сверх
        SEARCH_CODE_MAX_PER_MODULE в одном модуле) не показаны.
        """
        conn = self.db.read_conn()
        query = query.strip()
        if not query:
            return [], False

        fts_query = self._fts_query(query)
        conds = ["module_text_fts MATCH ?"]
        params: list = [fts_query]
        if source_id:
            conds.append("m.source_id = ?")
            params.append(source_id)
        if file_pattern:
            conds.append("m.file_path LIKE ?")
            params.append(f'%{file_pattern}%')
        where = ' AND '.join(conds)

        # No "ORDER BY rank": module_text_fts uses detail=none (size
        # contingency — see db.py), which doesn't support bm25()/rank.
        # На одну строку больше предела — чтобы знать, что он сработал.
        cap = SEARCH_CODE_FTS_MODULES
        rows = conn.execute(f"""
            SELECT f.rowid as module_id, m.name as module_name,
                   m.file_path, m.abs_path, m.source_id
            FROM module_text_fts f
            JOIN modules m ON m.id = f.rowid
            WHERE {where}
            ORDER BY m.name LIMIT ?""", params + [cap + 1]).fetchall()
        fts_capped = len(rows) > cap

        results, per_module_capped = self._extract_code_matches(rows[:cap], query, limit, offset=offset)
        truncated = fts_capped or per_module_capped

        # Fallback (D5: case-insensitive) — covers queries FTS5's unicode61
        # tokenizer won't prefix-match (e.g. leading punctuation). No stored
        # text to LIKE against (contentless table), so this scans the
        # on-disk file of every candidate module instead — capped, since
        # it's a real per-file read, not a SQL scan.
        if not results:
            conds = ["file_path != ''"]
            p: list = []
            if source_id:
                conds.append("source_id = ?")
                p.append(source_id)
            if file_pattern:
                conds.append("file_path LIKE ?")
                p.append(f'%{file_pattern}%')
            w = ' AND '.join(conds)
            cap = SEARCH_CODE_FALLBACK_MODULES
            rows = conn.execute(f"""
                SELECT id as module_id, name as module_name, file_path, abs_path, source_id
                FROM modules WHERE {w} LIMIT ?""", p + [cap + 1]).fetchall()
            results, per_module_capped = self._extract_code_matches(
                rows[:cap], query, limit, offset=offset)
            fallback_truncated = len(rows) > cap or per_module_capped
            # Пустой ответ при сработавшем пределе любого из путей — тоже
            # неполный: вхождение может быть в непросмотренном модуле.
            truncated = fallback_truncated if results else (truncated or fallback_truncated)

        return self._attach_source_labels(results), truncated

    def _extract_code_matches(self, rows, query: str, limit: int,
                               max_per_module: int | None = None,
                               offset: int = 0) -> tuple[list[dict], bool]:
        """Locate each occurrence of ``query`` (case-insensitive) inside the
        matched modules' text and build a module:line + ±1 line context
        result for it — the line number is computed from the match's
        character offset, per Этап 3's spec. module_text_fts is contentless
        (D6 size contingency), so the text is re-read from disk here rather
        than fetched from the DB.

        ``offset`` skips that many matches (across all candidate modules,
        in the same scan order) before collecting into ``results`` —
        there's no SQL LIMIT/OFFSET here since matches are found by
        scanning file text, not a query.

        Второй элемент — в каком-то модуле вхождений больше max_per_module
        (по умолчанию SEARCH_CODE_MAX_PER_MODULE) и лишние пропущены.
        """
        from .bsl_parser import read_bsl_text

        if max_per_module is None:
            max_per_module = SEARCH_CODE_MAX_PER_MODULE
        needle = query.casefold()
        if not needle:
            return [], False

        results = []
        skipped = 0
        capped = False
        for row in rows:
            path = self.module_file_path(row)
            content = read_bsl_text(path) if path else None
            if content is None:
                continue
            hay = content.casefold()
            lines = content.split('\n')

            start = 0
            hits = 0
            while hits < max_per_module and len(results) < limit:
                idx = hay.find(needle, start)
                if idx == -1:
                    break
                hits += 1
                start = idx + len(needle)
                if skipped < offset:
                    skipped += 1
                    continue
                line_no = content.count('\n', 0, idx) + 1
                line_idx = line_no - 1
                ctx = '\n'.join(lines[max(0, line_idx - 1):min(len(lines), line_idx + 2)])
                results.append({
                    'module_id': row['module_id'],
                    'module_name': row['module_name'],
                    'file_path': row['file_path'],
                    'source_id': row['source_id'],
                    'line': line_no,
                    'context': ctx,
                })
            if hits >= max_per_module and hay.find(needle, start) != -1:
                capped = True
            if len(results) >= limit:
                break
        return results, capped

    # ============================================
    # FTS HELPERS
    # ============================================

    def _fts_query(self, query: str) -> str:
        """Prepare FTS5 query with prefix matching."""
        terms = query.strip().split()
        if not terms:
            return '""'
        safe = []
        for t in terms:
            t = re.sub(r'["\'\(\)\*\-\+\~\^]', '', t)
            if t:
                safe.append(f'{t}*')
        return ' '.join(safe) if safe else '""'

    def _like_variants(self, query: str) -> list[str]:
        """
        Generate progressive-prefix fallback variants for LIKE search.

        When FTS5 returns no hits (e.g. unusual punctuation or the index
        tokenizer didn't match the user's morphology), we retry with
        progressively shorter prefixes, wrapping each in ``%...%`` for LIKE.

        Works for both English and Russian — the shortening is a raw
        character-by-character trim that doesn't depend on a locale.
        It matters more for Russian because inflection changes word
        endings (запись → записи → записью), but the same mechanism
        rescues rare English misses too. Stops at 2 chars minimum.

        Example: "Поле" → ["Поле", "Пол", "По"]
                 "Account" → ["Account", "Accoun", "Accou", "Acco", "Acc", "Ac"]
        """
        query = query.strip()
        variants = [query]
        while len(query) > 2:
            query = query[:-1]
            variants.append(query)
        return variants


# ============================================
# FORMATTERS (compact text for MCP responses)
# ============================================

def _src_suffix(row: dict) -> str:
    lbl = row.get('source_label') or row.get('source_id')
    return f"  @{lbl}" if lbl else ''


def format_search_results(results: list[dict], max_items: int = 20, start_index: int = 1) -> str:
    if not results:
        return "No results found."
    lines = [f"Found {len(results)} result(s):"]
    for i, r in enumerate(results[:max_items], start_index):
        full_name = r.get('full_name', r.get('object_full_name', ''))
        synonym = r.get('synonym', '')
        name = r.get('name', '') or r.get('attr_name', '')
        if 'object_full_name' in r and name:
            # Attribute rows (search_attributes / find_references): without
            # the attribute name two hits on one object print identically.
            full_name = f"{full_name}.{name}"
        type_desc = r.get('type_desc', '')
        comment = r.get('comment', '')
        parts = [f"{i}. {full_name}"]
        if synonym and synonym != name:
            parts.append(f"({synonym})")
        if type_desc:
            parts.append(f"[{type_desc}]")
        if comment:
            parts.append(f"— {comment[:80]}")
        parts.append(_src_suffix(r))
        lines.append(' '.join(p for p in parts if p))
    if len(results) > max_items:
        lines.append(f"... and {len(results) - max_items} more")
    return '\n'.join(lines)


def format_object_details(details: dict, detail: str = 'brief') -> str:
    """
    ``detail``: 'brief' (default, Этап 6 token budget) shows counts + the
    first 30 attributes + tabular section names without their column
    contents; 'full' shows everything.
    """
    if not details:
        return "Object not found."
    obj = details['object']
    src = details.get('source_label') or details.get('source_id') or ''
    title_suffix = f"  @{src}" if src else ''
    lines = [f"# {obj['full_name']}{title_suffix}",
             f"Синоним: {obj.get('synonym', '')}"]
    if obj.get('comment'):
        lines.append(f"Комментарий: {obj['comment']}")

    # Ambiguity warning
    matches = details.get('matches', [])
    if len(matches) > 1:
        other = [m['source_label'] for m in matches if m['source_id'] != details.get('source_id')]
        if other:
            lines.append(f"⚠ Этот объект есть также в источниках: {', '.join(other)}")

    if details.get('module'):
        m = details['module']
        flags = []
        if m.get('is_server'): flags.append('Сервер')
        if m.get('is_client'): flags.append('Клиент')
        if m.get('is_global'): flags.append('Глобальный')
        if m.get('is_privileged'): flags.append('Привилегированный')
        if m.get('server_call'): flags.append('ВызовСервера')
        if flags:
            lines.append(f"Флаги: {', '.join(flags)}")
    attrs = details.get('attributes', [])
    if attrs:
        shown_attrs = attrs[:30] if detail == 'brief' else attrs
        lines.append(f"\nРеквизиты ({len(attrs)}):")
        for a in shown_attrs:
            kind_label = f"[{a['kind']}] " if a['kind'] != 'Реквизит' else ''
            type_info = f" : {a['type_desc']}" if a.get('type_desc') else ''
            lines.append(f"  {kind_label}{a['name']}{type_info}")
        if len(shown_attrs) < len(attrs):
            lines.append(f"  ... and {len(attrs) - len(shown_attrs)} more (detail=\"full\" for all)")
    ts = details.get('tabular_sections', {})
    if ts:
        lines.append(f"\nТабличные части ({len(ts)}):")
        for ts_name, ts_attrs in ts.items():
            if detail == 'brief':
                lines.append(f"  {ts_name} ({len(ts_attrs)} реквизитов, detail=\"full\" для состава)")
                continue
            lines.append(f"  {ts_name}:")
            for a in ts_attrs:
                clean = a['name'].replace(f'ТЧ.{ts_name}.', '')
                type_info = f" : {a['type_desc']}" if a.get('type_desc') else ''
                lines.append(f"    {clean}{type_info}")
    forms = details.get('forms', [])
    if forms:
        lines.append(f"\nФормы ({len(forms)}):")
        for f in forms:
            lines.append(f"  {f['name']}")
    evs = details.get('enum_values', [])
    if evs:
        lines.append(f"\nЗначения ({len(evs)}):")
        for e in evs:
            syn = f" ({e['synonym']})" if e.get('synonym') and e['synonym'] != e['name'] else ''
            lines.append(f"  {e['name']}{syn}")
    return '\n'.join(lines)


# ============================================
# DIAGNOSTICS
# ============================================

_UNRESOLVED_GROUPS = ('module_missing', 'method_missing', 'other')


def _module_short_name(name_cf: str) -> str:
    """Имя объекта из имени модуля: 'общиймодуль.x.модуль' / 'справочник.x.…'
    → 'x'; голое имя общего модуля (строка из отчёта) — само имя."""
    parts = name_cf.split('.')
    return parts[1] if len(parts) > 1 else parts[0]


def _unresolved_breakdown(conn, top_limit: int = 20, hints_limit: int = 3) -> dict:
    """
    Нерезолвленные вызовы common_module/manager по причинам:
      module_missing — модуля с кодом нет в индексе и имя не совпадает ни с
                       одним объектом индекса (внешняя БСП, не выгруженная
                       подсистема);
      method_missing — модуль есть, процедуры с таким именем в нём нет;
                       defined_in подсказывает, в каких ДРУГИХ модулях она есть;
      other          — всё остальное, с причиной reason: 'resolvable' (и
                       модуль, и процедура есть — устаревший calls_resolved
                       или баг резолвера), 'name_matches' (имя совпадает с
                       объектом другого вида — вероятно, переменная, которую
                       парсер принял за модуль), 'no_module_name'.

    Модуль ищется тем же ключом, что в Indexer.resolve_calls (по всем
    источникам, без учёта регистра), иначе группы расходились бы с тем, что
    резолвер реально мог найти. «Есть» = есть хотя бы одна процедура: строка
    modules из report.txt без BSL-кода для резолвера всё равно пуста.
    """
    # name_cf модуля -> {name_cf процедуры}; только модули с кодом.
    module_procs: dict[str, set[str]] = {}
    # name_cf процедуры -> [(имя модуля, source_id)] — для подсказок.
    proc_homes: dict[str, list[tuple[str, str]]] = {}
    short_names: set[str] = set()
    for r in conn.execute(
            """SELECT m.name AS mname, m.name_cf AS mcf, m.source_id AS src,
                      p.name AS pname, p.name_cf AS pcf
               FROM procedures p JOIN modules m ON m.id = p.module_id"""):
        mcf = r['mcf'] or r['mname'].casefold()
        pcf = r['pcf'] or r['pname'].casefold()
        module_procs.setdefault(mcf, set()).add(pcf)
        if '.' not in mcf:
            module_procs.setdefault(f'общиймодуль.{mcf}.модуль', set()).add(pcf)
        proc_homes.setdefault(pcf, []).append((r['mname'], r['src'] or ''))
        short_names.add(_module_short_name(mcf))

    groups = {g: {'count': 0, 'items': []} for g in _UNRESOLVED_GROUPS}
    total = 0
    for r in conn.execute("""
            SELECT c.callee_kind AS kind, c.callee_module AS module,
                   c.callee_proc AS name, COUNT(*) AS freq
            FROM calls c
            LEFT JOIN calls_resolved cr ON cr.call_id = c.id
            WHERE cr.call_id IS NULL AND c.callee_kind IN ('common_module', 'manager')
            GROUP BY c.callee_kind, c.callee_module, c.callee_proc"""):
        module_cf = (r['module'] or '').casefold()
        proc_cf = (r['name'] or '').casefold()
        key = f'общиймодуль.{module_cf}.модуль' if r['kind'] == 'common_module' else module_cf
        item = {'kind': r['kind'], 'module': r['module'] or '', 'name': r['name'] or '',
                'freq': r['freq']}
        procs = module_procs.get(key)
        if not module_cf:
            group, item['reason'] = 'other', 'no_module_name'
        elif procs is not None and proc_cf in procs:
            group, item['reason'] = 'other', 'resolvable'
        elif procs is not None:
            group = 'method_missing'
            homes = sorted(set(proc_homes.get(proc_cf, [])))
            item['defined_in'] = [{'module': m, 'source_id': src}
                                  for m, src in homes[:hints_limit]]
        elif _module_short_name(module_cf) in short_names:
            group, item['reason'] = 'other', 'name_matches'
        else:
            group = 'module_missing'
        total += r['freq']
        groups[group]['count'] += r['freq']
        groups[group]['items'].append(item)

    result = {'total': total}
    for g, data in groups.items():
        data['items'].sort(key=lambda i: (-i['freq'], i['module'], i['name']))
        result[g] = {
            'count': data['count'],
            'pct': round(100.0 * data['count'] / total, 1) if total else 0.0,
            'top': data['items'][:top_limit],
        }
    return result


def call_graph_resolution_stats(conn, worst_modules_limit: int = 20,
                                 unresolved_names_limit: int = 20,
                                 min_calls_for_module: int = 5) -> dict:
    """
    Call-graph resolution quality metrics (Этап 7 IMPROVEMENT_PLAN.md):
    what fraction of `calls` edges actually resolved to a procedure in
    `calls_resolved`, broken down overall / by callee_kind / by the
    worst-resolving caller modules — plus the most frequent unresolved
    callee names (candidates for the platform stoplist, or a resolver
    bug) and how many procedures are &Вместо/&Around-style interceptors.
    """
    total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    resolved = conn.execute("SELECT COUNT(DISTINCT call_id) FROM calls_resolved").fetchone()[0]

    by_kind_rows = conn.execute("""
        SELECT c.callee_kind as kind, COUNT(*) as total,
               COUNT(cr.call_id) as resolved
        FROM calls c
        LEFT JOIN calls_resolved cr ON cr.call_id = c.id
        GROUP BY c.callee_kind
        ORDER BY total DESC
    """).fetchall()
    by_kind = {}
    for r in by_kind_rows:
        t, res = r['total'], r['resolved']
        by_kind[r['kind'] or '(unknown)'] = {
            'total': t, 'resolved': res,
            'pct': round(100.0 * res / t, 1) if t else 0.0,
        }

    worst_rows = conn.execute("""
        SELECT m.name as module_name, m.source_id,
               COUNT(*) as total, COUNT(cr.call_id) as resolved
        FROM calls c
        JOIN procedures p ON p.id = c.caller_id
        JOIN modules m ON m.id = p.module_id
        LEFT JOIN calls_resolved cr ON cr.call_id = c.id
        GROUP BY m.id
        HAVING total >= ?
        ORDER BY (CAST(resolved AS REAL) / total) ASC, total DESC
        LIMIT ?
    """, (min_calls_for_module, worst_modules_limit)).fetchall()
    worst_modules = [
        {
            'module': r['module_name'], 'source_id': r['source_id'],
            'total': r['total'], 'resolved': r['resolved'],
            'pct': round(100.0 * r['resolved'] / r['total'], 1) if r['total'] else 0.0,
        }
        for r in worst_rows
    ]

    unresolved_rows = conn.execute("""
        SELECT c.callee_module as module, c.callee_proc as name, COUNT(*) as freq
        FROM calls c
        LEFT JOIN calls_resolved cr ON cr.call_id = c.id
        WHERE cr.call_id IS NULL
        GROUP BY c.callee_module, c.callee_proc
        ORDER BY freq DESC
        LIMIT ?
    """, (unresolved_names_limit,)).fetchall()
    top_unresolved = [
        {'module': r['module'], 'name': r['name'], 'freq': r['freq']}
        for r in unresolved_rows
    ]

    override_count = conn.execute(
        "SELECT COUNT(DISTINCT caller_id) FROM calls WHERE callee_kind = 'override'"
    ).fetchone()[0]

    return {
        'total_calls': total,
        'resolved_calls': resolved,
        'resolved_pct': round(100.0 * resolved / total, 1) if total else 0.0,
        'resolved_by_kind': by_kind,
        'worst_modules': worst_modules,
        'top_unresolved': top_unresolved,
        'override_procedures_count': override_count,
        'unresolved_breakdown': _unresolved_breakdown(conn, unresolved_names_limit),
    }


def diagnose_index(project_manager, project_id: str) -> dict:
    """
    Compare BSL files on disk with indexed procedures.
    Finds procedures present in source but missing from index.
    Uses a greedy regex (more permissive than the parser) to detect mismatches.

    Also includes call-graph resolution metrics (Этап 7) — see
    call_graph_resolution_stats().
    """
    import re
    from pathlib import Path
    from .xml_walker import XMLWalker

    pm = project_manager
    project = pm.get_project(project_id)
    db = pm.get_db(project_id)
    conn = db.read_conn()  # read-only (Этап 4/D7)

    # Greedy regex: catches more than the parser (no $ anchor, allows ; and whitespace)
    RE_GREEDY = re.compile(
        r'^\s*(Процедура|Функция|Procedure|Function)\s+'
        r'([А-Яа-яA-Za-z0-9_]+)\s*\(',
        re.IGNORECASE | re.MULTILINE
    )

    # 1. Scan all BSL files with greedy regex
    file_procs = {}  # {(module_full_name, proc_name): info}
    walker = XMLWalker()

    for source in project.sources:
        if not source.xml_path:
            continue
        xml_path = pm.projects_dir / project_id / source.xml_path
        if not xml_path.exists():
            continue

        for mf in walker.walk(str(xml_path)):
            try:
                content = Path(mf.file_path).read_text(encoding='utf-8-sig')
            except Exception:
                try:
                    content = Path(mf.file_path).read_text(encoding='cp1251')
                except Exception:
                    continue

            for i, line in enumerate(content.replace('\r\n', '\n').split('\n'), 1):
                m = RE_GREEDY.match(line)
                if m:
                    proc_name = m.group(2)
                    key = (mf.full_name, proc_name)
                    file_procs[key] = {
                        'name': proc_name,
                        'module': mf.full_name,
                        'file': mf.relative_path,
                        'line': i,
                        'signature': line.strip()[:150],
                    }

    # 2. Get all indexed procedures
    indexed = {}
    rows = conn.execute("""
        SELECT p.name, m.name as module_name
        FROM procedures p JOIN modules m ON m.id = p.module_id
    """).fetchall()
    for r in rows:
        key = (r['module_name'], r['name'])
        indexed[key] = {'name': r['name'], 'module': r['module_name']}

    # 3. Build normalized lookup for fuzzy module matching
    # DB may store "ОбщиеНаСервере" while walker gives "ОбщийМодуль.ОбщиеНаСервере.Модуль"
    indexed_by_proc = {}  # proc_name → set of module name fragments
    for (mod, proc), info in indexed.items():
        indexed_by_proc.setdefault(proc, set()).add(mod)

    file_by_proc = {}
    for (mod, proc), info in file_procs.items():
        file_by_proc.setdefault(proc, set()).add(mod)

    def modules_match(file_mod: str, db_mods: set) -> bool:
        """Check if file module matches any DB module (fuzzy)."""
        for db_mod in db_mods:
            if db_mod == file_mod:
                return True
            # "ОбщиеНаСервере" matches "ОбщийМодуль.ОбщиеНаСервере.Модуль"
            if db_mod in file_mod or file_mod in db_mod:
                return True
        return False

    # 4. Compare
    missing = []
    for (file_mod, proc_name), info in sorted(file_procs.items()):
        if proc_name in indexed_by_proc and modules_match(file_mod, indexed_by_proc[proc_name]):
            continue
        missing.append(info)

    extra = []
    for (db_mod, proc_name), info in sorted(indexed.items()):
        if proc_name in file_by_proc and modules_match(db_mod, {m for m in file_by_proc[proc_name]}):
            continue
        # Skip procedures from report-only modules (no BSL file)
        extra.append(info)

    return {
        'total_in_files': len(file_procs),
        'total_indexed': len(indexed),
        'missing_count': len(missing),
        'missing': missing,
        'extra_count': len(extra),
        'extra': extra,
        'graph': call_graph_resolution_stats(conn),
        'parser': _parser_state_report(db, conn, project),
    }


def _parser_state_report(db, conn, project) -> dict:
    """Каким отпечатком парсера разобран каждый источник — чтобы было видно,
    что после правки парсера индекс ещё не переразобран (или уже был)."""
    from .parser_fingerprint import parser_fingerprint
    current = parser_fingerprint()
    states = db.get_parser_states(conn)
    sources = {}
    for s in project.sources:
        st = states.get(s.id) or {}
        sources[s.id] = {
            'fingerprint': st.get('fingerprint', ''),
            'up_to_date': bool(current) and st.get('fingerprint') == current,
            'updated_at': st.get('updated_at', ''),
            'last_full_reparse_at': st.get('last_full_reparse_at', ''),
            'last_full_reparse_reason': st.get('last_full_reparse_reason', ''),
        }
    return {'current': current, 'sources': sources}
