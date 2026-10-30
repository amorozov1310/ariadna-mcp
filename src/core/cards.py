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
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Меняется при любом изменении состава или вида карточки: карточки (и всё,
# что из них построено) с другим форматом считаются устаревшими.
CARD_FORMAT = 1
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


def build_cards(engine, source_id: str | None = None,
                module_name: str | None = None) -> Iterator[ProcedureCard]:
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

    synonyms: dict[tuple[str, str], str] = {}
    for r in conn.execute('SELECT source_id, full_name_cf, synonym FROM metadata_objects'):
        synonyms[(r['source_id'], r['full_name_cf'])] = r['synonym'] or ''
    any_source = defaultdict(str)
    for (src, fn), syn in synonyms.items():
        any_source[fn] = any_source[fn] or syn

    by_module: dict[int, list] = defaultdict(list)
    for r in rows:
        by_module[r['module_id']].append(r)

    for procs in by_module.values():
        first = procs[0]
        path = engine.module_file_path(first)
        lines = _read_lines(path) if path else None
        regions = _regions(lines) if lines is not None else []
        obj = _object_of(first['module_name'])
        obj_cf = obj.casefold()
        synonym = synonyms.get((first['source_id'], obj_cf)) or any_source.get(obj_cf, '')
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
