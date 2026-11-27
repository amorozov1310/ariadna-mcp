"""Манифест файлов выгрузки и безопасное применение её разницы."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import zipfile
import contextlib
import zlib
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MAX_ENTRIES = 250_000
MAX_PATH = 1024
MAX_UNPACKED = 32 * 1024**3
SUSPICIOUS_DELETE_FLOOR = 1000


def _checkpoint(_name: str) -> None:
    """Метка этапа транзакции; используется для инъекции сбоев в тестах."""


class DeltaError(ValueError):
    pass


class DeltaConflict(DeltaError):
    pass


class DeltaBusy(DeltaError):
    """Журнал источника сейчас используется другим процессом сервера."""


@contextlib.contextmanager
def transaction_lock(workspace: Path, *, blocking: bool = False):
    """Межпроцессная блокировка восстановления и изменения выгрузки."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    # На Windows открытый файл блокировки не позволяет удалить каталог источника.
    if workspace.parent.name == ".source-delta":
        project_dir = workspace.parent.parent
        path = project_dir / ".source-locks" / f"{workspace.name}.lock"
    else:
        path = workspace / "delta.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise DeltaBusy("Источник занят другой операцией") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise DeltaBusy("Источник занят другой операцией") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _unique_object_pairs(pairs):
    result = {}
    seen = set()
    for key, value in pairs:
        identity = key.casefold() if os.name == "nt" else key
        if identity in seen:
            raise DeltaError(f"Повтор ключа в JSON: {key}")
        seen.add(identity)
        result[key] = value
    return result


def parse_manifest_json(raw: str) -> dict:
    return json.loads(raw, object_pairs_hook=_unique_object_pairs)


_WINDOWS_RESERVED_CHARS = '<>:"|?*'


