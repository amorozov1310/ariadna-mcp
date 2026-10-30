"""
Замер лексического индекса по карточкам процедур (шаг 4, docs/hybrid-search.md).

За один проход по карточкам проекта строит несколько вариантов FTS5 во
временном каталоге и сравнивает размер, время построения, время запроса и
различимость ранга BM25 в топе:
  none   — фрагменты, contentless, detail=none (как module_text_fts);
  column — фрагменты, contentless, detail=column;
  full   — фрагменты, contentless, detail=full (позиции: фразы и частоты);
  head   — только заголовки карточек, detail=full (одна строка на процедуру).

Индекс проекта только читается. В контейнере:
    docker cp scripts/bench_card_fts.py ariadna:/tmp/
    docker exec -e PYTHONPATH=/app ariadna python /tmp/bench_card_fts.py --project ID
"""

import argparse
import os
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.cards import build_cards
from src.core.project_manager import ProjectManager

QUERIES = [
    'себестоимость списания товаров', 'НДС', 'курс валюты', 'остатки товаров на складе',
    'проведение документа', 'ЗаполнитьПоОстаткам', 'ПриСозданииНаСервере',
    'блокировка данных', 'печатная форма', 'права доступа пользователя',
    'контрагент договор', 'отправка электронной почты', 'обновление информационной базы',
    'регламентное задание', 'расчёт зарплаты', 'штрихкод номенклатуры',
]
VARIANTS = {
    'none': 'detail=none',
    'column': 'detail=column',
    'full': 'detail=full',
    'head': 'detail=full',
}
_WORD = re.compile(r'\w+')


def fts_query(text: str) -> str:
    return ' OR '.join(f'"{w}"*' for w in _WORD.findall(text.lower()) if len(w) > 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--limit', type=int, default=0, help='не больше N карточек (0 — все)')
    ap.add_argument('--data-dir', default=os.environ.get('DATA_DIR', './data'))
    ap.add_argument('--index-dir', default=os.environ.get('INDEX_DIR') or None)
    ap.add_argument('--work-dir', default=None)
    args = ap.parse_args(argv)

    work = Path(args.work_dir or tempfile.mkdtemp(prefix='cardfts-'))
    work.mkdir(parents=True, exist_ok=True)
    conns = {}
    for name, detail in VARIANTS.items():
        path = work / f'{name}.db'
        path.unlink(missing_ok=True)
        c = sqlite3.connect(path)
        c.execute('PRAGMA journal_mode=OFF')
        c.execute('PRAGMA synchronous=OFF')
        c.execute(f"CREATE VIRTUAL TABLE card_fts USING fts5(text, tokenize='unicode61', "
                  f"{detail}, content='', contentless_delete=1)")
        c.execute('CREATE TABLE chunks (id INTEGER PRIMARY KEY, procedure_id INTEGER, '
                  'chunk_no INTEGER, start_line INTEGER, end_line INTEGER)')
        conns[name] = c
    print(f'SQLite {sqlite3.sqlite_version}, каталог замера {work}')

    pm = ProjectManager(args.data_dir, args.index_dir)
    labels: dict[int, str] = {}
    t0 = time.time()
    cards = rows = 0
    insert_time = {name: 0.0 for name in VARIANTS}
    try:
        engine = pm.get_search(args.project)
        for card in build_cards(engine):
            cards += 1
            labels[card.procedure_id] = f'{card.module_name}.{card.procedure_name}'
            texts = card.chunk_texts()
            spans = [(c.chunk_no, c.start_line, c.end_line) for c in card.chunks] or [(0, card.start_line, card.end_line)]
            for name, c in conns.items():
                t = time.time()
                if name == 'head':
                    c.execute('INSERT INTO chunks VALUES (?, ?, 0, ?, ?)',
                              (card.procedure_id, card.procedure_id, card.start_line, card.end_line))
                    c.execute('INSERT INTO card_fts(rowid, text) VALUES (?, ?)', (card.procedure_id, card.header()))
                else:
                    for text, (no, s, e) in zip(texts, spans):
                        rowid = rows + 1 + no
                        c.execute('INSERT INTO chunks VALUES (?, ?, ?, ?, ?)', (rowid, card.procedure_id, no, s, e))
                        c.execute('INSERT INTO card_fts(rowid, text) VALUES (?, ?)', (rowid, text))
                insert_time[name] += time.time() - t
            rows += len(texts)
            if cards % 50000 == 0:
                for c in conns.values():
                    c.commit()
                print(f'  {cards} карточек, {rows} фрагментов, {time.time() - t0:.0f} с', flush=True)
            if args.limit and cards >= args.limit:
                break
    finally:
        pm.close_all()
    for c in conns.values():
        c.commit()
    build = time.time() - t0
    print(f'карточек {cards}, фрагментов {rows}, проход {build:.0f} с')

    for name, c in conns.items():
        t = time.time()
        c.execute("INSERT INTO card_fts(card_fts) VALUES ('optimize')")
        c.commit()
        optimize = time.time() - t
        size = (work / f'{name}.db').stat().st_size
        lat, distinct = [], []
        for q in QUERIES:
            m = fts_query(q)
            t = time.time()
            res = c.execute('SELECT f.rowid, bm25(card_fts) AS r, ch.procedure_id FROM card_fts f '
                            'JOIN chunks ch ON ch.id = f.rowid WHERE card_fts MATCH ? '
                            'ORDER BY r LIMIT 20', (m,)).fetchall()
            lat.append((time.time() - t) * 1000)
            distinct.append(len({round(r[1], 6) for r in res}))
        lat.sort()
        print(f'\n[{name}] размер {size / 2**20:.0f} МБ, вставка {insert_time[name]:.0f} с, '
              f'optimize {optimize:.0f} с; запрос p50 {statistics.median(lat):.0f} мс, '
              f'p95 {lat[int(len(lat) * .95) - 1]:.0f} мс; различных рангов в топ-20: '
              f'медиана {statistics.median(distinct):.0f}')
        for q in QUERIES[:4]:
            res = c.execute('SELECT ch.procedure_id FROM card_fts f JOIN chunks ch ON ch.id = f.rowid '
                            'WHERE card_fts MATCH ? ORDER BY bm25(card_fts) LIMIT 3', (fts_query(q),)).fetchall()
            print(f'  «{q}»: ' + ' | '.join(labels.get(r[0], '?') for r in res))
        c.close()
    print(f'\nфайлы замера: {work} (удалить вручную)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
