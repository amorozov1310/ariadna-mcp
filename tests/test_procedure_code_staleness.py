"""Код и границы процедуры обновляются одновременно при переиндексации."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'
PRICES = 'CommonModules/ЦеныСервер/Ext/Module.bsl'


def _pm(tmpdir: str) -> ProjectManager:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    pm = ProjectManager(tmpdir)
    pm.create_project('p1', 'p1')
    pm.add_source('p1', 'main', 'Main', xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def _code(pm, module, proc):
    return execute_tool(pm, 'get_procedure_code', {'project_id': 'p1', 'module_name': module,
                                                   'procedure_name': proc})


def test_procedure_code_uses_indexed_revision():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            path = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / PRICES
            before = _code(pm, 'ЦеныСервер', 'РассчитатьЦену')
            assert 'Функция РассчитатьЦену' in before and 'Возврат 100;' in before
            original = path.read_text(encoding='utf-8-sig')
            path.write_text('// добавлена строка\n' + original.replace('Возврат 100;', 'Возврат 120;'),
                            encoding='utf-8-sig')
            assert _code(pm, 'ЦеныСервер', 'РассчитатьЦену') == before

            pm.reindex('p1')
            after = _code(pm, 'ЦеныСервер', 'РассчитатьЦену')
            assert 'Функция РассчитатьЦену' in after and 'Возврат 120;' in after
            assert after.splitlines()[1] != before.splitlines()[1]
            assert '// добавлена строка' not in after
        finally:
            pm.close_all()
