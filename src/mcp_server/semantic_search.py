"""
semantic_search_code — поиск процедур по смыслу запроса (docs/adr/0002-semantic-search-code.md).

Сейчас режим один — лексический: BM25 по поисковым карточкам процедур
(src/core/cards.py) с корнями слов, приоритетом точного имени и
востребованности. Векторная ветка — будущий режим того же инструмента;
режим всегда указан в ответе явно (search_mode), чтобы агент не принимал
лексическую выдачу за векторную.

Модуль без импортов MCP SDK: server.py собирает из результата ответ
(текст + structuredContent), tools.execute_tool — только текст (Web UI).
"""

import hashlib
import re

from ..core.cards import CARD_FORMAT, search_cards, word_roots

SCHEMA_VERSION = '1'
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
SNIPPET_CHARS = 240
SEARCH_MODE = 'lexical'
MODE_REASON = 'векторный поиск не настроен — ранжирование по тексту карточек процедур'

_WORD = re.compile(r'\w+')


def _entity_id(source_id: str, module_name: str, procedure_name: str) -> str:
    """Устойчивый идентификатор процедуры: не меняется при переиндексации
    (в отличие от id строки в индексе), различает источники."""
    raw = f'{source_id}\0{module_name.casefold()}\0{procedure_name.casefold()}'
    return 'proc:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _terms(query: str) -> list[str]:
    words = [w for w in _WORD.findall(query.lower()) if len(w) >= 3]
    return words + [r for w in words for r in word_roots(w)]


def _read_lines(path: str | None) -> list[str] | None:
    if not path:
        return None
    try:
        raw = open(path, 'rb').read()
    except OSError:
        return None
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return None


def _snippet(lines: list[str] | None, hit: dict, terms: list[str]) -> tuple[str, int]:
    """Строка фрагмента, где встретилось слово запроса (иначе — объявление)."""
    if not lines:
        return '', 0
    start, end = max(1, hit['start_line']), min(len(lines), hit['end_line'])
    chosen = 0
    for n in range(start, end + 1):
        low = lines[n - 1].lower()
        if any(t in low for t in terms):
            chosen = n
            break
    if not chosen:
        chosen = max(1, min(hit['proc_start'], len(lines)))
    text = lines[chosen - 1].strip()
    return (text[:SNIPPET_CHARS - 1] + '…') if len(text) > SNIPPET_CHARS else text, chosen


def search(engine, project_id: str, query: str, source_id: str | None,
           limit: int | None, offset: int) -> dict | str:
    """Результат semantic_search_code: словарь (см. ADR 0002) или «Error: …»."""
    query = (query or '').strip()
    if not query:
        return 'Error: query is required'
    warnings: list[str] = []
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    if limit > MAX_LIMIT:
        warnings.append(f'limit={limit} урезан до {MAX_LIMIT} — дальше листайте через offset')
        limit = MAX_LIMIT
    limit = max(1, limit)
    offset = max(0, int(offset or 0))

    conn = engine.db.read_conn()
    has_code = conn.execute('SELECT EXISTS(SELECT 1 FROM procedures)').fetchone()[0]
    has_cards = conn.execute('SELECT EXISTS(SELECT 1 FROM card_chunks)').fetchone()[0]
    if has_code and not has_cards:
        return ('Error: индекс карточек процедур не построен — переиндексируйте проект '
                '(reindex), затем повторите запрос. Пока можно искать search_procedures/search_code.')
    if not has_code:
        warnings.append('в проекте нет проиндексированного кода BSL — искать нечего')
    old_format = conn.execute(
        'SELECT COUNT(*) FROM card_modules WHERE card_format != ?', (CARD_FORMAT,)).fetchone()[0]
    if old_format:
        warnings.append(f'карточки {old_format} модулей построены прежним форматом — '
                        'переиндексируйте проект, ранжирование станет точнее')

    state = engine.db.get_index_progress()
    if state['status'] == 'indexing':
        warnings.append('идёт переиндексация: выдача — по текущему состоянию индекса')

    want = offset + limit + 1
    hits = search_cards(engine, query, source_id=source_id, limit=want, pool=max(200, want * 4))
    page = hits[offset:offset + limit]
    has_more = len(hits) > offset + limit

    terms = _terms(query)
    files: dict[str, list[str] | None] = {}
    items = []
    for n, hit in enumerate(page, offset + 1):
        key = f"{hit['source_id']}\0{hit['module_name']}"
        if key not in files:
            files[key] = _read_lines(engine.module_file_path(hit))
        snippet, snippet_line = _snippet(files[key], hit, terms)
        items.append({
            'rank': n,
            'entity_id': _entity_id(hit['source_id'], hit['module_name'], hit['procedure_name']),
            'source_id': hit['source_id'],
            'kind': 'function' if hit['proc_kind'] in ('Функция', 'Function') else 'procedure',
            'module_name': hit['module_name'],
            'procedure_name': hit['procedure_name'],
            'is_export': bool(hit['is_export']),
            'relative_path': (hit['file_path'] or '').replace('\\', '/'),
            'start_line': hit['proc_start'],
            'end_line': hit['proc_end'],
            'match_start_line': hit['start_line'],
            'match_end_line': hit['end_line'],
            'content_hash': hit['content_hash'] or '',
            'match_methods': [hit['match']],
            'evidence_kind': 'source_excerpt' if snippet else 'card_header',
            'snippet': snippet,
            'snippet_line': snippet_line,
        })
    if any(not i['snippet'] for i in items):
        warnings.append('часть файлов модулей недоступна — у таких результатов нет фрагмента кода')

    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project_id,
        'query': query,
        'search_mode': SEARCH_MODE,
        'mode_reason': MODE_REASON,
        'freshness': {'index_state': state['status'], 'indexed_at': state['updated_at'],
                      'card_format': CARD_FORMAT},
        'items': items,
        'offset': offset,
        'limit': limit,
        'has_more': has_more,
        'next_offset': offset + limit if has_more else None,
        'warnings': warnings,
    }


_MATCH_LABEL = {'exact_name': 'имя', 'fts': 'текст'}


def format_text(data: dict) -> str:
    """Компактный текст того же результата — для модели и Web UI."""
    lines = [f"Режим: {data['search_mode']} ({data['mode_reason']}). "
             f"Индекс: {data['freshness']['index_state']}"
             + (f", {data['freshness']['indexed_at'][:16]}" if data['freshness']['indexed_at'] else '')]
    if not data['items']:
        lines.append('No results found.' if data['offset'] == 0
                     else f"No further results at offset={data['offset']}.")
    for item in data['items']:
        flags = ['экспорт'] if item['is_export'] else []
        flags += [_MATCH_LABEL.get(m, m) for m in item['match_methods']]
        lines.append(f"{item['rank']}. {item['module_name']}.{item['procedure_name']}  "
                     f"[строки {item['start_line']}-{item['end_line']}]  @{item['source_id']}  "
                     f"({', '.join(flags)})")
        if item['snippet']:
            lines.append(f"   L{item['snippet_line']}: {item['snippet']}")
    for w in data['warnings']:
        lines.append(f'Предупреждение: {w}')
    if data['has_more']:
        lines.append(f"(use offset={data['next_offset']} for more)")
    elif data['items']:
        lines.append(f"shown {len(data['items'])} of {data['offset'] + len(data['items'])}")
    lines.append('Код процедуры: get_procedure_code(module_name, procedure_name) из строки результата.')
    return '\n'.join(lines)
