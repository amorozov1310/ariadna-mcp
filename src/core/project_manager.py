"""
ProjectManager: CRUD for 1C projects, source management, DB pool.
Stores registry in /data/projects.json.
Each project gets its own SQLite at /data/projects/{id}/index.db — or at
{index_dir}/{id}/index.db when index_dir is given (Docker: том вне 9p).
"""

import json
import logging
import shutil
import sqlite3
import time
import os
import threading
import weakref
import zipfile
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import parser_fingerprint
from .db import Database
from .search import SearchEngine

logger = logging.getLogger('ariadna')

# Этап 4/D7: process-wide, not per-ProjectManager-instance. main.py runs
# the Web UI and the MCP server as two independent ProjectManager objects
# in the same process (see main.py:_start_web / _start_mcp) — a lock on
# self would only stop two reindexes started through the *same* one of
# those from overlapping, not a web-triggered and an MCP-triggered one on
# the same project. Keying by (data_dir, project_id) still lets genuinely
# different projects/data dirs run concurrently.
_REINDEX_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_REINDEX_LOCKS_GUARD = threading.Lock()

# Реестр projects.json меняют несколько ProjectManager одного процесса (Web UI,
# MCP SSE и HTTP — см. main.py), каждый по схеме «перечитать → изменить →
# записать». Без общей блокировки правка другого экземпляра между
# перечитыванием и записью затиралась. Поэтому блокировка — на процесс и на
# путь реестра, как _REINDEX_LOCKS; RLock — CRUD-методы вызывают друг друга
# (get_project → _load_registry → _save_registry при миграции). Под ней
# только работа с реестром: распаковка архивов, удаление файлов и
# переиндексация идут снаружи.
_REGISTRY_LOCKS: dict[str, threading.RLock] = {}

# Живые ProjectManager процесса по пути реестра. У каждого (Web UI, MCP SSE,
# MCP HTTP — см. main.py) свой пул Database: свой писатель и свои читающие
# соединения к тому же index.db. Общий пул намеренно не вводится — разные
# экземпляры пишут через разные соединения, и SQLite изолирует их
# транзакции. Но удаление проекта должно закрыть его индекс у всех: иначе
# на Linux MCP продолжал читать удалённый файл (и отдавал данные удалённого
# проекта для нового с тем же id), а на Windows rmtree падал с WinError 32.
_INSTANCES: dict[str, 'weakref.WeakSet[ProjectManager]'] = {}
_INSTANCES_GUARD = threading.Lock()

# Повторы os.replace при записи реестра (Windows, см. _write_registry_file):
# 10 попыток с паузой 20, 40, … мс — в сумме до ~0,9 с.
_REPLACE_ATTEMPTS = 10
_REPLACE_DELAY_SEC = 0.02

# Сколько ждать, пока projects.json снова появится, если он был и пропал. На
# /data через 9p (Docker Desktop на Windows) os.replace не атомарен: замерено
# 154 FileNotFoundError на ~2500 чтений при непрерывной записи. Внутри
# процесса чтение и запись под одной блокировкой, но второй процесс (сервер
# в контейнере и ariadna-index на хосте) так не видно.
_REGISTRY_MISSING_WAIT_SEC = 0.2
_REGISTRY_MISSING_POLL_SEC = 0.01


class ReindexInProgressError(RuntimeError):
    """Удаление отклонено: у проекта идёт переиндексация."""

    def __init__(self, project_id: str):
        super().__init__(f"идёт переиндексация проекта '{project_id}' — повторите после "
                         f"её завершения (get_index_status)")
        self.project_id = project_id


def _copy_sqlite(src: Path, dst: Path) -> None:
    """Копия БД через sqlite3 backup API — с данными, которые ещё лежат в
    -wal (копия одного файла их бы потеряла)."""
    source = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


@dataclass
class SourceInfo:
    id: str
    label: str
    source_type: str = 'main'          # 'main' (конфигурация) | 'extension'
    # Относительно каталога проекта (sources/{id}/…) или абсолютные — см.
    # ProjectManager.source_path.
    report_path: str = ''
    xml_path: str = ''
    indexed_at: str = ''


@dataclass
class IndexStats:
    total_objects: int = 0
    total_attributes: int = 0
    total_forms: int = 0
    total_modules: int = 0
    total_procedures: int = 0
    total_calls: int = 0
    last_indexed: str = ''
    duration_sec: float = 0


@dataclass
class ProjectInfo:
    id: str
    name: str
    description: str = ''
    created_at: str = ''
    status: str = 'empty'               # empty | indexing | ready | error
    sources: list[SourceInfo] = field(default_factory=list)
    index_stats: IndexStats = field(default_factory=IndexStats)


