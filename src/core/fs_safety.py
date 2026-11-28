"""Безопасное удаление файлов и каталогов в пределах разрешённого корня."""

import os
import stat
from pathlib import Path

from .fsretry import retry_on_permission_error, rmtree_with_retry


def _is_junction(path: Path) -> bool:
    """Определить junction/reparse point одинаково в Python 3.11 и новее."""
    try:
        info = os.lstat(path)
        attrs = info.st_file_attributes
    except (AttributeError, OSError):
        return False
    if not attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400) or not stat.S_ISDIR(info.st_mode):
        return False
    # Directory symlinks are reparse points too. Only mount-point tags are
    # junctions and require rmdir; without the tag, retain the 3.11-compatible
    # directory-reparse fallback.
    tag = getattr(info, 'st_reparse_tag', None)
    mount_point = getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)
    return tag == mount_point if tag is not None else True


def is_link_like(path: Path) -> bool:
    """True для symlink, junction и других reparse points, включая Python 3.11."""
    return path.is_symlink() or _is_junction(path)


def _lexically_within(path: Path, root: Path) -> bool:
    """Проверить расположение по строковому пути, не проходя по ссылкам."""
    path_abs = Path(os.path.abspath(path))
    root_abs = Path(os.path.abspath(root))
    try:
        return path_abs != root_abs and path_abs.is_relative_to(root_abs)
    except ValueError:
        return False


def directory_is_safe(directory: Path, allowed_root: Path | None = None) -> bool:
    """Проверить, что каталог для обхода не является ссылкой и лежит в корне."""
    directory = Path(directory)
    if is_link_like(directory) or not directory.is_dir():
        return False
    if allowed_root is None:
        return True
    allowed_root = Path(allowed_root)
    if not _lexically_within(directory, allowed_root):
        return False
    try:
        return Path(os.path.realpath(directory)).is_relative_to(Path(os.path.realpath(allowed_root)))
    except (OSError, ValueError):
        return False


def remove_path_safely(path: Path, allowed_root: Path, *, recursive: bool = True) -> bool:
    """Удалить объект внутри корня; внешнюю ссылку удалить только как ссылку.

    Возвращает False, если путь отсутствует или его расположение небезопасно.

    Проверка пути и последующее удаление не атомарны. Процесс с правом менять
    каталоги может подменить компонент между ними (TOCTOU); защита рассчитана
    на недоверенные ссылки в дереве, а не на гонку с враждебным процессом.
    """
    path, allowed_root = Path(path), Path(allowed_root)
    if not _lexically_within(path, allowed_root):
        return False
    if is_link_like(path):
        # unlink удаляет symlink, а rmdir — junction; ни один не обходит target.
        def remove_link():
            try:
                if _is_junction(path):
                    os.rmdir(path)
                else:
                    path.unlink()
            except FileNotFoundError:
                pass
        retry_on_permission_error(remove_link)
        return True
    try:
        resolved = Path(os.path.realpath(path))
        resolved_root = Path(os.path.realpath(allowed_root))
        if not resolved.is_relative_to(resolved_root):
            return False
    except (OSError, ValueError):
        return False
    if not path.exists():
        return True
    if path.is_dir():
        if recursive:
            rmtree_with_retry(path)
        else:
            retry_on_permission_error(lambda: path.rmdir())
    else:
        retry_on_permission_error(lambda: path.unlink(missing_ok=True))
    return True
