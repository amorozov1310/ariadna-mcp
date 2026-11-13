"""
Сырьё для знаний о базе (инструмент knowledge_sources): отчёты с запросами их схем
компоновки данных и регистры, которыми эти отчёты пользуются.

Запрос типового отчёта — проверенный разработчиками ответ на бизнес-вопрос: в нём
видно, какие регистры, ресурсы и отборы отвечают, например, на «выручку без розницы».
Ариадна только извлекает и отдаёт это сырьё — без модели; знания (понятия «слова
пользователя → объект и как к нему обращаться») из него собирает Тесей. Отпечаток
объекта (object_stat.digest) меняется вместе с запросами, справкой и составом —
по нему Тесей пересобирает только изменившееся после переиндексации.
"""

import re

from .metadata_model import canonicalize_kind

# Вид объекта в тексте запроса (RU и EN) → русское имя вида.
_QUERY_KINDS = {
    'регистрнакопления': 'РегистрНакопления', 'регистрсведений': 'РегистрСведений',
    'регистрбухгалтерии': 'РегистрБухгалтерии', 'регистррасчета': 'РегистрРасчета',
    'документ': 'Документ', 'справочник': 'Справочник', 'плансчетов': 'ПланСчетов',
    'планвидовхарактеристик': 'ПланВидовХарактеристик', 'планвидоврасчета': 'ПланВидовРасчета',
    'перечисление': 'Перечисление', 'журналдокументов': 'ЖурналДокументов',
    'accumulationregister': 'РегистрНакопления', 'informationregister': 'РегистрСведений',
    'accountingregister': 'РегистрБухгалтерии', 'calculationregister': 'РегистрРасчета',
    'document': 'Документ', 'catalog': 'Справочник', 'chartofaccounts': 'ПланСчетов',
    'chartofcharacteristictypes': 'ПланВидовХарактеристик', 'chartofcalculationtypes': 'ПланВидовРасчета',
    'enum': 'Перечисление', 'documentjournal': 'ЖурналДокументов',
}
_REF = re.compile(r'(?<![\wЁё.])(' + '|'.join(_QUERY_KINDS) + r')\.([\wЁё]+)', re.I)

REGISTER_KINDS = ('РегистрНакопления', 'РегистрСведений', 'РегистрБухгалтерии', 'РегистрРасчета')
HELP_CHARS = 400            # справка в выдаче knowledge_sources
QUERY_CHARS = 30000         # запросы одного отчёта при include_query
USED_BY_SHOWN = 10          # отчётов, которые пользуются регистром, — по имени


def query_refs(text: str) -> list[str]:
    """Объекты метаданных, к которым обращается запрос: «РегистрНакопления.Х», «Документ.Y»…
    Виртуальные таблицы (.Остатки, .Обороты) и поля отбрасываются — только вид и имя.
    Ложные (псевдоним «Документ.Ссылка») отсеиваются при выдаче — по наличию объекта."""
    found = {}
    for kind, name in _REF.findall(text or ''):
        full = f'{_QUERY_KINDS[kind.casefold()]}.{name}'
        found.setdefault(full.casefold(), full)
    return sorted(found.values())


def sources(conn, kind: str | None = 'Отчет', full_name: str | None = None, source_id: str | None = None,
            include_query: bool = False, limit: int = 50, offset: int = 0) -> list[dict]:
    """Отчёты (с запросами схем) или регистры (с отчётами, которые ими пользуются)."""
    kind = canonicalize_kind(kind) if kind else 'Отчет'
    where, params = ['m.kind = ?'], [kind]
    if full_name:
        where.append('m.full_name_cf = ?'); params.append(full_name.strip().casefold())
    if source_id:
        where.append('m.source_id = ?'); params.append(source_id)
    if kind == 'Отчет' and not full_name:
        where.append('EXISTS (SELECT 1 FROM object_queries q WHERE q.object_id = m.id)')
    rows = conn.execute(
        f"""SELECT m.id, m.full_name, m.synonym, m.source_id, COALESCE(s.digest, '') AS digest
            FROM metadata_objects m LEFT JOIN object_stat s ON s.object_id = m.id
            WHERE {' AND '.join(where)} ORDER BY m.full_name LIMIT ? OFFSET ?""",
        params + [limit, offset]).fetchall()
    out = []
    for r in rows:
        item = {'full_name': r['full_name'], 'synonym': r['synonym'], 'source_id': r['source_id'],
                'digest': r['digest'][:12]}
        h = conn.execute('SELECT text FROM object_help_fts WHERE rowid = ?', (r['id'],)).fetchone()
        item['help'] = (h[0] if h else '')[:HELP_CHARS]
        if kind == 'Отчет':
            queries = conn.execute('SELECT template, dataset, query FROM object_queries WHERE object_id = ? ORDER BY rowid',
                                   (r['id'],)).fetchall()
            item['queries'] = [{'template': q['template'], 'dataset': q['dataset'], 'chars': len(q['query']),
                                **({'query': q['query']} if include_query else {})} for q in queries]
            # Только существующие объекты: псевдоним «Документ.Ссылка» — не объект.
            item['uses'] = [x[0] for x in conn.execute(
                """SELECT DISTINCT o.full_name FROM object_query_refs f
                   JOIN metadata_objects o ON o.full_name_cf = f.ref_cf
                   WHERE f.object_id = ? ORDER BY o.full_name""", (r['id'],))]
        elif kind in REGISTER_KINDS:
            used = [x[0] for x in conn.execute(
                """SELECT DISTINCT o.full_name FROM object_query_refs f JOIN metadata_objects o ON o.id = f.object_id
                   WHERE f.ref_cf = ? ORDER BY o.full_name""", (r['full_name'].casefold(),))]
            item['used_by'] = used
        out.append(item)
    return out


def format_sources(items: list[dict], start_index: int = 1) -> str:
    if not items:
        return 'No results found.'
    lines = []
    for i, it in enumerate(items, start_index):
        head = f"{i}. {it['full_name']}" + (f" ({it['synonym']})" if it['synonym'] else '') + f"  отпечаток {it['digest']}"
        lines.append(head)
        if it.get('help'):
            lines.append('   справка: ' + ' '.join(it['help'].split()))
        if 'uses' in it:
            lines.append('   источники: ' + (', '.join(it['uses']) or '—'))
        if 'used_by' in it:
            shown = it['used_by'][:USED_BY_SHOWN]
            more = f' и ещё {len(it["used_by"]) - len(shown)}' if len(it['used_by']) > len(shown) else ''
            lines.append(f"   в отчётах ({len(it['used_by'])}): " + (', '.join(shown) + more if shown else '—'))
        if 'queries' in it:
            lines.append('   запросы схем: ' + '; '.join(f"{q['template']}/{q['dataset'] or '—'} ({q['chars']} симв.)"
                                                     for q in it['queries']))
            budget = QUERY_CHARS
            for q in it['queries']:
                if 'query' not in q or budget <= 0:
                    continue
                text = q['query'][:budget]
                budget -= len(text)
                cut = '' if len(text) == len(q['query']) else f'\n// … запрос обрезан: {len(q["query"])} символов'
                lines.append(f"   --- {q['template']}/{q['dataset'] or '—'} ---\n{text}{cut}")
    return '\n'.join(lines)
