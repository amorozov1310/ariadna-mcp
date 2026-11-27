#!/usr/bin/env python3
"""Сравнить удержание памяти и SQLite handles при поиске по N индексам.

Все проекты синтетические и создаются во временном каталоге. Режим «до»
эмулирует неограниченный пул старого ProjectManager с писателем в пуле;
«после» использует штатный предел и отдельный writer, закрытый до поиска.
Каждый замер идёт в отдельном процессе, чтобы RSS не зависел
от предыдущего прогона.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _rss_mb() -> float:
    if sys.platform == 'win32':
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        process = kernel32.GetCurrentProcess()
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                               ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.WorkingSetSize / 1024 / 1024
    try:
        with open('/proc/self/status', encoding='ascii') as status:
            for line in status:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 if sys.platform != 'darwin' else 1024 * 1024)


def _fd_count() -> int:
    if sys.platform == 'win32':
        count = wintypes.DWORD()
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return count.value
    try:
        return len(os.listdir('/proc/self/fd'))
    except OSError:
        return -1


def _metrics(pm) -> dict:
    # Учитываем Database, сохранённые поисковыми движками после вытеснения
    # из менеджерского пула: именно они могли бы удерживать SQLite handle.
    databases = list(pm._db_instances)
    sqlite_connections = sum(bool(db.conn) + len(db._readers) for db in databases)
    return {'rss_mb': round(_rss_mb(), 1), 'sqlite_connections': sqlite_connections,
            'fd_or_handles': _fd_count()}


def _worker(project_count: int, pool_limit: int, modules: int, lines: int,
            legacy_writers: bool = False) -> dict:
    import src.core.project_manager as project_manager
    from src.core.db import Database
    from src.core.project_manager import ProjectManager

    project_manager.DB_POOL_LIMIT = pool_limit
    with tempfile.TemporaryDirectory(prefix='ariadna-bench-') as temp:
        pm = ProjectManager(str(Path(temp) / 'data'))
        for project_no in range(project_count):
            project_id = f'bench_{project_no:03d}'
            pm.create_project(project_id, project_id)
            db = (pm.get_db(project_id) if legacy_writers else
                  Database(str(pm._index_path(project_id))))
            if not legacy_writers:
                db.connect()
                db.init_schema()
            try:
                conn = db.conn
                conn.execute("INSERT INTO sources(id, label, source_type) VALUES ('main', 'synthetic', 'main')")
                for module_no in range(modules):
                    name = f'CommonModule.Benchmark{module_no:04d}'
                    content = '\n'.join(
                        f'Procedure WorkLine{line_no}() Export\n    benchmarkToken = {module_no + line_no}\nEndProcedure'
                        for line_no in range(lines))
                    cursor = conn.execute(
                        "INSERT INTO modules(source_id, name, module_type, file_path, line_count, name_cf) "
                        "VALUES ('main', ?, 'common', ?, ?, ?)",
                        (name, f'{name}.bsl', len(content.splitlines()), name.casefold()))
                    module_id = cursor.lastrowid
                    conn.execute('INSERT INTO module_text(module_id, content) VALUES (?, ?)',
                                 (module_id, zlib.compress(content.encode('utf-8'))))
                    conn.execute('INSERT INTO module_text_fts(rowid, text) VALUES (?, ?)', (module_id, content))
                conn.commit()
            finally:
                if not legacy_writers:
                    db.close()

        before = _metrics(pm)
        barrier = threading.Barrier(project_count)
        measured = threading.Barrier(project_count)
        peak = [before['sqlite_connections']]
        done = threading.Event()

        def sample_peak():
            while not done.is_set():
                peak[0] = max(peak[0], _metrics(pm)['sqlite_connections'])
                time.sleep(.002)

        sampler = threading.Thread(target=sample_peak, daemon=True)
        sampler.start()

        def search(project_no):
            barrier.wait()
            project_id = f'bench_{project_no:03d}'
            with pm.read_lease(project_id):
                # Все N объектов и их читающие соединения уже открыты, пока
                # каждая задача снимает метрику; так короткий пик не теряется
                # между двумя опросами измерителя.
                peak[0] = max(peak[0], _metrics(pm)['sqlite_connections'])
                measured.wait()
                return pm.get_search(project_id).search_code('benchmarkToken')

        try:
            with ThreadPoolExecutor(max_workers=project_count) as pool:
                list(pool.map(search, range(project_count)))
        finally:
            done.set()
            sampler.join()
        peak[0] = max(peak[0], _metrics(pm)['sqlite_connections'])
        after = _metrics(pm)
        pm.close_all()
        return {'before_search': before, 'after_search': after, 'peak_connections': peak[0]}


def _manifest_pool_benchmark() -> list[dict]:
    """Four cold-pool HTTP batches against index manifests and call graphs."""
    import src.core.project_manager as project_manager
    from fastapi.testclient import TestClient
    from src.core.project_manager import ProjectManager
    from src.web import app as app_module

    project_manager.DB_POOL_LIMIT = 8
    rows = []
    with tempfile.TemporaryDirectory(prefix='ariadna-manifest-bench-') as temp:
        pm = ProjectManager(str(Path(temp) / 'data'))
        project_ids = [f'manifest_{index:02d}' for index in range(16)]
        for project_id in project_ids:
            pm.create_project(project_id, project_id)
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            client = TestClient(app_module.app, headers={'Host': '127.0.0.1'})

            def request(index: int):
                project_id = project_ids[index % len(project_ids)]
                if index % 2:
                    return client.get(f'/api/projects/{project_id}/manifest')
                return client.get(f'/api/projects/{project_id}/graph/calltree',
                                  params={'procedure': 'Work'})

            for batch in range(4):
                with ThreadPoolExecutor(max_workers=32) as pool:
                    responses = list(pool.map(request, range(batch * 128, (batch + 1) * 128)))
                if any(response.status_code != 200 for response in responses):
                    raise RuntimeError(f'HTTP batch {batch + 1} returned a non-200 response')
                active_readers = sum(reader.active for db in pm._db_pool.values()
                                     for reader in db._readers)
                rows.append({'batch': batch + 1, 'requests': len(responses),
                             'databases': len(pm._db_pool), 'pins': sum(pm._db_pins.values()),
                             'active_readers': active_readers})
                if len(pm._db_pool) > 8 or pm._db_pins or active_readers:
                    raise RuntimeError(f'HTTP batch {batch + 1} left the database pool over limit')
        finally:
            app_module.set_pm(previous)
            pm.close_all()
    return rows


def _background_warm_benchmark() -> dict:
    """Загрузить синтетическую справку под нагрузкой чтения 16 индексов."""
    import shutil
    from fastapi.testclient import TestClient
    from src.core.project_manager import ProjectManager
    from src.web import app as app_module
    from tests.fixtures.generate_platform_help import context_book

    import src.core.project_manager as project_manager
    project_manager.DB_POOL_LIMIT = 8
    fixture = ROOT / 'tests' / 'fixtures' / 'platform_check'
    version = '8.3.28.1'
    project_ids = [f'background_{index:02d}' for index in range(16)]
    with tempfile.TemporaryDirectory(prefix='ariadna-background-bench-') as temp:
        pm = ProjectManager(str(Path(temp) / 'data'))
        previous = app_module._pm
        app_module.set_pm(pm)
        try:
            for project_id in project_ids:
                pm.create_project(project_id, project_id)
                source = pm.projects_dir / project_id / 'sources' / 'sample' / 'xml'
                shutil.copytree(fixture, source)
                pm.add_source(project_id, 'sample', 'synthetic')
                pm.reindex(project_id)
                project = pm.get_project(project_id)
                project.platform_version = ''
                pm._save_project_to_disk(project_id, project)

            client = TestClient(app_module.app, headers={'Host': '127.0.0.1'})
            barrier = threading.Barrier(33)

            def reader(worker: int) -> int:
                barrier.wait(timeout=15)
                total = 0
                for turn in range(64):
                    project_id = project_ids[(worker + turn) % len(project_ids)]
                    with pm.read_lease(project_id) as db:
                        total += db.read_conn().execute('SELECT COUNT(*) FROM modules').fetchone()[0]
                return total

            with ThreadPoolExecutor(max_workers=32) as pool:
                reads = [pool.submit(reader, index) for index in range(32)]
                barrier.wait(timeout=15)
                response = client.post('/api/admin/platform-help', data={'version': version},
                                       files={'context': ('shcntx_ru.hbk', context_book(version))})
                if response.status_code != 200:
                    raise RuntimeError(f'platform help upload returned {response.status_code}')
                if not all(future.result(timeout=60) > 0 for future in reads):
                    raise RuntimeError('background benchmark readers observed an empty index')

            for thread in [thread for thread in threading.enumerate()
                           if thread.name == f'platform-check-warm-{version}']:
                thread.join(timeout=60)
                if thread.is_alive():
                    raise RuntimeError('platform report coordinator did not finish')
            from src.core.platform_check import wait_report_jobs
            wait_report_jobs(list(pm._db_pool.values()))
            archives = sum(bool(list(pm._index_path(pid).parent.glob('platform-check-*.zip')))
                           for pid in project_ids)
            active_readers = sum(reader.active for db in pm._db_pool.values() for reader in db._readers)
            result = {'projects': len(project_ids), 'reports': archives,
                      'databases': len(pm._db_pool), 'pins': sum(pm._db_pins.values()),
                      'active_readers': active_readers}
            if (archives != len(project_ids) or len(pm._db_pool) > 8 or pm._db_pins or active_readers):
                raise RuntimeError(f'background warm benchmark failed: {result}')
            return result
        finally:
            app_module.set_pm(previous)
            pm.close_all()


def _reader_idle_benchmark() -> dict:
    """Всплеск читателей одного индекса и ленивая по времени очистка."""
    import src.core.db as db_module
    import src.core.project_manager as project_manager
    from src.core.db import Database
    from src.core.project_manager import ProjectManager

    old_idle = Database.READER_IDLE_SEC
    old_max_readers = Database.MAX_READERS
    old_pm_idle = project_manager.READER_IDLE_SEC
    old_db_idle = db_module.READER_IDLE_SEC
    Database.READER_IDLE_SEC = 0.5
    Database.MAX_READERS = 16
    project_manager.READER_IDLE_SEC = 0.5
    db_module.READER_IDLE_SEC = 0.5
    try:
        with tempfile.TemporaryDirectory(prefix='ariadna-reader-idle-bench-') as temp:
            pm = ProjectManager(str(Path(temp) / 'data'))
            pm.create_project('idle', 'synthetic')
            db = pm.get_db('idle')
            barrier = threading.Barrier(17)

            def read(_):
                with pm.read_lease('idle') as leased_db:
                    barrier.wait(timeout=10)
                    return leased_db.read_conn().execute('SELECT 1').fetchone()[0]

            try:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    futures = [pool.submit(read, index) for index in range(16)]
                    barrier.wait(timeout=10)
                    if not all(future.result(timeout=10) == 1 for future in futures):
                        raise RuntimeError('idle reader benchmark query failed')
                before = {'connections': sum(not reader._closed for reader in db._readers),
                          'rss_mb': round(_rss_mb(), 1)}
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and sum(
                        not reader._closed for reader in db._readers) > 1:
                    time.sleep(0.02)
                after = {'connections': sum(not reader._closed for reader in db._readers),
                         'rss_mb': round(_rss_mb(), 1)}
                if after['connections'] > 1:
                    raise RuntimeError(f'idle reader cleanup left too many connections: {after}')
                return {'idle_sec': 0.5, 'before': before, 'after': after}
            finally:
                pm.close_all()
    finally:
        Database.READER_IDLE_SEC = old_idle
        Database.MAX_READERS = old_max_readers
        project_manager.READER_IDLE_SEC = old_pm_idle
        db_module.READER_IDLE_SEC = old_db_idle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modules', type=int, default=120, help='синтетических модулей на проект')
    parser.add_argument('--lines', type=int, default=80, help='повторяющихся блоков кода на модуль')
    parser.add_argument('--output', type=Path, default=ROOT / 'scripts' / 'bench_memory_results.txt')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--projects', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--pool-limit', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--legacy-writers', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)

    if args.worker:
        print(json.dumps(_worker(args.projects, args.pool_limit, args.modules, args.lines,
                                 args.legacy_writers)))
        return 0

    rows = [f'Синтетический стенд: {args.modules} модулей x {args.lines} блоков на проект.',
            'RSS, SQLite-соединения, файловые дескрипторы (Windows: process handles).',
            '«До» — неограниченный пул со старым писателем; «после» — лимит 8 и отдельный writer.', '']
    for count in (1, 8, 32):
        rows.append(f'N={count}')
        for label, limit, legacy_writers in (('до', count, True), ('после', 8, False)):
            cmd = [sys.executable, str(Path(__file__).resolve()), '--worker', '--projects', str(count),
                   '--pool-limit', str(limit), '--modules', str(args.modules), '--lines', str(args.lines)]
            if legacy_writers:
                cmd.append('--legacy-writers')
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=ROOT)
            metrics = json.loads(result.stdout.strip().splitlines()[-1])
            rows.append(f"  {label}: до поиска {metrics['before_search']}; после поиска {metrics['after_search']}")
            if label == 'после':
                rows.append(f"  пиковое число соединений при N={count} одновременных операциях: "
                            f"{metrics['peak_connections']}")
        rows.append('')
    report = '\n'.join(rows)
    report += '\nПосле 4 серий по 128 HTTP-запросов манифеста индекса и графа по 16 проектам '
    report += '(до 32 одновременно; холодный пул):\n'
    manifest_rows = _manifest_pool_benchmark()
    for row in manifest_rows:
        report += (f"  серия {row['batch']}: запросов {row['requests']}; Database в пуле "
                   f"{row['databases']} (предел 8); закреплений {row['pins']}; "
                   f"активных читателей {row['active_readers']}\n")
    background = _background_warm_benchmark()
    report += ('\nР¤РѕРЅРѕРІС‹Р№ РїСЂРѕРіСЂРµРІ РїРѕСЃР»Рµ Р·Р°РіСЂСѓР·РєРё СЃРїСЂР°РІРєРё РїСЂРё 32 РїР°СЂР°Р»Р»РµР»СЊРЅС‹С… С‡РёС‚Р°С‚РµР»СЏС… Рё 16 РїСЂРѕРµРєС‚Р°С…:\n'
              f"  РѕС‚С‡С‘С‚РѕРІ {background['reports']}/{background['projects']}; Database РІ РїСѓР»Рµ "
              f"{background['databases']} (РїСЂРµРґРµР» 8); Р·Р°РєСЂРµРїР»РµРЅРёР№ {background['pins']}; "
              f"Р°РєС‚РёРІРЅС‹С… С‡РёС‚Р°С‚РµР»РµР№ {background['active_readers']}\n")
    idle = _reader_idle_benchmark()
    report += (f"\nВсплеск 16 читателей, ожидание порога {idle['idle_sec']} с:\n"
               f"  до очистки: {idle['before']['connections']} читательских соединений; RSS {idle['before']['rss_mb']} МБ\n"
               f"  после очистки: {idle['after']['connections']} читательских соединений; RSS {idle['after']['rss_mb']} МБ\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report + '\n', encoding='utf-8')
    print(report)
    print(f'Результат записан: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
