"""
ProjectManager: CRUD for 1C projects, source management, DB pool.
Stores registry in /data/projects.json.
Each project gets its own SQLite at /data/projects/{id}/index.db — or at
{index_dir}/{id}/index.db when index_dir is given (Docker: том вне 9p).
"""

import json
import logging
import queue
import shutil     # не используется напрямую — тесты подменяют shutil.rmtree через модуль
import sqlite3
import time
import os
import threading
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import BinaryIO
from weakref import WeakSet, WeakValueDictionary, ref

from . import parser_fingerprint
from .archives import extract_archive
from .db import Database, IndexRemovedError, READER_IDLE_SEC
from .fsretry import replace_with_retry, rmtree_with_retry
from .fs_safety import is_link_like, remove_path_safely
from .platform_help import PlatformHelpStore, default_search_roots
from .search import SearchEngine
from . import source_delta

logger = logging.getLogger('ariadna')

# В памяти остаётся не больше восьми открытых объектов Database. Каждый из
# них отдельно ограничивает читающие соединения (Database.MAX_READERS).
DB_POOL_LIMIT = 8
BASE_DIFF_CACHE_LIMIT = 64
BROKEN_CALL_CACHE_LIMIT = 64
PROJECT_ERROR_CACHE_LIMIT = 128
BACKGROUND_ERROR_CACHE_LIMIT = 64
PROJECT_ID_MAX_LENGTH = 64
_RESERVED_PROJECT_IDS = frozenset({'reports', 'platform-help'})
_WINDOWS_RESERVED_NAMES = frozenset({
    'con', 'prn', 'aux', 'nul',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
    *(f'com{i}' for i in ('¹', '²', '³')),
    *(f'lpt{i}' for i in ('¹', '²', '³')),
})


def _validate_project_id(project_id: str, *, allow_reserved: bool = False) -> None:
    if isinstance(project_id, str):
        # Windows reserves these device names even when followed by an
        # extension, and ignores trailing dots or spaces when resolving names.
        windows_name = project_id.rstrip(' .').split('.', 1)[0].casefold()
        if windows_name in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Project ID '{project_id}' is reserved by Windows.")
    if (not isinstance(project_id, str) or not project_id or
            len(project_id) > PROJECT_ID_MAX_LENGTH or
            not all(c.isalnum() or c in '-_' for c in project_id)):
        raise ValueError(
            f"Invalid project_id: '{project_id}'. Use up to {PROJECT_ID_MAX_LENGTH} "
            "letters, digits, hyphens or underscores.")
    if not allow_reserved and project_id.casefold() in _RESERVED_PROJECT_IDS:
        raise ValueError(f"Project ID '{project_id}' is reserved for Ariadna service data.")


def _resolved_path(path: Path) -> str:
    """Сравнение путей с учётом правил текущей файловой системы."""
    return os.path.normcase(str(path.resolve()))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except ValueError:
        return False

# Блокировка переиндексации — на процесс и на (data_dir, project_id), а не
# на экземпляр. main.py держит один ProjectManager на процесс (общий для Web
# UI и MCP), но блокировка охраняет файлы проекта, а не объект: и второй
# экземпляр на том же data_dir (тесты, отдельный инструмент в том же
# процессе) не начнёт параллельный прогон. Разные проекты и data_dir
# индексируются независимо. Другой процесс (ariadna-index рядом с сервером)
# её не видит — его запуск параллельно с сервером не поддерживается.
_REINDEX_LOCKS = WeakValueDictionary()
_REINDEX_LOCKS_GUARD = threading.Lock()

# Реестр projects.json меняется по схеме «перечитать → изменить → записать»
# из потоков серверов (Web UI и MCP — один ProjectManager на процесс,
# см. main.py) и фоновой переиндексации. Без блокировки правка
# одного потока между перечитыванием и записью другого затиралась бы.
# Блокировка — на процесс и путь реестра, как _REINDEX_LOCKS: она охраняет
# файл, а не объект. RLock — CRUD-методы вызывают друг друга (get_project →
# _load_registry → _save_registry при миграции). Под ней только работа с
# реестром: распаковка архивов, удаление файлов и переиндексация идут
# снаружи.
_REGISTRY_LOCKS = WeakValueDictionary()

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


class ProjectNotFoundError(KeyError):
    """get_db: проекта нет в реестре — обычно его удалили, пока запрос шёл
    (см. _delete_project). KeyError — как прежде, но MCP и Web UI отличают
    его от прочих KeyError и отвечают «проект удалён» без трассировки."""

    def __init__(self, project_id: str):
        super().__init__(f"Project '{project_id}' not found")
        self.project_id = project_id


# Ожидаемые ошибки запроса к проекту, который удаляют в этот момент.
PROJECT_GONE = (IndexRemovedError, ProjectNotFoundError)
PROJECT_GONE_MESSAGE = "проект удалён во время запроса — проверьте list_projects"


class ProjectFilesNotRemovedError(OSError):
    """Проект убран из реестра, но его каталог удалить не удалось."""

    def __init__(self, project_id: str, path: Path, cause: OSError):
        super().__init__(f"проект '{project_id}' удалён из реестра, но не удалось удалить "
                         f"{path}: {cause}. Повторите удаление, чтобы дочистить файлы")
        self.project_id = project_id
        self.path = path


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


def _index_error_text(e: Exception) -> str:
    """Причина упавшей индексации для человека: у повреждённой выгрузки
    текст ParseError («syntax error: line 1, column 0») сам по себе ничего
    не говорит."""
    import xml.etree.ElementTree as ET
    text = str(e) or type(e).__name__
    if isinstance(e, ET.ParseError):
        return (f'Файл выгрузки не читается как XML ({text}). Нужна выгрузка Конфигуратора '
                f'в файлы — загрузите её заново.')
    return text


