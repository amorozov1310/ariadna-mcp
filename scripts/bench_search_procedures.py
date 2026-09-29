"""
Замер SearchEngine.search_procedures на синтетическом индексе размером с ERP
(~500 тыс. процедур в ~20 тыс. модулей, кириллические имена в стиле 1С).

    python scripts/bench_search_procedures.py                # БД во временном каталоге
    python scripts/bench_search_procedures.py --db /tmp/b.db # переиспользовать/сохранить БД

Для сравнения «до/после» правки запускайте с одним и тем же --db: строится
только при первом запуске, дальше меряется поиск на тех же данных.
"""

import argparse
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.db import Database
from src.core.search import SearchEngine

VERBS = ['Заполнить', 'Получить', 'Установить', 'Проверить', 'Сформировать', 'Записать',
         'Рассчитать', 'Обновить', 'Очистить', 'Загрузить', 'Выгрузить', 'Создать',
         'Удалить', 'Найти', 'Подготовить', 'Отразить', 'Скопировать', 'Перенести']
NOUNS = ['Товары', 'Реквизиты', 'Документ', 'Остатки', 'Цены', 'Контрагента', 'Номенклатуру',
         'Партии', 'Настройки', 'ДанныеФормы', 'Движения', 'СписокОрганизаций', 'Склад',
         'ТабличнуюЧасть', 'СтатьиЗатрат', 'Себестоимость', 'Налоги', 'Взаиморасчеты',
         'Сотрудников', 'Начисления', 'ПараметрыОтбора', 'ВидыЦен', 'ЗаказКлиента']
SUFFIXES = ['', '', '', 'НаСервере', 'НаКлиенте', 'Сервер', 'ПоУмолчанию', 'Документа',
            'ВФорме', 'Отбора', 'Кэш']
EVENTS = ['ПриСозданииНаСервере', 'ПриОткрытии', 'ПередЗаписью', 'ОбработкаПроведения',
          'ОбработкаЗаполнения', 'ПриЗаписи', 'ОбработкаПроверкиЗаполнения', 'ПриИзменении']
KINDS = [('Документ', 'МодульОбъекта'), ('Документ', 'МодульМенеджера'),
         ('Справочник', 'МодульОбъекта'), ('Справочник', 'МодульМенеджера'),
         ('ОбщийМодуль', 'Модуль'), ('РегистрСведений', 'МодульНабораЗаписей'),
         ('Обработка', 'МодульОбъекта'), ('Отчет', 'МодульОбъекта')]

QUERIES = [
    # (подпись, аргументы search_procedures)
    ('подстрока, много совпадений: «ЗаполнитьТовары»', {'query': 'ЗаполнитьТовары'}),
    ('частое точное имя: «ПриСозданииНаСервере»', {'query': 'ПриСозданииНаСервере'}),
    ('нижний регистр: «себестоимость»', {'query': 'себестоимость'}),
    ('редкое имя: «ПеренестиВзаиморасчетыКэш»', {'query': 'ПеренестиВзаиморасчетыКэш'}),
    ('с фильтром модуля: «Проверить» + «Документ»', {'query': 'Проверить', 'module_filter': 'Документ'}),
]


def build(db_path: str, modules: int, per_module: int, seed: int = 1) -> None:
    rnd = random.Random(seed)
    db = Database(db_path)
    db.connect()
    db.init_schema()
    conn = db.conn
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("INSERT INTO sources (id, label) VALUES ('main', 'Main')")
    mod_rows, proc_rows = [], []
    pid = 0
    for mid in range(1, modules + 1):
        kind, mtype = KINDS[mid % len(KINDS)]
        name = f'{kind}.{rnd.choice(NOUNS)}{mid}.{mtype}'
        mod_rows.append((mid, 'main', name, mtype, name.casefold()))
        for _ in range(per_module):
            pid += 1
            if rnd.random() < 0.08:
                pname = rnd.choice(EVENTS)
            else:
                pname = rnd.choice(VERBS) + rnd.choice(NOUNS) + rnd.choice(SUFFIXES)
            proc_rows.append((pid, mid, pname, 'Процедура', pname.casefold(), pid % 7 == 0))
    conn.executemany(
        "INSERT INTO modules (id, source_id, name, module_type, name_cf) VALUES (?, ?, ?, ?, ?)",
        mod_rows)
    conn.executemany(
        "INSERT INTO procedures (id, module_id, name, kind, name_cf, is_export) "
        "VALUES (?, ?, ?, ?, ?, ?)", proc_rows)
    conn.commit()
    db.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', help='путь к БД замера (строится, если файла нет)')
    ap.add_argument('--modules', type=int, default=20_000)
    ap.add_argument('--per-module', type=int, default=25)
    ap.add_argument('--runs', type=int, default=5)
    args = ap.parse_args()

    tmp = None
    db_path = args.db
    if not db_path:
        tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmp.name, 'bench.db')
    if not os.path.exists(db_path):
        t0 = time.perf_counter()
        build(db_path, args.modules, args.per_module)
        print(f"БД построена за {time.perf_counter() - t0:.1f} с: "
              f"{args.modules} модулей × {args.per_module} процедур")

    db = Database(db_path)
    db.connect()
    engine = SearchEngine(db)
    total = db.conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0]
    print(f"Процедур в индексе: {total}")
    print(f"{'запрос':55} {'1-й, мс':>9} {'медиана, мс':>12} {'найдено':>8}")
    for label, kw in QUERIES:
        times = []
        found = 0
        for _ in range(args.runs):
            t0 = time.perf_counter()
            found = len(engine.search_procedures(limit=31, **kw))
            times.append((time.perf_counter() - t0) * 1000)
        print(f"{label:55} {times[0]:9.0f} {statistics.median(times):12.0f} {found:8}")
    db.close()
    if tmp:
        tmp.cleanup()


if __name__ == '__main__':
    main()
