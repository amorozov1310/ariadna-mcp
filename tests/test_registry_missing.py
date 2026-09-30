"""
Реестр projects.json во время его замены.

Вживую (Docker Desktop на Windows, /data через 9p): get_index_status на
существующий проект ответил «project 'zz_check' not found. Available: none».
На 9p os.replace не атомарен — читающий видит момент, когда файла нет.
Раньше _load_registry тогда делал реестр пустым, а
_save_project_to_disk записывал реестр только с одним проектом.

Теперь чтение идёт под той же блокировкой, что и запись (внутри процесса
файл не пропадает), а временное отсутствие файла, который раньше был
(другой процесс), пережидается короткими повторами; дальше — прежний кэш.
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core import project_manager as pm_module
from src.core.project_manager import ProjectManager


def _ids(pm) -> list[str]:
    return sorted(p.id for p in pm.list_projects())


def _hide_registry(tmpdir: str) -> Path:
    """«Другой процесс» удалил projects.json и ещё не положил новый."""
    registry = Path(tmpdir) / 'projects.json'
    hidden = registry.with_name('projects.json.hidden')
    os.replace(registry, hidden)
    return hidden


def test_list_projects_waits_for_registry_to_reappear():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.create_project('p2', 'p2')
        # Другой экземпляр переписал реестр (новый проект) и заменяет файл.
        other = ProjectManager(tmpdir)
        other.create_project('p3', 'p3')
        hidden = _hide_registry(tmpdir)
        threading.Timer(0.05, lambda: os.replace(hidden, Path(tmpdir) / 'projects.json')).start()
        assert _ids(pm) == ['p1', 'p2', 'p3']      # дождался нового файла


def test_list_projects_keeps_cache_while_registry_is_missing(caplog):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.create_project('p2', 'p2')
        _hide_registry(tmpdir)
        with caplog.at_level(logging.WARNING, logger='ariadna'):
            assert _ids(pm) == ['p1', 'p2']        # а не пустой список
        assert 'временно недоступен' in caplog.text
        assert pm.get_project('p1').id == 'p1'


def test_save_project_to_disk_without_registry_keeps_all_projects(monkeypatch):
    monkeypatch.setattr(pm_module, '_REGISTRY_MISSING_WAIT_SEC', 0.05)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        for pid in ('p1', 'p2', 'p3'):
            pm.create_project(pid, pid)
        project = pm.get_project('p1')
        _hide_registry(tmpdir)
        project.status = 'ready'
        pm._save_project_to_disk('p1', project)
        disk = json.loads((Path(tmpdir) / 'projects.json').read_text(encoding='utf-8'))
        assert sorted(disk['projects']) == ['p1', 'p2', 'p3']
        assert disk['projects']['p1']['status'] == 'ready'


def test_save_project_to_disk_with_corrupt_registry_keeps_all_projects(monkeypatch):
    monkeypatch.setattr(pm_module, '_REGISTRY_MISSING_WAIT_SEC', 0.05)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        for pid in ('p1', 'p2'):
            pm.create_project(pid, pid)
        project = pm.get_project('p2')
        (Path(tmpdir) / 'projects.json').write_text('{"projects": {"p1"', encoding='utf-8')
        pm._save_project_to_disk('p2', project)
        disk = json.loads((Path(tmpdir) / 'projects.json').read_text(encoding='utf-8'))
        assert sorted(disk['projects']) == ['p1', 'p2']


def test_first_run_without_registry_is_empty_and_does_not_wait():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        started = time.monotonic()
        assert pm.list_projects() == []
        assert time.monotonic() - started < pm_module._REGISTRY_MISSING_WAIT_SEC
        pm.create_project('p1', 'p1')
        assert _ids(ProjectManager(tmpdir)) == ['p1']


def test_crud_and_reads_in_parallel_never_see_empty_registry(monkeypatch):
    """Один экземпляр в цикле создаёт и удаляет проект, второй в цикле читает
    реестр. os.replace подменён неатомарным (удалить → пауза →
    переименовать), как на 9p: без общей блокировки чтения читатель
    видел бы отсутствующий файл."""
    real_replace = os.replace

    def non_atomic_replace(src, dst):
        if os.path.exists(dst):
            os.remove(dst)
        time.sleep(0.001)
        real_replace(src, dst)

    monkeypatch.setattr(pm_module.os, 'replace', non_atomic_replace)
    # Без ожидания: повторы замаскировали бы отсутствие блокировки чтения.
    monkeypatch.setattr(pm_module, '_REGISTRY_MISSING_WAIT_SEC', 0)
    with tempfile.TemporaryDirectory() as tmpdir:
        writer, reader = ProjectManager(tmpdir), ProjectManager(tmpdir)
        writer.create_project('keep', 'keep')
        # Сколько раз читатель застал файл отсутствующим (stat не нашёл его).
        # Не по предупреждению в логе: на Windows чтение только что
        # заменённого файла изредка получает отказ в доступе (его открыл
        # антивирус) — это не пропажа файла, и в работе её покрывают повторы.
        absent = []
        real_signature = reader._registry_signature

        def signature():
            sig = real_signature()
            if sig is None:
                absent.append(1)
            return sig

        reader._registry_signature = signature
        stop = threading.Event()
        errors: list = []

        def write():
            try:
                for i in range(60):
                    writer.create_project(f't{i}', 't')
                    writer.delete_project(f't{i}')
            except Exception as e:          # pragma: no cover
                errors.append(e)
            finally:
                stop.set()

        seen_empty = 0
        reads = 0
        t = threading.Thread(target=write)
        t.start()
        while not stop.is_set():
            reads += 1
            if 'keep' not in {p.id for p in reader.list_projects()}:
                seen_empty += 1
        t.join()
        assert not errors, errors
        assert reads > 10
        assert seen_empty == 0
        # Внутри процесса чтение ни разу не попало на отсутствующий файл.
        assert not absent, f"чтение видело отсутствующий файл {len(absent)} раз"
