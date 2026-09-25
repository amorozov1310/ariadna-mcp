"""
Indexer: parses a configuration report and populates per-project SQLite.
Each call indexes one source (report.txt) into the project's DB.
"""

import time
from pathlib import Path
from datetime import datetime

from .db import Database
from .report_parser import parse_report, MetadataObject


class Indexer:
    """Indexes one source at a time into a per-project SQLite DB."""

    def __init__(self, db: Database):
        self.db = db

    def index_report(self, report_path: str, source_id: str = 'main',
                     source_label: str = '', source_type: str = 'main') -> dict:
        """
        Parse configuration report and populate DB.
        
        Args:
            report_path: path to report.txt
            source_id: unique source identifier within project
            source_label: human-readable label
            source_type: 'main' or 'extension'
        
        Returns: stats dict
        """
        start = time.time()
        conn = self.db.conn
        if not conn:
            self.db.connect()
            conn = self.db.conn

        # Register source
        conn.execute(
            """INSERT OR REPLACE INTO sources (id, label, source_type, indexed_at)
               VALUES (?, ?, ?, ?)""",
            (source_id, source_label or source_id, source_type, datetime.now().isoformat())
        )

        # Parse report
        config, objects = parse_report(report_path)

        # Update source with config info
        conn.execute(
            "UPDATE sources SET config_name=?, config_version=? WHERE id=?",
            (config.name, config.version, source_id)
        )

        stats = {
            'objects': 0, 'attributes': 0, 'forms': 0,
            'modules': 0, 'enum_values': 0, 'tabular_sections': 0,
            'subsystem_items': 0,
        }

        for obj in objects:
            obj_id = self._insert_object(conn, obj, source_id)
            if not obj_id:
                continue
            stats['objects'] += 1

            # Этап 4/D7: obj_id may be a pre-existing row (UPSERT above),
            # in which case its attributes/forms/subsystem_content are
            # last run's — clear them before reinserting fresh ones. No-op
            # (nothing to delete) on a brand-new object.
            conn.execute("DELETE FROM attributes WHERE object_id=?", (obj_id,))
            conn.execute("DELETE FROM forms WHERE object_id=?", (obj_id,))
            conn.execute("DELETE FROM subsystem_content WHERE subsystem_id=?", (obj_id,))

            # Attributes
            for attr in obj.attributes:
                self._insert_attribute(conn, obj_id, attr)
                stats['attributes'] += 1

            # Tabular sections
            for ts_name, ts_attrs in obj.tabular_sections.items():
                self._insert_attribute_raw(conn, obj_id, ts_name, 'ТабличнаяЧасть')
                stats['tabular_sections'] += 1
                for ts_attr in ts_attrs:
                    self._insert_attribute(conn, obj_id, ts_attr, prefix=f"ТЧ.{ts_name}")
                    stats['attributes'] += 1

            # Forms
            for form in obj.forms:
                conn.execute("INSERT INTO forms (object_id, name) VALUES (?, ?)",
                             (obj_id, form.name))
                stats['forms'] += 1

            # Enum values
            for ev in obj.enum_values:
                self._insert_attribute_raw(
                    conn, obj_id, ev.name, 'ЗначениеПеречисления',
                    synonym=ev.synonym, comment=ev.comment)
                stats['enum_values'] += 1

            # Common module → modules table. name uses the same fully
            # dotted form index_bsl uses ("ОбщийМодуль.X.Модуль", not the
            # bare "X") so the two indexing passes agree on one identity
            # per module — see modules' UNIQUE(source_id, name) — and a
            # rerun UPSERTs onto the existing row instead of duplicating it
            # or leaving index_bsl unable to find it (Этап 4/D7).
            if obj.module_info:
                mi = obj.module_info
                dotted_name = f'ОбщийМодуль.{mi.name}.Модуль'
                conn.execute(
                    """INSERT INTO modules (source_id, object_id, name, module_type,
                       is_server, is_client, is_global, is_external, is_privileged, server_call,
                       name_cf)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_id, name) DO UPDATE SET
                           object_id=excluded.object_id, is_server=excluded.is_server,
                           is_client=excluded.is_client, is_global=excluded.is_global,
                           is_external=excluded.is_external, is_privileged=excluded.is_privileged,
                           server_call=excluded.server_call""",
                    (source_id, obj_id, dotted_name, 'Модуль',
                     mi.is_server, mi.is_client, mi.is_global,
                     mi.is_external, mi.is_privileged, mi.server_call,
                     dotted_name.casefold()))
                stats['modules'] += 1

            # Subsystem content
            if obj.subsystem_info and obj.subsystem_info.content:
                for item in obj.subsystem_info.content:
                    conn.execute(
                        "INSERT INTO subsystem_content (subsystem_id, content_name) VALUES (?, ?)",
                        (obj_id, item))
                    stats['subsystem_items'] += 1

        # Этап 4/D7: objects that existed for this source before this run
        # but aren't in the freshly parsed report anymore (renamed/removed)
        # — the UPSERT loop above never touches them, so clean them up
        # explicitly. Cascades to their attributes/forms/subsystem_content
        # and to any modules.object_id pointing at them.
        new_full_names = {obj.full_name for obj in objects}
        stale_ids = [
            r['id'] for r in conn.execute(
                "SELECT id, full_name FROM metadata_objects WHERE source_id=?", (source_id,))
            if r['full_name'] not in new_full_names
        ]
        if stale_ids:
            placeholders = ','.join('?' * len(stale_ids))
            conn.execute(f"DELETE FROM metadata_objects WHERE id IN ({placeholders})", stale_ids)

        conn.commit()

        duration = round(time.time() - start, 3)
        stats['duration_sec'] = duration
        stats['config_name'] = config.name
        stats['config_version'] = config.version

        self.db.update_state(status='ready', index_duration_sec=duration)
        return stats

    def _insert_object(self, conn, obj: MetadataObject, source_id: str) -> int | None:
        try:
            # Этап 4/D7: UPSERT instead of a plain INSERT, keyed on the
            # existing UNIQUE(full_name, source_id) — a rerun keeps the same
            # id for an object that's still there, so modules.object_id's
            # FK never has to CASCADE-delete a module (and its procedures/
            # calls) just because report.txt got re-parsed. See mo_au for
            # the matching metadata_fts sync trigger.
            cur = conn.execute(
                """INSERT INTO metadata_objects (source_id, name, full_name, kind, synonym, comment,
                   name_cf, full_name_cf, synonym_cf)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(full_name, source_id) DO UPDATE SET
                       name=excluded.name, kind=excluded.kind, synonym=excluded.synonym,
                       comment=excluded.comment, name_cf=excluded.name_cf,
                       full_name_cf=excluded.full_name_cf, synonym_cf=excluded.synonym_cf
                   RETURNING id""",
                (source_id, obj.name, obj.full_name, obj.kind, obj.synonym, obj.comment,
                 obj.name.casefold(), obj.full_name.casefold(), (obj.synonym or '').casefold()))
            return cur.fetchone()[0]
        except Exception:
            return None

    def _insert_attribute(self, conn, object_id: int, attr, prefix: str = '') -> int | None:
        name = f"{prefix}.{attr.name}" if prefix else attr.name
        try:
            cur = conn.execute(
                """INSERT INTO attributes (object_id, name, kind, type_desc, synonym, comment,
                   is_indexed, check_fill, name_cf)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (object_id, name, attr.kind, getattr(attr, 'type_desc', ''),
                 getattr(attr, 'synonym', ''), getattr(attr, 'comment', ''),
                 getattr(attr, 'is_indexed', False), getattr(attr, 'check_fill', ''),
                 name.casefold()))
            return cur.lastrowid
        except Exception:
            return None

    def _insert_attribute_raw(self, conn, object_id: int, name: str, kind: str,
                               type_desc: str = '', synonym: str = '', comment: str = ''):
        try:
            conn.execute(
                """INSERT INTO attributes (object_id, name, kind, type_desc, synonym, comment, name_cf)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (object_id, name, kind, type_desc, synonym, comment, name.casefold()))
        except Exception:
            pass

    # ============================================
    # BSL CODE INDEXING (Phase 2)
    # ============================================

    @staticmethod
    def scan_known_names(xml_paths: list[str]) -> tuple[set[str], dict[str, set[str]]]:
        """
        Pass 1 of the two-pass BSL indexer (project-wide): walk every
        source's XML dump — directory structure only, no BSL parsing — and
        collect the facts Pass 2 classifies calls against:
          - known_modules: lower-cased short names of all ОбщийМодуль objects
          - known_objects: {kind: {lower-cased object_name, ...}} for the
            rest (Catalogs, Documents, ...), used to validate manager calls
        This must span ALL sources of a project (main + extensions) since a
        call in one source can target a common module defined in another.
        """
        from .xml_walker import XMLWalker
        walker = XMLWalker()
        known_modules: set[str] = set()
        known_objects: dict[str, set[str]] = {}
        for xp in xml_paths:
            for mf in walker.walk(xp):
                if mf.object_kind == 'ОбщийМодуль':
                    known_modules.add(mf.object_name.lower())
                elif mf.object_kind != 'Конфигурация':
                    known_objects.setdefault(mf.object_kind, set()).add(mf.object_name.lower())
        return known_modules, known_objects

    @staticmethod
    def scan_known_factory_functions(common_module_files: list) -> dict[str, set[str]]:
        """
        Pass 1.5 of the two-pass BSL indexer (project-wide, Этап 7 cross-
        module follow-up): fully parses every common module's own .bsl
        file (not just its name, unlike scan_known_names) to learn which
        of its exported functions are "factory functions" — return a
        value they built themselves (see ParseResult.factory_functions /
        BSLParser's _RE_RETURN_NEW docstring) — so Pass 2 can recognize
        `Var = ОбщегоНазначения.НовыйПустойЛист()` as constructing an
        object anywhere in the project, not just within the same module.

        Takes ``common_module_files`` — a list of ModuleFile already
        filtered to object_kind == 'ОбщийМодуль' (the caller already has
        these from its own scan_known_names-equivalent walk; re-walking
        here would just duplicate that I/O). This is a real extra cost —
        it parses every common module's code a second time, once here and
        once for real in index_bsl's Pass 2 — accepted deliberately for
        the cross-module accuracy gain (see IMPROVEMENT_PLAN Этап 7).

        A module name defined in more than one source (e.g. an extension
        of the same name) has its factory functions unioned across all of
        them — a superset is harmless here since this is only ever used
        to *recognize* an assignment as constructing an object, never to
        resolve a call target.
        """
        from .bsl_parser import BSLParser

        result: dict[str, set[str]] = {}
        parser = BSLParser()
        for mf in common_module_files:
            parsed = parser.parse_file(mf.file_path)
            if parsed.has_errors or not parsed.factory_functions:
                continue
            result.setdefault(mf.object_name.lower(), set()).update(parsed.factory_functions)
        return result

    def index_bsl(self, xml_path: str, source_id: str = 'main',
                  known_modules: set[str] | None = None,
                  known_objects: dict[str, set[str]] | None = None,
                  known_factory_functions: dict[str, set[str]] | None = None,
                  source_label: str = '', source_type: str = 'main',
                  progress_cb=None, commit_every: int = 200) -> dict:
        """
        Index BSL code from XML config dump.
        Walks the XML directory, parses .bsl files, populates modules/procedures/calls.

        known_modules/known_objects are the project-wide Pass 1 result
        (see scan_known_names). When not supplied — e.g. a standalone
        single-source call — this source's own directory listing is used
        as a self-contained fallback, so calls within it still classify
        correctly; cross-source calls just won't be known yet.

        known_factory_functions is the project-wide Pass 1.5 result (see
        scan_known_factory_functions) — optional, cross-module factory-
        function recognition just doesn't happen without it.

        Этап 4/D7 incrementality: a file whose content hash matches what's
        already stored for that module is skipped entirely — not reparsed,
        its procedures/calls/module_text_fts row untouched. Everything is
        looked up and UPSERTed by (source_id, name) — modules' UNIQUE
        constraint — so this also replaces the old object_id-only special
        case for common modules with one mechanism that covers every kind.
        progress_cb(current, total), when given, is called periodically
        (every `commit_every` files) — each call coincides with a commit,
        satisfying "one transaction per file-batch" for free.
        """
        import hashlib
        from .xml_walker import XMLWalker
        from .bsl_parser import BSLParser

        start = time.time()
        conn = self.db.conn
        if not conn:
            self.db.connect()
            conn = self.db.conn

        # Register source if index_report() hasn't already (e.g. no report.txt
        # yet — XML-only source). modules.source_id has a FK on sources(id).
        # source_type matters beyond bookkeeping: resolve_calls() prefers
        # 'extension' sources when the same module/procedure exists in more
        # than one source, so an XML-only extension must register as such,
        # not silently fall back to 'main'.
        conn.execute(
            "INSERT OR IGNORE INTO sources (id, label, source_type) VALUES (?, ?, ?)",
            (source_id, source_label or source_id, source_type)
        )

        walker = XMLWalker()
        module_files = walker.walk(xml_path)
        total_files = len(module_files)

        if known_modules is None or known_objects is None:
            self_modules: set[str] = set()
            self_objects: dict[str, set[str]] = {}
            for mf in module_files:
                if mf.object_kind == 'ОбщийМодуль':
                    self_modules.add(mf.object_name.lower())
                elif mf.object_kind != 'Конфигурация':
                    self_objects.setdefault(mf.object_kind, set()).add(mf.object_name.lower())
            if known_modules is None:
                known_modules = self_modules
            if known_objects is None:
                known_objects = self_objects

        parser = BSLParser(known_modules=known_modules, known_objects=known_objects,
                           known_factory_functions=known_factory_functions)

        # Existing modules for this source, keyed by name (the UNIQUE
        # (source_id, name) identity) — lets every file be looked up once
        # instead of a per-file query, and is what makes the hash-skip
        # possible before any parsing happens.
        existing_modules = {
            r['name']: dict(r) for r in conn.execute(
                "SELECT id, object_id, file_hash, name FROM modules WHERE source_id=?", (source_id,))
        }
        seen_names: set[str] = set()

        stats = {'modules': 0, 'procedures': 0, 'calls': 0, 'lines': 0,
                  'files': 0, 'files_skipped': 0}

        for i, mf in enumerate(module_files):
            seen_names.add(mf.full_name)

            # Hash first, before any parsing — an unchanged file costs one
            # file read instead of a full parse + a batch of DB writes.
            try:
                file_hash = hashlib.md5(Path(mf.file_path).read_bytes()).hexdigest()
            except Exception:
                file_hash = ''

            prior = existing_modules.get(mf.full_name)
            if prior is not None and file_hash and prior['file_hash'] == file_hash:
                stats['files_skipped'] += 1
            else:
                result = parser.parse_file(mf.file_path)
                if not result.has_errors:
                    stats['files'] += 1
                    stats['lines'] += result.line_count

                    # Metadata link (for common-module flags, and so the FK
                    # stays populated) — kept if already set, else looked up
                    # by the metadata object's own full_name.
                    object_id = prior['object_id'] if prior else None
                    if object_id is None and mf.object_kind not in ('Конфигурация',):
                        meta_full_name = f'{mf.object_kind}.{mf.object_name}'
                        row = conn.execute(
                            "SELECT id FROM metadata_objects WHERE full_name = ? AND source_id = ? LIMIT 1",
                            (meta_full_name, source_id)).fetchone()
                        if row:
                            object_id = row['id']

                    is_server = is_client = is_global = is_privileged = server_call = False
                    if object_id and mf.object_kind == 'ОбщийМодуль':
                        mod_row = conn.execute(
                            "SELECT is_server, is_client, is_global, is_privileged, server_call "
                            "FROM modules WHERE object_id = ?", (object_id,)).fetchone()
                        if mod_row:
                            is_server = bool(mod_row['is_server'])
                            is_client = bool(mod_row['is_client'])
                            is_global = bool(mod_row['is_global'])
                            is_privileged = bool(mod_row['is_privileged'])
                            server_call = bool(mod_row['server_call'])

                    if prior is not None:
                        module_id = prior['id']
                        conn.execute(
                            """UPDATE modules SET object_id=?, file_path=?, file_hash=?,
                               line_count=?, abs_path=?, is_server=?, is_client=?, is_global=?,
                               is_privileged=?, server_call=?, name_cf=?
                               WHERE id=?""",
                            (object_id, mf.relative_path, file_hash, result.line_count, mf.file_path,
                             is_server, is_client, is_global, is_privileged, server_call,
                             mf.full_name.casefold(), module_id))
                        # Stale content from the previous version of this
                        # file — procedures cascades to calls; module_text_fts
                        # has no FK (FTS5 virtual tables can't carry one).
                        conn.execute("DELETE FROM procedures WHERE module_id=?", (module_id,))
                        conn.execute("DELETE FROM module_text_fts WHERE rowid=?", (module_id,))
                    else:
                        cur = conn.execute(
                            """INSERT INTO modules (source_id, object_id, name, module_type,
                               file_path, file_hash, is_server, is_client, is_global,
                               is_privileged, server_call, line_count, name_cf, abs_path)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (source_id, object_id, mf.full_name, mf.module_type,
                             mf.relative_path, file_hash,
                             is_server, is_client, is_global, is_privileged, server_call,
                             result.line_count, mf.full_name.casefold(), mf.file_path))
                        module_id = cur.lastrowid
                        existing_modules[mf.full_name] = {
                            'id': module_id, 'object_id': object_id, 'file_hash': file_hash}

                    stats['modules'] += 1

                    # FTS index for search_code (Этап 3/D6) — contentless:
                    # only the index is stored here, not the text itself
                    # (see db.py); the actual content lives on disk at
                    # modules.abs_path and gets re-read from there when a
                    # query matches.
                    conn.execute(
                        "INSERT INTO module_text_fts (rowid, text) VALUES (?, ?)",
                        (module_id, result.content))

                    # Procedures — individually inserted (need each one's
                    # lastrowid to link calls by line range below).
                    proc_id_map = {}
                    for proc in result.procedures:
                        cur = conn.execute(
                            """INSERT INTO procedures (module_id, name, kind, is_export,
                               start_line, end_line, params, directive, signature,
                               is_async, name_cf)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (module_id, proc.name, proc.kind, proc.is_export,
                             proc.start_line, proc.end_line, proc.params,
                             proc.directive, proc.signature,
                             proc.is_async, proc.name.casefold()))
                        proc_id_map[(proc.start_line, proc.end_line)] = cur.lastrowid
                        stats['procedures'] += 1

                    # Calls — batched via executemany (Этап 4/D7 item 3):
                    # unlike procedures, nothing needs an individual lastrowid
                    # back, so there's no reason to insert them one at a time.
                    call_rows = []
                    for call in result.calls:
                        caller_id = None
                        for (cs, ce), pid in proc_id_map.items():
                            if cs <= call.line <= ce:
                                caller_id = pid
                                break
                        if caller_id is None:
                            continue
                        callee_module = call.callee_module
                        if call.callee_kind == 'override' and not callee_module:
                            # An override always targets a same-named module
                            # in another source (D9/1.4) — this module's own
                            # dotted name is exactly the join key
                            # resolve_calls needs.
                            callee_module = mf.full_name
                        call_rows.append((caller_id, call.callee_name, callee_module,
                                          call.callee_proc, call.callee_kind, call.line, call.context))
                    if call_rows:
                        conn.executemany(
                            """INSERT INTO calls (caller_id, callee_name, callee_module,
                               callee_proc, callee_kind, line, context)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            call_rows)
                        stats['calls'] += len(call_rows)

            if (i + 1) % commit_every == 0 or i + 1 == total_files:
                conn.commit()
                if progress_cb:
                    progress_cb(i + 1, total_files)

        # Files that existed for this source last run but aren't in this
        # walk anymore (deleted/renamed on disk) — without this, incremental
        # reindex would keep their modules/procedures/calls forever.
        stale_names = set(existing_modules.keys()) - seen_names
        if stale_names:
            stale_ids = [existing_modules[n]['id'] for n in stale_names]
            id_placeholders = ','.join('?' * len(stale_ids))
            conn.execute(f"DELETE FROM module_text_fts WHERE rowid IN ({id_placeholders})", stale_ids)
            conn.execute(f"DELETE FROM modules WHERE id IN ({id_placeholders})", stale_ids)

        conn.commit()
        stats['duration_sec'] = round(time.time() - start, 3)
        return stats

    # ============================================
    # CALL RESOLUTION (Этап 1 — replaces name/LIKE matching with FK edges)
    # ============================================

    def resolve_calls(self) -> dict:
        """
        Resolve every extracted call to an actual procedure, populating
        calls_resolved. Pure post-processing over the current state of
        procedures/calls/modules/sources — safe to call after any subset
        of sources has been (re)indexed, since it always recomputes the
        whole table (cheap: a handful of table scans plus dict lookups,
        no per-call queries).

        Resolution rules (IMPROVEMENT_PLAN Этап 1.2):
          local          -> procedure in the same module (confidence=exact);
                            else an exported procedure of a GLOBAL common
                            module with that name (confidence=name_only)
          common_module  -> procedure of the common module with that short
                            name; extension overrides main when both exist
          manager        -> procedure of the matching МодульМенеджера
          override       -> the intercepted procedure in a DIFFERENT source
                            (confidence=override) — prefers 'main', since
                            that's what an extension directive overrides
        Calls that resolve to nothing simply get no calls_resolved row —
        that's normal (platform/external calls) and is what Этап 7's
        diagnostics measure.
        """
        conn = self.db.conn
        if not conn:
            self.db.connect()
            conn = self.db.conn

        conn.execute("DELETE FROM calls_resolved")

        source_type: dict[str, str] = {
            r['id']: (r['source_type'] or 'main')
            for r in conn.execute("SELECT id, source_type FROM sources")
        }

        # proc_id -> module_id : needed to resolve 'local' and 'override' calls
        # module_id -> {proc_name_lower: proc_id} : same-module local resolution
        proc_module: dict[int, int] = {}
        module_local_procs: dict[int, dict[str, int]] = {}
        for r in conn.execute("SELECT id, module_id, name FROM procedures"):
            proc_module[r['id']] = r['module_id']
            module_local_procs.setdefault(r['module_id'], {})[r['name'].lower()] = r['id']

        module_source: dict[int, str] = {
            r['id']: (r['source_id'] or '')
            for r in conn.execute("SELECT id, source_id FROM modules")
        }

        # (module_dotted_name_lower, proc_name_lower) -> [(proc_id, source_id), ...]
        # Covers common_module/manager/override resolution — all three key
        # off an exact modules.name match (see index_bsl for why the dotted
        # name is the right join key, no LIKE needed) — EXCEPT common
        # modules can end up stored under two different forms depending on
        # which pass created the row: index_report (report.txt present)
        # stores the bare short name ("CommonAtServer"), while index_bsl's
        # own fallback registration (XML-only source, no report.txt) uses
        # the fully dotted form ("ОбщийМодуль.CommonAtServer.Модуль") — the
        # same one common_module calls are looked up by below. Register
        # both keys for a bare-name row so resolution doesn't silently miss
        # every common-module call in a project that has report.txt.
        module_proc_index: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for r in conn.execute(
                """SELECT m.id as module_id, m.name as mname, p.id as pid, p.name as pname
                   FROM procedures p JOIN modules m ON m.id = p.module_id"""):
            src = module_source.get(r['module_id'], '')
            pname_l = r['pname'].lower()
            mname_l = r['mname'].lower()
            module_proc_index.setdefault((mname_l, pname_l), []).append((r['pid'], src))
            if '.' not in r['mname']:
                dotted = f'общиймодуль.{mname_l}.модуль'
                module_proc_index.setdefault((dotted, pname_l), []).append((r['pid'], src))

        # name_lower -> [(proc_id, source_id), ...] for exported procs of
        # GLOBAL common modules — the 'local' fallback (bare calls resolve
        # unqualified to a global module's export when not found locally).
        global_exports: dict[str, list[tuple[int, str]]] = {}
        for r in conn.execute(
                """SELECT p.id, p.name, m.id as module_id
                   FROM procedures p JOIN modules m ON m.id = p.module_id
                   WHERE p.is_export = 1 AND m.is_global = 1"""):
            global_exports.setdefault(r['name'].lower(), []).append(
                (r['id'], module_source.get(r['module_id'], '')))

        def pick_prefer_extension(candidates: list[tuple[int, str]]) -> int | None:
            if not candidates:
                return None
            ext = [c for c in candidates if source_type.get(c[1]) == 'extension']
            return (ext or candidates)[0][0]

        def pick_override_target(candidates: list[tuple[int, str]], exclude_source: str) -> int | None:
            pool = [c for c in candidates if c[1] != exclude_source]
            if not pool:
                return None
            main = [c for c in pool if source_type.get(c[1]) == 'main']
            return (main or pool)[0][0]

        inserts: list[tuple[int, int, str]] = []
        for call in conn.execute(
                "SELECT id, caller_id, callee_kind, callee_module, callee_proc FROM calls"):
            kind = call['callee_kind']
            caller_module_id = proc_module.get(call['caller_id'])
            callee_proc_l = (call['callee_proc'] or '').lower()
            callee_module_l = (call['callee_module'] or '').lower()

            callee_id = None
            confidence = None

            if kind == 'local':
                local_procs = module_local_procs.get(caller_module_id, {})
                if callee_proc_l in local_procs:
                    callee_id = local_procs[callee_proc_l]
                    confidence = 'exact'
                elif callee_proc_l in global_exports:
                    callee_id = pick_prefer_extension(global_exports[callee_proc_l])
                    confidence = 'name_only'

            elif kind == 'common_module':
                key = (f'общиймодуль.{callee_module_l}.модуль', callee_proc_l)
                candidates = module_proc_index.get(key)
                if candidates:
                    callee_id = pick_prefer_extension(candidates)
                    confidence = 'exact'

            elif kind == 'manager':
                key = (callee_module_l, callee_proc_l)
                candidates = module_proc_index.get(key)
                if candidates:
                    callee_id = pick_prefer_extension(candidates)
                    confidence = 'exact'

            elif kind == 'override':
                key = (callee_module_l, callee_proc_l)
                candidates = module_proc_index.get(key)
                if candidates:
                    caller_source = module_source.get(caller_module_id, '')
                    callee_id = pick_override_target(candidates, caller_source)
                    confidence = 'override'

            if callee_id is not None:
                inserts.append((call['id'], callee_id, confidence))

        for i in range(0, len(inserts), 2000):
            conn.executemany(
                "INSERT INTO calls_resolved (call_id, callee_id, confidence) VALUES (?, ?, ?)",
                inserts[i:i + 2000])
        conn.commit()
        return {'resolved': len(inserts)}
