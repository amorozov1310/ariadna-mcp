"""Манифест выгрузки и безопасная публикация только изменённых файлов."""

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
    """Откатывает незавершённую замену или удаляет старую резервную копию."""
    root = Path(root)
    backup = root.with_name(root.name + '.previous')
    if backup.exists():
        if root.exists():
            # Новый каталог уже опубликован целиком; осталось дочистить старый.
            shutil.rmtree(backup, ignore_errors=True)
        else:
            replace_with_retry(backup, root)
    for stage in root.parent.glob(f'.{root.name}-delta-stage-*'):
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def read_or_create_manifest(root: Path, sidecar: Path) -> dict:
    """Читает кэш из выгрузки; legacy-выгрузка сканируется и кэшируется разово."""
    root, sidecar = Path(root), Path(sidecar)
    recover_source(root)
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


def _replace_directory(stage: Path, root: Path) -> None:
    backup = root.with_name(root.name + '.previous')
    if backup.exists():
        rmtree_with_retry(backup)
    moved_old = False
    if root.exists():
        replace_with_retry(root, backup)
        moved_old = True
    try:
        replace_with_retry(stage, root)
    except Exception:
        if moved_old and backup.exists() and not root.exists():
            replace_with_retry(backup, root)
        raise
    # Новая выгрузка и её манифест уже опубликованы одним переименованием.
    # Если cleanup временно не удастся, recover_source дочистит backup позже.
    shutil.rmtree(backup, ignore_errors=True)


