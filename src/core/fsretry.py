"""
Повторы файловых операций при PermissionError (Windows).

На Windows только что записанный файл ненадолго открывает другой процесс —
антивирус (Defender проверяет каждый новый файл), индексатор поиска,
резервное копирование. Пока он держит файл без FILE_SHARE_DELETE, rename,
os.replace и rmtree каталога с этим файлом падают с «[WinError 5] Отказано
в доступе» или WinError 32. На загруженной машине проверка идёт дольше:
распаковка выгрузки в add_source (переименование каталога staging → xml)
падала примерно в каждом третьем прогоне быстрых тестов. Это не ошибка
данных, а занятость на доли секунды — поэтому несколько коротких повторов.
На Linux PermissionError — настоящий запрет доступа, повторы лишь чуть
задержат ту же ошибку.
"""

import os
import shutil
import time
from typing import Callable, TypeVar

T = TypeVar('T')

# 10 попыток с паузой 40, 80, … мс — в сумме до ~1,8 с.
RETRY_ATTEMPTS = 10
RETRY_DELAY_SEC = 0.04


def retry_on_permission_error(fn: Callable[[], T]) -> T:
    """Выполнить fn, повторяя при PermissionError; последняя ошибка —
    наружу."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn()
        except PermissionError:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_DELAY_SEC * (attempt + 1))
    raise AssertionError("unreachable")   # pragma: no cover


def replace_with_retry(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    """os.replace (и переименование каталога) с повторами."""
    retry_on_permission_error(lambda: os.replace(src, dst))


def rmtree_with_retry(path: str | os.PathLike) -> None:
    """shutil.rmtree с повторами; каталога уже нет — не ошибка."""
    def remove():
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass                # удалил кто-то ещё
    retry_on_permission_error(remove)

