"""Манифест выгрузки и безопасная публикация только изменённых файлов."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
import zlib
import uuid
from pathlib import Path, PurePosixPath

from .archives import UnsafeArchiveError, _Budget, _check_name
from .fsretry import replace_with_retry, rmtree_with_retry

MANIFEST_NAME = '.ariadna-manifest.json'
DELTA_META = '.ariadna-delta.json'
MAX_UPLOAD_BYTES = 8 * 1024 ** 3
MAX_DELTA_META_BYTES = 256 * 1024 ** 2
MAX_PATH_BYTES = 4096
_WINDOWS_DEVICES = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                    *(f'lpt{i}' for i in range(1, 10))}


def _included(path: Path) -> bool:
    return path.name != MANIFEST_NAME and not path.name.startswith('Template.')


def _hash_file(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {'sha256': digest.hexdigest(), 'size': size}


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, 'st_file_attributes', 0)
    reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return path.is_symlink() or bool(attributes & reparse)


def _assert_inside(root: Path, path: Path) -> None:
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    try:
        inside = os.path.commonpath((root_real, path_real)) == root_real
    except ValueError:
        inside = False
    if not inside:
        raise UnsafeArchiveError(f'Путь выходит за корень выгрузки: {path}')


def _check_tree(root: Path) -> None:
    """Отклоняет symlink/junction в самом корне и внутри выгрузки."""
    if _is_reparse(root):
        raise UnsafeArchiveError(f'Корень выгрузки не может быть ссылкой: {root}')
    if not root.exists():
        return
    root_real = Path(os.path.realpath(root))
    for path in root.rglob('*'):
        if _is_reparse(path):
            raise UnsafeArchiveError(f'Выгрузка содержит ссылку или reparse point: {path}')
        _assert_inside(root_real, path)


def build_manifest(root: Path) -> dict:
    """Сканирует каталог и возвращает хеши обычных файлов без макетов."""
    root = Path(root)
    _check_tree(root)
    files = {}
    if root.is_dir():
        for path in sorted(root.rglob('*')):
            if path.is_file() and _included(path):
                files[path.relative_to(root).as_posix()] = _hash_file(path)
    return {'version': manifest_version(files), 'files': files}


def manifest_version(files: dict) -> str:
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def recover_source(root: Path) -> None:
    """Roll back an interrupted file-level transaction; never remove another process stage."""
    root = Path(root)
    journal = root.parent / f'.{root.name}-delta-journal'
    if not journal.is_dir():
        return
    try:
        state = json.loads((journal / 'journal.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        shutil.rmtree(journal, ignore_errors=True)
        return
    except (OSError, ValueError) as exc:
        raise UnsafeArchiveError(f'Corrupt source update journal: {journal}') from exc
    if state.get('state') == 'committed':
        shutil.rmtree(journal, ignore_errors=True)
        return
    operations = state.get('operations', [])
    resolved = []
    for item in operations:
        name = item.get('path')
        rel = PurePosixPath(name) if name == MANIFEST_NAME else _safe_relpath(name)
        path = root.joinpath(*rel.parts)
        _assert_inside(root, path)
        resolved.append((item, path, journal / 'backup' / item['backup']))
    # Remove new files and empty shape-conflict directories before restoring old
    # files. This handles transitions such as file -> directory and its inverse.
    for item, path, _backup in sorted(resolved, key=lambda entry: len(entry[1].parts), reverse=True):
        if path.is_file() and not _is_reparse(path):
            path.unlink(missing_ok=True)
        elif path.is_dir() and not _is_reparse(path):
            try:
                path.rmdir()
            except OSError:
                pass
    for item, path, backup in sorted(resolved, key=lambda entry: len(entry[1].parts)):
        if item.get('existed') and backup.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f'.{path.name}.restore-{uuid.uuid4().hex}')
            shutil.copy2(backup, temp)
            replace_with_retry(temp, path)
    shutil.rmtree(journal, ignore_errors=True)


def read_or_create_manifest(root: Path, sidecar: Path) -> dict:
    """Читает кэш из выгрузки; legacy-выгрузка сканируется и кэшируется разово."""
    root, sidecar = Path(root), Path(sidecar)
    recover_source(root)
    _check_tree(root)
    cache_paths = [sidecar]
    if root.is_dir():
        cache_paths.append(root.parent / MANIFEST_NAME)  # миграция раннего sidecar формата
    for cache_path in cache_paths:
        try:
            value = json.loads(cache_path.read_text(encoding='utf-8'))
            files = value['files']
            if value.get('version') == manifest_version(files):
                if cache_path != sidecar:
                    write_manifest(sidecar, value)
                    cache_path.unlink(missing_ok=True)
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
    value = build_manifest(root)
    # Для ещё не созданного источника не создаём каталог до первой загрузки.
    if root.is_dir():
        write_manifest(sidecar, value)
    return value


def write_manifest(sidecar: Path, value: dict) -> None:
    sidecar = Path(sidecar)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.manifest-', dir=sidecar.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        replace_with_retry(temp, sidecar)
    finally:
        Path(temp).unlink(missing_ok=True)


def invalidate_manifest(root: Path) -> None:
    """Сбрасывает индекс хешей после замены файлов не через delta API."""
    root = Path(root)
    for path in (root / MANIFEST_NAME, root.parent / MANIFEST_NAME):
        try:
            path.unlink(missing_ok=True)
        except IsADirectoryError:
            shutil.rmtree(path)


def _safe_relpath(name: str) -> PurePosixPath:
    if '\\' in name or ':' in name or len(name.encode('utf-8')) > MAX_PATH_BYTES:
        raise UnsafeArchiveError(f'Недопустимый или слишком длинный путь файла: {name!r}')
    _check_name(name)
    parts = name.split('/')
    if any(part in ('', '.') or part.endswith((' ', '.')) for part in parts):
        raise UnsafeArchiveError(f'Некорректный путь файла: {name!r}')
    if any(part.split('.', 1)[0].casefold() in _WINDOWS_DEVICES for part in parts):
        raise UnsafeArchiveError(f'Недопустимое имя файла Windows: {name!r}')
    path = PurePosixPath(name)
    if not path.parts:
        raise UnsafeArchiveError(f'Некорректный путь файла: {name!r}')
    return path


def _write_changed_file(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path,
                        expected: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    with zf.open(info) as src, dest.open('wb') as out:
        while chunk := src.read(1024 * 1024):
            digest.update(chunk)
            count += len(chunk)
            out.write(chunk)
    if {'sha256': digest.hexdigest(), 'size': count} != expected:
        raise UnsafeArchiveError(f'Хеш файла не совпадает: {info.filename}')


def _journal_atomic(path: Path, value: dict) -> None:
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(temp, path)


def _remove_empty_parents(path: Path, root: Path) -> None:
    current = path.parent
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def apply_delta(root: Path, sidecar: Path, archive, filename: str,
                expected_version: str) -> dict:
    """Validate and transactionally apply changed files without cloning the source tree."""
    root, sidecar = Path(root), Path(sidecar)
    root.parent.mkdir(parents=True, exist_ok=True)
    recover_source(root)
    _check_tree(root)
    fd, upload = tempfile.mkstemp(prefix='.delta-upload-', dir=root.parent)
    journal = root.parent / f'.{root.name}-delta-journal'
    try:
        size = 0
        with os.fdopen(fd, 'wb') as out:
            while chunk := archive.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UnsafeArchiveError(f'Delta exceeds upload limit {MAX_UPLOAD_BYTES}')
                out.write(chunk)
        with zipfile.ZipFile(upload) as zf:
            budget = _Budget()
            entries: dict[str, zipfile.ZipInfo] = {}
            seen = set()
            for info in zf.infolist():
                raw = info.filename[:-1] if info.is_dir() and info.filename.endswith('/') else info.filename
                _check_name(raw)
                if len(raw.encode('utf-8')) > MAX_PATH_BYTES:
                    raise UnsafeArchiveError('Archive path is too long')
                if info.flag_bits & 1:
                    raise UnsafeArchiveError('Encrypted archives are not supported')
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or (kind and kind != stat.S_IFREG and not info.is_dir()):
                    raise UnsafeArchiveError(f'Unsupported file type: {info.filename!r}')
                rel = _safe_relpath(raw)
                key = rel.as_posix()
                if key in seen:
                    raise UnsafeArchiveError(f'Duplicate archive path: {info.filename!r}')
                seen.add(key)
                if info.is_dir():
                    if info.file_size:
                        raise UnsafeArchiveError('Directory entries cannot contain data')
                    continue
                if key == DELTA_META and info.file_size > MAX_DELTA_META_BYTES:
                    raise UnsafeArchiveError('Delta description exceeds limit')
                entries[key] = info
                budget.add(info.file_size)
            if DELTA_META not in entries:
                raise UnsafeArchiveError('Delta description is missing')
            meta = json.loads(zf.read(entries.pop(DELTA_META)))
            current = read_or_create_manifest(root, sidecar)
            _check_tree(root)
            if expected_version != current['version'] or meta.get('base_version') != expected_version:
                raise ValueError('Source manifest changed; fetch it again')
            target = meta.get('files')
            deleted = meta.get('deleted')
            if not isinstance(target, dict) or not isinstance(deleted, list):
                raise UnsafeArchiveError('Invalid delta description')
            if len(target) > budget.max_files or len(deleted) > budget.max_files:
                raise UnsafeArchiveError('Delta file count exceeds limit')
            normalized = set()
            for name, value in target.items():
                _safe_relpath(name)
                folded = os.path.normcase(name)
                if folded in normalized:
                    raise UnsafeArchiveError(f'Conflicting target paths: {name!r}')
                normalized.add(folded)
                if name in (DELTA_META, MANIFEST_NAME) or not _included(Path(name)):
                    raise UnsafeArchiveError(f'Reserved or excluded target path: {name!r}')
                if (not isinstance(value, dict) or not isinstance(value.get('sha256'), str)
                        or len(value['sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in value['sha256'])
                        or type(value.get('size')) is not int or value['size'] < 0):
                    raise UnsafeArchiveError(f'Invalid file manifest entry: {name!r}')
            if any(not isinstance(n, str) for n in deleted) or len(set(deleted)) != len(deleted):
                raise UnsafeArchiveError('Invalid deleted paths')
            for name in deleted:
                _safe_relpath(name)
            folded_target = {os.path.normcase(n) for n in target}
            for name in target:
                parts = name.split('/')
                if any(os.path.normcase('/'.join(parts[:i])) in folded_target
                       for i in range(1, len(parts))):
                    raise UnsafeArchiveError(f'Target contains file/directory conflict: {name!r}')
            if manifest_version(target) != meta.get('target_version'):
                raise UnsafeArchiveError('Target manifest hash does not match')
            changed = set(entries)
            expected_changed = {p for p, value in target.items() if current['files'].get(p) != value}
            expected_deleted = set(current['files']) - set(target)
            if changed != expected_changed or set(deleted) != expected_deleted:
                raise UnsafeArchiveError('Delta file list does not match target manifest')
            if not changed and not deleted and meta['target_version'] == current['version']:
                _check_tree(root)
                for name, expected in target.items():
                    path = root.joinpath(*_safe_relpath(name).parts)
                    if (not path.is_file() or _is_reparse(path)
                            or path.stat().st_size != expected['size']):
                        raise UnsafeArchiveError(f'Target file failed validation: {name}')
                return {'manifest': current, 'changed': False}

            operations = []
            affected = sorted(changed | set(deleted) | {MANIFEST_NAME})
            if journal.exists():
                raise UnsafeArchiveError('Another source transaction is active')
            (journal / 'backup').mkdir(parents=True)
            for index, name in enumerate(affected):
                path = root.joinpath(*_safe_relpath(name).parts) if name != MANIFEST_NAME else root / MANIFEST_NAME
                existed = path.is_file() and not _is_reparse(path)
                backup_name = f'{index:08d}.bak'
                operations.append({'path': name, 'backup': backup_name, 'existed': existed})
            state = {'state': 'applying', 'operations': operations}
            _journal_atomic(journal / 'journal.json', state)
            for item in operations:
                path = root / item['path']
                if item['existed']:
                    shutil.copy2(path, journal / 'backup' / item['backup'])

            # Stage incoming bytes in the transaction journal so a crash cannot
            # leave a partial temporary file in the published XML tree.
            staged_files = {}
            new_dir = journal / 'new'
            new_dir.mkdir()
            for index, name in enumerate(sorted(changed)):
                staged = new_dir / f'{index:08d}.new'
                digest = hashlib.sha256()
                count = 0
                with zf.open(entries[name]) as src, staged.open('wb') as out:
                    while chunk := src.read(1024 * 1024):
                        digest.update(chunk)
                        count += len(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if {'sha256': digest.hexdigest(), 'size': count} != target[name]:
                    raise UnsafeArchiveError(f'File hash mismatch: {name}')
                staged_files[name] = staged

            # Remove old tracked paths first to allow file/directory shape changes.
            for item in operations:
                path = root / item['path']
                if item['path'] == MANIFEST_NAME:
                    continue
                if item['path'] in deleted or item['path'] in changed:
                    if path.is_file() and not _is_reparse(path):
                        path.unlink()
                    _remove_empty_parents(path, root)

            for name in sorted(changed):
                expected = target[name]
                dest = root.joinpath(*_safe_relpath(name).parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if staged_files[name].stat().st_size != expected['size']:
                    raise UnsafeArchiveError(f'File size mismatch: {name}')
                replace_with_retry(staged_files[name], dest)

            # Excluded files are intentionally not represented in target and remain untouched.
            for name, expected in target.items():
                path = root.joinpath(*_safe_relpath(name).parts)
                if not path.is_file() or _is_reparse(path) or path.stat().st_size != expected['size']:
                    raise UnsafeArchiveError(f'Target file failed validation: {name}')
            result = {'version': meta['target_version'], 'files': target}
            manifest_temp = journal / 'new-manifest.json'
            with manifest_temp.open('w', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            replace_with_retry(manifest_temp, root / MANIFEST_NAME)
            _check_tree(root)
            state['state'] = 'committed'
            _journal_atomic(journal / 'journal.json', state)
            shutil.rmtree(journal, ignore_errors=True)
            return {'manifest': result, 'changed': True}

    except Exception as exc:
        if journal.exists():
            recover_source(root)
        if isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile,
                            json.JSONDecodeError, RuntimeError, EOFError, zlib.error)):
            raise UnsafeArchiveError(f'Corrupt delta archive: {exc}') from exc
        raise
    finally:
        Path(upload).unlink(missing_ok=True)
