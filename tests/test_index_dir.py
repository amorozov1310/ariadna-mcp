"""
Индексы SQLite в отдельном каталоге INDEX_DIR (том Docker вне 9p).

В Docker Desktop на Windows ./data монтируется через 9p: полный проход по
procedures в index.db перечитывал файл при каждом вызове (3,5–4,5 с против
0,1 с на именованном томе), а WAL и блокировки SQLite через 9p не работают.
ProjectManager(data_dir, index_dir) кладёт индекс в {index_dir}/{id}/index.db;
без index_dir всё как раньше — {data_dir}/projects/{id}/index.db.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


def _index_project(pm: ProjectManager, project_id: str = 'p1') -> None:
    pm.create_project(project_id, project_id)
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source(project_id, 'main', 'Main', report_file=f,
                      xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
    pm.reindex(project_id)


def _procedures(pm: ProjectManager, query: str = 'Рассчитать') -> str:
    return execute_tool(pm, 'search_procedures', {'project_id': 'p1', 'query': query})


def test_index_lives_in_index_dir_and_is_deleted_with_project():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as index_dir:
        pm = ProjectManager(data_dir, index_dir=index_dir)
        try:
            _index_project(pm)
            assert 'РассчитатьЦену' in _procedures(pm)
            assert (Path(index_dir) / 'p1' / 'index.db').is_file()
            assert not (Path(data_dir) / 'projects' / 'p1' / 'index.db').exists()
            # Выгрузки и реестр остаются в data_dir.
            assert (Path(data_dir) / 'projects' / 'p1' / 'sources' / 'main' / 'xml').is_dir()
            assert (Path(data_dir) / 'projects.json').is_file()

            pm.delete_project('p1')
            assert not (Path(data_dir) / 'projects' / 'p1').exists()
            assert not (Path(index_dir) / 'p1').exists()
        finally:
            pm.close_all()


def test_without_index_dir_index_stays_in_data_dir():
    with tempfile.TemporaryDirectory() as data_dir:
        pm = ProjectManager(data_dir)
        try:
            _index_project(pm)
            assert 'РассчитатьЦену' in _procedures(pm)
            assert (Path(data_dir) / 'projects' / 'p1' / 'index.db').is_file()
        finally:
            pm.close_all()


# ---------------------------------------------------------------------------
# Однократный перенос индексов из data_dir в index_dir
# ---------------------------------------------------------------------------

from src.core import project_manager as pm_module


def test_migration_copies_index_including_wal_and_keeps_old_file():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as index_dir:
        old_pm = ProjectManager(data_dir)
        new_pm = ProjectManager(data_dir, index_dir=index_dir)
        try:
            _index_project(old_pm)
            # Запись, которая пока лежит только в -wal: соединение не закрыто,
            # контрольной точки не было. Копия файла её бы потеряла.
            conn = old_pm.get_db('p1').conn
            conn.execute("INSERT INTO procedures (module_id, name, kind, name_cf) "
                         "SELECT id, 'ТолькоВЖурнале', 'Процедура', 'тольковжурнале' "
                         "FROM modules LIMIT 1")
            conn.commit()
            old_file = Path(data_dir) / 'projects' / 'p1' / 'index.db'
            assert Path(str(old_file) + '-wal').exists()
            before = _procedures(old_pm)

            assert new_pm.migrate_index_dir() == ['p1']
            new_file = Path(index_dir) / 'p1' / 'index.db'
            assert new_file.is_file() and old_file.is_file()
            assert _procedures(new_pm) == before
            assert 'ТолькоВЖурнале' in _procedures(new_pm, 'ТолькоВЖурнале')

            mtime = new_file.stat().st_mtime_ns
            assert new_pm.migrate_index_dir() == []          # повторно — ничего
            assert new_file.stat().st_mtime_ns == mtime
        finally:
            old_pm.close_all()
            new_pm.close_all()


def test_migration_skipped_without_index_dir_or_old_index():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as index_dir:
        pm = ProjectManager(data_dir)
        try:
            pm.create_project('p1', 'p1')
            assert pm.migrate_index_dir() == []               # index_dir не задан
        finally:
            pm.close_all()
        pm = ProjectManager(data_dir, index_dir=index_dir)
        try:
            assert pm.migrate_index_dir() == []               # старого индекса нет
            assert not (Path(index_dir) / 'p1').exists()
        finally:
            pm.close_all()


def test_failed_copy_leaves_no_index_and_does_not_raise(monkeypatch):
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as index_dir:
        old_pm = ProjectManager(data_dir)
        try:
            _index_project(old_pm)
        finally:
            old_pm.close_all()

        def broken_copy(src, dst):
            Path(dst).write_bytes(b'SQLite format 3\0 half')   # оборванная копия
            raise OSError('диск кончился')

        monkeypatch.setattr(pm_module, '_copy_sqlite', broken_copy)
        pm = ProjectManager(data_dir, index_dir=index_dir)
        try:
            assert pm.migrate_index_dir() == []
            assert list((Path(index_dir) / 'p1').iterdir()) == [], \
                "ни index.db, ни временного файла"
            # Проект просто переиндексируется на новом месте.
            pm.reindex('p1')
            assert 'РассчитатьЦену' in _procedures(pm)
        finally:
            pm.close_all()


def test_startup_migrates_before_resetting_stale_indexing(monkeypatch):
    """main._prepare_data: перенос раньше reset_stale_indexing — тот открывает
    БД через get_db и на новом месте создал бы пустой индекс, после чего
    перенос решил бы, что индекс уже есть."""
    from src import main as main_module
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as index_dir:
        old_pm = ProjectManager(data_dir)
        try:
            _index_project(old_pm)
            old_pm.update_project('p1', status='indexing')     # прогон «оборвался»
        finally:
            old_pm.close_all()

        monkeypatch.setenv('INDEX_DIR', index_dir)
        main_module._prepare_data(data_dir)

        pm = ProjectManager(data_dir, index_dir=index_dir)
        try:
            assert pm.get_project('p1').status == 'ready'
            assert 'РассчитатьЦену' in _procedures(pm)
        finally:
            pm.close_all()


def test_docker_compose_puts_indexes_on_named_volume():
    """Код читает INDEX_DIR, compose должен задать его и смонтировать туда
    именованный том с явным именем (на него ссылается документация)."""
    root = Path(__file__).parent.parent
    compose = (root / 'docker-compose.yml').read_text(encoding='utf-8')
    assert '- INDEX_DIR=/index' in compose
    assert '- ariadna-index:/index' in compose
    assert '- ./data:/data' in compose
    assert 'name: ariadna-index' in compose
    assert '/index' in (root / 'Dockerfile').read_text(encoding='utf-8')