def _path(value: str) -> str:
    """Проверить путь файла выгрузки по правилам POSIX и Windows сразу:
    разделитель — только «/», путь относительный, без пустых компонентов и
    «.»/«..», без двоеточий (буква диска, потоки NTFS) и служебных символов
    Windows; компонент не заканчивается точкой или пробелом."""
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH
            or "\\" in value or value.startswith("/") or "\x00" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise DeltaError(f"Недопустимый путь файла: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).as_posix() != value:
        raise DeltaError(f"Недопустимый путь файла: {value!r}")
    for part in parts:
        if (any(c in _WINDOWS_RESERVED_CHARS for c in part)
                or part.rstrip(" .") != part):
            raise DeltaError(f"Недопустимый путь файла: {value!r}")
    if any(part.casefold().startswith("template.") for part in parts):
        raise DeltaError(f"Макеты нельзя передавать: {value!r}")
    return value


def _staging_target(staging: Path, rel: str) -> Path:
    """Путь файла в каталоге распаковки. После разворачивания ссылок он обязан
    оставаться внутри staging: путь из манифеста уже проверен, но каталог
    распаковки мог быть подменён ссылкой."""
    staging_real = staging.resolve()
    out = staging.joinpath(*PurePosixPath(rel).parts)
    if not out.resolve().is_relative_to(staging_real):
        raise DeltaError(f"Путь выходит за каталог распаковки: {rel}")
    return out


def _validate_path_set(paths) -> None:
    """Проверить полные пути и единый регистр каждого общего родителя."""
    seen: dict[str, str] = {}
    for rel in paths:
        prefix = []
        for part in PurePosixPath(rel).parts:
            prefix.append(part)
            name = "/".join(prefix)
            key = name.casefold()
            previous = seen.get(key)
            if previous is not None and previous != name:
                raise DeltaError(f"Несогласованный регистр или повтор пути: {rel}")
            seen[key] = name
    full = {p.casefold() for p in paths}
    for rel in paths:
        parts = PurePosixPath(rel).parts
        for i in range(1, len(parts)):
            if "/".join(parts[:i]).casefold() in full:
                raise DeltaError(f"Файл используется как каталог: {rel}")


def is_reparse_point(path: Path) -> bool:
    """Проверка symlink, junction и reparse points, включая Python 3.11."""
    try:
        attrs = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return Path(path).is_symlink()
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_tree(root: Path) -> None:
    if is_reparse_point(root):
        raise DeltaError(f"Символическая ссылка в каталоге выгрузки: {root}")
    if not root.exists():
        return
    if not root.is_dir():
        raise DeltaError(f"Путь выгрузки не является каталогом: {root}")
    for base, dirs, files in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        for name in dirs + files:
            item = Path(base) / name
            if is_reparse_point(item) or not (item.is_dir() or item.is_file()):
                raise DeltaError(f"Ссылка или специальный файл в каталоге выгрузки: {item}")


def _managed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return not any(part.casefold().startswith("template.") or part.casefold() == 'logs' for part in parts)


def manifest(root: Path, cache_path: Path) -> dict:
    """Сканирует каталог и берёт SHA-256 из внешнего кэша при совпадении stat."""
    root = Path(root)
    _safe_tree(root)
    try:
        cached = json.loads(cache_path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        cached = {}
    old = cached.get("files", {}) if isinstance(cached, dict) else {}
    files: dict[str, dict] = {}
    root_stat = root.stat() if root.exists() else None
    if root_stat:
        generation = (root_stat.st_ctime_ns if os.name == "nt"
                      else getattr(root_stat, "st_birthtime_ns", None))
        root_identity = [root_stat.st_dev, root_stat.st_ino, generation]
    else:
        root_identity = None
    cache_valid = cached.get("root_identity") == root_identity if isinstance(cached, dict) else False
    if root.is_dir():
        for base, dirs, names in os.walk(root, followlinks=False, onerror=_raise_walk_error):
            dirs.sort()
            for name in sorted(names):
                full = Path(base) / name
                rel = full.relative_to(root).as_posix()
                if not _managed(rel):
                    continue
                rel = _path(rel)
                st = full.stat()
                prior = old.get(rel, {})
                if (os.name != "nt" and cache_valid and time.time_ns() - st.st_mtime_ns > 5_000_000_000 and prior.get("size") == st.st_size
                        and prior.get("mtime_ns") == st.st_mtime_ns
                        and prior.get("ctime_ns") == st.st_ctime_ns
                        and prior.get("ino") == st.st_ino):
                    digest = prior.get("sha256", "")
                else:
                    h = hashlib.sha256()
                    with full.open("rb") as f:
                        for block in iter(lambda: f.read(1024 * 1024), b""):
                            h.update(block)
                    digest = h.hexdigest()
                files[rel] = {"sha256": digest, "size": st.st_size,
                              "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
                              "ino": st.st_ino}
    public = {k: {"sha256": v["sha256"], "size": v["size"]} for k, v in sorted(files.items())}
    version = hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(cache_path, {"files": files, "version": version, "root_identity": root_identity})
    return {"version": version, "files": public}


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _journal_path(workspace: Path) -> Path:
    return workspace / "delta-journal.json"


def _actual_paths(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    files: dict[str, str] = {}
    dirs: dict[str, str] = {}
    if not root.is_dir():
        return files, dirs
    for base, names, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        for name in names:
            rel = (Path(base) / name).relative_to(root).as_posix()
            key = rel.casefold()
            if key in dirs and dirs[key] != rel:
                raise DeltaError(f"Коллизия регистра пути в выгрузке: {rel}")
            dirs[key] = rel
        for name in filenames:
            rel = (Path(base) / name).relative_to(root).as_posix()
            key = rel.casefold()
            if key in files and files[key] != rel:
                raise DeltaError(f"Коллизия регистра пути в выгрузке: {rel}")
            files[key] = rel
    return files, dirs


def _rename_case(root: Path, old_rel: str, new_rel: str, temp_rel: str) -> None:
    old = root.joinpath(*PurePosixPath(old_rel).parts)
    new = root.joinpath(*PurePosixPath(new_rel).parts)
    temp = root.joinpath(*PurePosixPath(temp_rel).parts)
    if old.exists() and old_rel != new_rel:
        os.replace(old, temp)
        new.mkdir(parents=True, exist_ok=True)
        _merge_dir_contents(temp, new)
        temp.rmdir()


def _merge_dir_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        target = destination / child.name
        if child.is_dir() and not is_reparse_point(child):
            if target.exists():
                if not target.is_dir():
                    raise DeltaError(f"Путь занят файлом при восстановлении: {target}")
                _merge_dir_contents(child, target)
                child.rmdir()
            else:
                os.replace(child, target)
        else:
            if target.exists():
                raise DeltaError(f"Путь занят файлом при восстановлении: {target}")
            os.replace(child, target)


def recover(root: Path, workspace: Path | None = None, *, lock_held: bool = False) -> bool:
    root = Path(root)
    workspace = Path(workspace) if workspace is not None else root.parent / f".{root.name}-delta-state"
    if not lock_held:
        try:
            with transaction_lock(workspace):
                return recover(root, workspace, lock_held=True)
        except DeltaBusy:
            return False
    return _recover_locked(root, workspace)


def _recover_locked(root: Path, workspace: Path) -> bool:
    journal = _journal_path(workspace)
    if not journal.exists():
        for orphan in (*workspace.glob('staging-*'), *workspace.glob('rollback-*')):
            shutil.rmtree(orphan, ignore_errors=True)
        return True
    data = json.loads(journal.read_text("utf-8"))
    backup = Path(data["backup"])
    staging = Path(data["staging"]) if data.get("staging") else None
    if data.get("committed"):
        shutil.rmtree(backup, ignore_errors=True)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        journal.unlink(missing_ok=True)
        return True
    old_by_fold = {rel.casefold(): rel for rel in data.get("old", [])}
    for rel in data.get("new", []):
        _, actual_files = _actual_paths(root)
        actual_rel = actual_files.get(rel.casefold(), rel)
        target = root.joinpath(*PurePosixPath(actual_rel).parts)
        prior = old_by_fold.get(rel.casefold())
        if prior is not None and not backup.joinpath(*PurePosixPath(prior).parts).exists():
            continue
        if target.is_file() or target.is_symlink():
            target.unlink(missing_ok=True)
    # Смена регистра каталога выполняется через промежуточное имя. Откатить
    # операции нужно в обратном порядке, используя целевой путь каждого шага:
    # для вложенных Old/Sub -> old/sub это сначала old/sub -> old/Sub, затем
    # old -> Old. Поиск только по casefold здесь создавал два дерева на POSIX.
    for move in reversed(data.get("dir_moves", [])):
        old_rel, new_rel, temp_rel = move[:3]
        if len(move) > 3 and move[3] == "planned":
            continue
        source = root.joinpath(*PurePosixPath(new_rel).parts)
        temp = root.joinpath(*PurePosixPath(temp_rel).parts)
        destination = root.joinpath(*PurePosixPath(old_rel).parts)
        if source.is_dir() and new_rel != old_rel:
            rollback_temp = source.with_name(f".delta-rollback-{os.getpid()}-{len(old_rel)}")
            os.replace(source, rollback_temp)
            destination.mkdir(parents=True, exist_ok=True)
            _merge_dir_contents(rollback_temp, destination)
            rollback_temp.rmdir()
        if temp.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            _merge_dir_contents(temp, destination)
            temp.rmdir()
    for rel in data.get("old", []):
        saved = backup.joinpath(*PurePosixPath(rel).parts)
        target = root.joinpath(*PurePosixPath(rel).parts)
        if saved.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            os.replace(saved, target)
    for rel in sorted(data.get("created_dirs", []), key=lambda p: p.count("/"), reverse=True):
        directory = root.joinpath(*PurePosixPath(rel).parts)
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    # Case-only renames can leave an empty directory under the target spelling
    # on case-sensitive filesystems. Remove only empty directories; excluded
    # files and any other user data keep the directory in place.
    for move in reversed(data.get("dir_moves", [])):
        directory = root.joinpath(*PurePosixPath(move[1]).parts)
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    shutil.rmtree(backup, ignore_errors=True)
    if staging is not None:
        shutil.rmtree(staging, ignore_errors=True)
    for rel in sorted(data.get("removed_dirs", []), key=lambda p: p.count("/")):
        root.joinpath(*PurePosixPath(rel).parts).mkdir(parents=True, exist_ok=True)
    if data.get("root_created"):
        try:
            root.rmdir()
        except OSError:
            pass
    journal.unlink(missing_ok=True)
    return True


def apply(root: Path, archive: BinaryIO, target_manifest: dict, base_version: str,
          current: dict, workspace: Path | None = None, *, lock_held: bool = False,
          journal_context: dict | None = None) -> dict:
    root = Path(root)
    workspace = Path(workspace) if workspace is not None else root.parent / f".{root.name}-delta-state"
    if not lock_held:
        with transaction_lock(workspace):
            recover(root, workspace, lock_held=True)
            return _apply_locked(root, archive, target_manifest, base_version, current, workspace,
                                 journal_context=journal_context)
    return _apply_locked(root, archive, target_manifest, base_version, current, workspace,
                         journal_context=journal_context)


def _apply_locked(root: Path, archive: BinaryIO, target_manifest: dict, base_version: str,
                  current: dict, workspace: Path, *, journal_context: dict | None = None) -> dict:
    """Устанавливает архив разницы; вызывающий удерживает reindex lock."""
    root = Path(root)
    if current["version"] != base_version:
        raise DeltaConflict("Манифест источника изменился; запросите его заново")
    _checkpoint('a')
    if not isinstance(target_manifest, dict) or not isinstance(target_manifest.get("files"), dict):
        raise DeltaError("Некорректный целевой манифест")
    desired = target_manifest["files"]
    if len(desired) > MAX_ENTRIES:
        raise DeltaError("Слишком много записей в целевом манифесте")
    normalized = {}
    total = 0
    for raw, spec in desired.items():
        rel = _path(raw)
        if not _managed(rel):
            raise DeltaError(f"Файл исключён из области разницы: {rel}")
        if not isinstance(spec, dict) or not isinstance(spec.get("size"), int) or spec["size"] < 0:
            raise DeltaError(f"Некорректный размер файла: {rel}")
        digest = spec.get("sha256", "")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DeltaError(f"Некорректный SHA-256: {rel}")
        total += spec["size"]
        normalized[rel] = {"sha256": digest, "size": spec["size"]}
    _validate_path_set(normalized)
    if total > MAX_UNPACKED:
        raise DeltaError("Целевой манифест превышает лимит размера")
    desired_version = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    if target_manifest.get("version") != desired_version:
        raise DeltaError("Версия целевого манифеста не соответствует его содержимому")

    workspace.mkdir(parents=True, exist_ok=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    if os.stat(workspace).st_dev != os.stat(root.parent).st_dev:
        raise DeltaError("Каталог отката и выгрузка находятся на разных файловых системах")
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=workspace))
    try:
        with zipfile.ZipFile(archive) as zf:
            all_infos = zf.infolist()
            infos = [i for i in all_infos if not i.is_dir()]
            if len(all_infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise DeltaError("Архив разницы превышает лимиты")
            seen = set()
            supplied = set()
            zip_files = []
            zip_prefixes = {}
            for info in all_infos:
                entry_name = info.filename.rstrip('/') if info.is_dir() else info.filename
                rel_name = _path(entry_name)
                if not _managed(rel_name):
                    raise DeltaError(f"Файл исключён из области разницы: {rel_name}")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (info.external_attr & 0x400 or kind == stat.S_IFLNK
                        or (kind and kind not in (stat.S_IFREG, stat.S_IFDIR))):
                    raise DeltaError(f"Недопустимый тип файла в ZIP: {rel_name}")
                key = rel_name.casefold()
                if key in seen:
                    raise DeltaError(f"Повтор файла в архиве: {rel_name}")
                seen.add(key)
                prefix = []
                for part in PurePosixPath(rel_name).parts:
                    prefix.append(part)
                    name = "/".join(prefix)
                    previous = zip_prefixes.get(name.casefold())
                    if previous is not None and previous != name:
                        raise DeltaError(f"Несогласованный регистр пути в ZIP: {rel_name}")
                    zip_prefixes[name.casefold()] = name
                if not info.is_dir():
                    zip_files.append(rel_name)
            _validate_path_set(zip_files)
            for info in infos:
                rel = _path(info.filename)
                spec = normalized.get(rel)
                if spec is None:
                    raise DeltaError(f"Файл архива отсутствует в целевом манифесте: {rel}")
                out = _staging_target(staging, rel)
                out.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                size = 0
                try:
                    with zf.open(info) as src, out.open("wb") as dst:
                        while block := src.read(1024 * 1024):
                            size += len(block)
                            if size > spec["size"]:
                                raise DeltaError(f"Размер файла не совпадает: {rel}")
                            h.update(block)
                            dst.write(block)
                except (RuntimeError, NotImplementedError) as exc:
                    raise DeltaError(f"Зашифрованная запись или неподдерживаемый метод ZIP: {rel}") from exc
                if size != spec["size"] or h.hexdigest() != spec["sha256"]:
                    raise DeltaError(f"Размер или SHA-256 не совпадает: {rel}")
                supplied.add(rel)
        old_files = current["files"]
        changed = {p for p, spec in normalized.items() if old_files.get(p) != spec}
        if changed != supplied:
            raise DeltaError("Архив должен содержать все изменённые и добавленные файлы")
        _checkpoint('b')
        removed = set(old_files) - set(normalized)
        if (len(removed) > SUSPICIOUS_DELETE_FLOOR
                and len(removed) > len(old_files) / 2):
            raise DeltaError("Разница удаляет слишком много файлов; выполните полную передачу выгрузки")
        affected = removed | changed
        backup = Path(tempfile.mkdtemp(prefix="rollback-", dir=workspace))
        journal = _journal_path(workspace)
        created_dirs = set()
        removed_dirs = set()
        root_created = not root.exists()
        for rel in changed:
            parent = PurePosixPath(rel).parent
            while str(parent) != ".":
                if not root.joinpath(*parent.parts).is_dir():
                    created_dirs.add(parent.as_posix())
                parent = parent.parent
        _, initial_dirs = _actual_paths(root)
        desired_dirs = set()
        for rel in normalized:
            parent = PurePosixPath(rel).parent
            while str(parent) != ".":
                desired_dirs.add(parent.as_posix())
                parent = parent.parent
        dir_moves = []
        for rel in sorted(desired_dirs, key=lambda p: p.count("/")):
            prior = initial_dirs.get(rel.casefold())
            if prior and prior != rel:
                # Родитель к этому шагу уже переименован. Перенести его новое
                # написание на исходный путь дочернего каталога, чтобы запланировать
                # и вложенную смену регистра (Old/Sub -> old/sub).
                source = prior
                for move in dir_moves:
                    old_parent, new_parent = move[:2]
                    old_parts = PurePosixPath(old_parent).parts
                    source_parts = PurePosixPath(source).parts
                    if (len(source_parts) > len(old_parts)
                            and tuple(p.casefold() for p in source_parts[:len(old_parts)])
                            == tuple(p.casefold() for p in old_parts)):
                        source = PurePosixPath(*PurePosixPath(new_parent).parts,
                                               *source_parts[len(old_parts):]).as_posix()
                if source != rel:
                    temp_rel = str(PurePosixPath(source).with_name(f".delta-case-{os.getpid()}-{len(dir_moves)}"))
                    dir_moves.append([source, rel, temp_rel, "planned"])
        journal_data = {"backup": str(backup), "old": sorted(affected & set(old_files)),
                        "new": sorted(changed), "staging": str(staging),
                        "created_dirs": sorted(created_dirs), "removed_dirs": [],
                        "dir_moves": dir_moves, "root_created": root_created,
                        "committed": False}
        if journal_context:
            journal_data.update(journal_context)
        _atomic_json(journal, journal_data)
        _checkpoint('c')
        root.mkdir(parents=True, exist_ok=True)
        _safe_tree(root)
        for move in dir_moves:
            old_rel, new_rel, temp_rel = move[:3]
            move[3] = "moving"
            _atomic_json(journal, journal_data)
            _rename_case(root, old_rel, new_rel, temp_rel)
            move[3] = "moved"
            _atomic_json(journal, journal_data)
        actual_files, _ = _actual_paths(root)
        for rel in sorted(affected & set(old_files), key=lambda x: x.count("/"), reverse=True):
            existing = actual_files.get(rel.casefold(), rel)
            src = root.joinpath(*PurePosixPath(existing).parts)
            if src.is_file():
                dst = backup.joinpath(*PurePosixPath(rel).parts)
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
        _checkpoint('d')
        candidates = set()
        for rel in affected & set(old_files):
            parent = PurePosixPath(rel).parent
            while str(parent) != ".":
                candidates.add(parent.as_posix())
                parent = parent.parent
        _, actual_dirs = _actual_paths(root)
        for rel in sorted(candidates, key=lambda p: p.count("/"), reverse=True):
            actual_rel = actual_dirs.get(rel.casefold(), rel)
            directory = root.joinpath(*PurePosixPath(actual_rel).parts)
            if directory.is_dir() and not any(directory.iterdir()):
                removed_dirs.add(actual_rel)
                journal_data["removed_dirs"] = sorted(removed_dirs)
                _atomic_json(journal, journal_data)
                directory.rmdir()
        for rel in sorted(changed, key=lambda x: x.count("/")):
            src = staging.joinpath(*PurePosixPath(rel).parts)
            dst = root.joinpath(*PurePosixPath(rel).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                if any(dst.iterdir()):
                    raise DeltaError("Нельзя заменить непустой каталог файлом: " + rel)
                removed_dirs.add(rel)
                journal_data["removed_dirs"] = sorted(removed_dirs)
                _atomic_json(journal, journal_data)
                dst.rmdir()
            elif dst.exists():
                dst.unlink()
            os.replace(src, dst)
        journal_data["removed_dirs"] = sorted(removed_dirs)
        _atomic_json(journal, journal_data)
        _checkpoint('e')
        actual_files, _ = _actual_paths(root)
        for rel, spec in normalized.items():
            actual_rel = actual_files.get(rel.casefold())
            file = root.joinpath(*PurePosixPath(actual_rel or rel).parts)
            if actual_rel != rel or not file.is_file() or file.stat().st_size != spec["size"]:
                raise DeltaError("Итоговый файл не прошёл проверку имени или размера: " + rel)
        # Удаления проверяются тем же способом: файла из прежнего манифеста,
        # которого нет в целевом, не должно остаться в выгрузке (в том числе
        # под другим регистром, если такого имени нет в целевом манифесте).
        for rel in removed:
            actual_rel = actual_files.get(rel.casefold())
            if actual_rel is not None and actual_rel not in normalized:
                raise DeltaError("Удалённый файл остался в выгрузке: " + rel)
        journal_data["committed"] = True
        _atomic_json(journal, journal_data)
        _checkpoint('f')
        shutil.rmtree(backup, ignore_errors=True)
        _checkpoint('g')
        journal.unlink(missing_ok=True)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        recover(root, workspace, lock_held=True)
        raise DeltaError("Повреждённый ZIP-архив разницы") from exc
    except Exception:
        recover(root, workspace, lock_held=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"applied": len(changed), "deleted": len(removed)}
