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
