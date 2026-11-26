"""Манифест файлов выгрузки и безопасное применение её разницы."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
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


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH
            or "\\" in value or value.startswith("/") or "\x00" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or (len(value) > 1 and value[1] == ":")):
        raise DeltaError(f"Недопустимый путь файла: {value!r}")
    p = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in p.parts) or p.as_posix() != value:
        raise DeltaError(f"Недопустимый путь файла: {value!r}")
    if any(part.startswith("Template.") for part in p.parts):
        raise DeltaError(f"Макеты нельзя передавать: {value!r}")
    return value


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
    return not any(part.startswith("Template.") or part.casefold() == 'logs' for part in parts)


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
    if root.is_dir():
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                full = Path(base) / name
                rel = full.relative_to(root).as_posix()
                if not _managed(rel):
                    continue
                rel = _path(rel)
                st = full.stat()
                prior = old.get(rel, {})
                if prior.get("size") == st.st_size and prior.get("mtime_ns") == st.st_mtime_ns:
                    digest = prior.get("sha256", "")
                else:
                    h = hashlib.sha256()
                    with full.open("rb") as f:
                        for block in iter(lambda: f.read(1024 * 1024), b""):
                            h.update(block)
                    digest = h.hexdigest()
                files[rel] = {"sha256": digest, "size": st.st_size,
                              "mtime_ns": st.st_mtime_ns}
    public = {k: {"sha256": v["sha256"], "size": v["size"]} for k, v in sorted(files.items())}
    version = hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(cache_path, {"files": files, "version": version})
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


def recover(root: Path, workspace: Path | None = None) -> None:
    root = Path(root)
    workspace = Path(workspace) if workspace is not None else root.parent / f".{root.name}-delta-state"
    journal = _journal_path(workspace)
    if not journal.exists():
        for orphan in (*workspace.glob('staging-*'), *workspace.glob('rollback-*')):
            shutil.rmtree(orphan, ignore_errors=True)
        return
    data = json.loads(journal.read_text("utf-8"))
    backup = Path(data["backup"])
    staging = Path(data["staging"]) if data.get("staging") else None
    if data.get("committed"):
        shutil.rmtree(backup, ignore_errors=True)
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        journal.unlink(missing_ok=True)
        return
    old_by_fold = {rel.casefold(): rel for rel in data.get("old", [])}
    for rel in data.get("new", []):
        target = root.joinpath(*PurePosixPath(rel).parts)
        prior = old_by_fold.get(rel.casefold())
        if prior is not None and not backup.joinpath(*PurePosixPath(prior).parts).exists():
            continue
        if target.is_file() or target.is_symlink():
            target.unlink(missing_ok=True)
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
    shutil.rmtree(backup, ignore_errors=True)
    if staging is not None:
        shutil.rmtree(staging, ignore_errors=True)
    journal.unlink(missing_ok=True)


def apply(root: Path, archive: BinaryIO, target_manifest: dict, base_version: str,
          current: dict, workspace: Path | None = None) -> dict:
    """Устанавливает архив разницы; вызывающий удерживает reindex lock."""
    root = Path(root)
    workspace = Path(workspace) if workspace is not None else root.parent / f".{root.name}-delta-state"
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
                out = staging.joinpath(*PurePosixPath(rel).parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                size = 0
                with zf.open(info) as src, out.open("wb") as dst:
                    while block := src.read(1024 * 1024):
                        size += len(block)
                        if size > spec["size"]:
                            raise DeltaError(f"Размер файла не совпадает: {rel}")
                        h.update(block)
                        dst.write(block)
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
        for rel in changed:
            parent = PurePosixPath(rel).parent
            while str(parent) != ".":
                candidate = root.joinpath(*parent.parts)
                if not candidate.is_dir():
                    created_dirs.add(parent.as_posix())
                parent = parent.parent
        _atomic_json(journal, {"backup": str(backup), "old": sorted(affected & set(old_files)),
                               "new": sorted(changed), "staging": str(staging),
                               "created_dirs": sorted(created_dirs), "committed": False})
        _checkpoint('c')
        root.mkdir(parents=True, exist_ok=True)
        _safe_tree(root)
        # Перемещаем старые файлы, а не копируем их; регистронезависимый путь
        # учитывает смену регистра имени на Windows.
        actual = {}
        for base, _, names in os.walk(root, followlinks=False, onerror=_raise_walk_error):
            for name in names:
                rel = (Path(base) / name).relative_to(root).as_posix()
                actual[rel.casefold()] = rel
        for rel in sorted(affected & set(old_files), key=lambda x: x.count("/"), reverse=True):
            existing = actual.get(rel.casefold(), rel)
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
        for rel in sorted(candidates, key=lambda p: p.count("/"), reverse=True):
            directory = root.joinpath(*PurePosixPath(rel).parts)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        for rel in sorted(changed, key=lambda x: x.count("/")):
            src = staging.joinpath(*PurePosixPath(rel).parts)
            dst = root.joinpath(*PurePosixPath(rel).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                if any(dst.iterdir()):
                    raise DeltaError(f"Нельзя заменить непустой каталог файлом: {rel}")
                dst.rmdir()
            elif dst.exists():
                dst.unlink()
            os.replace(src, dst)
        _checkpoint('e')
        for rel, spec in normalized.items():
            file = root.joinpath(*PurePosixPath(rel).parts)
            if not file.is_file() or file.stat().st_size != spec["size"]:
                raise DeltaError(f"Итоговый файл не прошёл проверку: {rel}")
        _atomic_json(journal, {"backup": str(backup), "old": sorted(affected & set(old_files)),
                               "new": sorted(changed), "staging": str(staging),
                               "created_dirs": sorted(created_dirs), "committed": True})
        _checkpoint('f')
        shutil.rmtree(backup, ignore_errors=True)
        _checkpoint('g')
        journal.unlink(missing_ok=True)
    except Exception:
        recover(root, workspace)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"applied": len(changed), "deleted": len(removed)}
