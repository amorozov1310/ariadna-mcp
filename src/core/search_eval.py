"""
Стенд оценки поиска процедур (шаг 5, docs/hybrid-search.md).

Набор вопросов — YAML или JSON (реальные наборы — в data/eval/, вне Git):

    project: myconf
    cases:
      - id: 1
        kind: paraphrase        # exact | paraphrase | ambiguous | negative | extension
        query: "где при проведении списывается себестоимость"
        expect:                 # правильные ответы; [] — в конфигурации этого нет
          - module: "ОбщийМодуль.РасчетСебестоимости"   # «.Модуль» можно не писать
            procedure: "РассчитатьСебестоимость"         # без procedure — любой из модуля

Методы — как агент вызвал бы поиск одним запросом:
  procedures — search_procedures(query): подстрока в имени процедуры;
  code       — search_code(query): фраза в тексте модулей, строка → процедура;
  cards      — search_cards(query): BM25 по карточкам процедур.

Метрики по вопросам с ответом: hit@1/@5/@10 (правильный ответ в топ-K),
MRR (1/место первого правильного), задержка. По отрицательным — сколько
результатов вернул метод (лексический поиск почти всегда что-то находит).

    python -m src.core.search_eval data/eval/myconf.yaml [--project ID] [--details]
    docker exec ariadna python -m src.core.search_eval /data/eval/myconf.yaml
"""

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cards import search_cards

METHODS = ('procedures', 'code', 'cards')
DEPTH = 10


@dataclass
class Case:
    id: str
    kind: str
    query: str
    expect: list[tuple[str, str]]   # (модуль casefold, процедура casefold или '')


@dataclass
class CaseResult:
    case: Case
    method: str
    rank: int | None                # место первого правильного, None — не найден в топ-DEPTH
    returned: int
    ms: float
    top: list[str] = field(default_factory=list)


def _norm_module(name: str) -> str:
    name = name.strip().casefold()
    return name[:-len('.модуль')] if name.endswith('.модуль') else name


def load_cases(path: str | Path) -> tuple[str, list[Case]]:
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() in ('.yaml', '.yml'):
        try:
            import yaml
        except ImportError as e:  # pragma: no cover — PyYAML идёт с uvicorn[standard]
            raise SystemExit('Для YAML нужен PyYAML; или сохраните набор как .json') from e
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    cases = []
    for i, c in enumerate(data.get('cases') or [], 1):
        expect = [(_norm_module(e['module']), (e.get('procedure') or '').strip().casefold())
                  for e in (c.get('expect') or [])]
        cases.append(Case(str(c.get('id', i)), c.get('kind', ''), str(c['query']), expect))
    return data.get('project', ''), cases


def _matches(module: str, procedure: str, expect: list[tuple[str, str]]) -> bool:
    mod = _norm_module(module)
    proc = procedure.casefold()
    return any(mod == em and (not ep or proc == ep) for em, ep in expect)


def _ranked(engine, method: str, query: str) -> list[tuple[str, str]]:
    """Выдача метода как список (модуль, процедура) без повторов, в порядке ранга."""
    if method == 'procedures':
        rows = [(r['module_name'], r['name']) for r in engine.search_procedures(query, limit=DEPTH * 3)]
    elif method == 'cards':
        rows = [(r['module_name'], r['procedure_name']) for r in search_cards(engine, query, limit=DEPTH)]
    elif method == 'code':
        conn = engine.db.read_conn()
        rows = []
        for r in engine.search_code(query, limit=DEPTH * 5):
            p = conn.execute(
                """SELECT p.name FROM procedures p JOIN modules m ON m.id = p.module_id
                   WHERE m.name = ? AND m.source_id = ? AND p.start_line <= ? AND p.end_line >= ?
                   ORDER BY p.start_line DESC LIMIT 1""",
                (r['module_name'], r['source_id'], r['line'], r['line'])).fetchone()
            rows.append((r['module_name'], p['name'] if p else ''))
    else:
        raise ValueError(method)
    seen, out = set(), []
    for row in rows:
        if row not in seen:
            seen.add(row)
            out.append(row)
    return out[:DEPTH]


