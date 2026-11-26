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


def _safe_tree(root: Path) -> None:
    if not root.exists():
        return
    if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
        raise DeltaError(f"Символическая ссылка в каталоге выгрузки: {root}")
    if not root.is_dir():
        raise DeltaError(f"Путь выгрузки не является каталогом: {root}")
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            item = Path(base) / name
            junction = getattr(item, 'is_junction', lambda: False)()
            if item.is_symlink() or junction or not (item.is_dir() or item.is_file()):
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


def _journal_path(root: Path) -> Path:
    return root.parent / f".{root.name}.delta-journal.json"


def recover(root: Path) -> None:
    journal = _journal_path(root)
    if not journal.exists():
        return
    data = json.loads(journal.read_text("utf-8"))
    backup = Path(data["backup"])
    if data.get("committed"):
        shutil.rmtree(backup, ignore_errors=True)
        journal.unlink(missing_ok=True)
        return
    old_by_fold = {rel.casefold(): rel for rel in data.get("old", [])}
    for rel in data.get("new", []):
        target = root.joinpath(*PurePosixPath(rel).parts)
        prior = old_by_fold.get(rel.casefold())
        # Если старый файл ещё не переименовали в backup, целевой путь всё ещё
        # содержит оригинал и удалять его при восстановлении нельзя.
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
    shutil.rmtree(backup, ignore_errors=True)
    journal.unlink(missing_ok=True)


def apply(root: Path, archive: BinaryIO, target_manifest: dict, base_version: str,
          current: dict) -> dict:
    """Устанавливает архив разницы; вызывающий удерживает reindex lock."""
    root = Path(root)
    recover(root)
    if current["version"] != base_version:
        raise DeltaConflict("Манифест источника изменился; запросите его заново")
    _checkpoint('a')
    if not isinstance(target_manifest, dict) or not isinstance(target_manifest.get("files"), dict):
        raise DeltaError("Некорректный целевой манифест")
    desired = target_manifest["files"]
    if len(desired) > MAX_ENTRIES:
        raise DeltaError("Слишком много записей в целевом манифесте")
    normalized = {}
    folded = set()
    total = 0
    for raw, spec in desired.items():
        rel = _path(raw)
        if rel.casefold() in folded:
            raise DeltaError(f"Повтор пути без учёта регистра: {rel}")
        folded.add(rel.casefold())
        if not isinstance(spec, dict) or not isinstance(spec.get("size"), int) or spec["size"] < 0:
            raise DeltaError(f"Некорректный размер файла: {rel}")
        digest = spec.get("sha256", "")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DeltaError(f"Некорректный SHA-256: {rel}")
        total += spec["size"]
        normalized[rel] = {"sha256": digest, "size": spec["size"]}
    if total > MAX_UNPACKED:
        raise DeltaError("Целевой манифест превышает лимит размера")
    desired_version = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    if target_manifest.get("version") != desired_version:
        raise DeltaError("Версия целевого манифеста не соответствует его содержимому")

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}-delta-", dir=root.parent))
    try:
        with zipfile.ZipFile(archive) as zf:
            all_infos = zf.infolist()
            infos = [i for i in all_infos if not i.is_dir()]
            if len(all_infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise DeltaError("Архив разницы превышает лимиты")
            seen = set()
            supplied = set()
            for info in all_infos:
                entry_name = info.filename.rstrip('/') if info.is_dir() else info.filename
                rel_name = _path(entry_name)
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind == stat.S_IFLNK or (kind and kind not in (stat.S_IFREG, stat.S_IFDIR)):
                    raise DeltaError(f"Недопустимый тип файла в ZIP: {rel_name}")
                key = rel_name.casefold()
                if key in seen:
                    raise DeltaError(f"Повтор файла в архиве: {rel_name}")
                seen.add(key)
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
        affected = removed | changed
        backup = Path(tempfile.mkdtemp(prefix=f".{root.name}-rollback-", dir=root.parent))
        journal = _journal_path(root)
        _atomic_json(journal, {"backup": str(backup), "old": sorted(affected & set(old_files)),
                               "new": sorted(changed), "committed": False})
        _checkpoint('c')
        root.mkdir(parents=True, exist_ok=True)
        _safe_tree(root)
        # Перемещаем старые файлы, а не копируем их; регистронезависимый путь
        # учитывает смену регистра имени на Windows.
        actual = {}
        for base, _, names in os.walk(root, followlinks=False):
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
        # Не оставляем пустые каталоги от удалённых файлов.
        for directory, dirs, names in os.walk(root, topdown=False):
            p = Path(directory)
            if p != root and not dirs and not names:
                p.rmdir()
        for rel, spec in normalized.items():
            file = root.joinpath(*PurePosixPath(rel).parts)
            if not file.is_file() or file.stat().st_size != spec["size"]:
                raise DeltaError(f"Итоговый файл не прошёл проверку: {rel}")
        _atomic_json(journal, {"backup": str(backup), "old": sorted(affected & set(old_files)),
                               "new": sorted(changed), "committed": True})
        _checkpoint('f')
        shutil.rmtree(backup, ignore_errors=True)
        _checkpoint('g')
        journal.unlink(missing_ok=True)
    except Exception:
        recover(root)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"applied": len(changed), "deleted": len(removed)}
