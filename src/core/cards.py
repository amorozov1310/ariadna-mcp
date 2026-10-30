"""
Поисковые карточки процедур — детерминированное текстовое представление
процедуры для лексического и (позже) векторного поиска.

Карточка собирается из уже построенного индекса и файла модуля, без LLM:
  - заголовок: вид, имя и подпись процедуры, модуль, объект метаданных и
    его синоним, область (#Область) и комментарий над процедурой;
  - фрагменты тела: строки процедуры подряд, каждый не длиннее
    CHUNK_CHARS символов, с номерами строк. Хвост длинной процедуры не
    отбрасывается — он уходит в следующие фрагменты.
content_hash — отпечаток всего, что видит поиск (формат карточки,
заголовок, тело): изменился синоним объекта или код — изменилась карточка.
Ничего о бизнес-смысле, чего нет в исходниках, карточка не утверждает.

Отладка и замер на своём индексе (в контейнере — через docker exec):
    python -m src.core.cards --project ID [--source ID] [--show N]
"""

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Меняется при любом изменении состава или вида карточки: карточки (и всё,
# что из них построено) с другим форматом считаются устаревшими.
CARD_FORMAT = 2
# Предел фрагмента тела в символах: ~500 токенов небольшой модели
# эмбеддингов на кириллическом BSL; уточняется замером на шаге выбора модели.
CHUNK_CHARS = 1500

_REGION_OPEN = re.compile(r'^\s*#\s*(?:Область|Region)\s+(\S+)', re.IGNORECASE)
_REGION_CLOSE = re.compile(r'^\s*#\s*(?:КонецОбласти|EndRegion)\b', re.IGNORECASE)
_COMMENT = re.compile(r'^\s*//(.*)$')
_DIRECTIVE = re.compile(r'^\s*&')


@dataclass
class CardChunk:
    chunk_no: int
    start_line: int
    end_line: int
    text: str


@dataclass
class ProcedureCard:
    procedure_id: int
    source_id: str
    module_name: str
    procedure_name: str
    kind: str
    is_export: bool
    directive: str
    signature: str
    start_line: int
    end_line: int
    relative_path: str
    object_full_name: str = ''
    object_synonym: str = ''
    region: str = ''
    doc_comment: str = ''
    body_available: bool = True
    chunks: list[CardChunk] = field(default_factory=list)
    content_hash: str = ''

    def header(self) -> str:
        lines = [self.signature or f'{self.kind} {self.procedure_name}()']
        if self.directive:
            lines[0] = f'{self.directive} {lines[0]}'
        # Имена 1С слитные (ТипСкладаПоУмолчанию) — для полнотекстового поиска
        # это одно слово; разбитое на слова имя находится и по фразе запроса.
        words = split_identifier(self.procedure_name)
        obj_name = self.object_full_name.split('.', 1)[-1] if self.object_full_name else ''
        if obj_name:
            words += ' | ' + split_identifier(obj_name)
        lines.append(f'Слова: {words}')
        roots = sorted({r for w in words.split() for r in word_roots(w)})
        if roots:
            lines.append(f'Корни: {" ".join(roots)}')
        lines.append(f'Модуль: {self.module_name}')
        if self.object_full_name:
            obj = self.object_full_name
            if self.object_synonym:
                obj += f' — {self.object_synonym}'
            lines.append(f'Объект: {obj}')
        if self.region:
            lines.append(f'Область: {self.region}')
        if self.doc_comment:
            lines.append(f'Описание: {self.doc_comment}')
        return '\n'.join(lines)

    def chunk_texts(self) -> list[str]:
        """Тексты для индексации: заголовок + фрагмент тела (или только заголовок)."""
        head = self.header()
        if not self.chunks:
            return [head]
        return [f'{head}\n\n{c.text}' for c in self.chunks]


_CAMEL = re.compile(r'[А-ЯЁA-Z]+(?![а-яёa-z])|[А-ЯЁA-Z]?[а-яёa-z]+|\d+')


def split_identifier(name: str) -> str:
    """«ТипСкладаПоУмолчанию» → «тип склада по умолчанию», «НДСЗаписи» → «ндс записи»."""
    return ' '.join(part.lower() for part in _CAMEL.findall(name))


# Корни для разных форм одного слова: «отправление» и «отправить» — «отпра»,
# «резервирование» и «зарезервировать» (без приставки) — «резер». Грубая
# замена стемминга: длина корня и список приставок подобраны на стенде
# оценки; общий корень у разных слов — цена за то, что формы совпадают.
ROOT_LEN = 5
_MIN_ROOTED = 7
_PREFIXES = ('пере', 'пред', 'под', 'при', 'раз', 'рас', 'вы', 'за', 'на', 'об', 'от', 'до', 'из')
_CYRILLIC = re.compile(r'^[а-яё]+$')


def word_roots(word: str) -> set[str]:
    """Корни слова (пусто для коротких и некириллических)."""
    word = word.lower()
    if len(word) < _MIN_ROOTED or not _CYRILLIC.match(word):
        return set()
    roots = {word[:ROOT_LEN]}
    for prefix in _PREFIXES:
        rest = word[len(prefix):]
        if word.startswith(prefix) and len(rest) >= _MIN_ROOTED:
            roots.add(rest[:ROOT_LEN])
            break
    return roots


def _read_lines(path: str) -> list[str] | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return None


def _regions(lines: list[str]) -> list[str]:
    """Область на каждой строке (индекс — номер строки с 0): «А/Б» для вложенных."""
    stack: list[str] = []
    result = []
    for line in lines:
        if m := _REGION_OPEN.match(line):
            stack.append(m.group(1))
        elif _REGION_CLOSE.match(line) and stack:
            stack.pop()
        result.append('/'.join(stack))
    return result


def _doc_comment(lines: list[str], start_line: int) -> str:
    """Комментарий сразу над объявлением (директивы &НаСервере пропускаются)."""
    i = start_line - 2  # строка над объявлением, индекс с 0
    while i >= 0 and _DIRECTIVE.match(lines[i]):
        i -= 1
    collected = []
    while i >= 0 and (m := _COMMENT.match(lines[i])):
        collected.append(m.group(1).strip())
        i -= 1
    collected.reverse()
    # Пустые строки-разделители «//» внутри описания сжимаются.
    return ' '.join(part for part in collected if part)


def chunk_body(lines: list[str], start_line: int, end_line: int,
               limit: int = CHUNK_CHARS) -> list[CardChunk]:
    """Строки start_line..end_line (с 1) подряд кусками не длиннее limit символов.
    Строка длиннее limit режется на части с тем же номером строки."""
    pieces: list[tuple[int, str]] = []
    for n in range(start_line, min(end_line, len(lines)) + 1):
        line = lines[n - 1].rstrip()
        pieces += [(n, line[i:i + limit]) for i in range(0, len(line), limit)] or [(n, '')]

    chunks: list[CardChunk] = []
    group: list[tuple[int, str]] = []
    size = 0
    for n, piece in pieces + [(0, None)]:
        if piece is None or (group and size + len(piece) + 1 > limit):
            if group:
                chunks.append(CardChunk(len(chunks), group[0][0], group[-1][0],
                                        '\n'.join(text for _, text in group)))
            group, size = [], 0
        if piece is not None:
            group.append((n, piece))
            size += len(piece) + 1
    return chunks


def _object_of(module_name: str) -> str:
    """«Справочник.Склады.Форма.ФормаЭлемента» → «Справочник.Склады»."""
    parts = module_name.split('.')
    return '.'.join(parts[:2]) if len(parts) >= 2 and parts[0] != 'Конфигурация' else ''


def _synonym_lookup(conn):
    """(source_id, имя модуля) → синоним его объекта; объект расширения без
    своего синонима берёт его из любого источника (заимствованный объект)."""
    synonyms: dict[tuple[str, str], str] = {}
    for r in conn.execute('SELECT source_id, full_name_cf, synonym FROM metadata_objects'):
        synonyms[(r['source_id'], r['full_name_cf'])] = r['synonym'] or ''
    any_source: dict[str, str] = defaultdict(str)
    for (_src, fn), syn in synonyms.items():
        any_source[fn] = any_source[fn] or syn

    def lookup(source_id: str, module_name: str) -> str:
        obj_cf = _object_of(module_name).casefold()
        return synonyms.get((source_id, obj_cf)) or any_source.get(obj_cf, '')
    return lookup


def build_cards(engine, source_id: str | None = None, module_name: str | None = None,
                module_ids: set[int] | None = None) -> Iterator[ProcedureCard]:
    """Карточки процедур индекса (SearchEngine engine), по модулю за раз.

    Файл модуля читается один раз на модуль; недоступный файл даёт карточки
    с одним заголовком (body_available=False), а не пропуск процедур.
    """
    conn = engine.db.read_conn()
    conds, params = [], []
    if source_id:
        conds.append('m.source_id = ?')
        params.append(source_id)
    if module_name:
        conds.append('m.name_cf = ?')
        params.append(module_name.casefold())
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
    rows = conn.execute(
        f"""SELECT p.id, p.name, p.kind, p.is_export, p.start_line, p.end_line,
                   p.directive, p.signature, m.id AS module_id, m.name AS module_name,
                   m.file_path, m.abs_path, m.source_id
            FROM procedures p JOIN modules m ON m.id = p.module_id
            {where} ORDER BY m.source_id, m.name, p.start_line""", params).fetchall()

    synonym_of = _synonym_lookup(conn)
    by_module: dict[int, list] = defaultdict(list)
    for r in rows:
        if module_ids is None or r['module_id'] in module_ids:
            by_module[r['module_id']].append(r)

    for procs in by_module.values():
        first = procs[0]
        path = engine.module_file_path(first)
        lines = _read_lines(path) if path else None
        regions = _regions(lines) if lines is not None else []
        obj = _object_of(first['module_name'])
        synonym = synonym_of(first['source_id'], first['module_name'])
        for p in procs:
            card = ProcedureCard(
                procedure_id=p['id'], source_id=p['source_id'], module_name=p['module_name'],
                procedure_name=p['name'], kind=p['kind'], is_export=bool(p['is_export']),
                directive=p['directive'] or '', signature=p['signature'] or '',
                start_line=p['start_line'], end_line=p['end_line'],
                relative_path=(p['file_path'] or '').replace('\\', '/'),
                object_full_name=obj, object_synonym=synonym,
                body_available=lines is not None,
            )
            if lines is not None:
                card.region = regions[p['start_line'] - 1] if 0 < p['start_line'] <= len(regions) else ''
                card.doc_comment = _doc_comment(lines, p['start_line'])
                card.chunks = chunk_body(lines, p['start_line'], p['end_line'])
            digest = hashlib.sha256(f'card:{CARD_FORMAT}\n{card.header()}\n'.encode('utf-8'))
            for c in card.chunks:
                digest.update(c.text.encode('utf-8'))
                digest.update(b'\x00')
            card.content_hash = 'sha256:' + digest.hexdigest()
            yield card


# Вес заголовка карточки относительно тела в BM25-ранге card_fts: совпадение в
# имени, синониме объекта, области или описании важнее совпадения в коде.
# Подбирается на стенде оценки (шаг 5); замена значения на совместимость не влияет.
HEAD_WEIGHT = 8.0
# Экспортная процедура — программный интерфейс модуля, обычно то, что ищут:
# её BM25-ранг усиливается на эту долю.
EXPORT_BONUS = 0.15
# Востребованность процедуры — сколько вызовов в графе ведут в неё: ранг
# усиливается на POPULARITY_WEIGHT * ln(1 + вызывающих). У исходного метода
# общего модуля вызывающих в десятки раз больше, чем у его обёрток.
POPULARITY_WEIGHT = 0.03
_WORD = re.compile(r'\w+')
_IDENTIFIER = re.compile(r'^\w{4,}$')


def refresh_card_index(engine, progress_cb=None, batch: int = 500) -> dict:
    """Пересобрать карточки в card_chunks/card_fts там, где они устарели.

    Устаревшим считается модуль, у которого есть процедура без фрагментов
    (модуль перепарсен — процедуры пересозданы), сменился синоним объекта
    или формат карточки (CARD_FORMAT). Остальные модули не трогаются —
    повторный reindex без изменений почти ничего не стоит.
    Возвращает {'modules': пересобрано модулей, 'chunks': вставлено фрагментов}.
    """
    db = engine.db
    conn = db.conn
    synonym_of = _synonym_lookup(conn)
    missing = {r[0] for r in conn.execute(
        """SELECT DISTINCT p.module_id FROM procedures p
           WHERE NOT EXISTS (SELECT 1 FROM card_chunks c WHERE c.procedure_id = p.id)""")}
    recorded = {r[0]: (r[1], r[2]) for r in conn.execute(
        'SELECT module_id, object_synonym, card_format FROM card_modules')}
    stale: dict[int, str] = {}
    for r in conn.execute(
            """SELECT m.id, m.name, m.source_id FROM modules m
               WHERE EXISTS (SELECT 1 FROM procedures p WHERE p.module_id = m.id)"""):
        synonym = synonym_of(r[2], r[1])
        if r[0] in missing or recorded.get(r[0]) != (synonym, CARD_FORMAT):
            stale[r[0]] = synonym

    stats = {'modules': 0, 'chunks': 0}
    if not stale:
        return stats
    ids = list(stale)
    for i in range(0, len(ids), batch):
        part = ids[i:i + batch]
        marks = ','.join('?' * len(part))
        conn.execute(f"""DELETE FROM card_chunks WHERE procedure_id IN
                         (SELECT id FROM procedures WHERE module_id IN ({marks}))""", part)
        conn.commit()
        for card in build_cards(engine, module_ids=set(part)):
            head = card.header()
            bodies = [(c.chunk_no, c.start_line, c.end_line, c.text) for c in card.chunks] \
                or [(0, card.start_line, card.end_line, '')]
            for no, start, end, body in bodies:
                cur = conn.execute(
                    """INSERT INTO card_chunks (procedure_id, chunk_no, start_line, end_line, content_hash)
                       VALUES (?, ?, ?, ?, ?)""",
                    (card.procedure_id, no, start, end, card.content_hash))
                conn.execute('INSERT INTO card_fts (rowid, head, body) VALUES (?, ?, ?)',
                             (cur.lastrowid, head, body))
                stats['chunks'] += 1
        conn.executemany(
            """INSERT INTO card_modules (module_id, object_synonym, card_format) VALUES (?, ?, ?)
               ON CONFLICT(module_id) DO UPDATE SET object_synonym=excluded.object_synonym,
                   card_format=excluded.card_format""",
            [(m, stale[m], CARD_FORMAT) for m in part])
        conn.commit()
        stats['modules'] += len(part)
        if progress_cb:
            progress_cb(stats['modules'], len(ids))
    return stats


def card_fts_query(text: str, max_terms: int = 24) -> str:
    """Запрос MATCH: слова запроса и их корни (word_roots) через OR, по префиксу.

    Слова короче трёх букв («по», «на», «в») отбрасываются, если есть другие:
    как префикс они совпадают с огромной частью словаря — ранг не улучшают,
    а запрос на крупной конфигурации замедляют в разы."""
    words = []
    for w in _WORD.findall(text.lower()):
        if w not in words:
            words.append(w)
    long_words = [w for w in words if len(w) >= 3]
    words = long_words or [w for w in words if len(w) > 1]
    terms = list(words)
    for w in words:
        terms += [r for r in sorted(word_roots(w)) if r not in terms]
    return ' OR '.join(f'"{t}"*' for t in terms[:max_terms])


# Общие поля выдачи search_cards: границы процедуры и файл модуля — для
# ссылки на источник (semantic_search_code), чтения фрагмента и get_procedure_code.
_RESULT_COLUMNS = ('p.start_line AS proc_start, p.end_line AS proc_end, p.is_export, '
                   'p.kind AS proc_kind, m.file_path, m.abs_path')


def _exact_name_rows(conn, name: str, source_id: str | None, limit: int):
    """Процедуры с именем, ровно равным запросу: сначала самые вызываемые
    (исходный метод, а не его обёртки), затем экспортные методы общих модулей."""
    conds, params = ['p.name_cf = ?'], [name.casefold()]
    if source_id:
        conds.append('m.source_id = ?')
        params.append(source_id)
    return conn.execute(
        f"""SELECT p.id AS procedure_id, 0 AS chunk_no, p.start_line, p.end_line, 0.0 AS score,
                   p.name AS procedure_name, m.name AS module_name, m.source_id,
                   (SELECT COUNT(*) FROM calls_resolved r WHERE r.callee_id = p.id) AS callers,
                   {_RESULT_COLUMNS},
                   (SELECT c.content_hash FROM card_chunks c WHERE c.procedure_id = p.id
                    ORDER BY c.chunk_no LIMIT 1) AS content_hash,
                   'exact_name' AS match
            FROM procedures p JOIN modules m ON m.id = p.module_id
            WHERE {' AND '.join(conds)}
            ORDER BY callers DESC, p.is_export DESC, m.name_cf LIKE 'общиймодуль.%' DESC, m.name
            LIMIT ?""", params + [limit]).fetchall()


def search_cards(engine, query: str, source_id: str | None = None,
                 limit: int = 20, pool: int = 200) -> list[dict]:
    """Лексический поиск по карточкам с рангом BM25 — по процедурам.

    Из пула лучших фрагментов (pool) у каждой процедуры остаётся лучший;
    rank — место процедуры с 1 (для слияния рангов на шаге гибридного поиска).
    start_line/end_line — строки найденного фрагмента, proc_start/proc_end —
    всей процедуры; match — 'exact_name' (имя равно запросу) или 'fts'.
    """
    match = card_fts_query(query)
    if not match:
        return []
    conn = engine.db.read_conn()
    exact = _exact_name_rows(conn, query.strip(), source_id, limit) \
        if _IDENTIFIER.match(query.strip()) else []
    conds, params = ['card_fts MATCH ?'], [match]
    if source_id:
        conds.append('m.source_id = ?')
        params.append(source_id)
    rows = conn.execute(
        f"""SELECT c.procedure_id, c.chunk_no, c.start_line, c.end_line,
                   bm25(card_fts, {HEAD_WEIGHT}, 1.0) * (1 + {EXPORT_BONUS} * p.is_export) AS score,
                   p.name AS procedure_name, m.name AS module_name, m.source_id,
                   (SELECT COUNT(*) FROM calls_resolved r WHERE r.callee_id = p.id) AS callers,
                   {_RESULT_COLUMNS}, c.content_hash, 'fts' AS match
            FROM card_fts f
            JOIN card_chunks c ON c.id = f.rowid
            JOIN procedures p ON p.id = c.procedure_id
            JOIN modules m ON m.id = p.module_id
            WHERE {' AND '.join(conds)}
            ORDER BY score LIMIT ?""", params + [pool]).fetchall()
    if POPULARITY_WEIGHT:
        # BM25 в SQLite отрицательный: чем меньше, тем лучше — множитель > 1 поднимает.
        rows = sorted(rows, key=lambda r: r['score'] * (1 + POPULARITY_WEIGHT * math.log1p(r['callers'])))
    results, seen = [], set()
    for r in list(exact) + list(rows):
        if r['procedure_id'] in seen:
            continue
        seen.add(r['procedure_id'])
        item = dict(r)
        item['rank'] = len(results) + 1
        results.append(item)
        if len(results) >= limit:
            break
    return results


def _stats_main(argv=None) -> int:
    import argparse
    import os
    import statistics
    import time

    from .project_manager import ProjectManager

    ap = argparse.ArgumentParser(prog='python -m src.core.cards', description='Карточки процедур: замер и просмотр')
    ap.add_argument('--project', required=True)
    ap.add_argument('--source')
    ap.add_argument('--data-dir', default=os.environ.get('DATA_DIR', './data'))
    ap.add_argument('--index-dir', default=os.environ.get('INDEX_DIR') or None)
    ap.add_argument('--show', type=int, default=0, help='напечатать N первых карточек')
    args = ap.parse_args(argv)

    pm = ProjectManager(args.data_dir, args.index_dir)
    try:
        engine = pm.get_search(args.project)
        t0 = time.time()
        cards = chunks = no_body = with_comment = 0
        chunk_sizes, header_sizes, chunks_per_card = [], [], []
        shown = 0
        for card in build_cards(engine, source_id=args.source):
            cards += 1
            no_body += not card.body_available
            with_comment += bool(card.doc_comment)
            header_sizes.append(len(card.header()))
            chunks_per_card.append(len(card.chunks))
            for c in card.chunks:
                chunks += 1
                chunk_sizes.append(len(c.text))
            if shown < args.show:
                shown += 1
                print(f'--- {card.module_name}.{card.procedure_name} {card.content_hash[:19]}')
                print(card.chunk_texts()[0][:1200])
        dt = time.time() - t0

        def pct(values, q):
            return sorted(values)[min(len(values) - 1, int(len(values) * q))] if values else 0

        print(f'карточек: {cards}, фрагментов: {chunks}, без тела (файл недоступен): {no_body}, '
              f'с комментарием: {with_comment} ({100 * with_comment / max(cards, 1):.0f}%)')
        print(f'фрагментов на карточку: среднее {statistics.mean(chunks_per_card or [0]):.2f}, '
              f'p95 {pct(chunks_per_card, .95)}, макс {max(chunks_per_card or [0])}')
        print(f'символов во фрагменте: p50 {pct(chunk_sizes, .5)}, p95 {pct(chunk_sizes, .95)}; '
              f'в заголовке: p50 {pct(header_sizes, .5)}, p95 {pct(header_sizes, .95)}')
        print(f'всего символов тел: {sum(chunk_sizes):,}; сборка: {dt:.1f} с')
    finally:
        pm.close_all()
    return 0


if __name__ == '__main__':
    raise SystemExit(_stats_main())
