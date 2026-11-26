"""Хеш-манифест выгрузки и безопасное атомарное применение её разницы."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from .archives import UnsafeArchiveError, _Budget, _check_name
from .fsretry import replace_with_retry, rmtree_with_retry

MANIFEST_NAME = '.ariadna-manifest.json'
DELTA_META = '.ariadna-delta.json'
MAX_UPLOAD_BYTES = 8 * 1024 ** 3
MAX_DELTA_META_BYTES = 256 * 1024 ** 2


def _included(path: Path) -> bool:
    return not path.name.startswith('Template.')


def _hash_file(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {'sha256': digest.hexdigest(), 'size': size}


def build_manifest(root: Path) -> dict:
    """Сканирует каталог и возвращает стабильный манифест без макетов."""
    files = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink():
            raise UnsafeArchiveError(f'Выгрузка содержит символическую ссылку: {path}')
        if path.is_file() and _included(path):
            files[path.relative_to(root).as_posix()] = _hash_file(path)
    return {'version': manifest_version(files), 'files': files}


def manifest_version(files: dict) -> str:
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def read_or_create_manifest(root: Path, sidecar: Path) -> dict:
    """Читает сохранённый манифест; legacy-источник сканируется один раз."""
    try:
        value = json.loads(sidecar.read_text(encoding='utf-8'))
        files = value['files']
        if value.get('version') == manifest_version(files):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    value = build_manifest(root)
    write_manifest(sidecar, value)
    return value


def write_manifest(sidecar: Path, value: dict) -> None:
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.manifest-', dir=sidecar.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        replace_with_retry(temp, sidecar)
    finally:
        Path(temp).unlink(missing_ok=True)


def _safe_relpath(name: str) -> PurePosixPath:
    if '\\' in name or ':' in name:
        raise UnsafeArchiveError(f'Недопустимый путь файла: {name!r}')
    normalized = name
    _check_name(normalized)
    if any(part in ('', '.') for part in normalized.split('/')):
        raise UnsafeArchiveError(f'Некорректный путь файла: {name!r}')
    path = PurePosixPath(normalized)
    if not path.parts:
        raise UnsafeArchiveError(f'Некорректный путь файла: {name!r}')
    if normalized.startswith('./'):
        raise UnsafeArchiveError(f'Некорректный путь файла: {name!r}')
    return path


def apply_delta(root: Path, sidecar: Path, archive, filename: str, expected_version: str) -> dict:
    """Проверяет и применяет zip с метаданными и изменёнными файлами."""
    root, sidecar = Path(root), Path(sidecar)
    root.parent.mkdir(parents=True, exist_ok=True)
    fd, upload = tempfile.mkstemp(prefix='.delta-upload-', dir=root.parent)
    stage = Path(tempfile.mkdtemp(prefix='.delta-stage-', dir=root.parent))
    backup = root.with_name(root.name + '.previous')
    try:
        size = 0
        with os.fdopen(fd, 'wb') as out:
            while chunk := archive.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UnsafeArchiveError(f'Разница больше лимита {MAX_UPLOAD_BYTES} байт')
                out.write(chunk)
        with zipfile.ZipFile(upload) as zf:
            budget = _Budget()
            entries = {}
            for info in zf.infolist():
                _check_name(info.filename)
                if info.flag_bits & 0x1:
                    raise UnsafeArchiveError(f'Зашифрованный архив не поддерживается: {info.filename!r}')
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or (file_type and file_type != stat.S_IFREG and not info.is_dir()):
                    raise UnsafeArchiveError(f'Недопустимый тип файла: {info.filename!r}')
                if info.is_dir():
                    continue
                rel = _safe_relpath(info.filename)
                if rel.as_posix() == DELTA_META and info.file_size > MAX_DELTA_META_BYTES:
                    raise UnsafeArchiveError(
                        f'Манифест разницы больше {MAX_DELTA_META_BYTES} байт')
                if rel.as_posix() in entries:
                    raise UnsafeArchiveError(f'Повторяющийся путь в архиве: {info.filename!r}')
                entries[rel.as_posix()] = info
                budget.add(info.file_size)
            if DELTA_META not in entries:
                raise UnsafeArchiveError('В архиве нет описания разницы')
            meta = json.loads(zf.read(entries.pop(DELTA_META)))
            current = read_or_create_manifest(root, sidecar)
            if expected_version != current['version'] or meta.get('base_version') != expected_version:
                raise ValueError('Манифест источника изменился; запросите его заново')
            target_files = meta.get('files')
            deleted = meta.get('deleted')
            if not isinstance(target_files, dict) or not isinstance(deleted, list):
                raise UnsafeArchiveError('Некорректное описание разницы')
            if len(target_files) > budget.max_files:
                raise UnsafeArchiveError(f'Манифест содержит больше {budget.max_files} файлов')
            for name, value in target_files.items():
                _safe_relpath(name)
                if name == DELTA_META:
                    raise UnsafeArchiveError(f'Путь зарезервирован для метаданных разницы: {name!r}')
                if (not isinstance(value, dict) or not isinstance(value.get('sha256'), str)
                        or len(value['sha256']) != 64 or not isinstance(value.get('size'), int)
                        or value['size'] < 0):
                    raise UnsafeArchiveError(f'Некорректная запись манифеста: {name!r}')
            if any(not isinstance(name, str) for name in deleted):
                raise UnsafeArchiveError('Некорректный список удалённых файлов')
            if len(set(deleted)) != len(deleted):
                raise UnsafeArchiveError('Список удалённых файлов содержит повторы')
            if len(deleted) > budget.max_files:
                raise UnsafeArchiveError(f'Список удалённых файлов больше {budget.max_files}')
            for name in deleted:
                _safe_relpath(name)
            if manifest_version(target_files) != meta.get('target_version'):
                raise UnsafeArchiveError('Хеш целевого манифеста не совпадает')
            changed = set(entries)
            expected_changed = {p for p, v in target_files.items() if current['files'].get(p) != v}
            if changed != expected_changed or set(deleted) != set(current['files']) - set(target_files):
                raise UnsafeArchiveError('Список файлов разницы не совпадает с манифестом')
            if root.exists():
                for path in root.rglob('*'):
                    if path.is_symlink():
                        raise UnsafeArchiveError(f'Выгрузка содержит символическую ссылку: {path}')
            shutil.copytree(root, stage, dirs_exist_ok=True)
            for name, info in entries.items():
                rel = _safe_relpath(name)
                dest = stage.joinpath(*rel.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                count = 0
                with zf.open(info) as src, dest.open('wb') as out:
                    while chunk := src.read(1024 * 1024):
                        h.update(chunk)
                        count += len(chunk)
                        out.write(chunk)
                if {'sha256': h.hexdigest(), 'size': count} != target_files[name]:
                    raise UnsafeArchiveError(f'Хеш файла не совпадает: {name}')
            for name in deleted:
                rel = _safe_relpath(name)
                (stage.joinpath(*rel.parts)).unlink(missing_ok=True)
            if backup.exists():
                rmtree_with_retry(backup)
            replace_with_retry(root, backup)
            try:
                replace_with_retry(stage, root)
            except Exception:
                replace_with_retry(backup, root)
                raise
            rmtree_with_retry(backup)
            result = {'version': meta['target_version'], 'files': target_files}
            write_manifest(sidecar, result)
            return result
    except (zipfile.BadZipFile, zipfile.LargeZipFile, json.JSONDecodeError,
            RuntimeError, EOFError, zlib.error) as e:
        raise UnsafeArchiveError(f'Архив разницы повреждён: {e}') from e
    finally:
        Path(upload).unlink(missing_ok=True)
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