class ProjectManager:
    """
    Manages multiple 1C configuration projects within one container.
    
    File structure:
        /data/
        ├── projects.json
        └── projects/
            └── {project_id}/
                ├── index.db
                └── sources/
                    └── {source_id}/
                        ├── report.txt
                        └── xml/

    index_dir, если задан, — отдельный каталог для индексов SQLite:
    {index_dir}/{project_id}/index.db вместо projects/{project_id}/. В Docker
    это именованный том: ./data монтируется через 9p, где SQLite перечитывает
    файл при каждом полном проходе и не работают WAL и блокировки. Переменную
    окружения INDEX_DIR читают вызывающие (main.py, web/app.py), а не этот
    класс — иначе тесты с ProjectManager(tmpdir) уехали бы в каталог
    разработчика.
    """

    def __init__(self, data_dir: str = '/data', index_dir: str | None = None):
        self.data_dir = Path(data_dir)
        self.index_dir = Path(index_dir) if index_dir else None
        self.projects_dir = self.data_dir / 'projects'
        self.registry_path = self.data_dir / 'projects.json'
        self._db_pool: dict[str, Database] = {}
        # Пул открывают несколько потоков (пул SDK, фоновая переиндексация):
        # без блокировки два потока создавали два Database на один проект, и
        # writer одного терялся.
        self._db_lock = threading.Lock()
        self._registry: dict[str, ProjectInfo] | None = None
        # (st_mtime_ns, st_size) projects.json на момент последней загрузки или
        # собственной записи; None — кэш не сверен с диском.
        self._registry_sig: tuple[int, int] | None = None

        # Ensure directories exist
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        with _INSTANCES_GUARD:
            _INSTANCES.setdefault(self._instances_key(), weakref.WeakSet()).add(self)

    def _instances_key(self) -> str:
        return str(self.registry_path.resolve())

    def _siblings(self) -> list['ProjectManager']:
        """Все живые ProjectManager процесса с тем же реестром, включая себя."""
        with _INSTANCES_GUARD:
            return list(_INSTANCES.get(self._instances_key(), ()))

    def _close_project_db_everywhere(self, project_id: str) -> None:
        """Закрыть Database проекта и убрать его из пула у всех экземпляров
        процесса с тем же реестром — перед удалением или заменой файла
        индекса. Трогаются только Database с тем же путём индекса: экземпляр
        с другим index_dir держит другой файл."""
        index_path = os.path.abspath(self._index_path(project_id))
        for pm in self._siblings():
            with pm._db_lock:
                db = pm._db_pool.get(project_id)
                if db is None or os.path.abspath(db.db_path) != index_path:
                    continue
                del pm._db_pool[project_id]
            db.close()

    # ============================================
    # REGISTRY
    # ============================================

    def _registry_lock(self) -> threading.RLock:
        """Общая на процесс блокировка реестра по его пути (_REGISTRY_LOCKS)."""
        key = str(self.registry_path.resolve())
        with _REINDEX_LOCKS_GUARD:
            lock = _REGISTRY_LOCKS.get(key)
            if lock is None:
                lock = _REGISTRY_LOCKS[key] = threading.RLock()
            return lock

    def _registry_signature(self) -> tuple[int, int] | None:
        try:
            st = self.registry_path.stat()
        except FileNotFoundError:
            return None
        return st.st_mtime_ns, st.st_size

    def _read_registry_text(self, wait: bool) -> tuple[tuple[int, int], str] | None:
        """(подпись, текст) projects.json; None — файла нет. wait — файл был
        раньше, и его отсутствие, скорее всего, временное (замена через 9p):
        подождать его появления до _REGISTRY_MISSING_WAIT_SEC."""
        deadline = time.monotonic() + (_REGISTRY_MISSING_WAIT_SEC if wait else 0)
        while True:
            sig = self._registry_signature()
            if sig is not None:
                try:
                    return sig, self.registry_path.read_text(encoding='utf-8')
                except OSError:
                    pass    # пропал между stat и чтением / занят заменой (Windows)
            if time.monotonic() >= deadline:
                return None
            time.sleep(_REGISTRY_MISSING_POLL_SEC)

    def _registry_known(self) -> bool:
        """Реестр на диске уже был: этот экземпляр его читал или писал."""
        return self._registry_sig is not None or bool(self._registry)

    def _load_registry(self) -> dict[str, ProjectInfo]:
        """Реестр проектов из projects.json.

        Под той же блокировкой, что и запись (_registry_lock): иначе чтение
        попадало между удалением файла и появлением нового и видело пустой
        реестр («project not found. Available: none»).

        В одном процессе работают несколько независимых ProjectManager (MCP
        SSE, MCP HTTP и Web UI — см. main.py), и каждый может менять файл.
        Поэтому кэш отдаётся, только пока файл не изменился с последней
        загрузки или собственной записи, иначе реестр перечитывается —
        иначе MCP не видел проектов и источников, добавленных через Web UI,
        и затирал их своей устаревшей копией.
        """
        with self._registry_lock():
            return self._load_registry_locked()

    def _load_registry_locked(self) -> dict[str, ProjectInfo]:
        cached = self._registry
        sig = self._registry_signature()
        if cached is not None and sig is not None and sig == self._registry_sig:
            return cached

        known = self._registry_known()
        read = self._read_registry_text(wait=known)
        if read is None and known:
            # Файл был и пропал: другой процесс заменяет его на ФС без
            # атомарной замены. Пустой реестр здесь — ложное «проектов нет» и
            # риск затереть им настоящий; отдаём прежний кэш.
            logger.warning("projects.json временно недоступен (идёт замена?) — "
                           "используется прежний реестр")
            return cached if cached is not None else {}

        migrated = False
        if read is not None:
            sig, text = read
            try:
                data = json.loads(text)
                projects = {}
                for pid, pdata in data.get('projects', {}).items():
                    project, dropped = self._project_from_dict(pdata)
                    projects[pid] = project
                    migrated = migrated or dropped
            except (json.JSONDecodeError, TypeError, OSError):
                if cached is not None:
                    # Файл, скорее всего, пишет другой экземпляр прямо сейчас.
                    # Пустой реестр здесь опасен: следующая запись затёрла бы
                    # им настоящий. Отдаём прежний кэш, повторим в следующий раз.
                    return cached
                projects = {}
                sig = None
            self._registry = projects
            self._registry_sig = sig
        else:
            self._registry = {}
            self._registry_sig = None

        if migrated:
            # Записать сразу, чтобы отброшенные поля не разбирались заново
            # при каждой загрузке.
            try:
                self._save_registry()
            except OSError:
                logger.exception("Не удалось записать обновлённый реестр проектов")

        return self._registry

    def _write_registry_file(self, data: dict):
        """Атомарная запись projects.json: другой экземпляр, перечитывающий
        файл, не должен увидеть его наполовину записанным."""
        tmp = self.registry_path.with_name(
            f'{self.registry_path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        # На Windows os.replace падает с PermissionError, пока другой поток
        # держит projects.json открытым на чтение (_load_registry) — это доли
        # миллисекунды, поэтому несколько коротких повторов.
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, self.registry_path)
                return
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(_REPLACE_DELAY_SEC * (attempt + 1))

    @classmethod
    def _project_from_dict(cls, pdata: dict) -> tuple['ProjectInfo', bool]:
        """Собрать ProjectInfo из записи реестра, отбросив поля, оставшиеся
        от выгрузки из информационной базы.

        Выгрузка из информационной базы убрана, но реестры, записанные
        раньше, могут содержать `dump` у проекта или у источника и
        `parent_id` у расширения. SourceInfo(**s) на таких
        ключах упал бы, поэтому они просто выбрасываются; второй элемент
        результата говорит, было ли что выбрасывать — по нему реестр
        перезаписывается один раз.
        """
        pdata = dict(pdata)
        raw_sources = pdata.pop('sources', [])
        stats = IndexStats(**pdata.pop('index_stats', {}))
        dropped = pdata.pop('dump', None) is not None

        sources: list[SourceInfo] = []
        for s in raw_sources:
            s = dict(s)
            for stale in ('dump', 'parent_id', 'extension'):
                if s.pop(stale, None) is not None:
                    dropped = True
            sources.append(SourceInfo(**s))

        return ProjectInfo(**pdata, sources=sources, index_stats=stats), dropped

    @staticmethod
    def _project_to_dict(proj: 'ProjectInfo') -> dict:
        return {
            'id': proj.id,
            'name': proj.name,
            'description': proj.description,
            'created_at': proj.created_at,
            'status': proj.status,
            'sources': [asdict(s) for s in proj.sources],
            'index_stats': asdict(proj.index_stats),
        }

    def _save_registry(self):
        """Save project registry to JSON — the *whole* in-memory registry
        as this instance currently sees it, including projects it hasn't
        touched. Fine for CRUD (create/delete/add_source/...), which is
        synchronous and short, so the in-memory copy this instance is
        working from is essentially current. NOT used by reindex() — see
        _save_project_to_disk for why a long background job needs a
        narrower write."""
        # Именно текущий кэш, без повторного _load_registry(): вызывающий уже
        # изменил в нём проект, а перечитывание (если файл успел поменять
        # другой экземпляр) выбросило бы эти изменения.
        registry = self._registry if self._registry is not None else self._load_registry()
        data = {'projects': {pid: self._project_to_dict(proj) for pid, proj in registry.items()}}
        self._write_registry_file(data)
        self._registry_sig = self._registry_signature()

    def _save_project_to_disk(self, project_id: str, project: 'ProjectInfo | None' = None):
        """Записать результат переиндексации одного проекта, перечитав файл
        и не трогая остальные проекты (Этап 4/D7).

        reindex() держит свой объект проекта всё время прогона и передаёт
        его сюда: реестр за это время мог перечитаться, и registry[project_id]
        — уже другой объект, без статуса, который выставляет reindex. Из
        объекта берутся только поля, которыми владеет переиндексация
        (status, index_stats и у уже известных источников indexed_at/
        report_path); остальное — список источников, имя — остаётся как на
        диске, чтобы не потерять источник, добавленный через Web UI во время
        долгого прогона, и не вернуть удалённый.
        """
        with self._registry_lock():
            self._save_project_to_disk_locked(project_id, project)

    def _save_project_to_disk_locked(self, project_id: str, project: 'ProjectInfo | None'):
        if project is None:
            project = self._load_registry().get(project_id)
            if project is None:
                return

        # Основа — файл на диске. Если его нет или он не читается (замена
        # через 9p другим процессом), — кэш этого экземпляра, но не пустой
        # словарь: иначе реестр записался бы с одним этим проектом.
        disk = None
        read = self._read_registry_text(wait=self._registry_known())
        if read is not None:
            try:
                disk = json.loads(read[1])
            except (json.JSONDecodeError, TypeError):
                disk = None
        if not isinstance(disk, dict):
            if self._registry:
                logger.warning("projects.json недоступен при записи проекта %s — за основу "
                               "взят реестр из памяти", project_id)
            disk = {'projects': {pid: self._project_to_dict(p)
                                 for pid, p in (self._registry or {}).items()}}
        projects = disk.setdefault('projects', {})
        ours = self._project_to_dict(project)
        entry = projects.get(project_id)
        if entry is None:
            projects[project_id] = ours
        else:
            entry['status'] = ours['status']
            entry['index_stats'] = ours['index_stats']
            our_sources = {s['id']: s for s in ours['sources']}
            for src in entry.get('sources', []):
                mine = our_sources.get(src.get('id'))
                if mine is None:
                    continue
                src['indexed_at'] = mine['indexed_at']
                if mine['report_path']:
                    src['report_path'] = mine['report_path']

        self._write_registry_file(disk)
        # Кэш этого экземпляра с записанным файлом не совпадает (остальные
        # проекты и источники взяты с диска) — перечитать при следующем обращении.
        self._registry_sig = None

    # ============================================
    # PROJECT CRUD
    # ============================================

    def list_projects(self) -> list[ProjectInfo]:
        """List all projects."""
        return list(self._load_registry().values())

    def create_project(self, project_id: str, name: str, description: str = '') -> ProjectInfo:
        """Create a new project. Returns ProjectInfo."""
        with self._registry_lock():
            return self._create_project(project_id, name, description)

    def _create_project(self, project_id: str, name: str, description: str) -> ProjectInfo:
        registry = self._load_registry()

        if project_id in registry:
            raise ValueError(f"Project '{project_id}' already exists")

        # Validate id: alphanumeric + underscores/hyphens
        if not project_id or not all(c.isalnum() or c in '-_' for c in project_id):
            raise ValueError(f"Invalid project_id: '{project_id}'. Use alphanumeric, hyphens, underscores.")

        project = ProjectInfo(
            id=project_id,
            name=name,
            description=description,
            created_at=datetime.now().isoformat(),
            status='empty',
        )

        # Create directories
        project_dir = self.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / 'sources').mkdir(exist_ok=True)

        registry[project_id] = project
        self._save_registry()
        return project

    def get_project(self, project_id: str) -> ProjectInfo:
        """Get project by ID. Raises KeyError if not found."""
        registry = self._load_registry()
        if project_id not in registry:
            raise KeyError(f"Project '{project_id}' not found")
        return registry[project_id]

    def update_project(self, project_id: str, **kwargs) -> ProjectInfo:
        """Update project fields (name, description, status)."""
        with self._registry_lock():
            project = self.get_project(project_id)
            for key, value in kwargs.items():
                if hasattr(project, key) and key not in ('id', 'created_at', 'sources', 'index_stats'):
                    setattr(project, key, value)
            self._save_registry()
            return project

    @contextmanager
    def _no_reindex(self, project_id: str):
        """Блокировка переиндексации проекта на время удаления, без ожидания.

        Удаление чистит строки тем же writer'ом, которым пишет индексатор: во
        время прогона оно могло оставить в индексе строки уже удалённого
        источника (индексатор дописал их после чистки). Ждать прогон нельзя —
        он идёт минутами, поэтому отказ с ReindexInProgressError."""
        lock = self._reindex_lock(project_id)
        if not lock.acquire(blocking=False):
            raise ReindexInProgressError(project_id)
        try:
            yield
        finally:
            lock.release()

    def delete_project(self, project_id: str):
        """Delete project and all its data from disk. Во время переиндексации
        проекта — ReindexInProgressError, ничего не удаляется."""
        with self._no_reindex(project_id):
            self._delete_project(project_id)

    def _delete_project(self, project_id: str):
        self.get_project(project_id)          # KeyError, если проекта нет

        # Индекс проекта — закрыть у всех ProjectManager процесса, не только
        # у себя (см. _INSTANCES).
        self._close_project_db_everywhere(project_id)

        # Remove from disk — долго на больших выгрузках, поэтому вне
        # блокировки реестра.
        project_dir = self.projects_dir / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)
        index_parent = self._index_path(project_id).parent
        if index_parent != project_dir and index_parent.exists():
            shutil.rmtree(index_parent)

        with self._registry_lock():
            registry = self._load_registry()
            if registry.pop(project_id, None) is not None:
                self._save_registry()

    # ============================================
    # SOURCE MANAGEMENT
    # ============================================

    def add_source(
        self,
        project_id: str,
        source_id: str,
        label: str,
        source_type: str = 'main',
        report_file: BinaryIO | None = None,
        report_filename: str = '',
        xml_archive: BinaryIO | None = None,
        xml_archive_filename: str = '',
    ) -> SourceInfo:
        """
        Add a source to a project.

        report_file: file-like object with report.txt content (upload)
        xml_archive: file-like object with .zip or .tar.gz of XML dump (upload)

        If files are None, assumes they are already at the expected paths
        (volume mount scenario).
        """
        def check_new(project: ProjectInfo):
            if any(s.id == source_id for s in project.sources):
                raise ValueError(f"Source '{source_id}' already exists in project '{project_id}'")

        check_new(self.get_project(project_id))

        # Файлы — вне блокировки реестра: распаковка архива идёт минутами.
        source_dir = self.projects_dir / project_id / 'sources' / source_id
        source_dir.mkdir(parents=True, exist_ok=True)

        report_path = ''
        xml_path = ''

        # Save report file
        if report_file is not None:
            report_dest = source_dir / 'report.txt'
            with open(report_dest, 'wb') as f:
                while chunk := report_file.read(8192):
                    f.write(chunk)
            report_path = f'sources/{source_id}/report.txt'
        elif (source_dir / 'report.txt').exists():
            report_path = f'sources/{source_id}/report.txt'

        # Extract XML archive
        if xml_archive is not None:
            xml_dest = source_dir / 'xml'
            xml_dest.mkdir(exist_ok=True)
            fname = xml_archive_filename.lower()
            if fname.endswith('.zip'):
                self._extract_zip(xml_archive, xml_dest)
            elif fname.endswith('.tar.gz') or fname.endswith('.tgz'):
                self._extract_tar(xml_archive, xml_dest)
            else:
                # Try zip first, then tar
                try:
                    self._extract_zip(xml_archive, xml_dest)
                except zipfile.BadZipFile:
                    xml_archive.seek(0)
                    self._extract_tar(xml_archive, xml_dest)
            xml_path = f'sources/{source_id}/xml'
        elif (source_dir / 'xml').exists():
            xml_path = f'sources/{source_id}/xml'

        source = SourceInfo(
            id=source_id,
            label=label,
            source_type=source_type,
            report_path=report_path,
            xml_path=xml_path,
        )
        with self._registry_lock():
            # Реестр мог измениться, пока распаковывался архив.
            project = self.get_project(project_id)
            check_new(project)
            project.sources.append(source)
            self._save_registry()
        return source

    def update_source(
        self,
        project_id: str,
        source_id: str,
        label: str | None = None,
        source_type: str | None = None,
        report_file: BinaryIO | None = None,
        report_filename: str = '',
        xml_archive: BinaryIO | None = None,
        xml_archive_filename: str = '',
    ) -> SourceInfo:
        """
        Update an existing source: replace its files and/or change its label/type.

        - If ``report_file`` is given, the old report.txt is replaced.
        - If ``xml_archive`` is given, the old xml/ folder is wiped and the new
          archive extracted.
        - If both files are None, only metadata (label/source_type) is updated.

        Caller must run ``reindex(project_id, source_id=source_id)`` afterwards
        to refresh the database.
        """
        def find_source() -> SourceInfo:
            project = self.get_project(project_id)
            source = next((s for s in project.sources if s.id == source_id), None)
            if source is None:
                raise ValueError(f"Source '{source_id}' not found in project '{project_id}'")
            return source

        find_source()
        changes: dict = {}

        # Файлы — вне блокировки реестра: распаковка архива идёт минутами.
        source_dir = self.projects_dir / project_id / 'sources' / source_id
        source_dir.mkdir(parents=True, exist_ok=True)

        # Replace report file
        if report_file is not None:
            report_dest = source_dir / 'report.txt'
            with open(report_dest, 'wb') as f:
                while chunk := report_file.read(8192):
                    f.write(chunk)
            changes['report_path'] = f'sources/{source_id}/report.txt'

        # Replace XML archive
        if xml_archive is not None:
            xml_dest = source_dir / 'xml'
            # Wipe existing xml/ folder so old files don't linger
            if xml_dest.exists():
                shutil.rmtree(xml_dest)
            xml_dest.mkdir()
            fname = xml_archive_filename.lower()
            if fname.endswith('.zip'):
                self._extract_zip(xml_archive, xml_dest)
            elif fname.endswith('.tar.gz') or fname.endswith('.tgz'):
                self._extract_tar(xml_archive, xml_dest)
            else:
                try:
                    self._extract_zip(xml_archive, xml_dest)
                except zipfile.BadZipFile:
                    xml_archive.seek(0)
                    self._extract_tar(xml_archive, xml_dest)
            changes['xml_path'] = f'sources/{source_id}/xml'

        # Update metadata fields
        if label is not None:
            changes['label'] = label
        if source_type is not None:
            changes['source_type'] = source_type

        # Reset indexed timestamp so dashboard shows source needs reindex
        changes['indexed_at'] = ''

        with self._registry_lock():
            source = find_source()          # реестр мог измениться за распаковку
            for key, value in changes.items():
                setattr(source, key, value)
            self._save_registry()
        return source

    def preview_remove_source(self, project_id: str, source_id: str) -> dict:
        """
        Count what remove_source(project_id, source_id) would delete,
        without deleting anything (Этап 6/D13) — backs the "here's what
        would be removed" summary the MCP tool returns when called
        without confirm=true.
        """
        project = self.get_project(project_id)
        source = next((s for s in project.sources if s.id == source_id), None)
        if source is None:
            raise KeyError(f"Source '{source_id}' not found in project '{project_id}'")

        counts = {'objects': 0, 'attributes': 0, 'forms': 0, 'modules': 0,
                  'procedures': 0, 'calls': 0}
        try:
            conn = self.get_db(project_id).read_conn()
            obj_ids = [r[0] for r in conn.execute(
                "SELECT id FROM metadata_objects WHERE source_id=?", (source_id,)).fetchall()]
            counts['objects'] = len(obj_ids)
            if obj_ids:
                placeholders = ','.join('?' * len(obj_ids))
                counts['attributes'] = conn.execute(
                    f"SELECT COUNT(*) FROM attributes WHERE object_id IN ({placeholders})",
                    obj_ids).fetchone()[0]
                counts['forms'] = conn.execute(
                    f"SELECT COUNT(*) FROM forms WHERE object_id IN ({placeholders})",
                    obj_ids).fetchone()[0]
            counts['modules'] = conn.execute(
                "SELECT COUNT(*) FROM modules WHERE source_id=?", (source_id,)).fetchone()[0]
            counts['procedures'] = conn.execute(
                """SELECT COUNT(*) FROM procedures p JOIN modules m ON m.id = p.module_id
                   WHERE m.source_id=?""", (source_id,)).fetchone()[0]
            counts['calls'] = conn.execute(
                """SELECT COUNT(*) FROM calls c JOIN procedures p ON p.id = c.caller_id
                   JOIN modules m ON m.id = p.module_id WHERE m.source_id=?""",
                (source_id,)).fetchone()[0]
        except Exception:
            pass  # DB not indexed yet — a files-only preview is still useful

        source_dir = self.projects_dir / project_id / 'sources' / source_id
        return {
            'source_id': source_id,
            'label': source.label,
            'source_type': source.source_type,
            'files_exist': source_dir.exists(),
            **counts,
        }

    def remove_source(self, project_id: str, source_id: str):
        """Remove source from project (registry, files, AND indexed DB rows).
        Во время переиндексации проекта — ReindexInProgressError, ничего не
        удаляется (см. _no_reindex)."""
        with self._no_reindex(project_id):
            self._remove_source(project_id, source_id)

    def _remove_source(self, project_id: str, source_id: str):
        project = self.get_project(project_id)
        source = next((s for s in project.sources if s.id == source_id), None)
        if source is None:
            return  # nothing to do

        # Wipe DB rows for this source first (while DB still references it).
        # _clear_source_data handles all cascading deletes including the
        # `sources` row, attributes, modules, procedures, calls, FTS rebuild.
        try:
            db = self.get_db(project_id)
            self._clear_source_data(db, source_id)
            db.conn.commit()
        except Exception:
            # If DB doesn't exist yet (project never indexed), skip — not fatal
            pass

        # Remove source directory — вне блокировки реестра.
        source_dir = self.projects_dir / project_id / 'sources' / source_id
        if source_dir.exists():
            shutil.rmtree(source_dir)

        # Remove from registry
        with self._registry_lock():
            project = self.get_project(project_id)
            project.sources = [s for s in project.sources if s.id != source_id]
            self._save_registry()

    def source_path(self, project_id: str, rel: str) -> Path:
        """Путь к файлу или каталогу источника (report_path, xml_path).

        Обычно путь относительный — от каталога проекта (sources/{id}/xml).
        Абсолютный путь берётся как есть: так источник может лежать вне
        data_dir — медленные тесты указывают им на реальные выгрузки в
        data/projects, а реестр, индексы и сгенерированный report.txt держат
        во временном каталоге (tests/corpus_sandbox.py). Единственное место,
        где путь источника склеивается с каталогом проекта."""
        path = Path(rel)
        return path if path.is_absolute() else self.projects_dir / project_id / path

    def get_source_report_path(self, project_id: str, source_id: str) -> Path | None:
        """Get absolute path to source's report.txt."""
        project = self.get_project(project_id)
        for s in project.sources:
            if s.id == source_id and s.report_path:
                return self.source_path(project_id, s.report_path)
        return None

    def get_source_xml_path(self, project_id: str, source_id: str) -> Path | None:
        """Get absolute path to source's XML directory."""
        project = self.get_project(project_id)
        for s in project.sources:
            if s.id == source_id and s.xml_path:
                return self.source_path(project_id, s.xml_path)
        return None



    # ============================================
    # DATABASE POOL
    # ============================================

    def _index_path(self, project_id: str, legacy: bool = False) -> Path:
        """Единственное место, где строится путь к индексу проекта.
        legacy=True — прежнее место в data_dir (источник переноса)."""
        base = self.projects_dir if legacy or self.index_dir is None else self.index_dir
        return base / project_id / 'index.db'

    def migrate_index_dir(self) -> list[str]:
        """Однократный перенос индексов из data_dir/projects в index_dir.

        Для проекта, у которого нового индекса нет, а старый есть, — копия
        через backup API во временный файл и os.replace: оборванная копия не
        должна выглядеть готовым индексом. Старый файл не удаляется. Ошибка
        одного проекта не прерывает перенос остальных: он просто будет
        переиндексирован. Вызывать при старте, до любых get_db — тот создал
        бы пустой индекс на новом месте. Возвращает id перенесённых проектов.
        """
        if self.index_dir is None:
            return []
        migrated: list[str] = []
        for project in self.list_projects():
            new = self._index_path(project.id)
            old = self._index_path(project.id, legacy=True)
            if new.exists() or not old.exists():
                continue
            tmp = new.with_name(new.name + '.migrating')
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                started = time.monotonic()
                _copy_sqlite(old, tmp)
                self._close_project_db_everywhere(project.id)
                os.replace(tmp, new)
            except Exception:
                logger.exception("Не удалось перенести индекс проекта %s из %s в %s — "
                                 "проект будет переиндексирован", project.id, old, new)
                for leftover in (tmp, Path(f'{tmp}-journal'), Path(f'{tmp}-wal'),
                                 Path(f'{tmp}-shm')):
                    try:
                        leftover.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.exception("Не удалось удалить %s", leftover)
                continue
            logger.info("Индекс проекта %s перенесён в %s: %.1f МБ за %.1f с. "
                        "Старый файл %s больше не используется — его можно удалить вручную.",
                        project.id, new, new.stat().st_size / 1024 / 1024,
                        time.monotonic() - started, old)
            migrated.append(project.id)
        return migrated

    def get_db(self, project_id: str) -> Database:
        """Get or open SQLite connection for project."""
        with self._db_lock:
            db = self._db_pool.get(project_id)
            if db is not None and db.conn:
                return db

            db_path = str(self._index_path(project_id))
            db = Database(db_path)
            db.connect()
            db.init_schema()
            self._db_pool[project_id] = db
            return db

    def get_search(self, project_id: str) -> SearchEngine:
        """Get SearchEngine instance for project."""
        db = self.get_db(project_id)
        roots: dict[str, str] = {}
        try:
            project = self.get_project(project_id)
        except KeyError:
            project = None
        for src in (project.sources if project else []):
            if src.xml_path:
                roots[src.id] = str(self.source_path(project_id, src.xml_path))
        return SearchEngine(db, source_roots=roots)

    # ============================================
    # INDEXING
    # ============================================

    def reindex(self, project_id: str, source_id: str | None = None) -> IndexStats:
        """
        Run indexing for project, synchronously (blocks until done — use
        reindex_async() for a large corpus so MCP/HTTP callers don't time
        out; see IMPROVEMENT_PLAN Этап 4/D7).

        source_id=None → full reindex (all sources)
        source_id="ext_agro" → reindex only that source

        Этап 4/D7 incrementality: this no longer wipes the DB (or the one
        source's rows) before indexing — index_report/index_bsl UPSERT and
        skip-unchanged-by-hash on their own, so a rerun with nothing
        changed on disk is an order of magnitude faster than a cold index.
        """
        from .indexer import Indexer
        from .xml_walker import XMLWalker

        project, db = self._mark_indexing(project_id)

        # Этап 4/D7 item 3: durability is not a concern for an index that's
        # fully rebuildable from source — trade it for write throughput for
        # the duration of this reindex, then restore.
        db.conn.execute("PRAGMA synchronous=OFF")

        try:
            indexer = Indexer(db)

            sources = project.sources if source_id is None else [
                s for s in project.sources if s.id == source_id]
            if source_id is not None and not sources:
                raise KeyError(f"Source '{source_id}' not found in project '{project_id}'")

            # Two-pass indexing, Pass 1 (IMPROVEMENT_PLAN 1.1): scan every
            # source's XML dump for common-module and metadata-object names
            # before parsing any BSL for calls, so a call in one source can
            # be classified against a module defined in another (e.g. an
            # extension calling into main, or vice versa). Always scans ALL
            # of the project's sources (not just the one being reindexed),
            # since a partial reindex of one source still needs to know
            # about common modules defined in the others.
            walker = XMLWalker()
            walked: dict[str, list] = {}
            for s in project.sources:
                if s.xml_path:
                    p = self.source_path(project_id, s.xml_path)
                    if p.exists():
                        walked[s.id] = walker.walk(str(p))
            known_modules, known_objects, common_module_files = Indexer.collect_known_facts(
                [mf for mfs in walked.values() for mf in mfs])

            # Этап 7 (cross-module factory-function inference): a second,
            # full parse of every common module — real extra cost (see
            # scan_known_factory_functions' own docstring), accepted for
            # the accuracy gain (fewer bogus common_module edges from
            # `Var = ОбщегоНазначения.НовыйПустойЛист()`-style calls).
            db.update_state(progress_phase='factory_functions')
            known_factory_functions = Indexer.scan_known_factory_functions(common_module_files)

            total_files = sum(len(walked.get(s.id, [])) for s in sources)
            progress = {'done': 0, 'phase': 'bsl'}
            current_fp = parser_fingerprint.parser_fingerprint()
            parser_states = db.get_parser_states()

            def progress_cb(current: int, total: int):
                # `current`/`total` are within one source's own index_bsl
                # call — translate to a running total across all sources
                # being (re)indexed in this call.
                db.update_state(
                    progress_current=progress['done'] + current,
                    progress_total=total_files, progress_phase=progress['phase'])

            db.update_state(progress_total=total_files, progress_phase='report')

            # Этап 4/D7: durations sum across sources (real per-call work),
            # but object/module/procedure/call *counts* do not — index_bsl's
            # own stats dict only reflects files it actually reparsed this
            # run, so a rerun that skips everything (incrementality) would
            # report near-zero counts despite the DB being fully populated.
            # _calc_stats_from_db (an actual COUNT(*) over the tables)
            # always reflects reality regardless of what got skipped.
            duration_sec = 0.0
            for source in sources:
                reason = self._full_reparse_reason(db, source.id, parser_states.get(source.id),
                                                   current_fp)
                if reason:
                    logger.info("Проект %s, источник %s: полный перепарс BSL — %s",
                                project_id, source.id, reason)
                progress['phase'] = 'bsl_full' if reason else 'bsl'
                stats = self._index_source(indexer, project_id, source,
                                            known_modules, known_objects,
                                            known_factory_functions,
                                            progress_cb=progress_cb,
                                            force_reparse=bool(reason))
                if current_fp and source.id in walked:
                    db.set_parser_fingerprint(source.id, current_fp, reason)
                duration_sec += stats.get('duration_sec', 0)
                source.indexed_at = datetime.now().isoformat()
                progress['done'] += len(walked.get(source.id, []))

            total_stats = self._calc_stats_from_db(db)
            total_stats.duration_sec = duration_sec

            # Resolve the call graph (1.2) — recomputed in full every time,
            # regardless of full vs. partial reindex: it's cheap (table
            # scans + dict joins, no per-call queries) and a partial reindex
            # of one source can change how other sources' calls resolve
            # (e.g. an extension now overriding a main-config procedure).
            db.update_state(progress_phase='resolving')
            indexer.resolve_calls()

            total_stats.last_indexed = datetime.now().isoformat()
            total_stats.duration_sec = round(total_stats.duration_sec, 2)
            project.index_stats = total_stats
            project.status = 'ready'
            db.update_state(status='ready', progress_phase='', progress_current=0,
                             progress_total=0, index_duration_sec=total_stats.duration_sec)
            self._save_project_to_disk(project_id, project)

            return total_stats

        except Exception as e:
            project.status = 'error'
            self._save_project_to_disk(project_id, project)
            db.update_state(status='error', progress_phase='')
            raise
        finally:
            db.conn.execute("PRAGMA synchronous=NORMAL")
            db.conn.commit()

    def _mark_indexing(self, project_id: str) -> tuple['ProjectInfo', Database]:
        """Записать начало переиндексации: status='indexing' в реестр и в
        index_state (прогресс 0, фаза 'scanning'). Возвращает проект и БД.

        Реестр — узкой записью «перечитать → записать один ключ» (Этап 4/D7),
        а не update_project()/_save_registry(): те сбросили бы на диск весь
        кэш этого экземпляра и затёрли бы правки других проектов из другого
        ProjectManager (Web UI и MCP — у каждого свой, см. main.py) за время
        долгого прогона.

        Запись идемпотентна: reindex_async делает её синхронно до ответа
        «started», и reindex() в потоке повторяет её — теми же значениями и
        до того, как прогон что-либо изменил. Поэтому флаг «уже записано»
        не нужен, а reindex() без reindex_async (CLI, тесты) остаётся
        самодостаточным."""
        project = self.get_project(project_id)
        db = self.get_db(project_id)
        project.status = 'indexing'
        self._save_project_to_disk(project_id, project)
        db.update_state(status='indexing', progress_current=0, progress_total=0,
                        progress_phase='scanning')
        return project, db

    def reindex_async(self, project_id: str, source_id: str | None = None) -> dict:
        """
        Start reindex() in a background thread and return immediately
        (IMPROVEMENT_PLAN Этап 4/D7) — for MCP/HTTP callers, so indexing a
        large corpus never risks a client timeout. Progress is polled via
        get_db(project_id).get_stats() (status/progress_current/
        progress_total/progress_phase), same as a synchronous reindex in
        progress looks like from another thread.

        Only one reindex per project may run at a time — a second call
        while one is in flight returns status='already_running' instead of
        starting a concurrent one (which would corrupt the walk/UPSERT
        bookkeeping index_bsl relies on).

        К возврату 'started' статус 'indexing' уже записан (реестр и
        index_state). status='error' (и текст в 'error') — начать не
        удалось, поток не запущен.
        """
        lock = self._reindex_lock(project_id)
        if not lock.acquire(blocking=False):
            return {'status': 'already_running', 'project_id': project_id}

        # Статус 'indexing' — синхронно, до ответа «started»: иначе его
        # записывал поток уже после ответа, и get_index_status сразу после
        # reindex отдавал прежний 'ready' (вживую — 2 прогона из 3). Если
        # запись не удалась (проект удалили между вызовами), поток не
        # запускается.
        try:
            self._mark_indexing(project_id)
        except Exception as e:
            lock.release()
            logger.exception("Не удалось начать переиндексацию проекта %s", project_id)
            return {'status': 'error', 'project_id': project_id,
                    'error': e.args[0] if isinstance(e, KeyError) and e.args else str(e)}

        def _run():
            try:
                self.reindex(project_id, source_id=source_id)
            except Exception:
                # status='error' reindex() уже записал; причина нужна в логе.
                logger.exception("Фоновая переиндексация проекта %s (источник %s) упала",
                                 project_id, source_id or 'все')
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True, name=f'reindex-{project_id}').start()
        return {'status': 'started', 'project_id': project_id}

    def reset_stale_indexing(self) -> list[str]:
        """Снять статус 'indexing' с проектов, у которых он остался от
        прерванного запуска (Этап U0).

        Переиндексация — фоновый поток внутри процесса: если процесс
        перезапустили или уронили посреди прогона, поток умирает, а
        status='indexing' остаётся в реестре навсегда. Дашборд после этого
        показывает вечный спиннер и прячет статистику, а кнопка
        «Переиндексировать» выглядит заблокированной — при том, что не
        индексируется ничего.

        Вызывать ОДИН раз при старте процесса, до запуска рабочих потоков:
        в этот момент ни один прогон идти не может по определению, поэтому
        любой 'indexing' заведомо протухший. Прежний индекс на диске цел и
        пригоден (индексация инкрементальная, UPSERT), поэтому проект
        возвращается в 'ready', если он хоть раз успешно индексировался, и
        в 'empty', если нет. Возвращает id тронутых проектов.
        """
        reset: dict[str, str] = {}
        with self._registry_lock():
            for project in self._load_registry().values():
                if project.status != 'indexing':
                    continue
                project.status = 'ready' if project.index_stats.last_indexed else 'empty'
                reset[project.id] = project.status
            if reset:
                self._save_registry()
        for project_id, status in reset.items():
            try:
                db = self.get_db(project_id)
                db.update_state(status=status, progress_phase='',
                                 progress_current=0, progress_total=0)
            except Exception:
                logger.exception("Не удалось сбросить index_state проекта %s", project_id)
        if reset:
            logger.warning(
                "Статус 'indexing' был протухшим (прогон не пережил перезапуск) "
                "и сброшен у проектов: %s", ', '.join(reset))
        return list(reset)

    # Сколько задание может числиться «выполняется», прежде чем считать,
    # что агент до него не дошёл. Выгрузка ERP занимает минуты; шесть часов
    # — заведомо с запасом, чтобы не убить настоящую долгую выгрузку, если
    # сервер успел перезапуститься, пока агент работал.
    DUMP_RUNNING_TIMEOUT_HOURS = 6




    def _reindex_soon(self, project_id: str, source_id: str | None = None) -> dict:
        """reindex_async(), but never dropped when one is already running
        (Этап U0/U3).

Переиндексация одного источника, запрошенная, пока идёт другая, с
        обычным reindex_async() просто терялась бы: вызов вернул бы
        'already_running', и источник остался бы непроиндексированным.
        Здесь ждущий поток блокируется на той же блокировке проекта и
        повторяет попытку, когда текущий прогон закончится.
        """
        result = self.reindex_async(project_id, source_id=source_id)
        if result['status'] != 'already_running':
            return result

        def _wait_and_run():
            lock = self._reindex_lock(project_id)
            for _ in range(20):
                lock.acquire()   # blocks until the in-flight reindex is done
                lock.release()
                if self.reindex_async(project_id, source_id=source_id)['status'] != 'already_running':
                    return
            logger.warning("Не дождались очереди на переиндексацию %s/%s",
                           project_id, source_id)

        threading.Thread(target=_wait_and_run, daemon=True,
                          name=f'reindex-wait-{project_id}').start()
        return {'status': 'queued', 'project_id': project_id}

    def _reindex_lock(self, project_id: str) -> threading.Lock:
        """Process-wide reindex lock for this (data_dir, project_id) — see
        the module-level _REINDEX_LOCKS comment for why it can't be a
        plain instance attribute (Этап 4/D7)."""
        key = (str(self.data_dir), project_id)
        with _REINDEX_LOCKS_GUARD:
            lock = _REINDEX_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _REINDEX_LOCKS[key] = lock
            return lock

    @staticmethod
    def _full_reparse_reason(db: Database, source_id: str, state: dict | None,
                             current_fp: str) -> str:
        """Почему BSL источника надо переразобрать целиком, не глядя на хеши
        файлов; '' — не надо. Источник без разобранного кода (новый) не в
        счёт: переразбирать нечего, отпечаток просто запишется после."""
        if not current_fp or (state and state['fingerprint'] == current_fp):
            return ''
        has_parsed_code = db.conn.execute(
            "SELECT 1 FROM modules WHERE source_id=? AND file_hash != '' LIMIT 1",
            (source_id,)).fetchone()
        if not has_parsed_code:
            return ''
        if state is None or not state['fingerprint']:
            return 'в индексе нет отпечатка парсера (индекс старого формата)'
        return f"изменилась версия парсера ({state['fingerprint']} → {current_fp})"

    def _index_source(self, indexer, project_id: str, source: SourceInfo,
                       known_modules: set[str] | None = None,
                       known_objects: dict[str, set[str]] | None = None,
                       known_factory_functions: dict[str, set[str]] | None = None,
                       progress_cb=None, force_reparse: bool = False) -> dict:
        """Index one source (report + optionally BSL from XML dump)."""
        stats = {'objects': 0, 'attributes': 0, 'forms': 0,
                 'modules': 0, 'procedures': 0, 'calls': 0, 'duration_sec': 0}

        xml_path = None
        if source.xml_path:
            xml_path = self.source_path(project_id, source.xml_path)

        # 1. Index report (metadata)
        report_path = None
        if source.report_path:
            report_path = self.source_path(project_id, source.report_path)

        # Этап 5/вариант A: отчёт Конфигуратора больше не обязательный
        # ручной шаг — если у источника есть только XML-выгрузка, генерируем
        # report.txt из неё (src/core/report_generator.py). Срабатывает один
        # раз: как только report.txt появится на диске, source.report_path
        # больше не пуст, и это условие для него больше не выполняется.
        if (report_path is None or not report_path.exists()) and xml_path and xml_path.exists():
            from .report_generator import generate_report
            source_dir = self.projects_dir / project_id / 'sources' / source.id
            generated_path = source_dir / 'report.txt'
            if generate_report(xml_path, generated_path):
                report_path = generated_path
                source.report_path = f'sources/{source.id}/report.txt'

        if report_path and report_path.exists():
            report_stats = indexer.index_report(str(report_path), source_id=source.id,
                                                 source_label=source.label,
                                                 source_type=source.source_type)
            for k in ('objects', 'attributes', 'forms', 'modules', 'duration_sec'):
                stats[k] = report_stats.get(k, 0)

        # 2. Index BSL code (from XML dump)
        if xml_path and xml_path.exists():
            bsl_stats = indexer.index_bsl(str(xml_path), source_id=source.id,
                                           known_modules=known_modules,
                                           known_objects=known_objects,
                                           known_factory_functions=known_factory_functions,
                                           source_label=source.label,
                                           source_type=source.source_type,
                                           force_reparse=force_reparse,
                                           progress_cb=progress_cb)
            stats['modules'] += bsl_stats.get('modules', 0)
            stats['procedures'] = bsl_stats.get('procedures', 0)
            stats['calls'] = bsl_stats.get('calls', 0)
            stats['duration_sec'] += bsl_stats.get('duration_sec', 0)

        return stats

    def _clear_source_data(self, db: Database, source_id: str):
        """Remove all data for a specific source from the DB."""
        conn = db.conn
        if not conn:
            return
        # Get object IDs for this source
        obj_ids = [r[0] for r in conn.execute(
            "SELECT id FROM metadata_objects WHERE source_id=?", (source_id,)
        ).fetchall()]

        if obj_ids:
            placeholders = ','.join('?' * len(obj_ids))
            conn.execute(f"DELETE FROM forms WHERE object_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM attributes WHERE object_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM subsystem_content WHERE subsystem_id IN ({placeholders})", obj_ids)

        conn.execute("DELETE FROM parser_state WHERE source_id=?", (source_id,))

        # module_text_fts has no FK to modules (FTS5 virtual tables can't
        # carry one) — its rowid mirrors modules.id, so remove those rows
        # explicitly before the cascade-deleted modules disappear.
        mod_ids = [r[0] for r in conn.execute(
            "SELECT id FROM modules WHERE source_id=?", (source_id,)
        ).fetchall()]
        if mod_ids:
            placeholders = ','.join('?' * len(mod_ids))
            conn.execute(f"DELETE FROM module_text_fts WHERE rowid IN ({placeholders})", mod_ids)

        conn.execute("DELETE FROM modules WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM metadata_objects WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

        # Rebuild FTS
        conn.execute("INSERT INTO metadata_fts(metadata_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO attributes_fts(attributes_fts) VALUES('rebuild')")
        conn.commit()

    def _calc_stats_from_db(self, db: Database) -> IndexStats:
        """Calculate stats by counting rows in DB."""
        conn = db.conn
        return IndexStats(
            total_objects=conn.execute("SELECT COUNT(*) FROM metadata_objects").fetchone()[0],
            total_attributes=conn.execute("SELECT COUNT(*) FROM attributes").fetchone()[0],
            total_forms=conn.execute("SELECT COUNT(*) FROM forms").fetchone()[0],
            total_modules=conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0],
            total_procedures=conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0],
            total_calls=conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
        )

    # ============================================
    # HELPERS
    # ============================================

    def _extract_zip(self, fileobj: BinaryIO, dest: Path):
        """Extract zip archive to destination."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            while chunk := fileobj.read(8192):
                tmp.write(chunk)
            tmp_path = tmp.name
        try:
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                zf.extractall(dest)
        finally:
            os.unlink(tmp_path)

    def _extract_tar(self, fileobj: BinaryIO, dest: Path):
        """Extract tar.gz archive to destination."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp:
            while chunk := fileobj.read(8192):
                tmp.write(chunk)
            tmp_path = tmp.name
        try:
            with tarfile.open(tmp_path, 'r:*') as tf:
                tf.extractall(dest)
        finally:
            os.unlink(tmp_path)

    def close_all(self):
        """Close all open database connections."""
        with self._db_lock:
            pool = list(self._db_pool.values())
            self._db_pool.clear()
        for db in pool:
            db.close()
