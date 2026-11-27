#!/usr/bin/env python3
"""Сравнить удержание памяти и SQLite handles при поиске по N индексам.

Все проекты синтетические и создаются во временном каталоге. Режим «до»
эмулирует неограниченный пул старого ProjectManager; «после» использует
штатный предел. Каждый замер идёт в отдельном процессе, чтобы RSS не зависел
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
import zlib
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
    databases = list(pm._db_pool.values())
    sqlite_connections = sum(bool(db.conn) + len(db._readers) for db in databases)
    return {'rss_mb': round(_rss_mb(), 1), 'sqlite_connections': sqlite_connections,
            'file_descriptors': _fd_count()}


def _worker(project_count: int, pool_limit: int, modules: int, lines: int) -> dict:
    import src.core.project_manager as project_manager
    from src.core.project_manager import ProjectManager

    project_manager.DB_POOL_LIMIT = pool_limit
    with tempfile.TemporaryDirectory(prefix='ariadna-bench-') as temp:
        pm = ProjectManager(str(Path(temp) / 'data'))
        for project_no in range(project_count):
            project_id = f'bench_{project_no:03d}'
            pm.create_project(project_id, project_id)
            db = pm.get_db(project_id)
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

        before = _metrics(pm)
        for project_no in range(project_count):
            pm.get_search(f'bench_{project_no:03d}').search_code('benchmarkToken')
        after = _metrics(pm)
        pm.close_all()
        return {'before_search': before, 'after_search': after}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modules', type=int, default=120, help='синтетических модулей на проект')
    parser.add_argument('--lines', type=int, default=80, help='повторяющихся блоков кода на модуль')
    parser.add_argument('--output', type=Path, default=ROOT / 'scripts' / 'bench_memory_results.txt')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--projects', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--pool-limit', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)

    if args.worker:
        print(json.dumps(_worker(args.projects, args.pool_limit, args.modules, args.lines)))
        return 0

    rows = [f'Синтетический стенд: {args.modules} модулей x {args.lines} блоков на проект.',
            '«До» — неограниченный пул (эмуляция исходного ProjectManager); «после» — пул с лимитом 8.', '']
    for count in (1, 8, 32):
        rows.append(f'N={count}')
        for label, limit in (('до', count), ('после', 8)):
            cmd = [sys.executable, str(Path(__file__).resolve()), '--worker', '--projects', str(count),
                   '--pool-limit', str(limit), '--modules', str(args.modules), '--lines', str(args.lines)]
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=ROOT)
            metrics = json.loads(result.stdout.strip().splitlines()[-1])
            rows.append(f"  {label}: до поиска {metrics['before_search']}; после поиска {metrics['after_search']}")
        rows.append('')
    report = '\n'.join(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report + '\n', encoding='utf-8')
    print(report)
    print(f'Результат записан: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
