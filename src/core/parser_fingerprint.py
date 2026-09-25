"""
Отпечаток парсера: хеш кода, от которого зависит результат разбора BSL
(процедуры и вызовы в таблицах procedures/calls).

Инкрементальная переиндексация пропускает файл выгрузки, если его хеш не
изменился, — но хеш файла ничего не знает о коде, который его разбирает.
Отпечаток хранится в индексе по каждому источнику (таблица parser_state), и
при его смене ProjectManager.reindex() переразбирает все файлы источника.

Что входит:
  - bsl_parser.py, bsl_reference.py — сам разбор и стоп-листы;
  - xml_walker.py — full_name модулей (ключ хранения), путь к Form.xml и
    read_form_attributes (реквизиты формы меняют разбор модуля формы);
  - из indexer.py — только исходник функций, формирующих сохранённый
    результат разбора (_INDEXER_FUNCTIONS). resolve_calls пересчитывается
    целиком при каждом reindex, index_report перезаписывает метаданные
    всегда — их правка полного перепарса не требует, а на ERP он стоит
    минут.
Путь к файлам в хеш не входит, окончания строк нормализуются — отпечаток
одинаков для одного кода в dev (Windows/Linux) и в Docker-образе.
"""

import functools
import hashlib
import inspect
import logging
from pathlib import Path

logger = logging.getLogger('ariadna')

_HERE = Path(__file__).parent
_SOURCE_FILES = ('bsl_parser.py', 'bsl_reference.py', 'xml_walker.py')
_INDEXER_FUNCTIONS = ('collect_known_facts', 'scan_known_names',
                      'scan_known_factory_functions', 'index_bsl')


def _normalize(text: str) -> str:
    return text.lstrip('﻿').replace('\r\n', '\n').replace('\r', '\n')


def compute_parser_fingerprint() -> str:
    """Без кэша. Пустая строка — исходников нет (например, только .pyc):
    тогда отпечаток не сравнивается и поведение прежнее."""
    from .indexer import Indexer
    h = hashlib.sha256()
    try:
        for name in _SOURCE_FILES:
            text = (_HERE / name).read_bytes().decode('utf-8-sig')
            h.update(f'\0{name}\0'.encode())
            h.update(_normalize(text).encode('utf-8'))
        for name in _INDEXER_FUNCTIONS:
            h.update(f'\0indexer.{name}\0'.encode())
            h.update(_normalize(inspect.getsource(getattr(Indexer, name))).encode('utf-8'))
    except (OSError, TypeError, UnicodeDecodeError):
        logger.warning("Не удалось вычислить отпечаток парсера — нет исходников; "
                       "смена версии парсера не будет вызывать полный перепарс")
        return ''
    return h.hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def parser_fingerprint() -> str:
    """Отпечаток текущего кода — считается один раз на процесс."""
    return compute_parser_fingerprint()
