"""
Шум ProactorEventLoop на Windows (запуск без Docker): клиент резко закрыл
соединение — в лог шёл ERROR «Exception in callback
_ProactorBasePipeTransport._call_connection_lost()» с трассировкой
ConnectionResetError [WinError 10054]. Фильтр понижает до DEBUG только его.
"""

import logging
import sys

import pytest

from src import main as main_module
from src.main import ProactorConnectionResetFilter

_MSG = 'Exception in callback _ProactorBasePipeTransport._call_connection_lost()'


def _record(msg: str, exc: BaseException | None, level=logging.ERROR) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc is not None else None
    return logging.LogRecord('asyncio', level, __file__, 1, msg, None, exc_info)


@pytest.fixture
def asyncio_logger():
    log = logging.getLogger('asyncio')
    saved_filters, saved_level = list(log.filters), log.level
    yield log
    log.filters[:] = saved_filters
    log.setLevel(saved_level)


def test_connection_reset_in_call_connection_lost_becomes_debug(asyncio_logger):
    asyncio_logger.setLevel(logging.INFO)
    record = _record(_MSG, ConnectionResetError(10054, 'Удалённый хост принудительно разорвал'))
    assert ProactorConnectionResetFilter().filter(record) is False      # при INFO не пишется
    assert record.levelno == logging.DEBUG and record.levelname == 'DEBUG'

    asyncio_logger.setLevel(logging.DEBUG)
    record = _record(_MSG, ConnectionResetError(10054, 'x'))
    assert ProactorConnectionResetFilter().filter(record) is True       # при DEBUG — виден
    assert record.levelno == logging.DEBUG


@pytest.mark.parametrize('msg, exc', [
    (_MSG, OSError('другая ошибка')),                                  # не ConnectionResetError
    (_MSG, None),
    ('Exception in callback Something.else()', ConnectionResetError(10054, 'x')),
    ('Task exception was never retrieved', RuntimeError('boom')),
])
def test_other_asyncio_errors_pass_unchanged(msg, exc):
    record = _record(msg, exc)
    assert ProactorConnectionResetFilter().filter(record) is True
    assert record.levelno == logging.ERROR


def test_filter_installed_only_on_windows(asyncio_logger):
    asyncio_logger.filters[:] = []
    main_module._quiet_proactor_connection_reset('linux')
    assert not asyncio_logger.filters
    main_module._quiet_proactor_connection_reset('win32')
    main_module._quiet_proactor_connection_reset('win32')   # повторно — не дублируется
    assert [type(f) for f in asyncio_logger.filters] == [ProactorConnectionResetFilter]


def test_filter_through_logger(asyncio_logger, caplog):
    """Сквозь настоящий логгер asyncio: шум не попадает в лог, другие
    ошибки попадают."""
    asyncio_logger.filters[:] = []
    asyncio_logger.setLevel(logging.NOTSET)
    main_module._quiet_proactor_connection_reset('win32')
    with caplog.at_level(logging.INFO):
        try:
            raise ConnectionResetError(10054, 'x')
        except ConnectionResetError:
            asyncio_logger.error(_MSG, exc_info=True)
        asyncio_logger.error('Exception in callback other()', exc_info=(RuntimeError, RuntimeError('y'), None))
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == ['Exception in callback other()']


@pytest.mark.skipif(sys.platform != 'win32', reason='ProactorEventLoop — только Windows')
def test_main_installs_filter_on_windows(monkeypatch, asyncio_logger):
    asyncio_logger.filters[:] = []
    main_module._quiet_proactor_connection_reset()
    assert any(isinstance(f, ProactorConnectionResetFilter) for f in asyncio_logger.filters)
