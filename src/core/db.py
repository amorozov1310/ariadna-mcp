"""
SQLite database: per-project schema with FTS5.
Each project gets its own .db file. source_id tracks
multi-source projects (base config + extensions).
"""

import sqlite3
import os
import threading
import time
from datetime import datetime


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    source_type TEXT DEFAULT 'main',
    config_name TEXT DEFAULT '',
    config_version TEXT DEFAULT '',
    indexed_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metadata_objects (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT REFERENCES sources(id),
    name        TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    synonym     TEXT DEFAULT '',
    comment     TEXT DEFAULT '',
    -- Case-folded copies (str.casefold, filled in Python at insert time —
    -- see D5) so Cyrillic search is case-insensitive: SQLite's built-in
    -- NOCASE collation only folds ASCII.
    name_cf      TEXT DEFAULT '',
    full_name_cf TEXT DEFAULT '',
    synonym_cf   TEXT DEFAULT '',
    UNIQUE(full_name, source_id)
);

CREATE TABLE IF NOT EXISTS attributes (
    id          INTEGER PRIMARY KEY,
    object_id   INTEGER NOT NULL REFERENCES metadata_objects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    type_desc   TEXT DEFAULT '',
    synonym     TEXT DEFAULT '',
    comment     TEXT DEFAULT '',
    is_indexed  BOOLEAN DEFAULT FALSE,
    check_fill  TEXT DEFAULT '',
    name_cf     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS forms (
    id          INTEGER PRIMARY KEY,
    object_id   INTEGER NOT NULL REFERENCES metadata_objects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subsystem_content (
    id           INTEGER PRIMARY KEY,
    subsystem_id INTEGER NOT NULL REFERENCES metadata_objects(id) ON DELETE CASCADE,
    content_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modules (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT REFERENCES sources(id),
    object_id    INTEGER REFERENCES metadata_objects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    module_type  TEXT NOT NULL,
    file_path    TEXT DEFAULT '',
    file_hash    TEXT DEFAULT '',
    is_server    BOOLEAN DEFAULT FALSE,
    is_client    BOOLEAN DEFAULT FALSE,
    is_global    BOOLEAN DEFAULT FALSE,
    is_external  BOOLEAN DEFAULT FALSE,
    is_privileged BOOLEAN DEFAULT FALSE,
    server_call  BOOLEAN DEFAULT FALSE,
    line_count   INTEGER DEFAULT 0,
    name_cf      TEXT DEFAULT '',
    -- Absolute path on disk (Этап 3) — module_text_fts is contentless
    -- (stores no text, only the index) to keep the DB from ballooning to
    -- GB scale on a large corpus; search_code re-reads the file directly
    -- for the handful of matches it needs line/context from.
    abs_path     TEXT DEFAULT '',
    -- Этап 4/D7: (source_id, name) is the stable identity a rerun looks a
    -- module up by — for incremental reindex to skip/update in place
    -- instead of accumulating duplicate rows, both index_report (common
    -- modules, flags-only) and index_bsl (everything, including refiling
    -- an existing report.txt-created row) must agree on one name per
    -- module, hence UPSERTing on this rather than plain INSERT.
    UNIQUE(source_id, name)
);

CREATE TABLE IF NOT EXISTS procedures (
    id          INTEGER PRIMARY KEY,
    module_id   INTEGER NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    is_export   BOOLEAN DEFAULT FALSE,
    start_line  INTEGER DEFAULT 0,
    end_line    INTEGER DEFAULT 0,
    params      TEXT DEFAULT '',
    directive   TEXT DEFAULT '',
    signature   TEXT DEFAULT '',
    is_async    BOOLEAN DEFAULT FALSE,
    name_cf     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY,
    caller_id     INTEGER NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    callee_name   TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_proc   TEXT NOT NULL,
    callee_kind   TEXT DEFAULT '',   -- local | common_module | manager | override
    line          INTEGER DEFAULT 0,
    context       TEXT DEFAULT ''
);

-- Resolved call graph edges: which actual procedure a call targets, once
-- the two-pass indexer could establish that by fact rather than a name
-- guess. confidence: exact (module+name matched) | name_only (bare local
-- call resolved to a global common module's export) | override (extension
-- &Вместо/&После/&Перед/&ИзменениеИКонтроль interceptor edge).
-- Calls that stay unresolved (platform/external code) simply have no row
-- here — see diagnose_index (Этап 7) for how much of the graph resolves.
CREATE TABLE IF NOT EXISTS calls_resolved (
    call_id    INTEGER REFERENCES calls(id) ON DELETE CASCADE,
    callee_id  INTEGER REFERENCES procedures(id) ON DELETE CASCADE,
    confidence TEXT DEFAULT 'exact'
);
CREATE INDEX IF NOT EXISTS idx_cr_callee ON calls_resolved(callee_id);
CREATE INDEX IF NOT EXISTS idx_cr_call ON calls_resolved(call_id);

CREATE VIRTUAL TABLE IF NOT EXISTS metadata_fts USING fts5(
    full_name, name, synonym, comment,
    content='metadata_objects', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS attributes_fts USING fts5(
    name, synonym, type_desc, comment,
    content='attributes', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS procedures_fts USING fts5(
    name, signature,
    content='procedures', content_rowid='id'
);

-- Full-text index of every indexed BSL module (Этап 3 / D6) — replaces the
-- old search_code, which only grepped calls.context (recognized call
-- sites) and missed assignments, query text, comments. rowid == modules.id.
--
-- Both the plan's size contingencies are in play here, because on the
-- BSHP corpus (~600MB of raw BSL text as UTF-8) the naive version blew
-- the DB up to ~2.1GB either way:
--   - detail=none: drops per-token position tracking (no phrase/NEAR
--     queries, no bm25()/rank) — MATCH and prefix ("term*") still work,
--     which is all search_code needs.
--   - content='' (contentless): FTS5 stores only the index, not the text
--     itself — turns out that duplicated raw text, not the position
--     index, was the actual multi-GB cost (detail=none alone only saved
--     ~3%). search_code re-reads the source .bsl file straight from disk
--     (modules.abs_path) for the handful of rows a query actually
--     matches, and computes the line/±1 context from that.
-- contentless_delete=1: lets DELETE/INSERT OR REPLACE work on a contentless
-- table at all (needed for _clear_source_data and reindexing an existing
-- module) — without it contentless FTS5 tables are insert-only.
CREATE VIRTUAL TABLE IF NOT EXISTS module_text_fts USING fts5(
    text, tokenize='unicode61', detail=none, content='', contentless_delete=1
);

CREATE TRIGGER IF NOT EXISTS mo_ai AFTER INSERT ON metadata_objects BEGIN
    INSERT INTO metadata_fts(rowid, full_name, name, synonym, comment)
    VALUES (new.id, new.full_name, new.name, new.synonym, new.comment);
END;
CREATE TRIGGER IF NOT EXISTS mo_ad AFTER DELETE ON metadata_objects BEGIN
    INSERT INTO metadata_fts(metadata_fts, rowid, full_name, name, synonym, comment)
    VALUES ('delete', old.id, old.full_name, old.name, old.synonym, old.comment);
END;
-- Этап 4/D7: incremental reindex UPSERTs metadata_objects (ON CONFLICT DO
-- UPDATE) to keep ids — and modules.object_id's FK — stable across reruns,
-- instead of delete+reinsert. That path is an UPDATE, not an INSERT, so it
-- needs its own trigger to keep metadata_fts in sync.
CREATE TRIGGER IF NOT EXISTS mo_au AFTER UPDATE ON metadata_objects BEGIN
    INSERT INTO metadata_fts(metadata_fts, rowid, full_name, name, synonym, comment)
    VALUES ('delete', old.id, old.full_name, old.name, old.synonym, old.comment);
    INSERT INTO metadata_fts(rowid, full_name, name, synonym, comment)
    VALUES (new.id, new.full_name, new.name, new.synonym, new.comment);
END;
CREATE TRIGGER IF NOT EXISTS attr_ai AFTER INSERT ON attributes BEGIN
    INSERT INTO attributes_fts(rowid, name, synonym, type_desc, comment)
    VALUES (new.id, new.name, new.synonym, new.type_desc, new.comment);
END;
CREATE TRIGGER IF NOT EXISTS attr_ad AFTER DELETE ON attributes BEGIN
    INSERT INTO attributes_fts(attributes_fts, rowid, name, synonym, type_desc, comment)
    VALUES ('delete', old.id, old.name, old.synonym, old.type_desc, old.comment);
END;
CREATE TRIGGER IF NOT EXISTS proc_ai AFTER INSERT ON procedures BEGIN
    INSERT INTO procedures_fts(rowid, name, signature)
    VALUES (new.id, new.name, new.signature);
END;
CREATE TRIGGER IF NOT EXISTS proc_ad AFTER DELETE ON procedures BEGIN
    INSERT INTO procedures_fts(procedures_fts, rowid, name, signature)
    VALUES ('delete', old.id, old.name, old.signature);
END;

CREATE INDEX IF NOT EXISTS idx_mo_kind ON metadata_objects(kind);
CREATE INDEX IF NOT EXISTS idx_mo_source ON metadata_objects(source_id);
CREATE INDEX IF NOT EXISTS idx_attr_object ON attributes(object_id);
CREATE INDEX IF NOT EXISTS idx_forms_object ON forms(object_id);
CREATE INDEX IF NOT EXISTS idx_modules_source ON modules(source_id);
CREATE INDEX IF NOT EXISTS idx_modules_object ON modules(object_id);
CREATE INDEX IF NOT EXISTS idx_proc_module ON procedures(module_id);
CREATE INDEX IF NOT EXISTS idx_proc_name ON procedures(name);
CREATE INDEX IF NOT EXISTS idx_proc_name_cf ON procedures(name_cf);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_proc, callee_module);

CREATE TABLE IF NOT EXISTS index_state (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    created_at          TEXT DEFAULT '',
    updated_at          TEXT DEFAULT '',
    status              TEXT DEFAULT 'empty',
    index_duration_sec  REAL DEFAULT 0,
    -- Background reindex progress (Этап 4/D7) — polled via get_index_status
    -- while a reindex_async() run is in flight, instead of the caller
    -- blocking on it.
    progress_current    INTEGER DEFAULT 0,
    progress_total      INTEGER DEFAULT 0,
    progress_phase      TEXT DEFAULT ''
);

-- Отпечаток парсера (src/core/parser_fingerprint.py), которым разобран
-- BSL-код источника. По источнику, а не на весь индекс: reindex одного
-- источника не делает «свежими» остальные. Отдельная таблица, а не столбец
-- в sources/index_state: IF NOT EXISTS добавит её и в индекс старого
-- формата без пересоздания (_recreate_if_stale_schema не нужен).
CREATE TABLE IF NOT EXISTS parser_state (
    source_id                TEXT PRIMARY KEY,
    fingerprint              TEXT NOT NULL DEFAULT '',
    updated_at               TEXT DEFAULT '',
    last_full_reparse_at     TEXT DEFAULT '',
    last_full_reparse_reason TEXT DEFAULT ''
);
"""


class _Rows:
    """Результат запроса читающего соединения, уже целиком прочитанный:
    fetchone/fetchall/итерация, как у sqlite3.Cursor."""

    def __init__(self, rows: list, description):
        self._rows = rows
        self._pos = 0
        self.description = description

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self) -> list:
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    def __iter__(self):
        while self._pos < len(self._rows):
            yield self.fetchone()


class _ReaderConnection:
    """Читающее соединение, которое можно безопасно закрыть из другого потока.

    Database.close() закрывает читающие соединения всех потоков (иначе на
    Windows index.db оставался занят). Но sqlite3.Connection.close(), пока
    другой поток выполняет на нём запрос, роняет процесс (segfault в
    CPython): так падал сервер при удалении проекта под нагрузкой MCP.
    Поэтому запрос выполняется и дочитывается целиком под блокировкой
    соединения, а close() ждёт её — не дольше одного запроса. Флаг
    закрытия ставится до ожидания: новые запросы на этом соединении уже не
    начинаются, иначе поток, выполняющий запросы подряд, перехватывал бы
    блокировку раньше close() (Lock не справедлив), и тот ждал бы вечно."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()
        self._closed = False

    def execute(self, sql: str, params=()) -> _Rows:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            cur = self._conn.execute(sql, params)
            try:
                return _Rows(cur.fetchall(), cur.description)
            finally:
                cur.close()

    def refuse_new(self) -> None:
        """Новые запросы на этом соединении больше не начнутся."""
        self._closed = True

    def close(self) -> None:
        self.refuse_new()
        with self._lock:                # дождаться текущего запроса
            self._conn.close()          # повторный close() sqlite3 безвреден

    def __getattr__(self, name):
        return getattr(self._conn, name)


class Database:
    """Per-project SQLite database wrapper."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None  # the single writer
        self._local = threading.local()               # per-thread readers (Этап 4/D7)
        # Все выданные читающие соединения — чтобы close() закрыл и те, что
        # открыты в других потоках (пул SDK, Web UI, TestClient). Иначе на
        # Windows файл индекса оставался занятым: rmtree в delete_project и
        # очистка временных каталогов падали с WinError 32. Поколение
        # растёт при каждом close(): поток, чьё соединение закрыли извне,
        # замечает это в read_conn и открывает новое.
        self._readers: set[_ReaderConnection] = set()
        self._readers_lock = threading.Lock()
        self._generation = 0
        # Индекс удаляется (delete_project): читающие соединения больше не
        # открываются — ни к старому файлу, который сейчас удаляет rmtree
        # (на Windows он бы стал занят), ни к новому пустому.
        self._retired = False
        # (st_dev, st_ino) файла индекса при открытии — см. file_replaced().
        self._file_id: tuple[int, int] | None = None

    def _open_conn(self) -> sqlite3.Connection:
        """Open one connection with the PRAGMAs/functions every connection
        needs, whether it's the writer or a per-thread reader. WAL mode
        (already on) is what lets several connections to the same file
        coexist — one writing, the rest reading — without blocking."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        # D5: str.casefold does full Unicode case folding (unlike SQLite's
        # built-in NOCASE, which is ASCII-only). *_cf columns are filled with
        # str.casefold() at insert time; параметр для сравнения с ними
        # приводится в Python до SQL (casefold(?) на каждой строке стоил
        # втрое больше времени). Функция в SQL остаётся для колонок без _cf
        # (casefold(a.type_desc)); deterministic=True позволяет SQLite
        # вычислить casefold(?) от параметра один раз на запрос.
        conn.create_function('casefold', 1, lambda s: s.casefold() if s is not None else None,
                             deterministic=True)
        return conn

    def connect(self) -> sqlite3.Connection:
        self._recreate_if_stale_schema()
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.conn = self._open_conn()
        self._file_id = self._current_file_id()
        return self.conn

    def _current_file_id(self) -> tuple[int, int] | None:
        """(st_dev, st_ino) файла индекса; None — файла нет. На Windows
        st_ino — настоящий идентификатор файла (Python 3.12+)."""
        try:
            st = os.stat(self.db_path)
        except OSError:
            return None
        return st.st_dev, st.st_ino

    def file_replaced(self) -> bool:
        """Файл индекса удалён или заменён другим с тех пор, как этот
        Database его открыл (другой процесс, ariadna-index рядом с сервером,
        docker volume rm, ручное удаление). Соединения тогда читают старый
        файл: на Linux удалённый файл остаётся доступным через открытые
        дескрипторы, и поиск отдавал бы данные, которых уже нет.

        Один os.stat. Если st_ino недоступен (0 — старый Python на Windows),
        сравнивается только наличие файла."""
        if self._file_id is None or self.db_path in (':memory:', ''):
            return False
        current = self._current_file_id()
        if current is None:
            return True
        if current[1] == 0 or self._file_id[1] == 0:
            return False
        return current != self._file_id

    def read_conn(self) -> '_ReaderConnection':
        """Per-thread read connection (Этап 4/D7): search/call-graph/
        diagnostics reads no longer share the single writer connection
        object across threads — each thread gets its own, opened lazily
        and cached, so a background reindex on the writer doesn't
        serialize concurrent reads through the same Connection object.
        Callers must never write through this connection."""
        conn = getattr(self._local, 'conn', None)
        if conn is not None and getattr(self._local, 'generation', None) == self._generation:
            return conn
        if self._retired:
            raise sqlite3.OperationalError(f"индекс удалён: {self.db_path}")
        # Соединения нет или его закрыл close() из другого потока.
        if not os.path.exists(self.db_path) and self.db_path not in (':memory:', ''):
            if self._generation > 0:
                # Этот Database уже закрывали, а файла нет: индекс удалили
                # (delete_project, удаление снаружи). Создать его заново
                # здесь — значит оставить пустой index.db удалённого проекта.
                raise sqlite3.OperationalError(f"индекс удалён: {self.db_path}")
            # Nothing to read yet — make sure the writer has created
            # the file/schema first rather than racing it here.
            self.connect()
            self.init_schema()
        # Открыть и зарегистрировать — под той же блокировкой, что retire() и
        # close(): иначе соединение, открытое между проверкой _retired и
        # регистрацией, пережило бы закрытие, и на Windows rmtree удаляемого
        # индекса падал бы с WinError 32. Открытие — миллисекунды.
        with self._readers_lock:
            if self._retired:
                raise sqlite3.OperationalError(f"индекс удалён: {self.db_path}")
            conn = _ReaderConnection(self._open_with_retry())
            self._readers.add(conn)
            self._local.generation = self._generation
        self._local.conn = conn
        return conn

    def _open_with_retry(self, attempts: int = 3) -> sqlite3.Connection:
        """Этап U0/U13: opening a *new* reader occasionally fails with
        `unable to open database file` under load (seen once in the audit —
        a search returned HTTP 500 and did not reproduce on eight retries).
        It's transient — SQLite is opening the WAL/-shm sidecars while the
        indexer holds them — so a couple of short retries turn a 500 into a
        slightly slower successful request. Only the open is retried; a
        genuinely missing/corrupt file still raises after the last attempt."""
        delay = 0.05
        for attempt in range(1, attempts + 1):
            try:
                return self._open_conn()
            except sqlite3.OperationalError:
                if attempt == attempts:
                    raise
                time.sleep(delay)
                delay *= 3
        raise AssertionError("unreachable")   # pragma: no cover

    def _recreate_if_stale_schema(self):
        """
        No migrations (per IMPROVEMENT_PLAN): the index is fully rebuilt
        from source on reindex, so a DB created under an older schema is
        just deleted and recreated empty rather than ALTERed in place.
        Detected by probing for something added in the latest schema
        revision — index_state.progress_phase (Этап 4/D7: background
        reindex progress tracking needs these columns to exist, and
        db.update_state() would otherwise fail with "no such column" on
        an older DB) — rather than a version counter, since existing
        on-disk DBs predate any such counter.
        """
        if self.db_path in (':memory:', '') or not os.path.exists(self.db_path):
            return
        try:
            probe = sqlite3.connect(self.db_path)
            try:
                has_calls_table = probe.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='calls'"
                ).fetchone()[0]
                has_new_schema = probe.execute(
                    "SELECT COUNT(*) FROM pragma_table_info('index_state') WHERE name='progress_phase'"
                ).fetchone()[0]
            finally:
                probe.close()
        except sqlite3.Error:
            return
        if has_calls_table and not has_new_schema:
            for suffix in ('', '-wal', '-shm'):
                path = self.db_path + suffix
                if os.path.exists(path):
                    os.remove(path)

    def init_schema(self):
        if not self.conn:
            self.connect()
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def reset(self):
        """Clear all data, keep schema."""
        if not self.conn:
            self.connect()
        for table in ['calls_resolved', 'calls', 'procedures', 'module_text_fts', 'modules',
                       'subsystem_content', 'forms', 'attributes', 'metadata_objects',
                       'sources', 'index_state', 'parser_state']:
            self.conn.execute(f"DELETE FROM {table}")
        for fts in ('metadata_fts', 'attributes_fts', 'procedures_fts'):
            try:
                self.conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            except Exception:
                pass
        self.conn.commit()

    def retire(self):
        """Закрыть навсегда: файл индекса удаляется. В отличие от close(),
        read_conn потом не открывает новые соединения, а бросает
        OperationalError."""
        with self._readers_lock:
            self._retired = True
        self.close()

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
        # Читающие соединения всех потоков: открыты с check_same_thread=False,
        # поэтому закрывать их отсюда можно. Их потоки при следующем
        # read_conn увидят новое поколение и откроют соединение заново.
        with self._readers_lock:
            readers = list(self._readers)
            self._readers.clear()
            self._generation += 1
        # Сначала запретить новые запросы на всех соединениях, потом ждать
        # каждое: ожидание — самый долгий из текущих запросов, а не их сумма.
        for reader in readers:
            reader.refuse_new()
        for reader in readers:
            try:
                reader.close()
            except sqlite3.Error:
                pass
        self._local.conn = None

    def get_stats(self) -> dict:
        # Read-only — uses this thread's own connection (Этап 4/D7) so
        # polling status during a background reindex never contends with
        # the writer connection the indexer thread is using.
        conn = self.read_conn()
        stats = {}
        for table in ['metadata_objects', 'attributes', 'forms', 'modules',
                       'procedures', 'calls', 'sources']:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            stats[table] = row['cnt']
        kinds = conn.execute(
            "SELECT kind, COUNT(*) as cnt FROM metadata_objects GROUP BY kind ORDER BY cnt DESC"
        ).fetchall()
        stats['kinds'] = {r['kind']: r['cnt'] for r in kinds}
        state = conn.execute("SELECT * FROM index_state WHERE id=1").fetchone()
        stats['status'] = state['status'] if state else 'empty'
        stats['updated_at'] = state['updated_at'] if state else ''
        stats['index_duration_sec'] = state['index_duration_sec'] if state else 0
        stats['progress_current'] = state['progress_current'] if state else 0
        stats['progress_total'] = state['progress_total'] if state else 0
        stats['progress_phase'] = state['progress_phase'] if state else ''
        return stats

    def get_kind_counts(self) -> dict[str, int]:
        """Разбивка объектов по видам, без остальной статистики (Этап U0).

        Дашборду из get_stats() нужна, по сути, только она: остальные числа
        уже лежат в реестре (ProjectInfo.index_stats) и достаются бесплатно.
        А COUNT(*) по calls и procedures — это полный проход по полутора
        миллионам и полумиллиону строк, из-за которого страница проекта во
        время индексации открывалась десятки секунд. Здесь один GROUP BY по
        metadata_objects (десятки тысяч строк).
        """
        conn = self.read_conn()
        rows = conn.execute(
            "SELECT kind, COUNT(*) as cnt FROM metadata_objects GROUP BY kind ORDER BY cnt DESC"
        ).fetchall()
        return {r['kind']: r['cnt'] for r in rows}

    def get_index_progress(self) -> dict:
        """Только строка index_state — без COUNT(*) по таблицам (Этап U0).

        Дашборд опрашивает прогресс раз в 1.5 секунды, и делать это через
        get_stats() нельзя: тот считает COUNT(*) по семи таблицам, среди
        которых calls (на БСХП это ~1.5 млн строк). Во время самой
        индексации, когда индексатор пишет пачками при synchronous=OFF,
        такой полный проход занимает десятки секунд — опрос прогресса
        подвисал ровно тогда, когда прогресс и нужен, и страница выглядела
        замершей. Здесь читается одна строка по первичному ключу.
        """
        conn = self.read_conn()
        state = conn.execute("SELECT * FROM index_state WHERE id=1").fetchone()
        if state is None:
            return {'status': 'empty', 'progress_current': 0,
                    'progress_total': 0, 'progress_phase': '', 'updated_at': ''}
        return {
            'status': state['status'],
            'progress_current': state['progress_current'],
            'progress_total': state['progress_total'],
            'progress_phase': state['progress_phase'],
            'updated_at': state['updated_at'],
        }

    def get_parser_states(self, conn: sqlite3.Connection | None = None) -> dict[str, dict]:
        """{source_id: строка parser_state}. reindex читает писателем,
        diagnose_index передаёт своё читающее соединение (read_conn)."""
        conn = conn or self.conn or self.read_conn()
        return {r['source_id']: dict(r) for r in conn.execute("SELECT * FROM parser_state")}

    def set_parser_fingerprint(self, source_id: str, fingerprint: str,
                               full_reparse_reason: str = ''):
        """Записать отпечаток, которым только что разобран источник;
        full_reparse_reason — если разбор был полным из-за смены парсера."""
        now = datetime.now().isoformat()
        if not self.conn:
            self.connect()
        self.conn.execute(
            """INSERT INTO parser_state (source_id, fingerprint, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET fingerprint=excluded.fingerprint,
                                                    updated_at=excluded.updated_at""",
            (source_id, fingerprint, now))
        if full_reparse_reason:
            self.conn.execute(
                "UPDATE parser_state SET last_full_reparse_at=?, last_full_reparse_reason=? "
                "WHERE source_id=?", (now, full_reparse_reason, source_id))
        self.conn.commit()

    def update_state(self, **kwargs):
        now = datetime.now().isoformat()
        if not self.conn:
            self.connect()
        existing = self.conn.execute("SELECT id FROM index_state WHERE id=1").fetchone()
        if not existing:
            self.conn.execute("INSERT INTO index_state (id, created_at, updated_at) VALUES (1, ?, ?)", (now, now))
        for key, value in kwargs.items():
            self.conn.execute(f"UPDATE index_state SET {key}=?, updated_at=? WHERE id=1", (value, now))
        self.conn.commit()