def evaluate(engine, cases: list[Case], methods=METHODS) -> list[CaseResult]:
    results = []
    for case in cases:
        for method in methods:
            t = time.perf_counter()
            ranked = _ranked(engine, method, case.query)
            ms = (time.perf_counter() - t) * 1000
            rank = next((i for i, (m, p) in enumerate(ranked, 1) if _matches(m, p, case.expect)), None)
            results.append(CaseResult(case, method, rank, len(ranked), ms,
                                      [f'{m}.{p}' for m, p in ranked[:3]]))
    return results


def summarize(results: list[CaseResult]) -> dict[str, dict]:
    """Метрики по методам: всё и в разрезе вида вопроса."""
    out: dict[str, dict] = {}
    for method in dict.fromkeys(r.method for r in results):
        mine = [r for r in results if r.method == method]
        positive = [r for r in mine if r.case.expect]
        negative = [r for r in mine if not r.case.expect]

        def block(rs):
            n = len(rs) or 1
            return {
                'n': len(rs),
                'hit@1': sum(1 for r in rs if r.rank == 1) / n,
                'hit@5': sum(1 for r in rs if r.rank and r.rank <= 5) / n,
                'hit@10': sum(1 for r in rs if r.rank and r.rank <= 10) / n,
                'mrr': sum(1 / r.rank for r in rs if r.rank) / n,
            }
        lat = sorted(r.ms for r in mine)
        out[method] = {
            **block(positive),
            'by_kind': {k: block([r for r in positive if r.case.kind == k])
                        for k in dict.fromkeys(r.case.kind for r in positive)},
            'negatives': len(negative),
            'negatives_with_results': sum(1 for r in negative if r.returned),
            'ms_p50': statistics.median(lat) if lat else 0,
            'ms_p95': lat[max(0, int(len(lat) * .95) - 1)] if lat else 0,
        }
    return out


def format_summary(summary: dict[str, dict]) -> str:
    lines = ['метод        вопросов  hit@1  hit@5  hit@10   MRR   p50 мс  p95 мс  отриц.(с выдачей)']
    for method, s in summary.items():
        lines.append(f"{method:<12} {s['n']:>8}  {s['hit@1']:>5.0%}  {s['hit@5']:>5.0%}  {s['hit@10']:>6.0%}"
                     f"  {s['mrr']:>5.2f}  {s['ms_p50']:>6.0f}  {s['ms_p95']:>6.0f}"
                     f"  {s['negatives']}({s['negatives_with_results']})")
        for kind, b in s['by_kind'].items():
            lines.append(f"  {kind or '—':<10} {b['n']:>8}  {b['hit@1']:>5.0%}  {b['hit@5']:>5.0%}"
                         f"  {b['hit@10']:>6.0%}  {b['mrr']:>5.2f}")
    return '\n'.join(lines)


def _main(argv=None) -> int:
    import argparse
    import os

    from .project_manager import ProjectManager

    ap = argparse.ArgumentParser(prog='python -m src.core.search_eval', description=__doc__.splitlines()[1])
    ap.add_argument('cases', help='набор вопросов .yaml/.json')
    ap.add_argument('--project', help='id проекта (по умолчанию — project из набора)')
    ap.add_argument('--methods', default=','.join(METHODS))
    ap.add_argument('--details', action='store_true', help='построчно: место и топ-3 по каждому вопросу')
    ap.add_argument('--data-dir', default=os.environ.get('DATA_DIR', './data'))
    ap.add_argument('--index-dir', default=os.environ.get('INDEX_DIR') or None)
    args = ap.parse_args(argv)

    project, cases = load_cases(args.cases)
    project = args.project or project
    if not project:
        raise SystemExit('Не указан проект: --project или project: в наборе')
    pm = ProjectManager(args.data_dir, args.index_dir)
    try:
        results = evaluate(pm.get_search(project), cases, tuple(args.methods.split(',')))
    finally:
        pm.close_all()
    print(f'Проект {project}, вопросов {len(cases)}')
    print(format_summary(summarize(results)))
    if args.details:
        for r in results:
            mark = f'#{r.rank}' if r.rank else ('—' if r.case.expect else f'{r.returned} рез.')
            print(f'\n[{r.case.id} {r.case.kind}] {r.method}: {mark}  «{r.case.query}»')
            for t in r.top:
                print(f'    {t}')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
