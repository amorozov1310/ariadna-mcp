"""Безопасное удаление файлов и каталогов в пределах разрешённого корня."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .fsretry import retry_on_permission_error, rmtree_with_retry


@dataclass(frozen=True)
class RemovalResult:
    """Результат удаления; ошибка означает, что удаление не завершено."""

    removed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.removed


def is_link_like(path: Path) -> bool:
    """Распознать symlink и любой Windows reparse point, включая неизвестные теги."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, 'st_file_attributes', 0)
    return bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _lexically_within(path: Path, root: Path) -> bool:
    path_abs = Path(os.path.abspath(path))
    root_abs = Path(os.path.abspath(root))
    try:
        return path_abs != root_abs and path_abs.is_relative_to(root_abs)
    except ValueError:
        return False


def _resolved_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    try:
        resolved = Path(os.path.realpath(path))
        resolved_root = Path(os.path.realpath(root))
        return ((allow_equal or resolved != resolved_root) and
                resolved.is_relative_to(resolved_root))
    except (OSError, ValueError):
        return False


def _belongs_to_protected(path: Path, protected_roots) -> bool:
    try:
        resolved = Path(os.path.realpath(path))
        return any(resolved == Path(os.path.realpath(root)) or
                   resolved.is_relative_to(Path(os.path.realpath(root)))
                   for root in protected_roots)
    except (OSError, ValueError):
        return True


def directory_is_safe(directory: Path, allowed_root: Path | None = None, *,
                      protected_roots=()) -> bool:
    """Проверить, что каталог не является ссылкой и расположен в разрешённом проекте."""
    directory = Path(directory)
    if is_link_like(directory) or not directory.is_dir():
        return False
    if allowed_root is None:
        return not _belongs_to_protected(directory, protected_roots)
    allowed_root = Path(allowed_root)
    return (_lexically_within(directory, allowed_root) and
            _resolved_within(directory, allowed_root) and
            not _belongs_to_protected(directory, protected_roots))


def remove_path_safely(path: Path, allowed_root: Path, *, recursive: bool = True,
                       protected_roots=()) -> RemovalResult:
    """Удалить путь только под разрешённым корнем и вне защищённых проектов.

    У ссылки проверяется расположение самой ссылки и реального пути родителя;
    цель ссылки не обходится. Ошибка или частичная очистка возвращается как
    неуспешный структурированный результат. Проверка и удаление не атомарны:
    защита рассчитана на ссылки в дереве, а не на гонку с враждебным процессом.
    """
    path, allowed_root = Path(path), Path(allowed_root)
    if not _lexically_within(path, allowed_root):
        return RemovalResult(False, 'путь вне разрешённого корня')
    parent = path.parent
    if not _resolved_within(parent, allowed_root, allow_equal=True):
        return RemovalResult(False, 'родитель пути разрешается вне корня')
    if _belongs_to_protected(parent, protected_roots):
        return RemovalResult(False, 'родитель пути принадлежит другому проекту')

    linked = is_link_like(path)
    if linked:
        # Даже при удалении только самой ссылки запрещаем путь, ведущий в
        # соседний зарегистрированный проект.
        if _belongs_to_protected(path, protected_roots):
            return RemovalResult(False, 'цель ссылки принадлежит другому проекту')
        try:
            # Удаляется только сама ссылка/reparse point, никогда её цель.
            def remove_link():
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():  # junction/reparse point каталога
                    os.rmdir(path)
                else:
                    path.unlink()
            retry_on_permission_error(remove_link)
            return RemovalResult(True)
        except OSError as exc:
            return RemovalResult(False, str(exc))

    if not _resolved_within(path, allowed_root):
        if path.exists() or is_link_like(path):
            return RemovalResult(False, 'разрешённый путь находится вне корня')
    if _belongs_to_protected(path, protected_roots):
        return RemovalResult(False, 'путь принадлежит другому проекту')
    try:
        if not path.exists():
            return RemovalResult(True)
        if path.is_dir():
            if recursive:
                rmtree_with_retry(path)
            else:
                retry_on_permission_error(path.rmdir)
        else:
            retry_on_permission_error(lambda: path.unlink(missing_ok=True))
        return RemovalResult(True)
    except OSError as exc:
        return RemovalResult(False, str(exc))
