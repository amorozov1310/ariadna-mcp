"""
Распаковка загруженной XML-выгрузки (zip, tar.gz) в каталог источника.

Архив приходит от клиента Web UI, поэтому до записи первого файла
проверяется целиком:
  - пути только относительные и внутри каталога назначения (без «..»,
    абсолютных путей и букв дисков);
  - только обычные файлы и каталоги — символические и жёсткие ссылки,
    устройства отклоняются;
  - число файлов и суммарный распакованный размер ограничены (защита от
    «zip-бомб»): ARIADNA_ARCHIVE_MAX_FILES, ARIADNA_ARCHIVE_MAX_BYTES.
Распаковка идёт во временный каталог рядом с назначением и подменяет его
только целиком: при ошибке прежняя выгрузка источника остаётся на месте.
"""

import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

# Выгрузка крупной типовой конфигурации — порядка 100 тыс. файлов и 10 ГБ;
# умолчания с большим запасом, но конечные.
DEFAULT_MAX_FILES = 1_000_000
DEFAULT_MAX_BYTES = 64 * 1024 ** 3


class UnsafeArchiveError(ValueError):
    """Архив отклонён: опасный путь или тип записи, превышены пределы."""


def _limit(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ''))
    except ValueError:
        return default
    return value if value > 0 else default


def _check_name(name: str) -> None:
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    if (normalized.startswith('/') or (len(normalized) > 1 and normalized[1] == ':')
            or '..' in path.parts):
        raise UnsafeArchiveError(f'Архив отклонён: путь вне каталога выгрузки ({name!r})')


class _Budget:
    def __init__(self):
        self.max_files = _limit('ARIADNA_ARCHIVE_MAX_FILES', DEFAULT_MAX_FILES)
        self.max_bytes = _limit('ARIADNA_ARCHIVE_MAX_BYTES', DEFAULT_MAX_BYTES)
        self.files = 0
        self.bytes = 0

    def add(self, size: int) -> None:
        self.files += 1
        self.bytes += size
        if self.files > self.max_files:
            raise UnsafeArchiveError(
                f'Архив отклонён: больше {self.max_files} файлов (ARIADNA_ARCHIVE_MAX_FILES)')
        if self.bytes > self.max_bytes:
            raise UnsafeArchiveError(
                f'Архив отклонён: распакованный размер больше {self.max_bytes} байт '
                '(ARIADNA_ARCHIVE_MAX_BYTES)')


def _check_zip(zf: zipfile.ZipFile) -> None:
    budget = _Budget()
    for info in zf.infolist():
        _check_name(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise UnsafeArchiveError(f'Архив отклонён: символическая ссылка {info.filename!r}')
        if not info.is_dir():
            # Больше file_size zipfile не распакует: чтение ограничено им.
            budget.add(info.file_size)


def _check_tar(tf: tarfile.TarFile) -> None:
    budget = _Budget()
    for member in tf.getmembers():
        _check_name(member.name)
        if member.isdir():
            continue
        if not member.isfile():
            raise UnsafeArchiveError(
                f'Архив отклонён: {member.name!r} — не обычный файл (ссылка или устройство)')
        budget.add(member.size)


def _spool(fileobj: BinaryIO, directory: Path) -> Path:
    fd, tmp = tempfile.mkstemp(prefix='.upload-', dir=directory)
    with os.fdopen(fd, 'wb') as out:
        while chunk := fileobj.read(1024 * 1024):
            out.write(chunk)
    return Path(tmp)


def extract_archive(fileobj: BinaryIO, filename: str, dest: Path) -> None:
    """Проверить и распаковать архив в dest, заменив его содержимое целиком.

    Формат — по расширению (.zip, .tar.gz/.tgz); иначе пробуется zip, затем tar.
    UnsafeArchiveError (подкласс ValueError) — архив отклонён, dest не тронут.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    spooled = _spool(fileobj, dest.parent)
    staging = Path(tempfile.mkdtemp(prefix=f'.{dest.name}-', dir=dest.parent))
    try:
        lower = filename.lower()
        if lower.endswith('.zip') or (not lower.endswith(('.tar.gz', '.tgz'))
                                      and zipfile.is_zipfile(spooled)):
            with zipfile.ZipFile(spooled) as zf:
                _check_zip(zf)
                zf.extractall(staging)
        else:
            try:
                tf = tarfile.open(spooled, 'r:*')
            except tarfile.TarError as e:
                raise UnsafeArchiveError(f'Архив не распознан как zip или tar.gz: {e}') from e
            with tf:
                _check_tar(tf)
                if hasattr(tarfile, 'data_filter'):
                    tf.extractall(staging, filter='data')
                else:  # Python без фильтров: записи уже проверены выше
                    tf.extractall(staging)
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
    except zipfile.BadZipFile as e:
        raise UnsafeArchiveError(f'Архив повреждён: {e}') from e
    finally:
        spooled.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