def apply_delta(root: Path, sidecar: Path, archive, filename: str,
                expected_version: str) -> dict:
    """Проверяет ZIP и атомарно публикует stage с hardlink-копиями неизменённых файлов."""
    root, sidecar = Path(root), Path(sidecar)
    root.parent.mkdir(parents=True, exist_ok=True)
    recover_source(root)
    fd, upload = tempfile.mkstemp(prefix='.delta-upload-', dir=root.parent)
    stage = None
    try:
        # Ограничение проверяется на принимаемом потоке до какой-либо обработки.
        size = 0
        with os.fdopen(fd, 'wb') as out:
            while chunk := archive.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UnsafeArchiveError(f'Разница больше лимита {MAX_UPLOAD_BYTES} байт')
                out.write(chunk)

        with zipfile.ZipFile(upload) as zf:
            budget = _Budget()
            entries: dict[str, zipfile.ZipInfo] = {}
            seen_paths = set()
            for info in zf.infolist():
                raw_name = info.filename[:-1] if info.is_dir() and info.filename.endswith('/') else info.filename
                _check_name(raw_name)
                if len(raw_name.encode('utf-8')) > MAX_PATH_BYTES:
                    raise UnsafeArchiveError(f'Слишком длинное имя в архиве: {info.filename!r}')
                if info.flag_bits & 0x1:
                    raise UnsafeArchiveError(f'Зашифрованный архив не поддерживается: {info.filename!r}')
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or (file_type and file_type != stat.S_IFREG and not info.is_dir()):
                    raise UnsafeArchiveError(f'Недопустимый тип файла: {info.filename!r}')
                rel = _safe_relpath(raw_name)
                key = rel.as_posix()
                if key in seen_paths:
                    raise UnsafeArchiveError(f'Повторяющийся путь в архиве: {info.filename!r}')
                seen_paths.add(key)
                if info.is_dir():
                    if info.file_size:
                        raise UnsafeArchiveError(f'Запись-каталог содержит данные: {info.filename!r}')
                    budget.add(0)  # каталоги тоже входят в лимит записей
                    continue
                if rel.as_posix() == DELTA_META and info.file_size > MAX_DELTA_META_BYTES:
                    raise UnsafeArchiveError(f'Манифест разницы больше {MAX_DELTA_META_BYTES} байт')
                entries[key] = info
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
            if len(target_files) > budget.max_files or len(deleted) > budget.max_files:
                raise UnsafeArchiveError(f'Манифест превышает лимит {budget.max_files} файлов')
            folded_names = set()
            for name, value in target_files.items():
                _safe_relpath(name)
                if name in (DELTA_META, MANIFEST_NAME):
                    raise UnsafeArchiveError(f'Путь зарезервирован: {name!r}')
                folded = os.path.normcase(name)
                if folded in folded_names:
                    raise UnsafeArchiveError(f'Имена конфликтуют на файловой системе: {name!r}')
                folded_names.add(folded)
                if (not isinstance(value, dict) or not isinstance(value.get('sha256'), str)
                        or len(value['sha256']) != 64
                        or any(c not in '0123456789abcdef' for c in value['sha256'])
                        or not isinstance(value.get('size'), int) or value['size'] < 0):
                    raise UnsafeArchiveError(f'Некорректная запись манифеста: {name!r}')
            if any(not isinstance(name, str) for name in deleted) or len(set(deleted)) != len(deleted):
                raise UnsafeArchiveError('Некорректный или повторяющийся список удалений')
            for name in deleted:
                _safe_relpath(name)
            # Нельзя иметь одновременно файл и его подкаталог в целевом дереве.
            for name in target_files:
                parts = name.split('/')
                if any('/'.join(parts[:i]) in target_files for i in range(1, len(parts))):
                    raise UnsafeArchiveError(f'Целевой путь одновременно файл и каталог: {name!r}')
            if manifest_version(target_files) != meta.get('target_version'):
                raise UnsafeArchiveError('Хеш целевого манифеста не совпадает')

            changed = set(entries)
            expected_changed = {p for p, value in target_files.items()
                                if current['files'].get(p) != value}
            if (changed != expected_changed
                    or set(deleted) != set(current['files']) - set(target_files)):
                raise UnsafeArchiveError('Список файлов разницы не совпадает с манифестом')
            if (not changed and not deleted
                    and meta['target_version'] == current['version']):
                return {'manifest': current, 'changed': False}

            _check_tree(root)
            stage = Path(tempfile.mkdtemp(prefix=f'.{root.name}-delta-stage-', dir=root.parent))
            # Сначала подготовить целевое дерево без удаляемых имён. На Windows
            # это также корректно обрабатывает переименование только по регистру.
            for name, expected in target_files.items():
                rel = _safe_relpath(name)
                dest = stage.joinpath(*rel.parts)
                if name in entries:
                    _write_changed_file(zf, entries[name], dest, expected)
                    continue
                source = root.joinpath(*rel.parts)
                _assert_inside(root, source)
                if _is_reparse(source) or not source.is_file():
                    raise UnsafeArchiveError(f'Неизменённый файл отсутствует или является ссылкой: {name}')
                if source.stat().st_size != expected['size']:
                    raise UnsafeArchiveError(f'Размер неизменённого файла не совпадает: {name}')
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, dest)
                except OSError as e:
                    raise UnsafeArchiveError(
                        f'Не удалось создать hardlink неизменённого файла {name!r}; '
                        'нужна файловая система с поддержкой hardlink') from e

            # Проверить наличие и размер каждого целевого файла до публикации.
            # Хеши изменённых файлов уже проверены при распаковке; неизменённые
            # связаны с проверенным прежним состоянием hardlink-ом.
            for name, expected in target_files.items():
                path = stage.joinpath(*_safe_relpath(name).parts)
                if not path.is_file() or _is_reparse(path) or path.stat().st_size != expected['size']:
                    raise UnsafeArchiveError(f'Целевой файл не прошёл проверку: {name}')
            result = {'version': meta['target_version'], 'files': target_files}
            write_manifest(stage / MANIFEST_NAME, result)
            _replace_directory(stage, root)
            stage = None
            return {'manifest': result, 'changed': True}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, json.JSONDecodeError,
            RuntimeError, EOFError, zlib.error) as e:
        raise UnsafeArchiveError(f'Архив разницы повреждён: {e}') from e
    finally:
        Path(upload).unlink(missing_ok=True)
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
