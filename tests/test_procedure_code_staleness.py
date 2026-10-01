"""
get_procedure_code после изменения файла модуля без переиндексации.

Инструмент режет текущий файл по номерам строк из индекса. Если выгрузку
обновили («обновить источник» в Web UI), а reindex не запускали, под
заголовком процедуры оказывались чужие строки. Теперь md5 файла сверяется
с modules.file_hash, и при расхождении первой строкой идёт предупреждение.
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
PRICES = 'CommonModules/ЦеныСервер/Ext/Module.bsl'
FORM = 'Catalogs/ВидыИспользованияРабочегоВремени/Forms/ФормаЭлемента/Ext/Form/Module.bsl'
WARNING = 'файл модуля изменён после индексации'


def _pm(tmpdir: str) -> ProjectManager:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    pm = ProjectManager(tmpdir)
    pm.create_project('p1', 'p1')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def _code(pm, module, proc):
    return execute_tool(pm, 'get_procedure_code', {'project_id': 'p1', 'module_name': module,
                                                   'procedure_name': proc})


def test_unchanged_file_has_no_warning():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            text = _code(pm, 'ЦеныСервер', 'РассчитатьЦену')
            assert 'Функция РассчитатьЦену' in text and WARNING not in text, text
            # Модуль формы: хеш индексатора включает Form.xml — без изменений
            # предупреждения тоже нет.
            text = _code(pm, 'Справочник.ВидыИспользованияРабочегоВремени.Форма.ФормаЭлемента',
                         'ПослеЗаписи')
            assert 'Процедура ПослеЗаписи' in text and WARNING not in text, text
        finally:
            pm.close_all()


def test_file_changed_after_indexing_is_flagged():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            path = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / PRICES
            path.write_bytes('// новая строка 1\n// новая строка 2\n'.encode('utf-8') + path.read_bytes())

            text = _code(pm, 'ЦеныСервер', 'РассчитатьЦену')
            first = text.splitlines()[0]
            assert WARNING in first and 'reindex' in first, text
            # Код всё равно отдаётся — по старым границам.
            assert '// Lines' in text

            # После reindex границы снова верные, предупреждения нет.
            pm.reindex('p1')
            text = _code(pm, 'ЦеныСервер', 'РассчитатьЦену')
            assert WARNING not in text and 'Функция РассчитатьЦену' in text, text
        finally:
            pm.close_all()


def test_empty_file_hash_means_no_warning():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            db = pm.get_db('p1')
            db.conn.execute("UPDATE modules SET file_hash = '' WHERE name LIKE '%.ЦеныСервер.%'")
            db.conn.commit()
            path = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / PRICES
            path.write_bytes(b'// x\n' + path.read_bytes())
            assert WARNING not in _code(pm, 'ЦеныСервер', 'РассчитатьЦену')
        finally:
            pm.close_all()
