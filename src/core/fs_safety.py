"""Безопасное удаление файлов и каталогов в пределах разрешённого корня."""

import os
from pathlib import Path

from .fsretry import retry_on_permission_error, rmtree_with_retry


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, 'isjunction', None)
    return bool(checker and checker(path))


def is_link_like(path: Path) -> bool:
    """True для symlink и Windows junction (если поддерживается Python)."""
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
        if not recursive:
            return False
        rmtree_with_retry(path)
    else:
        retry_on_permission_error(lambda: path.unlink(missing_ok=True))
    return True
