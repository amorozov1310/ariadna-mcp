"""
Database.close() закрывает читающие соединения всех потоков.

read_conn держит по соединению на поток (threading.local), а close()
закрывал только соединение текущего потока. Соединения из потоков пула
SDK, Web UI или TestClient оставались открытыми: на Linux незаметно, а на
Windows index.db оставался занят — очистка TemporaryDirectory и rmtree в
delete_project падали с WinError 32.

На Linux открытый файл удаляется и так, поэтому тесты проверяют сами
соединения: после close() каждое закрыто (запрос через него — ошибка),
а файлы индекса удаляются. На Windows то же проверяет CI.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.db import Database
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool


class _Worker:
    """Поток, который живёт до stop() и выполняет в себе переданные функции."""

    def __init__(self):
        self._jobs: list = []
        self._cv = threading.Condition()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._jobs or self._stopped)
                if not self._jobs:
                    return
                fn, box, done = self._jobs.pop(0)
            try:
                box['result'] = fn()
            except Exception as e:
                box['error'] = e
            done.set()

    def call(self, fn):
        box, done = {}, threading.Event()
        with self._cv:
            self._jobs.append((fn, box, done))
            self._cv.notify()
        assert done.wait(10)
        if 'error' in box:
            raise box['error']
        return box['result']

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify()
        self._thread.join(10)


def _closed(conn: sqlite3.Connection) -> bool:
    """Закрыто ли само соединение sqlite3 (под обёрткой _ReaderConnection).
    Через обёртку закрытое соединение списанного Database отвечает
    IndexRemovedError, а не ProgrammingError."""
    raw = getattr(conn, '_conn', conn)
    try:
        raw.execute('SELECT 1')
    except sqlite3.ProgrammingError:
        return True
    return False


def _index_files(db_path: str) -> list[str]:
    return [p for p in (db_path, db_path + '-wal', db_path + '-shm') if os.path.exists(p)]


def test_close_closes_reader_of_another_thread_and_it_reopens():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'index.db')
        db = Database(db_path)
        db.connect()
        db.init_schema()
        worker = _Worker()
        try:
            other = worker.call(db.read_conn)
            assert worker.call(lambda: db.get_stats()['status']) is not None
            own = db.read_conn()
            assert own is not other

            db.close()
            assert _closed(other) and _closed(own)

            # Поток, чьё соединение закрыли извне, открывает новое, а не
            # падает с «Cannot operate on a closed database».
            reopened = worker.call(db.read_conn)
            assert reopened is not other and not _closed(reopened)
            assert worker.call(lambda: db.get_stats()) is not None

            db.close()
            assert _closed(reopened)
            for path in _index_files(db_path):
                os.remove(path)            # на Windows — WinError 32, если занят
            assert not _index_files(db_path)
        finally:
            worker.stop()
            db.close()


def _pm_with_project(tmpdir: str, index_dir: str) -> ProjectManager:
    import io
    import zipfile
    fixtures = Path(__file__).parent / 'fixtures'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (fixtures / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(fixtures / 'xml_ru'))
    buf.seek(0)
    pm = ProjectManager(tmpdir, index_dir=index_dir)
    pm.create_project('p1', 'p1')
    with open(fixtures / 'report_ru.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


@pytest.mark.parametrize('separate_index_dir', [False, True])
def test_delete_project_after_reads_from_several_threads(separate_index_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        index_dir = os.path.join(tmpdir, 'index') if separate_index_dir else None
        data_dir = os.path.join(tmpdir, 'data')
        pm = _pm_with_project(data_dir, index_dir)
        workers = [_Worker() for _ in range(3)]
        try:
            db = pm.get_db('p1')
            readers = [w.call(db.read_conn) for w in workers]
            for w in workers:
                assert 'Status: ready' in w.call(
                    lambda: execute_tool(pm, 'get_index_status', {'project_id': 'p1'}))
            index_parent = pm._index_path('p1').parent

            pm.delete_project('p1')
            assert all(_closed(c) for c in readers)
            assert not index_parent.exists()
            assert not (Path(data_dir) / 'projects' / 'p1').exists()
        finally:
            for w in workers:
                w.stop()
            pm.close_all()


def test_close_all_closes_readers_of_all_threads():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_project(os.path.join(tmpdir, 'data'), None)
        worker = _Worker()
        try:
            db_path = str(pm._index_path('p1'))
            reader = worker.call(lambda: pm.get_db('p1').read_conn())
            pm.close_all()
            assert _closed(reader)
            for path in _index_files(db_path):
                os.remove(path)
        finally:
            worker.stop()
            pm.close_all()


def test_close_while_other_threads_query_does_not_crash():
    """close() закрывает читающие соединения всех потоков. Раньше это был
    sqlite3.Connection.close() прямо посреди запроса другого потока — CPython
    падал с segfault (удаление проекта под нагрузкой MCP роняло сервер).
    Теперь close() ждёт окончания текущего запроса соединения."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'index.db'))
        db.connect()
        db.init_schema()
        db.conn.executemany("INSERT INTO sources (id, label) VALUES (?, ?)",
                            [(f's{i}', 'x' * 200) for i in range(2000)])
        db.conn.commit()
        stop = threading.Event()
        errors = []

        def read():
            while not stop.is_set():
                try:
                    # Долгий запрос: SQLite отпускает GIL на время шагов, и
                    # close() из основного потока попадает в его середину.
                    rows = db.read_conn().execute(
                        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                        "WHERE x < 3000) SELECT x, (SELECT label FROM sources LIMIT 1) "
                        "FROM c").fetchall()
                    assert len(rows) == 3000
                except sqlite3.ProgrammingError:
                    pass                    # закрыто — следующий read_conn откроет новое
                except Exception as e:      # pragma: no cover
                    errors.append(e)
                    return

        threads = [threading.Thread(target=read) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            for _ in range(30):
                db.close()
                time.sleep(0.001)
        finally:
            stop.set()
            for t in threads:
                t.join()
            db.close()
        assert not errors, errors