@dataclass
class ProjectInfo:
    id: str
    name: str
    description: str = ''
    created_at: str = ''
    status: str = 'empty'               # empty | indexing | ready | error
    sources: list[SourceInfo] = field(default_factory=list)
    index_stats: IndexStats = field(default_factory=IndexStats)
    # База — проект той же конфигурации без доработок (обычно типовая):
    # с ней сравнивает get_base_diff, из её индекса строится первый индекс
    # проекта (_seed_from_base). '' — базы нет.
    base_project: str = ''
    # Новый релиз той же типовой — проект с его выгрузкой: с ним
    # get_update_conflicts пересекает доработки проекта. '' — не задан.
    release_project: str = ''
    platform_version: str = ''  # пусто — новейшая загруженная справка


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
        service_root = self.index_dir or self.data_dir
        legacy_help_root = service_root / 'platform-help'
        help_root = service_root / '.ariadna-service' / 'platform-help'
        if legacy_help_root.exists() and not help_root.exists():
            # Переносить копированием: в старой раскладке каталог мог также
            # содержать индекс проекта с ID platform-help. Старые файлы
            # остаются доступными для безопасной очистки/ручного восстановления.
            shutil.copytree(legacy_help_root, help_root)
        self.platform_help = PlatformHelpStore(help_root)
        self.projects_dir = self.data_dir / 'projects'
        self.registry_path = self.data_dir / 'projects.json'
        self._db_pool: OrderedDict[str, Database] = OrderedDict()
        self._db_overflow: set[str] = set()
        self._db_instances = WeakSet()
        # Пул открывают несколько потоков (пул SDK, фоновая переиндексация):
        # без блокировки два потока создавали два Database на один проект, и
        # writer одного терялся.
        self._db_lock = threading.Lock()
        self._db_pins: dict[str, int] = {}
        self._db_pins_changed = threading.Condition(self._db_lock)
        # Один открывающий поток на проект; Event позволяет ждать его вне
        # блокировки пула и не сериализует открытия разных индексов.
        self._db_opening: dict[str, threading.Event] = {}
        self._closing = False
        self._pool_generation = 0
        self._reader_trim_lock = threading.Lock()
        self._reader_trim_stop: threading.Event | None = None
        self._reader_trim_thread: threading.Thread | None = None
        self._reader_trim_closing = False
        self._registry: dict[str, ProjectInfo] | None = None
        # Дополнительные записи старого реестра с casefold-конфликтами:
        # сохраняются на диске, но не доступны как проекты до ручного
        # разрешения конфликта.
        self._registry_quarantined: dict[str, ProjectInfo] = {}
        # (st_mtime_ns, st_size) projects.json на момент последней загрузки или
        # собственной записи; None — кэш не сверен с диском.
        self._registry_sig: tuple[int, int] | None = None
        # Последняя сводка отличий по паре (проект, с чем, источник): (ключ,
        # BaseDiff) — см. _diff_between. На крупной конфигурации — секунды.
        self._base_diffs: OrderedDict[tuple, tuple] = OrderedDict()
        self._base_diff_lock = threading.Lock()
        # Сравнение одной пары считается одним потоком (остальные ждут его
        # результат); фоновый прогрев — warm_diffs.
        # Слабые ссылки оставляют только блокировки активных вызовов.
        self._diff_pair_locks = WeakValueDictionary()
        self._warming: dict[str, threading.Thread] = {}
        # project_id -> (ключ состояния индекса, результат find_broken_calls)
        self._broken_calls: OrderedDict[str, tuple] = OrderedDict()
        # Фоновые переиндексации (reindex_async) — close_all их дожидается.
        self._reindexing: set[threading.Thread] = set()
        # Восстановление схемы идёт через одного воркера: несколько больших
        # проектов не запускают полную индексацию одновременно.
        self._schema_reindex_queue: queue.Queue[str] = queue.Queue()
        self._schema_reindex_pending: set[str] = set()
        self._schema_reindex_lock = threading.Lock()
        self._schema_reindex_worker: threading.Thread | None = None
        self._schema_reindex_worker_running = False
        self._reindex_local = threading.local()
        self._platform_workers: set[threading.Thread] = set()
        self._closing_platform_workers = False
        self._platform_warm_pending: OrderedDict[str, tuple[str, bool]] = OrderedDict()
        self._platform_warm_coordinator: threading.Thread | None = None
        # Кэш причин ошибок индексации; постоянная копия хранится рядом с
        # проектом в index-error.txt.
        self._index_errors: OrderedDict[str, str] = OrderedDict()
        self._checks_warming: dict[str, threading.Thread] = {}
        self._closing_checks = False
        self._checks_failed: OrderedDict[str, tuple] = OrderedDict()
        self._warm_failed: OrderedDict[str, tuple] = OrderedDict()

        # Ensure directories exist
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        # Ключ блокировки реестра — от каталога, который точно существует, и
        # один раз: resolve() самого projects.json на Windows в момент его
        # замены (файла нет) мог дать другую запись пути, чем при
        # существующем файле, — и чтение брало другую блокировку.
        self._registry_lock_key = str(self.data_dir.resolve() / 'projects.json')
        # Завершить атомарное восстановление выгрузок до запуска фоновых потоков.
        for project in self._load_registry().values():
            from .platform_check import cleanup_report_temps, report_directory
            report_dir = report_directory(self._index_path(project.id))
            cleanup_report_temps(report_dir, self.index_dir or self.projects_dir)
            # Удалить незавершённые архивы старой раскладки и старого
            # открытого каталога reports/<id>, оставшихся после обновления.
            legacy_report_dir = self._legacy_report_directory(project.id)
            cleanup_report_temps(legacy_report_dir, service_root)
            try:
                remove_path_safely(legacy_report_dir, service_root, recursive=False)
            except OSError:
                pass
            old_index_parent = self._index_path(project.id).parent
            if old_index_parent != self.platform_help.root and old_index_parent != (
                    (self.index_dir or self.data_dir) / 'reports'):
                cleanup_report_temps(old_index_parent, self.index_dir or self.projects_dir)
            legacy_index_parent = self._index_path(project.id, legacy=True).parent
            if legacy_index_parent != old_index_parent:
                cleanup_report_temps(legacy_index_parent, self.projects_dir)
            for source in project.sources:
                local_root = self.projects_dir / project.id / 'sources' / source.id / 'xml'
                root = self.get_source_xml_path(project.id, source.id) or local_root
                workspace = self._source_delta_workspace(project.id, source.id)
                # Корпус из slow-тестов остаётся нетронутым: его журналы не лежат
                # в каталоге XML. Восстановление касается только журнала проекта
                # и выполняется, лишь если удалось взять его блокировку.
                self._recover_source_delta(project.id, source.id, root, workspace)
        old_reports_root = (self.index_dir or self.data_dir) / 'reports'
        if not any(project.id.casefold() == 'reports'
                   for project in self._load_registry().values()):
            try:
                remove_path_safely(old_reports_root, service_root, recursive=False)
            except OSError:
                pass

    def _retire_project_db(self, project_id: str) -> None:
        """Закрыть Database проекта и убрать его из пула — перед удалением
        или заменой файла индекса. Пул один на процесс (один ProjectManager
        на Web UI и MCP, см. main.py), так что закрыть индекс достаточно
        здесь. retire(), а не close(): запросы, которые уже держат этот
        Database (SearchEngine), не должны открыть файл заново."""
        with self._db_pins_changed:
            db = self._db_pool.pop(project_id, None)
            self._db_overflow.discard(project_id)
            opening = self._db_opening.get(project_id)
            self._db_pins_changed.notify_all()
        if db is not None:
            db.retire()
        # Дождаться открытия, начатого до удаления из реестра: его поток
        # повторно проверит проект и закроет соединение до удаления файлов.
        if opening is not None:
            opening.wait()

    def _invalidate_analysis_caches(self, project_id: str) -> None:
        """Освободить результаты, зависящие от индекса проекта."""
        with self._base_diff_lock:
            self._broken_calls.pop(project_id, None)
            for key in [key for key in self._base_diffs if project_id in key[:2]]:
                self._base_diffs.pop(key, None)
            # Активные locks живут в WeakValueDictionary до завершения
            # последнего потока. Удаляем только результат сравнения.

    def _forget_project_resources(self, project_id: str) -> None:
        """Удалить кэши и завершённые задачи, привязанные к удалённому проекту."""
        self._invalidate_analysis_caches(project_id)
        with self._base_diff_lock:
            self._platform_warm_pending.pop(project_id, None)
            self._warm_failed.pop(project_id, None)
            self._checks_failed.pop(project_id, None)
            for mapping in (self._warming, self._checks_warming):
                thread = mapping.get(project_id)
                if thread is None or not thread.is_alive():
                    mapping.pop(project_id, None)
            self._index_errors.pop(project_id, None)

    # ============================================
    # REGISTRY
    # ============================================

    def _registry_lock(self) -> threading.RLock:
        """Общая на процесс блокировка реестра по его пути (_REGISTRY_LOCKS)."""
        key = self._registry_lock_key
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

        Внутри процесса реестр один (один ProjectManager на Web UI и MCP, см.
        main.py), но файл может поменять второй процесс — ariadna-index или
        python -m src.core.indexer рядом с сервером, сервер в контейнере и
        нативный запуск на одном data/. Поэтому кэш отдаётся, только пока
        файл не изменился с последней загрузки или собственной записи, иначе
        реестр перечитывается — иначе изменения второго процесса не были бы
        видны и затирались бы устаревшей копией.
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
            quarantined = {}
            try:
                data = json.loads(text)
                projects = {}
                project_ids_by_key = {}
                for pid, pdata in data.get('projects', {}).items():
                    project, dropped = self._project_from_dict(pdata)
                    key = pid.casefold()
                    previous = project_ids_by_key.get(key)
                    if previous is not None:
                        # Старый реестр с Capital/capital остаётся читаемым:
                        # первый ID сохраняет доступ, последующие конфликтующие
                        # записи карантинируются в памяти и не получают путь,
                        # который на Windows совпадал бы с путём первого.
                        logger.error("Конфликт ID проектов '%s' и '%s' (casefold=%s); "
                                     "оставлен доступным '%s', '%s' пропущен. "
                                     "Исправьте projects.json вручную.",
                                     previous, pid, key, previous, pid)
                        quarantined[pid] = project
                        continue
                    project_ids_by_key[key] = pid
                    projects[pid] = project
                    migrated = migrated or dropped
            except (json.JSONDecodeError, TypeError, OSError):
                if cached is not None:
                    # Файл, скорее всего, пишет другой процесс прямо сейчас.
                    # Пустой реестр здесь опасен: следующая запись затёрла бы
                    # им настоящий. Отдаём прежний кэш, повторим в следующий раз.
                    return cached
                projects = {}
                sig = None
            self._registry = projects
            self._registry_quarantined = quarantined
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
        """Атомарная запись projects.json: другой процесс, перечитывающий
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
        от убранных возможностей.

        Выгрузка из информационной базы и загрузка отчёта по конфигурации
        убраны, но реестры, записанные раньше, могут содержать `dump` у
        проекта или у источника, `parent_id` у расширения и `report_path` у
        источника. SourceInfo(**s) на таких
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
            for stale in ('dump', 'parent_id', 'extension', 'report_path'):
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
            'base_project': proj.base_project,
            'release_project': proj.release_project,
            'platform_version': proj.platform_version,
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
        # другой процесс) выбросило бы эти изменения.
        registry = self._registry if self._registry is not None else self._load_registry()
        all_projects = dict(registry)
        all_projects.update(self._registry_quarantined)
        data = {'projects': {pid: self._project_to_dict(proj)
                             for pid, proj in all_projects.items()}}
        self._write_registry_file(data)
        self._registry_sig = self._registry_signature()

    def _save_project_to_disk(self, project_id: str, project: 'ProjectInfo | None' = None):
        """Записать результат переиндексации одного проекта, перечитав файл
        и не трогая остальные проекты (Этап 4/D7).

        reindex() держит свой объект проекта всё время прогона и передаёт
        его сюда: реестр за это время мог перечитаться, и registry[project_id]
        — уже другой объект, без статуса, который выставляет reindex. Из
        объекта берутся только поля, которыми владеет переиндексация
        (status, index_stats и у уже известных источников indexed_at);
        остальное — список источников, имя — остаётся как на
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

        _validate_project_id(project_id)

        existing_id = next((pid for pid in registry if pid.casefold() == project_id.casefold()), None)
        if existing_id is not None:
            raise ValueError(f"Project '{existing_id}' already exists (IDs are case-insensitive).")

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
            selected_help_version = self.platform_help_version(project_id) if 'platform_version' in kwargs else None
        if selected_help_version is not None:
            from .platform_check import select_report_version
            select_report_version(self._index_path(project_id), selected_help_version)
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

    def delete_project(self, project_id: str) -> bool:
        """Удалить проект и данные; вернуть False, если проект уже отсутствует.

        Во время переиндексации выбрасывается ReindexInProgressError и ничего
        не удаляется. Повторное удаление отсутствующего проекта безопасно.
        """
        with self._no_reindex(project_id):
            return self._delete_project(project_id)

    def _delete_project(self, project_id: str) -> bool:
        """Порядок важен: реестр → закрытие индекса → файлы.

        Пока проект в реестре, любой запрос MCP проходит проверку проекта и
        через get_db открывает (или создаёт пустой) index.db — посреди
        удаления это оставляло на томе новый пустой индекс-сироту, а на
        Windows мешало rmtree. Поэтому проект сначала убирается из реестра:
        новые запросы получают «not found», а get_db без проекта в реестре
        индекс не открывает и не создаёт. Затем индекс закрывается в пуле
        (_retire_project_db), затем удаляются файлы (на больших выгрузках —
        секунды).

        Если удалить файлы не удалось (Windows, файл занят), проекта в
        реестре уже нет, а исключение называет путь. Повторный вызов для
        проекта, которого нет в реестре, но чьи каталоги остались, дочищает
        их. Возвращает False, если проекта и его файлов уже нет, иначе True."""
        _validate_project_id(project_id, allow_reserved=True)
        project_dir = self.projects_dir / project_id
        index_parent = self._index_path(project_id).parent
        current_index_root = self.index_dir or self.projects_dir
        legacy_report_dir = self._legacy_report_directory(project_id)
        service_root = self.index_dir or self.data_dir
        service_reports_root = service_root / 'reports'
        report_dir = current_index_root / '.ariadna-service' / 'reports' / project_id
        project_index_path = _resolved_path(index_parent)
        with self._registry_lock():
            registry = self._load_registry()
            quarantined = next((pid for pid in self._registry_quarantined
                                if pid.casefold() == project_id.casefold()), None)
            if quarantined is not None:
                raise ValueError(f"Нельзя удалить проект '{project_id}', пока в projects.json "
                                 f"есть конфликтующая запись '{quarantined}'. Исправьте конфликт вручную.")
            # Даже на регистрозависимой ОС ID остаются уникальными по
            # casefold. Если старый реестр содержит конфликт, не удаляем
            # каталог, который может принадлежать другой записи.
            conflicting = next((pid for pid in registry
                                if pid != project_id and
                                _resolved_path(self._index_path(pid).parent) == project_index_path), None)
            if conflicting is not None:
                raise ValueError(f"Нельзя удалить проект '{project_id}': его каталог совпадает "
                                 f"с каталогом проекта '{conflicting}'. Исправьте projects.json.")
            if registry.pop(project_id, None) is not None:
                self._save_registry()
            elif not any(path.exists() or is_link_like(path)
                         for path in (project_dir, index_parent, legacy_report_dir,
                                      report_dir)):
                return False

        with self._db_pins_changed:
            self._db_pins_changed.notify_all()

        # Не удалять каталог, пока поток отчёта ещё может атомарно опубликовать
        # архив рядом с индексом. Новые задачи для Database блокируются здесь.
        with self._db_lock:
            db = self._db_pool.get(project_id)
        if db is not None:
            from .platform_check import wait_report_jobs
            wait_report_jobs([db], timeout=None)
        self._retire_project_db(project_id)
        self._forget_project_resources(project_id)

        from .platform_check import cleanup_report_temps, forget_report_project, report_directory
        from .fs_safety import directory_is_safe
        forget_report_project(self._index_path(project_id))
        # Старая раскладка хранила архивы в reports/<id> (а ещё раньше —
        # рядом с index.db). Удаляем незаконченные файлы и готовые архивы в
        # обеих раскладках, не обходя родительский каталог reports целиком.
        cleanup_dirs = [legacy_report_dir]
        if index_parent not in (self.platform_help.root, service_reports_root):
            cleanup_dirs.append(index_parent)
        old_index_parent = self._index_path(project_id, legacy=True).parent
        if old_index_parent not in (index_parent, self.platform_help.root, service_reports_root):
            cleanup_dirs.append(old_index_parent)
        for directory in cleanup_dirs:
            if directory == old_index_parent:
                cleanup_root = self.projects_dir
            elif directory == index_parent:
                cleanup_root = current_index_root
            else:
                cleanup_root = service_root
            cleanup_report_temps(directory, cleanup_root)

        # Каталоги проекта удаляются рекурсивно только если они строго внутри
        # корня и не являются предком служебного каталога или другого проекта.
        candidates = (project_dir, index_parent, legacy_report_dir, report_dir)
        service_paths = (self.platform_help.root, service_reports_root,
                         service_root / '.ariadna-service')
        other_project_paths = [self.projects_dir / pid for pid in registry]
        other_project_paths += [current_index_root / pid for pid in registry]
        roots = {project_dir: self.projects_dir,
                 index_parent: current_index_root,
                 legacy_report_dir: service_root,
                 report_dir: current_index_root}
        for path in dict.fromkeys(candidates):
            if not path.exists() and not is_link_like(path):
                continue
            try:
                root = roots[path]
                resolved = path.resolve()
                protected = any(
                    resolved == service.resolve() or
                    _is_within(service, path)
                    for service in service_paths if service.exists()
                )
                protected = protected or any(
                    _resolved_path(other) == _resolved_path(path) or
                    _is_within(other, path)
                    for other in other_project_paths if other.exists()
                )
                if is_link_like(path):
                    if not remove_path_safely(path, root):
                        logger.warning("Не удалось безопасно удалить ссылку проекта %s", path)
                    continue
                if not _is_within(path, root) or protected:
                    # Для ID, совпадающих со старым общим каталогом справки
                    # или отчётов, удаляем только явно принадлежащие проекту
                    # файлы; содержимое родителя остаётся нетронутым.
                    if path == self.platform_help.root or path == index_parent:
                        if not directory_is_safe(path, root):
                            logger.warning("Пропускаю очистку небезопасного каталога %s", path)
                            continue
                        for pattern in ('index.db', 'index.db-wal', 'index.db-shm',
                                        'index.db-journal', 'platform-check*.zip',
                                        '.platform-check-*.zip.*.tmp'):
                            for owned in path.glob(pattern):
                                if owned.is_file():
                                    remove_path_safely(owned, path, recursive=False)
                    elif path == legacy_report_dir or path == report_dir:
                        if not remove_path_safely(path, root):
                            logger.warning("Пропускаю небезопасный каталог отчётов %s", path)
                    else:
                        if not remove_path_safely(path, root):
                            logger.warning("Пропускаю небезопасный каталог проекта %s", path)
                else:
                    if not remove_path_safely(path, root):
                        logger.warning("Пропускаю небезопасный каталог проекта %s", path)
            except OSError as e:
                logger.error("Проект %s убран из реестра, но каталог %s удалить не удалось: %s. "
                             "Повторите удаление, чтобы дочистить.", project_id, path, e)
                raise ProjectFilesNotRemovedError(project_id, path, e) from e

        # Удалить пустую старую общую папку после очистки последнего проекта.
        try:
            remove_path_safely(service_reports_root, service_root, recursive=False)
        except OSError:
            pass

        Database.forget_schema_lock(str(self._index_path(project_id)))
        return True

    # ============================================
    # SOURCE MANAGEMENT
    # ============================================

    def add_source(
        self,
        project_id: str,
        source_id: str,
        label: str,
        source_type: str = 'main',
        xml_archive: BinaryIO | None = None,
        xml_archive_filename: str = '',
    ) -> SourceInfo:
        """
        Add a source to a project.

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

        xml_path = ''

        # Extract XML archive (проверка путей и пределов — src/core/archives.py)
        if xml_archive is not None:
            extract_archive(xml_archive, xml_archive_filename, source_dir / 'xml')
            xml_path = f'sources/{source_id}/xml'
        elif (source_dir / 'xml').exists():
            xml_path = f'sources/{source_id}/xml'

        source = SourceInfo(
            id=source_id,
            label=label,
            source_type=source_type,
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
        xml_archive: BinaryIO | None = None,
        xml_archive_filename: str = '',
    ) -> SourceInfo:
        with self._no_reindex(project_id):
            source = next((s for s in self.get_project(project_id).sources if s.id == source_id), None)
            if source is None:
                raise ValueError(f"Source '{source_id}' not found in project '{project_id}'")
            workspace = self._source_delta_workspace(project_id, source_id)
            root = self._source_file_root(project_id, source_id, source)
            with source_delta.transaction_lock(workspace):
                self._recover_source_delta(project_id, source_id, root, workspace, lock_held=True)
                return self._update_source_unlocked(project_id, source_id, label, source_type,
                                                    xml_archive, xml_archive_filename)

    def _update_source_unlocked(
        self,
        project_id: str,
        source_id: str,
        label: str | None = None,
        source_type: str | None = None,
        xml_archive: BinaryIO | None = None,
        xml_archive_filename: str = '',
    ) -> SourceInfo:
        """
        Update an existing source: replace its files and/or change its label/type.

        - If ``xml_archive`` is given, the old xml/ folder is wiped and the new
          archive extracted.
        - Otherwise only metadata (label/source_type) is updated.

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

        # Replace XML archive
        if xml_archive is not None:
            # Старый xml/ заменяется целиком (без остатков прежних файлов) и
            # только после успешной проверки и распаковки нового архива.
            extract_archive(xml_archive, xml_archive_filename, source_dir / 'xml')
            changes['xml_path'] = f'sources/{source_id}/xml'
            self._source_manifest_cache_path(project_id, source_id).unlink(missing_ok=True)

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
            with self.read_lease(project_id) as db:
                conn = db.read_conn()
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
                       JOIN modules m ON m.id = p.module_id
                       WHERE m.source_id=? AND (c.callee_kind != 'manager_object' OR EXISTS
                             (SELECT 1 FROM calls_resolved cr WHERE cr.call_id = c.id))""",
                    (source_id,)).fetchone()[0]
        except Exception:
            pass  # До первой индексации счётчики остаются нулевыми.

        return {
            'source_id': source_id,
            'label': source.label,
            'source_type': source.source_type,
            **counts,
        }

    def remove_source(self, project_id: str, source_id: str):
        """Remove source from project (registry, files, AND indexed DB rows).
        Во время переиндексации проекта — ReindexInProgressError, ничего не
        удаляется (см. _no_reindex)."""
        with self._no_reindex(project_id):
            source = next((s for s in self.get_project(project_id).sources if s.id == source_id), None)
            if source is None:
                return
            workspace = self._source_delta_workspace(project_id, source_id)
            root = self._source_file_root(project_id, source_id, source)
            with source_delta.transaction_lock(workspace):
                self._recover_source_delta(project_id, source_id, root, workspace, lock_held=True)
                self._remove_source(project_id, source_id)
                self._source_manifest_cache_path(project_id, source_id).unlink(missing_ok=True)
            try:
                workspace.rmdir()
            except OSError:
                pass

    def _remove_source(self, project_id: str, source_id: str):
        project = self.get_project(project_id)
        source = next((s for s in project.sources if s.id == source_id), None)
        if source is None:
            return  # nothing to do

        # Wipe DB rows for this source first (while DB still references it).
        # _clear_source_data handles all cascading deletes including the
        # `sources` row, attributes, modules, procedures, calls, FTS rebuild.
        try:
            if self._index_path(project_id).exists():
                db = Database(str(self._index_path(project_id)))
                try:
                    db.connect()
                    db.init_schema()
                    self._db_instances.add(db)
                    self._clear_source_data(db, source_id)
                    db.update_state()
                finally:
                    db.close()
        except Exception:
            # If DB doesn't exist yet (project never indexed), skip — not fatal
            pass

        # Remove source directory — вне блокировки реестра.
        source_dir = self.projects_dir / project_id / 'sources' / source_id
        if source_dir.exists():
            rmtree_with_retry(source_dir)

        # Remove from registry
        with self._registry_lock():
            project = self.get_project(project_id)
            project.sources = [s for s in project.sources if s.id != source_id]
            self._save_registry()

    def source_path(self, project_id: str, rel: str) -> Path:
        """Путь к каталогу источника (xml_path).

        Обычно путь относительный — от каталога проекта (sources/{id}/xml).
        Абсолютный путь берётся как есть: так источник может лежать вне
        data_dir — медленные тесты указывают им на реальные выгрузки в
        data/projects, а реестр и индексы держат во временном каталоге (tests/corpus_sandbox.py). Единственное место,
        где путь источника склеивается с каталогом проекта."""
        path = Path(rel)
        return path if path.is_absolute() else self.projects_dir / project_id / path

    def get_source_xml_path(self, project_id: str, source_id: str) -> Path | None:
        """Get absolute path to source's XML directory."""
        project = self.get_project(project_id)
        for s in project.sources:
            if s.id == source_id and s.xml_path:
                return self.source_path(project_id, s.xml_path)
        return None

    def _source_manifest_cache_path(self, project_id: str, source_id: str) -> Path:
        return self.projects_dir / project_id / '.source-manifests' / f'{source_id}.json'

    def _source_delta_workspace(self, project_id: str, source_id: str) -> Path:
        return self.projects_dir / project_id / '.source-delta' / source_id

    def _recover_source_delta(self, project_id: str, source_id: str, root: Path,
                              workspace: Path, *, lock_held: bool = False) -> bool:
        """Восстановить файлы и регистрацию пути из журнала одной транзакции."""
        journal = source_delta._journal_path(workspace)
        try:
            data = json.loads(journal.read_text('utf-8')) if journal.exists() else {}
        except (OSError, ValueError):
            data = {}
        recovered = source_delta.recover(root, workspace, lock_held=lock_held)
        if recovered and not data.get('committed') and 'prior_xml_path' in data:
            with self._registry_lock():
                project = self._load_registry().get(project_id)
                source = next((s for s in project.sources if s.id == source_id), None) if project else None
                if source is not None and source.xml_path != data['prior_xml_path']:
                    source.xml_path = data['prior_xml_path']
                    self._save_registry()
        return recovered

    def _is_local_source_root(self, root: Path, project_id: str, source_id: str) -> bool:
        local_root = self.projects_dir / project_id / 'sources' / source_id / 'xml'
        try:
            return (Path(root).absolute() == local_root.absolute()
                    and not source_delta.is_reparse_point(local_root))
        except OSError:
            return False

    def _source_file_root(self, project_id: str, source_id: str, source=None) -> Path:
        if source is None:
            project = self.get_project(project_id)
            source = next((s for s in project.sources if s.id == source_id), None)
        if source is None:
            raise KeyError(f"Source '{source_id}' not found")
        return (self.get_source_xml_path(project_id, source_id)
                or self.projects_dir / project_id / 'sources' / source_id / 'xml')

    def ensure_source_xml_path(self, project_id: str, source_id: str) -> None:
        """Зарегистрировать существующую локальную выгрузку перед ответом ready."""
        project = self.get_project(project_id)
        source = next((s for s in project.sources if s.id == source_id), None)
        if source is None or source.xml_path:
            return
        root = self._source_file_root(project_id, source_id, source)
        if root.is_dir():
            source_delta._safe_tree(root)
        has_files = False
        if root.is_dir():
            for _base, _dirs, files in os.walk(root, followlinks=False,
                                               onerror=source_delta._raise_walk_error):
                if files:
                    has_files = True
                    break
        if has_files:
            with self._registry_lock():
                source = next((s for s in self.get_project(project_id).sources if s.id == source_id), None)
                if source is not None and not source.xml_path:
                    source.xml_path = f'sources/{source_id}/xml'
                    self._save_registry()

    def source_file_manifest(self, project_id: str, source_id: str) -> dict:
        """Файловый манифест выгрузки; кэш расположен рядом с проектом, вне xml/."""
        with self._no_reindex(project_id):
            root = self._source_file_root(project_id, source_id)
            workspace = self._source_delta_workspace(project_id, source_id)
            with source_delta.transaction_lock(workspace):
                self._recover_source_delta(project_id, source_id, root, workspace, lock_held=True)
                return source_delta.manifest(root, self._source_manifest_cache_path(project_id, source_id))

    def apply_source_delta(self, project_id: str, source_id: str, archive: BinaryIO,
                           base_version: str, target_manifest: dict) -> dict:
        """Применить разницу под той же блокировкой, что защищает индексацию."""
        with self._no_reindex(project_id):
            project = self.get_project(project_id)
            source = next((s for s in project.sources if s.id == source_id), None)
            if source is None:
                raise KeyError(f"Source '{source_id}' not found")
            root = self._source_file_root(project_id, source_id, source)
            workspace = self._source_delta_workspace(project_id, source_id)
            # Восстановить предыдущую транзакцию до вычисления версии, иначе
            # base_version может описать файлы, которые сейчас откатятся.
            with source_delta.transaction_lock(workspace):
                self._recover_source_delta(project_id, source_id, root, workspace, lock_held=True)
                cache = self._source_manifest_cache_path(project_id, source_id)
                current = source_delta.manifest(root, cache)
                if current['version'] != base_version:
                    raise source_delta.DeltaConflict("Манифест источника изменился; запросите его заново")
                # Регистрируем путь до публикации, чтобы сбой после commit не
                # оставил файлы невидимыми для индексатора.
                registered = False
                prior_xml_path = source.xml_path
                if not source.xml_path:
                    with self._registry_lock():
                        source = next((s for s in self.get_project(project_id).sources if s.id == source_id), None)
                        if source is not None:
                            source.xml_path = f'sources/{source_id}/xml'
                            self._save_registry()
                            registered = True
                try:
                    return source_delta.apply(root, archive, target_manifest, base_version, current,
                                              workspace, lock_held=True,
                                              journal_context={'prior_xml_path': prior_xml_path})
                except Exception:
                    if registered:
                        with self._registry_lock():
                            source = next((s for s in self.get_project(project_id).sources
                                           if s.id == source_id), None)
                            if source is not None and source.xml_path == f'sources/{source_id}/xml':
                                source.xml_path = ''
                                self._save_registry()
                    raise



    # ============================================
    # DATABASE POOL
    # ============================================

    def _index_path(self, project_id: str, legacy: bool = False) -> Path:
        """Единственное место, где строится путь к индексу проекта.
        legacy=True — прежнее место в data_dir (источник переноса)."""
        registry = self._registry
        if registry is not None:
            conflicting = next((pid for pid in registry
                                if pid != project_id and
                                pid.casefold() == project_id.casefold()), None)
            if conflicting is not None:
                raise ValueError(f"ID проекта '{project_id}' конфликтует с '{conflicting}' "
                                 "без учёта регистра; исправьте projects.json.")
        base = self.projects_dir if legacy or self.index_dir is None else self.index_dir
        return base / project_id / 'index.db'

    def _legacy_report_directory(self, project_id: str) -> Path:
        """Путь отчётов до перехода в скрытое служебное пространство."""
        base = self.index_dir or self.data_dir
        return base / 'reports' / project_id

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
                self._retire_project_db(project.id)
                replace_with_retry(tmp, new)
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
        """Get or open SQLite connection for project.

        Закэшированный Database отдаётся, только пока его файл индекса — тот
        же, что при открытии (Database.file_replaced, один os.stat): если
        файл удалили или заменили снаружи, старые соединения закрываются и
        индекс открывается заново."""
        self._ensure_reader_trim_thread()
        generation = self._pool_generation
        while True:
            evicted = []
            is_opener = False
            with self._db_pins_changed:
                if self._closing:
                    raise RuntimeError("ProjectManager is closing")
                if generation != self._pool_generation:
                    raise RuntimeError("Database request interrupted by close_all")
                if project_id not in self._load_registry():
                    raise ProjectNotFoundError(project_id)
                db = self._db_pool.get(project_id)
                if db is not None and db.conn and not db.file_replaced():
                    self._db_pool.move_to_end(project_id)
                    return db
                if db is not None:
                    self._db_pool.pop(project_id, None)
                    self._db_overflow.discard(project_id)
                    evicted.append(db)
                opening = self._db_opening.get(project_id)
                if opening is not None:
                    event = opening
                else:
                    limit = max(1, DB_POOL_LIMIT)
                    if len(self._db_pool) + len(self._db_opening) >= limit:
                        evict_id = next((pid for pid in self._db_pool
                                         if not self._db_pins.get(pid)), None)
                        if evict_id is not None:
                            other = self._db_pool.pop(evict_id)
                            self._db_overflow.discard(evict_id)
                            evicted.append(other)
                        # Мягкий предел: если все соединения заняты, открываем
                        # ещё одно. Операция не должна ждать освобождения пула.
                        else:
                            self._db_overflow.add(project_id)
                    event = threading.Event()
                    self._db_opening[project_id] = event
                    is_opener = True
            for old_db in evicted:
                old_db.retire()
            if not is_opener:
                # Ждём открытие этого индекса вне общей блокировки и затем
                # заново проверяем пул/реестр.
                event.wait()
                continue
            db = None
            try:
                db = Database(str(self._index_path(project_id)))
                self._db_instances.add(db)
                # Первое открытие выполняется под блокировкой всего пути:
                # конкурирующий статус/читатель не должен менять journal_mode
                # до завершения схемы и публикации Database в пуле.
                with db._schema_lock:
                    db.connect()
                    db.init_schema()
                    with self._db_pins_changed:
                        self._db_opening.pop(project_id, None)
                        if self._closing or project_id not in self._load_registry():
                            db.retire()
                            event.set()
                            self._db_pins_changed.notify_all()
                            if self._closing:
                                raise RuntimeError("ProjectManager is closing")
                            raise ProjectNotFoundError(project_id)
                        manager_ref = ref(self)
                        def trim_after_lease(manager_ref=manager_ref):
                            manager = manager_ref()
                            if manager is not None:
                                manager._trim_db_pool()
                        db._pool_lease_released = trim_after_lease
                        self._db_pool[project_id] = db
                        event.set()
                        self._db_pins_changed.notify_all()
            except BaseException:
                if db is not None:
                    db.close()
                with self._db_pins_changed:
                    self._db_opening.pop(project_id, None)
                    self._db_overflow.discard(project_id)
                    event.set()
                    self._db_pins_changed.notify_all()
                raise
            if db.stale_schema_recreated or db.rebuild_required:
                self._handle_stale_schema(project_id, db)
            # Параллельные открытия резервируют места до публикации и могут
            # временно обойти мягкий предел. Сжимаем пул после каждого открытия.
            self._trim_db_pool()
            return db

    @contextmanager
    def read_lease(self, project_id: str):
        """Закрепить индекс в пуле на всю пользовательскую операцию."""
        # Сначала резервируем запись пула, чтобы get_db не вытеснил только что
        # открытый объект до начала аренды читателя.
        self._pin_db(project_id)
        try:
            db = self.get_db(project_id)
            with db.read_lease():
                yield db
        finally:
            self._unpin_db(project_id)

    @contextmanager
    def project_pin(self, project_id: str):
        """Удерживать Database в пуле, не перенося читатель между потоками."""
        self._pin_db(project_id)
        try:
            self.get_db(project_id)
            yield
        finally:
            # close_all() joins report workers while holding _db_lock. Their
            # pins must be released without trimming that same pool lock.
            self._unpin_db(project_id, trim_pool=not self._closing_platform_workers)

    def _start_pinned_worker(self, target, name: str, project_ids, *, on_created=None) -> threading.Thread | None:
        """Передать потоку закрепления индексов на всё время его работы."""
        pins = ExitStack()
        try:
            for project_id in dict.fromkeys(project_ids):
                pins.enter_context(self.project_pin(project_id))
        except (KeyError, IndexRemovedError):
            pins.close()
            return None

        def run():
            try:
                target()
            finally:
                pins.close()

        thread = threading.Thread(target=run, daemon=True, name=name)
        try:
            if on_created is not None:
                on_created(thread)
            thread.start()
        except RuntimeError:
            pins.close()
            raise
        return thread

    def _pin_db(self, project_id: str) -> None:
        with self._db_pins_changed:
            self._db_pins[project_id] = self._db_pins.get(project_id, 0) + 1

    def _unpin_db(self, project_id: str, *, trim_pool: bool = True) -> None:
        with self._db_pins_changed:
            count = self._db_pins.get(project_id, 0) - 1
            if count > 0:
                self._db_pins[project_id] = count
            else:
                self._db_pins.pop(project_id, None)
            self._db_pins_changed.notify_all()
        if trim_pool:
            self._trim_db_pool()

    def _trim_db_pool(self) -> None:
        """Сжать мягкое переполнение после освобождения любой аренды читателя."""
        retired = []
        with self._db_pins_changed:
            limit = max(1, DB_POOL_LIMIT)
            while len(self._db_pool) > limit:
                def idle(pid):
                    db = self._db_pool[pid]
                    return (not self._db_pins.get(pid)
                            and not any(reader.active for reader in db._readers))

                evict_id = next((pid for pid in self._db_pool
                                 if pid in self._db_overflow and idle(pid)), None)
                if evict_id is None:
                    evict_id = next((pid for pid in self._db_pool if idle(pid)), None)
                if evict_id is None:
                    break
                retired.append(self._db_pool.pop(evict_id))
                self._db_overflow.discard(evict_id)
            self._db_pins_changed.notify_all()
        for db in retired:
            db.retire()

    def _ensure_reader_trim_thread(self) -> None:
        """Запустить один общий для менеджера лёгкий таймер очистки читателей."""
        with self._reader_trim_lock:
            if self._reader_trim_closing:
                return
            if self._reader_trim_thread is not None and self._reader_trim_thread.is_alive():
                return
            stop = threading.Event()
            interval = min(1.0, max(0.05, max(0.0, READER_IDLE_SEC) / 4))

            def trim_readers():
                while not stop.wait(interval):
                    with self._db_lock:
                        databases = list(self._db_pool.values())
                    for db in databases:
                        db.trim_idle_readers()

            thread = threading.Thread(target=trim_readers, daemon=True,
                                      name='sqlite-reader-idle-trim')
            self._reader_trim_stop = stop
            self._reader_trim_thread = thread
            thread.start()

    def _handle_stale_schema(self, project_id: str, db: Database) -> None:
        """Пометить удалённый устаревший индекс и поставить его в очередь."""
        try:
            project = self.get_project(project_id)
        except KeyError:
            return

        available_sources = [
            source for source in project.sources
            if source.xml_path and self.source_path(project_id, source.xml_path).is_dir()
        ]
        if not available_sources:
            message = (
                '\u0418\u043d\u0434\u0435\u043a\u0441 \u0441\u043e\u0437\u0434\u0430\u043d \u043f\u043e \u0441\u0442\u0430\u0440\u043e\u0439 \u0441\u0445\u0435\u043c\u0435 \u0438 \u0431\u044b\u043b \u043f\u0435\u0440\u0435\u0441\u043e\u0437\u0434\u0430\u043d. '
                '\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 XML \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430; \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u0435 \u0435\u0451 \u0437\u0430\u043d\u043e\u0432\u043e \u0438 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u043f\u0435\u0440\u0435\u0438\u043d\u0434\u0435\u043a\u0441\u0430\u0446\u0438\u044e.'
            )
            with self._registry_lock():
                project = self.get_project(project_id)
                project.status = 'error'
                self._save_project_to_disk(project_id, project)
            self._set_index_error(project_id, message)
            db.update_state(status='error', progress_phase='')
            logger.warning('Проект %s: %s', project_id, message)
            return

        with self._schema_reindex_lock:
            with self._registry_lock():
                project = self.get_project(project_id)
                project.status = 'indexing'
                self._save_project_to_disk(project_id, project)
            db.update_state(
                status='indexing', progress_current=0, progress_total=0,
                progress_phase='scanning')
            # Текущий синхронный или асинхронный reindex уже перестроит
            # индекс. В очередь попадают только открытия со стороны чтения.
            if self._reindex_lock(project_id).locked() or project_id in self._schema_reindex_pending:
                return
            self._schema_reindex_pending.add(project_id)
            self._schema_reindex_queue.put(project_id)
            if not self._schema_reindex_worker_running:
                self._schema_reindex_worker_running = True
                self._schema_reindex_worker = threading.Thread(
                    target=self._run_schema_reindex_queue, daemon=True,
                    name='schema-reindex-queue')
                self._schema_reindex_worker.start()

    def _run_schema_reindex_queue(self) -> None:
        while True:
            with self._schema_reindex_lock:
                try:
                    project_id = self._schema_reindex_queue.get_nowait()
                except queue.Empty:
                    self._schema_reindex_worker_running = False
                    self._schema_reindex_worker = None
                    return
            try:
                lock = self._reindex_lock(project_id)
                result = self.reindex_async(project_id)
                if result['status'] == 'already_running':
                    # The in-flight synchronous/asynchronous reindex claimed
                    # the stale index and already owns the rebuild.
                    lock.acquire()
                    lock.release()
                elif result['status'] == 'started':
                    lock.acquire()
                    lock.release()
                elif result['status'] == 'error':
                    logger.error('Не удалось поставить на переиндексацию %s: %s',
                                 project_id, result.get('error', 'неизвестная ошибка'))
            finally:
                with self._schema_reindex_lock:
                    self._schema_reindex_pending.discard(project_id)
                self._schema_reindex_queue.task_done()

    def get_search(self, project_id: str) -> SearchEngine:
        """Get SearchEngine instance for project."""
        return SearchEngine(self.get_db(project_id), glossary=self.get_glossary(project_id))

    def get_glossary(self, project_id: str):
        """Словарь проекта (src/core/glossary.py): data/projects/{id}/glossary.json."""
        from .glossary import FILENAME, Glossary
        return Glossary(self.projects_dir / project_id / FILENAME)

    # ============================================
    # BASE (отличия от типовой)
    # ============================================

    def set_base_project(self, project_id: str, base_id: str) -> ProjectInfo:
        """Задать базу проекта ('' — убрать). База — другой проект реестра.
        Новый релиз, ставший базой (проект обновлён на него), сбрасывается."""
        release_id = self.get_project(project_id).release_project
        return self.set_comparison(project_id, base_id,
                                   '' if release_id == (base_id or '').strip() else release_id)

    def set_comparison(self, project_id: str, base_id: str, release_id: str = '') -> ProjectInfo:
        """База (типовая того же релиза, что проект) и новый релиз (типовая, на
        которую предстоит обновление) проекта; '' — не задан. Оба — другие
        проекты реестра, новый релиз — не база."""
        base_id, release_id = (base_id or '').strip(), (release_id or '').strip()
        with self._registry_lock():
            project = self.get_project(project_id)
            for other, what in ((base_id, 'базой'), (release_id, 'новым релизом')):
                if other == project_id:
                    raise ValueError(f"Проект не может быть {what} самому себе")
                if other:
                    self.get_project(other)
            if release_id and release_id == base_id:
                raise ValueError("Новый релиз и база — разные проекты: база — типовая того же "
                                 "релиза, что и проект, новый релиз — типовая, на которую обновляетесь")
            project.base_project = base_id
            project.release_project = release_id
            self._save_registry()
        with self._base_diff_lock:
            for key in [k for k in self._base_diffs if project_id in k[:2]]:
                del self._base_diffs[key]
        return project

    def _base_of(self, project_id: str) -> str:
        """id базы проекта; ValueError с объяснением — базы нет или она не готова."""
        base_id = self.get_project(project_id).base_project
        if not base_id:
            raise ValueError(f"У проекта «{project_id}» не задана база — укажите её в настройках "
                             f"проекта (проект с типовой конфигурацией)")
        try:
            base = self.get_project(base_id)
        except KeyError:
            raise ValueError(f"База «{base_id}» проекта «{project_id}» не найдена — проект удалён? "
                             f"Укажите базу заново в настройках")
        if base.status != 'ready':
            raise ValueError(f"Индекс базы «{base_id}» не готов (статус {base.status}) — "
                             f"проиндексируйте её")
        return base_id

    def main_source(self, project_id: str) -> str:
        """id основной конфигурации проекта: источник вида main (первый), иначе
        первый источник. Называться он может как угодно — не обязательно «main»."""
        try:
            sources = self.get_project(project_id).sources
        except KeyError:
            return 'main'
        main = next((s for s in sources if (s.source_type or 'main') == 'main'), None)
        return (main or (sources[0] if sources else None)).id if sources else 'main'

    def platform_mode(self, project_id: str) -> str:
        with self.read_lease(project_id) as db:
            row = db.read_conn().execute(
                "SELECT compatibility_mode FROM sources WHERE id=?", (self.main_source(project_id),)
            ).fetchone()
        return row[0] if row else ''

    def platform_mode_known(self, project_id: str) -> bool:
        """Режим совместимости берётся из индекса: до индексации основной
        конфигурации он неизвестен, а не «не задан»."""
        with self.read_lease(project_id) as db:
            row = db.read_conn().execute(
                "SELECT indexed_at FROM sources WHERE id=?", (self.main_source(project_id),)
            ).fetchone()
        return bool(row and row[0])

    def platform_help_version(self, project_id: str | None = None) -> str:
        version = self.get_project(project_id).platform_version if project_id else ''
        return version or self.platform_help.newest()

    def sync_platform_help(self) -> None:
        """Запустить поиск и построение справки установленной платформы."""
        sync = partial(self.platform_help.sync_installed, default_search_roots(), background=False)
        self._start_platform_worker(sync, 'platform-help')

    def warm_platform_checks(self, version: str = '', *, project_id: str | None = None) -> None:
        """Прогреть отчёты после изменения справки или выбора версии в проекте."""
        projects = (self.get_project(project_id),) if project_id else self.list_projects()
        with self._base_diff_lock:
            if self._closing_platform_workers:
                return
            for project in projects:
                self._platform_warm_pending[project.id] = (version, project_id is not None)
                self._platform_warm_pending.move_to_end(project.id)
            coordinator = self._platform_warm_coordinator
            if coordinator is not None and coordinator.is_alive():
                return

            name = f'platform-check-warm-{project_id or version or "auto"}'
            coordinator = threading.Thread(target=self._run_platform_warm_queue, daemon=True, name=name)
            self._platform_warm_coordinator = coordinator
            self._platform_workers.add(coordinator)
            try:
                coordinator.start()
            except RuntimeError:
                self._platform_workers.discard(coordinator)
                self._platform_warm_coordinator = None
                logger.exception('Не удалось запустить координатор прогрева проверок платформы')

    def _run_platform_warm_queue(self) -> None:
        from .platform_check import warm_ready_report
        current = threading.current_thread()
        try:
            while True:
                with self._base_diff_lock:
                    if self._closing_platform_workers or not self._platform_warm_pending:
                        return
                    project_id, (version, specific) = self._platform_warm_pending.popitem(last=False)
                current.name = f'platform-check-warm-{project_id if specific else version or "auto"}'
                try:
                    project = self.get_project(project_id)
                    newest = self.platform_help.newest()
                    target = (project.platform_version if specific else version) or newest
                    if (project.status != 'ready'
                            or (not specific and not version and project.platform_version)
                            or (project.platform_version or newest) != target):
                        continue
                    index = self.platform_help.get(target) if target else None
                    if index is None:
                        continue
                    with self.project_pin(project.id):
                        engine = self.get_search(project.id)
                        try:
                            warm_ready_report(engine, index, self.platform_mode(project.id), project.id,
                                              project_pin=self.project_pin)
                        finally:
                            engine.db.close_thread_reader()
                except KeyError:
                    logger.info('Проект %s удалён до прогрева проверки платформы', project_id)
                except Exception:
                    logger.exception('Проект %s: не удалось запустить проверку платформы', project_id)
        finally:
            with self._base_diff_lock:
                self._platform_workers.discard(current)
                if self._platform_warm_coordinator is current:
                    self._platform_warm_coordinator = None
                # Запрос мог попасть между последней проверкой очереди и
                # очисткой ссылки на координатор. Передать его новому потоку.
                if self._platform_warm_pending and not self._closing_platform_workers:
                    project_id, (version, specific) = next(iter(self._platform_warm_pending.items()))
                    name = f'platform-check-warm-{project_id if specific else version or "auto"}'
                    coordinator = threading.Thread(target=self._run_platform_warm_queue,
                                                   daemon=True, name=name)
                    self._platform_warm_coordinator = coordinator
                    self._platform_workers.add(coordinator)
                    try:
                        coordinator.start()
                    except RuntimeError:
                        self._platform_workers.discard(coordinator)
                        self._platform_warm_coordinator = None
                        logger.exception('Не удалось перезапустить координатор прогрева проверок платформы')

    def _start_platform_worker(self, target, name: str) -> None:
        def run():
            try:
                target()
            except Exception:
                logger.exception('Фоновая задача справки платформы завершилась с ошибкой (%s)', name)
            finally:
                with self._base_diff_lock:
                    self._platform_workers.discard(threading.current_thread())

        thread = threading.Thread(target=run, daemon=True, name=name)
        with self._base_diff_lock:
            if self._closing_platform_workers:
                return
            self._platform_workers.add(thread)
            try:
                thread.start()
            except RuntimeError:
                self._platform_workers.discard(thread)
                logger.exception('Не удалось запустить фоновую задачу справки платформы (%s)', name)

    def _diff_sources(self, project_id: str, base_id: str, source_id: str | None) -> tuple[str, str]:
        """(источник проекта, источник базы) для сравнения: по умолчанию — их
        основные конфигурации; явный source_id, отличный от основной, — тот же
        id с обеих сторон (расширение против расширения)."""
        own = self.main_source(project_id)
        src = source_id or own
        return src, (self.main_source(base_id) if src == own else src)

    def config_version(self, project_id: str, source_id: str | None = None) -> str:
        """«Имя версия» конфигурации источника из индекса; '' — нет индекса."""
        source_id = source_id or self.main_source(project_id)
        try:
            with self.read_lease(project_id) as db:
                row = db.read_conn().execute(
                    "SELECT config_name, config_version FROM sources WHERE id = ?", (source_id,)).fetchone()
        except (KeyError, IndexRemovedError):
            return ''
        return f"{row['config_name']} {row['config_version']}".strip() if row else ''

    @staticmethod
    def _diff_key(db: Database) -> tuple:
        row = db.read_conn().execute(
            "SELECT updated_at, status FROM index_state WHERE id = 1").fetchone()
        try:
            stat = os.stat(db.db_path)
            file_revision = (stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                             stat.st_ctime_ns, stat.st_size)
        except OSError:
            file_revision = None
        return (db.db_path, row['updated_at'] if row else '', row['status'] if row else '',
                file_revision)

    def get_base_diff(self, project_id: str, source_id: str | None = None):
        """Отличия проекта от базы (src/core/base_diff.py). Пересчитывается,
        только когда переиндексирован проект или база. ValueError — базы нет,
        она не готова или проект сейчас индексируется."""
        return self._diff_between(project_id, self._base_of(project_id), source_id)

    def _cached_diff(self, project_id: str, base_id: str, source_id: str | None = None):
        """Посчитанный и не устаревший BaseDiff пары; None — нет."""
        pair = (project_id, base_id, *self._diff_sources(project_id, base_id, source_id))
        with self.read_lease(project_id) as db, self.read_lease(base_id) as base_db:
            key = (self._diff_key(db), self._diff_key(base_db))
            with self._base_diff_lock:
                cached = self._base_diffs.get(pair)
                if cached is not None:
                    self._base_diffs.move_to_end(pair)
        return cached[1] if cached and cached[0] == key else None

    def _diff_between(self, project_id: str, base_id: str, source_id: str | None = None):
        """BaseDiff проекта против любого другого проекта (кэш — по паре).

        Первый раз после запуска на крупной конфигурации — десятки секунд:
        индексы (гигабайты) читаются с диска; дальше — секунды или кэш."""
        from .base_diff import compute_base_diff
        if self.get_project(project_id).status == 'indexing':
            raise ValueError(f"Проект «{project_id}» сейчас индексируется — сравнение после окончания")
        src, base_src = self._diff_sources(project_id, base_id, source_id)
        pair = (project_id, base_id, src, base_src)
        with self._base_diff_lock:
            lock = self._diff_pair_locks.setdefault(pair, threading.Lock())
        # Порядок во всех путях один: сначала аренды обоих читателей, затем
        # блокировка пары. Ожидая читателя, нельзя удерживать пару: иначе
        # экспорт отчёта может занять её и ждать читателя у восьми diff-запросов.
        with self.read_lease(project_id) as db, self.read_lease(base_id) as base_db:
            key = (self._diff_key(db), self._diff_key(base_db))
            with lock:
                with self._base_diff_lock:
                    cached = self._base_diffs.get(pair)
                    if cached is not None:
                        self._base_diffs.move_to_end(pair)
                if cached is not None and cached[0] == key:
                    return cached[1]
                # compute_base_diff делает несколько последовательных SQL;
                # обе аренды остаются активны до окончания расчёта.
                diff = compute_base_diff(db.read_conn(), base_db.read_conn(), base_id, src, base_src)
                with self._base_diff_lock:
                    self._base_diffs[pair] = (key, diff)
                    self._base_diffs.move_to_end(pair)
                    while len(self._base_diffs) > BASE_DIFF_CACHE_LIMIT:
                        self._base_diffs.popitem(last=False)
        return diff

    def warm_diffs(self, project_id: str, release_id: str = '') -> bool:
        """Готовы ли сравнения раздела «Отличия» проекта (с базой и, если задан,
        релиза с базой). Нет — считает их в фоне и возвращает False: Web UI
        показывает «идёт сравнение» и ждёт (diff_warming). ValueError — базы
        или релиза нет, они не готовы."""
        base_id = self._base_of(project_id)
        release_id = (release_id or '').strip()
        pairs = [(project_id, base_id)] + ([(release_id, base_id)] if release_id else [])
        missing = tuple(p for p in pairs if self._cached_diff(*p) is None)
        if not missing:
            return True
        # Кто-то из пары индексируется — считать нечего: результат устарел бы
        # сразу. Сравнение пересчитает _warm_related, когда индексация кончится.
        busy = sorted({pid for pair in missing for pid in pair
                       if self.get_project(pid).status == 'indexing'})
        if busy:
            raise ValueError(f"Проект «{busy[0]}» сейчас индексируется — сравнение после окончания")
        with self._base_diff_lock:
            if self._warm_failed.get(project_id) == missing:
                # Фоновый расчёт этих пар упал — пусть вызывающий посчитает
                # сам и покажет ошибку, а не ждёт бесконечно.
                del self._warm_failed[project_id]
                return True
            running = self._warming.get(project_id)
            if running is not None and running.is_alive():
                return False

            def run():
                try:
                    for pair in missing:
                        self._diff_between(*pair)
                except Exception:
                    logger.exception("Проект %s: сравнение для «Отличий» не посчитано", project_id)
                    with self._base_diff_lock:
                        self._warm_failed[project_id] = missing
                        self._warm_failed.move_to_end(project_id)
                        while len(self._warm_failed) > BACKGROUND_ERROR_CACHE_LIMIT:
                            self._warm_failed.popitem(last=False)
                finally:
                    with self._base_diff_lock:
                        current = threading.current_thread()
                        for worker_key, worker in list(self._warming.items()):
                            if worker is not current and not worker.is_alive():
                                self._warming.pop(worker_key, None)

            thread = self._start_pinned_worker(
                run, f'diff-{project_id}', (pid for pair in missing for pid in pair),
                on_created=lambda worker: self._warming.__setitem__(project_id, worker))
            if thread is None:
                return True
        return False

    def index_error(self, project_id: str) -> str:
        """Причина последней ошибки; sidecar сохраняет её между запусками."""
        with self._base_diff_lock:
            message = self._index_errors.get(project_id)
            if message is not None:
                self._index_errors.move_to_end(project_id)
        if message is not None:
            return message
        try:
            return (self.projects_dir / project_id / 'index-error.txt').read_text(encoding='utf-8')
        except OSError:
            return ''

    def _set_index_error(self, project_id: str, message: str) -> None:
        with self._base_diff_lock:
            self._index_errors[project_id] = message
            self._index_errors.move_to_end(project_id)
            while len(self._index_errors) > PROJECT_ERROR_CACHE_LIMIT:
                self._index_errors.popitem(last=False)
        error_path = self.projects_dir / project_id / 'index-error.txt'
        try:
            error_path.write_text(message, encoding='utf-8')
        except OSError:
            logger.exception('Не удалось сохранить причину ошибки индекса проекта %s', project_id)

    def _clear_index_error(self, project_id: str) -> None:
        with self._base_diff_lock:
            self._index_errors.pop(project_id, None)
        try:
            (self.projects_dir / project_id / 'index-error.txt').unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception('Не удалось удалить причину ошибки индекса проекта %s', project_id)

    def diff_warming(self, project_id: str) -> bool:
        """Идёт ли фоновый расчёт сравнений проекта (warm_diffs)."""
        with self._base_diff_lock:
            thread = self._warming.get(project_id)
        return thread is not None and thread.is_alive()

    def _warm_related(self, project_id: str) -> None:
        """После переиндексации — заранее пересчитать сравнения, где проект —
        сам проект, его база или новый релиз: «Отличия» откроются без ожидания."""
        for p in self.list_projects():
            if not p.base_project or project_id not in (p.id, p.base_project, p.release_project):
                continue
            try:
                self.warm_diffs(p.id, p.release_project)
            except (ValueError, KeyError):
                pass

    def get_update_conflicts(self, project_id: str, release_id: str = '', source_id: str | None = None):
        # После расчёта сравнений ещё читаются перехваты исходного проекта.
        # Удержать его аренду до конца всего ответа (включая MCP-форматирование).
        with self.read_lease(project_id):
            return self._get_update_conflicts(project_id, release_id, source_id)

    def _get_update_conflicts(self, project_id: str, release_id: str = '',
                              source_id: str | None = None):
        """Конфликты обновления (base_diff.compute_update_conflicts): доработки
        проекта против его базы × изменения релиза release_id (новая типовая)
        против той же базы; release_id пуст — новый релиз из настроек проекта.
        ValueError — базы или релиза нет, они не готовы."""
        from .base_diff import compute_update_conflicts
        base_id = self._base_of(project_id)
        release_id = (release_id or '').strip() or self.get_project(project_id).release_project
        if not release_id:
            raise ValueError(f"У проекта «{project_id}» не задан новый релиз — укажите его в Web UI "
                             f"(«Отличия», настройка сравнения) или передайте release_project")
        if release_id in (project_id, base_id):
            raise ValueError(f"Новый релиз должен быть отдельным проектом — не самим проектом "
                             f"и не его базой «{base_id}»")
        try:
            release = self.get_project(release_id)
        except KeyError:
            raise ValueError(f"Проект «{release_id}» не найден")
        if release.status != 'ready':
            raise ValueError(f"Индекс релиза «{release_id}» не готов (статус {release.status}) — "
                             f"проиндексируйте его")
        # Основная конфигурация у релиза — своя (её id может отличаться);
        # явный источник-расширение — тот же id и в релизе.
        own = self.main_source(project_id)
        release_src = None if (source_id or own) == own else source_id
        return compute_update_conflicts(self.get_base_diff(project_id, source_id),
                                        self._diff_between(release_id, base_id, release_src),
                                        release_id, self.intercepts(project_id, source_id or own))

    def intercepts(self, project_id: str, source_id: str | None = None, module_name: str = '',
                   procedure_name: str = '') -> list:
        with self.read_lease(project_id):
            return self._intercepts(project_id, source_id, module_name, procedure_name)

    def _intercepts(self, project_id: str, source_id: str | None = None, module_name: str = '',
                    procedure_name: str = '') -> list:
        """Перехваты процедур источника source_id в остальных источниках проекта
        (расширениях): рёбра override графа вызовов (&Вместо/&После/… в коде) и
        перехваты событий заимствованных форм (callType в Form.xml расширения,
        src/core/form_events.py). module_name/procedure_name сужают до одной
        процедуры (имена — как в индексе, регистр не важен)."""
        from .base_diff import Intercept
        source_id = source_id or self.main_source(project_id)
        # Только ребро самой директивы (строка объявления): ПродолжитьВызов()
        # внутри перехвата — тоже override, но это вызов, а не перехват.
        conds = ["c.callee_kind = 'override'", "c.line = p.start_line", "m.source_id != ?"]
        params = [source_id]
        if module_name:
            conds.append("casefold(c.callee_module) = ?")
            params.append(module_name.casefold())
        if procedure_name:
            conds.append("casefold(c.callee_proc) = ?")
            params.append(procedure_name.casefold())
        rows = self.get_db(project_id).read_conn().execute(
            f"""SELECT c.callee_module, c.callee_proc, c.context, p.name AS ext_proc, m.source_id
                FROM calls c JOIN procedures p ON p.id = c.caller_id
                JOIN modules m ON m.id = p.module_id
                WHERE {' AND '.join(conds)}
                ORDER BY c.callee_module, c.callee_proc, m.source_id""", params).fetchall()
        found = [Intercept(r['callee_module'], r['callee_proc'], r['source_id'], r['ext_proc'],
                           (r['context'] or '').strip()) for r in rows]
        found += self._form_event_intercepts(project_id, source_id, module_name, procedure_name)
        found.sort(key=lambda it: (it.module, it.procedure, it.source_id))
        return found

    def _form_event_intercepts(self, project_id: str, source_id: str, module_name: str = '',
                               procedure_name: str = '') -> list:
        """Перехваты событий заимствованных форм по связям из индекса."""
        from .base_diff import Intercept
        from .form_events import CALL_TYPES, CALL_TYPES_EN, FORM_EVENT_MARK
        conds = ["e.source_id != ?", "ef.call_type != ''", "mf.call_type = ''"]
        params = [source_id, source_id]
        if module_name:
            conds.append("e.name_cf = ?")
            params.append(module_name.casefold())
        if procedure_name:
            conds.append("casefold(mf.handler) = ?")
            params.append(procedure_name.strip().casefold())
        rows = self.get_db(project_id).read_conn().execute(
            f"""SELECT e.name, e.source_id, s.script_is_russian, ef.call_type,
                       ef.handler, mf.handler AS target
                FROM form_events ef JOIN modules e ON e.id = ef.module_id
                JOIN sources s ON s.id = e.source_id
                JOIN modules m ON m.name_cf = e.name_cf AND m.source_id = ?
                JOIN form_events mf ON mf.module_id = m.id AND mf.owner = ef.owner AND mf.event = ef.event
                WHERE {' AND '.join(conds)} ORDER BY e.id, ef.owner, ef.event""", params)
        result = []
        for r in rows:
            keywords = CALL_TYPES if r['script_is_russian'] else CALL_TYPES_EN
            if keyword := keywords.get(r['call_type'].casefold()):
                target = r['target']
                result.append(Intercept(r['name'], target, r['source_id'], r['handler'],
                                        f'&{keyword}:{target} {FORM_EVENT_MARK}'))
        return result

    def overview_counts(self, project_id: str) -> dict:
        """Числа для карточек «Обзора» — только из уже посчитанного: ни
        сравнение, ни проверка вызовов здесь не запускаются (на крупной
        конфигурации это десятки секунд). Нет в кэше — ключа нет, карточка
        показывает описание вместо числа.

        diff — итоги сравнения с типовой (BaseDiff.totals); broken — число
        вызовов несуществующих и неэкспортных методов; glossary — записей словаря."""
        out: dict = {}
        try:
            out['glossary'] = len(self.get_glossary(project_id).entries())
        except Exception:
            pass
        project = self.get_project(project_id)
        if project.status != 'ready':
            return out
        base_id = project.base_project
        if base_id:
            try:
                if self.get_project(base_id).status == 'ready':
                    diff = self._cached_diff(project_id, base_id)
                    if diff is not None:
                        out['diff'] = diff.totals()
            except Exception:       # база удалена, индекс пересоздаётся — просто без чисел
                pass
        try:
            if self.broken_calls_ready(project_id):
                with self._base_diff_lock:
                    result = self._broken_calls[project_id][1]
                out['broken'] = len(result['missing']) + len(result['not_export'])
        except Exception:
            pass
        return out

    def broken_calls_ready(self, project_id: str) -> bool:
        """Посчитана ли проверка вызовов для текущего индекса (есть в кэше)."""
        # _diff_key выполняет SQL через читателя. Удерживаем Database в пуле
        # и соединение до завершения сверки с кэшем.
        with self.read_lease(project_id) as db:
            key = self._diff_key(db)
        with self._base_diff_lock:
            cached = self._broken_calls.get(project_id)
            if cached is not None:
                self._broken_calls.move_to_end(project_id)
        return bool(cached and cached[0] == key)

    def warm_broken_calls(self, project_id: str) -> bool:
        """Готова ли проверка вызовов; нет — считает её в фоне (на крупной
        конфигурации это десятки секунд) и возвращает False: страница
        открывается сразу с «Идёт проверка…» и ждёт."""
        with self._base_diff_lock:
            if self._closing_checks:
                return False
        if self.broken_calls_ready(project_id):
            return True
        with self.read_lease(project_id) as db:
            key = self._diff_key(db)
        with self._base_diff_lock:
            if self._closing_checks:
                return False
            failed = self._checks_failed.get(project_id)
            if failed and failed[0] == key:
                # Упала на этом же индексе — показать причину, а не крутить заново.
                del self._checks_failed[project_id]
                raise ValueError(f"Проверка вызовов не посчитана: {failed[1]}")
            running = self._checks_warming.get(project_id)
            if running is not None and running.is_alive():
                return False

            def run():
                try:
                    for attempt in range(2):
                        try:
                            self.broken_calls(project_id)
                            break
                        except (KeyError, IndexRemovedError):
                            try:
                                self.get_project(project_id)
                            except KeyError:
                                logger.info('Проект %s удалён во время проверки вызовов', project_id)
                                return
                            if attempt:
                                raise
                except Exception as e:
                    logger.exception("Проект %s: проверка вызовов не посчитана", project_id)
                    with self._base_diff_lock:
                        self._checks_failed[project_id] = (key, str(e) or type(e).__name__)
                        self._checks_failed.move_to_end(project_id)
                        while len(self._checks_failed) > BACKGROUND_ERROR_CACHE_LIMIT:
                            self._checks_failed.popitem(last=False)
                finally:
                    with self._base_diff_lock:
                        current = threading.current_thread()
                        for worker_key, worker in list(self._checks_warming.items()):
                            if worker is not current and not worker.is_alive():
                                self._checks_warming.pop(worker_key, None)

            thread = self._start_pinned_worker(
                run, f'checks-{project_id}', (project_id,),
                on_created=lambda worker: self._checks_warming.__setitem__(project_id, worker))
            if thread is None:
                return True
        return False

    def checks_warming(self, project_id: str) -> bool:
        """Идёт ли фоновая проверка вызовов проекта."""
        with self._base_diff_lock:
            thread = self._checks_warming.get(project_id)
        return thread is not None and thread.is_alive()

    def broken_calls(self, project_id: str, source_id: str = '', module_name: str = '') -> dict:
        """Вызовы несуществующих и неэкспортных методов (src/core/broken_calls.py).
        Полный результат кэшируется до переиндексации: страница и постраничная
        выдача MCP не пересчитывают его на каждый запрос."""
        with self.read_lease(project_id) as db:
            return self._broken_calls_for_db(project_id, db, source_id, module_name)

    def _broken_calls_for_db(self, project_id: str, db: Database,
                             source_id: str = '', module_name: str = '') -> dict:
        from .broken_calls import find_broken_calls
        key = self._diff_key(db)
        with self._base_diff_lock:
            cached = self._broken_calls.get(project_id)
        if not cached or cached[0] != key:
            conn = db.read_conn()
            value = find_broken_calls(conn, read_module=db.read_module_text)
            # Индекс мог измениться, пока считалась сводка. Старый расчёт
            # допустимо вернуть текущему вызову, но публиковать его нельзя.
            if self._diff_key(db) == key:
                candidate = (key, value)
                with self._base_diff_lock:
                    current = self._broken_calls.get(project_id)
                    if current is None or current[0] != key:
                        self._broken_calls[project_id] = candidate
                        self._broken_calls.move_to_end(project_id)
                        while len(self._broken_calls) > BROKEN_CALL_CACHE_LIMIT:
                            self._broken_calls.popitem(last=False)
                cached = candidate
            else:
                cached = (key, value)
        result = cached[1]
        if not source_id and not module_name:
            return result
        mf = module_name.strip().casefold()

        def keep(i: dict) -> bool:
            return ((not source_id or i['source_id'] == source_id)
                    and (not mf or mf in i['caller_module'].casefold() or mf in i['module'].casefold()))
        return {
            'missing': [i for i in result['missing'] if keep(i)],
            'not_export': [i for i in result['not_export'] if keep(i)],
            'external': [e for e in result['external']
                         if keep(e['sample']) or (mf and mf in e['module'].casefold())],
            'dynamic': result['dynamic'],
        }

    def get_procedure_diff(self, project_id: str, module_name: str, procedure_name: str,
                           source_id: str | None = None, base_id: str | None = None) -> dict:
        base_id = base_id or self._base_of(project_id)
        # Внутри чтутся outline и текст каждого проекта; держим оба индекса
        # закреплёнными между этими последовательными запросами.
        with self.read_lease(project_id), self.read_lease(base_id):
            return self._get_procedure_diff(project_id, module_name, procedure_name,
                                            source_id, base_id)

    def _get_procedure_diff(self, project_id: str, module_name: str, procedure_name: str,
                            source_id: str | None = None, base_id: str | None = None) -> dict:
        """Текст процедуры в базе и в проекте и их diff. status: changed |
        same | added | removed | missing (нет ни там, ни там) | unreadable
        (процедура в индексе есть, а текста нет — base_unreadable /
        current_unreadable говорят, с какой стороны). base_id — сравнить не с
        базой проекта, а с другим проектом (релиз против базы проекта)."""
        from .base_diff import procedure_text_diff
        base_id = base_id or self._base_of(project_id)
        own = self.main_source(project_id)
        explicit = source_id if source_id and source_id != own else None

        def side(pid: str) -> tuple[str, list[str] | None, bool]:
            with self.read_lease(pid):
                engine = self.get_search(pid)
                outline = engine.get_module_outline(module_name,
                                                    source_id=explicit or self.main_source(pid))
                if not outline:
                    return '', None, False
                mod = outline['module']
                proc = next((p for p in outline['procedures']
                             if p['name'].casefold() == procedure_name.strip().casefold()), None)
                if not proc:
                    return mod['name'], None, False
                text = engine.db.read_module_text(mod['id'])
                if text is None:
                    return mod['name'], None, True
                lines = text.split('\n')[proc['start_line'] - 1:proc['end_line']]
                return f"{mod['name']}.{proc['name']}", lines, False

        base_name, base_lines, base_unreadable = side(base_id)
        cur_name, cur_lines, cur_unreadable = side(project_id)
        base_known = base_lines is not None or base_unreadable
        cur_known = cur_lines is not None or cur_unreadable
        if not base_known and not cur_known:
            status = 'missing'
        elif not base_known:
            status = 'added'
        elif not cur_known:
            status = 'removed'
        elif base_unreadable or cur_unreadable:
            status = 'unreadable'
        else:
            status = 'same' if base_lines == cur_lines else 'changed'
        name = cur_name or base_name or f"{module_name}.{procedure_name}"
        return {
            'name': name, 'status': status, 'base_project': base_id,
            'base_unreadable': base_unreadable, 'current_unreadable': cur_unreadable,
            'diff': procedure_text_diff(base_lines, cur_lines, f"{base_id}: {name}",
                                        f"{project_id}: {name}")
            if status in ('changed', 'added', 'removed') else '',
        }

    # ============================================
    # INDEXING
    # ============================================

    def reindex(self, project_id: str, source_id: str | None = None) -> IndexStats:
        """Сериализовать синхронную индексацию с фоновыми запусками."""
        async_owned = getattr(self._reindex_local, 'project_id', None) == project_id
        lock = self._reindex_lock(project_id)
        if not async_owned:
            lock.acquire()
        try:
            return self._reindex_locked(project_id, source_id=source_id)
        finally:
            if not async_owned:
                lock.release()

    def _reindex_locked(self, project_id: str, source_id: str | None = None) -> IndexStats:
        """
        Run indexing for project, synchronously (blocks until done — use
        reindex_async() for a large corpus so MCP/HTTP callers don't time
        out; see IMPROVEMENT_PLAN Этап 4/D7).

        source_id=None → full reindex (all sources)
        source_id="ext1" → reindex only that source

        Этап 4/D7 incrementality: this no longer wipes the DB (or the one
        source's rows) before indexing — index_xml_metadata/index_bsl UPSERT and
        skip-unchanged-by-hash on their own, so a rerun with nothing
        changed on disk is an order of magnitude faster than a cold index.
        """
        from .indexer import Indexer
        from .xml_walker import XMLWalker

        db = Database(str(self._index_path(project_id)))
        self._db_instances.add(db)
        try:
            db.connect()
            db.init_schema()
            project, _ = self._mark_indexing(project_id, db=db)
        except Exception:
            db.close()
            raise
        succeeded = False
        # Писатель живёт отдельно от пула. Читатели пула могут видеть его
        # commits через WAL и не могут вытеснить его соединение.
        self._invalidate_analysis_caches(project_id)
        if db.rebuild_required:
            # Маркер требует перестроить весь индекс, поэтому частичную
            # переиндексацию повышаем до полного прохода по источникам.
            source_id = None
        if source_id is None:
            try:
                self._seed_from_base(project, db)
            except Exception:
                # Засев — только ускорение: без него индекс строится с нуля.
                logger.exception("Проект %s: индекс базы не скопирован — индексация с нуля",
                                 project_id)

        # Этап 4/D7 item 3: durability is not a concern for an index that's
        # fully rebuildable from source — trade it for write throughput for
        # the duration of this reindex, then restore.
        db.conn.execute("PRAGMA synchronous=OFF")

        try:
            if db.rebuild_required and not any(
                    source.xml_path and self.source_path(project_id, source.xml_path).is_dir()
                    for source in project.sources):
                raise RuntimeError(self.index_error(project_id) or
                                   'Для восстановления индекса требуется XML-выгрузка проекта.')
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
            known_factory_functions = self._factory_functions(db, common_module_files)

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

            db.update_state(progress_total=total_files, progress_phase='metadata')

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
                                            force_reparse=bool(reason),
                                            module_files=walked.get(source.id))
                if current_fp and source.id in walked:
                    db.set_parser_fingerprint(source.id, current_fp, reason)
                duration_sec += stats.get('duration_sec', 0)
                source.indexed_at = datetime.now().isoformat()
                progress['done'] += len(walked.get(source.id, []))

            # Resolve the call graph (1.2) — recomputed in full every time,
            # regardless of full vs. partial reindex: it's cheap (table
            # scans + dict joins, no per-call queries) and a partial reindex
            # of one source can change how other sources' calls resolve
            # (e.g. an extension now overriding a main-config procedure).
            # Пересчёт пропускается, если с прошлого его входы не менялись
            # (graph_state): обновление без изменений в выгрузке.
            resolver = Indexer.resolver_fingerprint()
            if db.graph_is_current(resolver):
                logger.info('Проект %s: модули не изменились — граф вызовов не пересчитывается',
                            project_id)
            else:
                db.update_state(progress_phase='resolving')
                indexer.resolve_calls()
                db.set_graph_resolved(resolver)

            total_stats = self._calc_stats_from_db(db)
            total_stats.duration_sec = duration_sec

            # Поисковые карточки процедур — только для изменившихся модулей
            # (src/core/cards.py). Вспомогательный индекс: его сбой пишется в
            # лог и не делает весь индекс ошибочным.
            db.update_state(progress_phase='cards', progress_current=0, progress_total=0)
            try:
                from .cards import refresh_card_index
                card_stats = refresh_card_index(
                    SearchEngine(db),
                    progress_cb=lambda done, total: db.update_state(
                        progress_current=done, progress_total=total))
                if card_stats['modules']:
                    logger.info('Проект %s: карточки процедур пересобраны для %d модулей (%d фрагментов)',
                                project_id, card_stats['modules'], card_stats['chunks'])
            except Exception:
                logger.exception('Проект %s: карточки процедур не построены', project_id)

            # Если справка сменилась во время прогрева, повторим подготовку
            # перед ready. Изменение в самом конце подхватит сверка после ready.
            from .platform_check import ReportUnavailable, prune_report_archives, warm_ready_report, warm_report
            ready_at = datetime.now().isoformat()
            warmed_key = None
            warmed_keys = set()
            for attempt in range(2):
                try:
                    version = self.platform_help_version(project_id)
                    index = self.platform_help.get(version) if version else None
                    if index is None:
                        break
                    if attempt == 0:
                        db.update_state(progress_phase='platform_check')
                    # Отчёт строится синхронно до публикации ready. Удерживаем
                    # Database и читателя на весь проход: иначе пул может
                    # вытеснить индекс между получением SearchEngine и чтением
                    # отчёта, после чего ready будет опубликован без снимка.
                    with self.project_pin(project_id):
                        engine = self.get_search(project_id)
                        try:
                            with engine.db.read_lease():
                                key = warm_report(engine, index, self.platform_mode(project_id),
                                                  ready_at, warmed_key)
                        finally:
                            engine.db.close_thread_reader()
                    if key is None or key == warmed_key:
                        break
                    warmed_key = key
                    warmed_keys.add(key)
                except (ReportUnavailable, FileNotFoundError):
                    continue
                except Exception:
                    logger.exception('Проект %s: снимок проверки платформы не построен', project_id)
                    break

            try:
                prune_report_archives(db.db_path, keep=warmed_keys)
            except OSError:
                logger.exception('Проект %s: не удалось удалить архивы предыдущего индекса', project_id)

            total_stats.last_indexed = datetime.now().isoformat()
            total_stats.duration_sec = round(total_stats.duration_sec, 2)
            project.index_stats = total_stats
            project.status = 'ready'
            db.update_state(status='ready', progress_phase='', progress_current=0,
                             progress_total=0, index_duration_sec=total_stats.duration_sec, updated_at=ready_at)
            self._save_project_to_disk(project_id, project)
            self._clear_index_error(project_id)
            db.clear_rebuild_marker()
            try:
                version = self.platform_help_version(project_id)
                index = self.platform_help.get(version) if version else None
                if index is not None:
                    with self.project_pin(project_id):
                        warm_ready_report(self.get_search(project_id), index, self.platform_mode(project_id),
                                          project_id, warmed_key, project_pin=self.project_pin)
            except FileNotFoundError:
                pass  # Удаление справки запускает свой прогрев после смены версии.
            except Exception:
                logger.exception('Проект %s: не удалось запустить проверку платформы', project_id)
            succeeded = True
            return total_stats

        except Exception as e:
            self._set_index_error(project_id, _index_error_text(e))
            project.status = 'error'
            self._save_project_to_disk(project_id, project)
            db.update_state(status='error', progress_phase='')
            raise
        finally:
            if db.conn:
                db.conn.execute("PRAGMA synchronous=NORMAL")
                db.conn.commit()
            db.close()
            if succeeded:
                self._warm_related(project_id)

    def _seed_from_base(self, project: 'ProjectInfo', db: Database) -> bool:
        """Первый индекс проекта с базой — копия индекса базы: дальше обычная
        инкрементальная переиндексация переразбирает только модули, чей хеш
        отличается от типовой (сотни вместо десятков тысяч). Только для
        пустого индекса и готовой базы, которая сейчас не индексируется;
        источники базы, которых нет в проекте, из копии удаляются. True —
        индекс засеян."""
        base_id = project.base_project
        if not base_id or base_id == project.id:
            return False
        conn = db.conn
        if conn.execute("SELECT 1 FROM modules LIMIT 1").fetchone() or \
                conn.execute("SELECT 1 FROM metadata_objects LIMIT 1").fetchone():
            return False
        try:
            if self.get_project(base_id).status != 'ready':
                return False
        except KeyError:
            return False
        base_path = self._index_path(base_id)
        if not base_path.exists():
            return False
        lock = self._reindex_lock(base_id)
        if not lock.acquire(blocking=False):
            return False
        try:
            db.update_state(progress_phase='base')
            source = sqlite3.connect(str(base_path))
            try:
                if not Database.has_current_schema(source):
                    return False
                source.backup(conn)
            finally:
                source.close()
        finally:
            lock.release()
        keep = {s.id for s in project.sources}
        for (sid,) in conn.execute("SELECT id FROM sources").fetchall():
            if sid not in keep:
                self._clear_source_data(db, sid)
        # Копия принесла index_state базы (status='ready') — вернуть свой.
        db.update_state(status='indexing', progress_phase='scanning')
        logger.info("Проект %s: индекс построен от базы %s — переразбираются только отличия",
                    project.id, base_id)
        return True

    def _mark_indexing(self, project_id: str, *, db: Database | None = None) -> tuple['ProjectInfo', Database]:
        """Записать начало переиндексации: status='indexing' в реестр и в
        index_state (прогресс 0, фаза 'scanning'). Возвращает проект и БД.

        Реестр — узкой записью «перечитать → записать один ключ» (Этап 4/D7),
        а не update_project()/_save_registry(): те сбросили бы на диск весь
        кэш реестра и затёрли бы правки других проектов, сделанные за время
        долгого прогона вторым процессом (ariadna-index, нативный запуск на
        том же data/).

        Запись идемпотентна: reindex_async делает её синхронно до ответа
        «started», и reindex() в потоке повторяет её — теми же значениями и
        до того, как прогон что-либо изменил. Поэтому флаг «уже записано»
        не нужен, а reindex() без reindex_async (CLI, тесты) остаётся
        самодостаточным."""
        project = self.get_project(project_id)
        owns_db = db is None
        db = db or Database(str(self._index_path(project_id)))
        if owns_db:
            self._db_instances.add(db)
            # Статус переиндексации и открытие читателем могут прийтись на
            # первый запуск индекса; не выпускать путь между PRAGMA WAL,
            # схемой и первой записью состояния.
            db._schema_lock.acquire()
            try:
                db.connect()
                db.init_schema()
            except BaseException:
                db._schema_lock.release()
                db.close()
                raise
        try:
            project.status = 'indexing'
            self._save_project_to_disk(project_id, project)
            db.update_state(status='indexing', progress_current=0, progress_total=0,
                            progress_phase='scanning')
            return project, db
        finally:
            if owns_db:
                try:
                    db.close()
                finally:
                    db._schema_lock.release()

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
            self._clear_index_error(project_id)
            self._mark_indexing(project_id)
        except Exception as e:
            lock.release()
            logger.exception("Не удалось начать переиндексацию проекта %s", project_id)
            return {'status': 'error', 'project_id': project_id,
                    'error': e.args[0] if isinstance(e, KeyError) and e.args else str(e)}

        def _run():
            try:
                self._reindex_local.project_id = project_id
                self.reindex(project_id, source_id=source_id)
            except Exception:
                # status='error' reindex() уже записал; причина нужна в логе.
                logger.exception("Фоновая переиндексация проекта %s (источник %s) упала",
                                 project_id, source_id or 'все')
            finally:
                try:
                    del self._reindex_local.project_id
                except AttributeError:
                    pass
                lock.release()
                with self._base_diff_lock:
                    self._reindexing.discard(threading.current_thread())

        thread = threading.Thread(target=_run, daemon=True, name=f'reindex-{project_id}')
        with self._base_diff_lock:
            self._reindexing.add(thread)
        thread.start()
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
        любой 'indexing' заведомо протухший. Если рядом с индексом есть
        маркер schema-reindex-required, старый файл уже был удалён и проект
        восстанавливается из XML заново. Возвращает id тронутых проектов.
        """
        reset: dict[str, str] = {}
        with self._registry_lock():
            projects = list(self._load_registry().values())
        for snapshot in projects:
            marker_path = Path(str(self._index_path(snapshot.id)) + '.reindex-required')
            if marker_path.exists():
                try:
                    self.get_db(snapshot.id)
                    reset[snapshot.id] = self.get_project(snapshot.id).status
                except Exception:
                    logger.exception("Не удалось возобновить восстановление индекса %s", snapshot.id)
                continue
            if snapshot.status != 'indexing':
                continue
            with self._registry_lock():
                project = self.get_project(snapshot.id)
                if project.status != 'indexing':
                    continue
                project.status = 'ready' if project.index_stats.last_indexed else 'empty'
                reset[project.id] = project.status
                self._save_project_to_disk(project.id, project)
        for project_id, status in reset.items():
            if Path(str(self._index_path(project_id)) + '.reindex-required').exists():
                continue
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
    # что агент до него не дошёл. Выгрузка крупной конфигурации занимает минуты; шесть часов
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
        the module-level _REINDEX_LOCKS comment for why it is per process
        and per project rather than per instance (Этап 4/D7)."""
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

    @staticmethod
    def _factory_functions(db: Database, common_module_files: list) -> dict[str, set[str]]:
        """Indexer.scan_known_factory_functions с кэшем по файлу (factory_cache):
        общий модуль разбирается, только если изменились его размер, время
        изменения или отпечаток парсера. Результат тот же, что без кэша."""
        from concurrent.futures import ThreadPoolExecutor
        from .indexer import Indexer, module_file_stat
        from .xml_walker import IO_THREADS
        fp = parser_fingerprint.parser_fingerprint()
        conn = db.conn
        cached = {r['file_path']: r for r in conn.execute("SELECT * FROM factory_cache")}
        with ThreadPoolExecutor(IO_THREADS) as pool:
            stats = list(pool.map(lambda mf: module_file_stat(mf.file_path), common_module_files))
        result: dict[str, set[str]] = {}
        rows = []
        for mf, stat in zip(common_module_files, stats):
            key = f'{stat}|{fp}'
            hit = cached.get(mf.file_path)
            if stat and hit is not None and hit['stat'] == key:
                functions = set(filter(None, hit['functions'].split(',')))
            else:
                functions = Indexer.scan_known_factory_functions([mf]).get(mf.object_name.lower(), set())
            if functions:
                result.setdefault(mf.object_name.lower(), set()).update(functions)
            if stat:
                rows.append((mf.file_path, mf.object_name, key, ','.join(sorted(functions))))
        conn.execute("DELETE FROM factory_cache")
        conn.executemany("INSERT OR REPLACE INTO factory_cache VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        return result

    def _index_source(self, indexer, project_id: str, source: SourceInfo,
                       known_modules: set[str] | None = None,
                       known_objects: dict[str, set[str]] | None = None,
                       known_factory_functions: dict[str, set[str]] | None = None,
                       progress_cb=None, force_reparse: bool = False,
                       module_files: list | None = None) -> dict:
        """Index one source: metadata and BSL code from its XML dump.
        module_files — обход выгрузки, уже сделанный reindex (см. index_bsl)."""
        stats = {'objects': 0, 'attributes': 0, 'forms': 0,
                 'modules': 0, 'procedures': 0, 'calls': 0, 'duration_sec': 0}

        xml_path = None
        if source.xml_path:
            xml_path = self.source_path(project_id, source.xml_path)

        # 1. Metadata: из Configuration.xml и файлов объектов выгрузки
        # (src/core/xml_metadata.py). Выгрузка Конфигуратора («Выгрузить
        # конфигурацию в файлы»); без Configuration.xml — только код.
        from .xml_metadata import has_configuration
        if xml_path and has_configuration(xml_path):
            meta_stats = indexer.index_xml_metadata(str(xml_path), source_id=source.id,
                                                    source_label=source.label,
                                                    source_type=source.source_type,
                                                    force=force_reparse)
            for k in ('objects', 'attributes', 'forms', 'modules', 'duration_sec'):
                stats[k] = meta_stats.get(k, 0)
        elif xml_path and xml_path.exists():
            # Выгрузка сменилась на такую, где метаданных нет (например, разница
            # удалила Configuration.xml) — прежние метаданные этого источника
            # больше не описывают выгрузку: иначе API отдавал бы ready со
            # старыми объектами. Код переразбирается ниже как обычно.
            self._clear_source_metadata(indexer.db, source.id)
            logger.warning("Проект %s, источник %s: в выгрузке нет Configuration.xml — "
                           "метаданные не индексируются, только код. Нужна выгрузка "
                           "Конфигуратора «Выгрузить конфигурацию в файлы».",
                           project_id, source.id)

        # 2. Index BSL code (from XML dump)
        if xml_path and xml_path.exists():
            # Граф вызовов устарел, если модули переразобраны, добавлены или
            # удалены (src/core/db.py: graph_state). Здесь, а не в index_bsl:
            # его исходник входит в отпечаток парсера. Флаг — до записи: если
            # индексация оборвётся, граф пересчитает следующий reindex; ничего
            # не изменилось — прежнее состояние возвращается.
            db = indexer.db

            def module_count():
                return db.conn.execute(
                    "SELECT COUNT(*) FROM modules WHERE source_id=?", (source.id,)).fetchone()[0]
            modules_before = module_count()
            was_dirty = db.graph_dirty()
            db.mark_graph_dirty()
            db.conn.commit()
            bsl_stats = indexer.index_bsl(str(xml_path), source_id=source.id,
                                           known_modules=known_modules,
                                           known_objects=known_objects,
                                           known_factory_functions=known_factory_functions,
                                           source_label=source.label,
                                           source_type=source.source_type,
                                           force_reparse=force_reparse,
                                           progress_cb=progress_cb,
                                           module_files=module_files)
            if not was_dirty and not bsl_stats.get('files') and module_count() == modules_before:
                db.set_graph_dirty(False)
            stats['modules'] += bsl_stats.get('modules', 0)
            stats['procedures'] = bsl_stats.get('procedures', 0)
            stats['calls'] = bsl_stats.get('calls', 0)
            stats['duration_sec'] += bsl_stats.get('duration_sec', 0)

        return stats

    def _clear_source_metadata(self, db: Database, source_id: str):
        """Удалить метаданные источника, оставив разобранный код.

        Нужен, когда источник переходит в режим без метаданных (в выгрузке
        больше нет Configuration.xml): строки прежних объектов иначе
        оставались бы в индексе и API показывал бы удалённые объекты."""
        conn = db.conn
        if not conn:
            return
        obj_ids = [r[0] for r in conn.execute(
            "SELECT id FROM metadata_objects WHERE source_id=?", (source_id,)
        ).fetchall()]
        if obj_ids:
            placeholders = ','.join('?' * len(obj_ids))
            module_ids = [r[0] for r in conn.execute(
                f"SELECT id FROM modules WHERE object_id IN ({placeholders})", obj_ids
            ).fetchall()]
            if module_ids:
                module_placeholders = ','.join('?' * len(module_ids))
                conn.execute(f"DELETE FROM module_text_fts WHERE rowid IN ({module_placeholders})", module_ids)
            conn.execute(f"DELETE FROM forms WHERE object_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM attributes WHERE object_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM subsystem_content WHERE subsystem_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM object_properties WHERE object_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM object_help_fts WHERE rowid IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM object_queries WHERE object_id IN ({placeholders})", obj_ids)
            conn.execute(f"DELETE FROM object_query_refs WHERE object_id IN ({placeholders})", obj_ids)
        conn.execute("DELETE FROM metadata_objects WHERE source_id=?", (source_id,))
        # FTS5 не поддерживает FK; каскад мог удалить модули и оставить их
        # rowid в полнотекстовом индексе.
        conn.execute("DELETE FROM module_text_fts WHERE rowid NOT IN (SELECT id FROM modules)")
        db.mark_graph_dirty()
        conn.execute("INSERT INTO metadata_fts(metadata_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO attributes_fts(attributes_fts) VALUES('rebuild')")
        conn.commit()

    def _clear_source_data(self, db: Database, source_id: str):
        """Remove all data for a specific source from the DB."""
        conn = db.conn
        if not conn:
            return
        # module_text_fts has no FK to modules (FTS5 virtual tables can't
        # carry one) — its rowid mirrors modules.id, so remove those rows
        # explicitly before clearing metadata cascades to the modules.
        mod_ids = [r[0] for r in conn.execute(
            "SELECT id FROM modules WHERE source_id=?", (source_id,)
        ).fetchall()]
        if mod_ids:
            placeholders = ','.join('?' * len(mod_ids))
            conn.execute(f"DELETE FROM module_text_fts WHERE rowid IN ({placeholders})", mod_ids)

        self._clear_source_metadata(db, source_id)
        conn.execute("DELETE FROM parser_state WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM modules WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM metadata_objects WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        db.mark_graph_dirty()

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
            total_calls=conn.execute(
                """SELECT COUNT(*) FROM calls c WHERE c.callee_kind != 'manager_object' OR EXISTS
                         (SELECT 1 FROM calls_resolved cr WHERE cr.call_id = c.id)""").fetchone()[0],
        )

    # ============================================
    # HELPERS
    # ============================================

    def close_all(self):
        """Close all open database connections."""
        from .platform_check import wait_report_jobs
        with self._reader_trim_lock:
            self._reader_trim_closing = True
            trim_stop = self._reader_trim_stop
            trim_thread = self._reader_trim_thread
            if trim_stop is not None:
                trim_stop.set()
        if trim_thread is not None:
            trim_thread.join(timeout=5)
        with self._base_diff_lock:
            self._closing_checks = True
        # Фоновые переиндексации и сравнения (warm_diffs) работают с индексами —
        # дождаться их. Переиндексация пишет status='ready' чуть раньше, чем
        # отпускает соединение: закрыть его под ней — на Windows access
        # violation, а файлы останутся занятыми. Сначала переиндексации: в
        # конце они сами запускают прогрев сравнений.
        schema_worker = self._schema_reindex_worker
        if schema_worker is not None:
            schema_worker.join(timeout=120)
        with self._base_diff_lock:
            reindexing = list(self._reindexing)
            platform_workers = list(self._platform_workers)
            self._closing_platform_workers = True
        try:
            for thread in reindexing:
                thread.join(timeout=120)
            with self._base_diff_lock:
                warming = list(self._warming.values())
            for thread in warming:
                thread.join(timeout=60)
            with self._base_diff_lock:
                checks = list(self._checks_warming.values())
            for thread in checks:
                thread.join()
            for thread in platform_workers:
                thread.join()
            # Теперь фоновые операции завершены. Разбудить все get_db,
            # ожидающие открытие; их generation устареет, поэтому
            # они завершатся ошибкой даже после сброса флага shutdown.
            with self._db_pins_changed:
                self._closing = True
                self._pool_generation += 1
                opening = list(self._db_opening.values())
                self._db_pins_changed.notify_all()
            for event in opening:
                event.wait()
            with self._db_lock:
                pool = list(self._db_pool.values())
            # Report workers release project pins through _db_lock, so never
            # join them while holding the pool lock.
            if not wait_report_jobs(pool, timeout=30):
                logger.error('Фоновые записи отчётов платформы не завершились за 30 секунд')
                raise TimeoutError('Фоновые записи отчётов платформы не завершились за 30 секунд')
            with self._db_lock:
                self._db_pool.clear()
                self._db_overflow.clear()
                databases = list(self._db_instances)
            for db in databases:
                db.retire()
        finally:
            with self._base_diff_lock:
                self._closing_platform_workers = False
                self._closing_checks = False
            with self._db_pins_changed:
                self._closing = False
                self._db_pins_changed.notify_all()
            with self._reader_trim_lock:
                self._reader_trim_thread = None
                self._reader_trim_stop = None
                self._reader_trim_closing = False
