"""
Regression tests for the MCP usability audit (2026-09-16), found by
driving the live server as an agent would against the ERP index:

- a common module's natural short name resolved to a *different* module
  containing it as a substring (ОбщегоНазначения → МеждународныйУчетОбщегоНазначения);
- get_procedure_code / search_code could not read module files when the
  index was built by another process (Windows host paths inside the
  Linux container): stored file_path had backslashes, abs_path was D:\\...;
- procedure names were matched case-sensitively in get_procedure_code and
  get_call_tree, unlike every search tool;
- search_attributes / find_references never printed the attribute name,
  so two hits on one object were indistinguishable.

Uses execute_tool() directly, like test_mcp_tools.py, to stay on the
test's own thread (per-thread DB readers vs. Windows tempdir teardown).
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'

_OWN_MODULE = """\
Функция ЗначениеРеквизитаОбъекта(Ссылка, ИмяРеквизита) Экспорт
\tВозврат "общий";
КонецФункции
"""

_DECOY_MODULE = """\
Функция ЗначениеРеквизитаОбъекта(Ссылка, ИмяРеквизита) Экспорт
\tВозврат "международный";
КонецФункции
"""


def _pm_with_xml_project(tmpdir: str) -> ProjectManager:
    """Project 'p1' built the volume way: XML placed first, then registered.

    Adds two common modules where one name contains the other; the decoy
    sorts first alphabetically, so it is also the first row inserted.
    """
    pm = ProjectManager(data_dir=tmpdir)
    pm.create_project('p1', 'p1')
    xml = Path(tmpdir) / 'projects' / 'p1' / 'sources' / 'main' / 'xml'
    shutil.copytree(FIXTURES / 'xml_ru', xml)
    for name, text in (('МеждународныйУчетОбщегоНазначения', _DECOY_MODULE),
                       ('ОбщегоНазначения', _OWN_MODULE)):
        ext = xml / 'CommonModules' / name / 'Ext'
        ext.mkdir(parents=True)
        (ext / 'Module.bsl').write_text(text, encoding='utf-8-sig')
    pm.add_source('p1', 'main', 'Main')
    pm.reindex('p1')
    return pm


def _simulate_foreign_index(pm: ProjectManager):
    """Make the index look like it was built on Windows and is now served
    from elsewhere: backslash relative paths, absolute paths that don't exist."""
    db = pm.get_db('p1')
    db.conn.execute("UPDATE modules SET file_path = replace(file_path, '/', '\\'), "
                    "abs_path = 'D:\\nowhere\\' || file_path")
    db.conn.commit()


def test_module_short_name_prefers_exact_common_module():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            out = execute_tool(pm, 'get_module_outline', {'module_path': 'ОбщегоНазначения'})
            assert out.startswith('Module: ОбщийМодуль.ОбщегоНазначения.Модуль'), out
        finally:
            pm.close_all()


def test_get_procedure_code_short_module_name_and_any_case():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            out = execute_tool(pm, 'get_procedure_code', {
                'module_path': 'общегоназначения', 'procedure_name': 'значениереквизитаобъекта'})
            assert 'Возврат "общий"' in out, out
            assert 'международный' not in out
        finally:
            pm.close_all()


def test_code_tools_work_with_index_built_elsewhere():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            _simulate_foreign_index(pm)
            code = execute_tool(pm, 'get_procedure_code', {
                'module_path': 'ОбщийМодуль.ОбщегоНазначения.Модуль',
                'procedure_name': 'ЗначениеРеквизитаОбъекта'})
            assert 'Возврат "общий"' in code, code
            found = execute_tool(pm, 'search_code', {'query': 'Возврат "международный"'})
            assert 'МеждународныйУчетОбщегоНазначения' in found, found
        finally:
            pm.close_all()


def test_call_tree_procedure_name_case_insensitive_with_short_module():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            out = execute_tool(pm, 'get_call_tree', {
                'procedure_name': 'значениереквизитаобъекта',
                'module_name': 'ОбщегоНазначения', 'direction': 'up', 'depth': 1})
            assert 'ОбщийМодуль.ОбщегоНазначения.Модуль' in out, out
            assert 'МеждународныйУчет' not in out, out
        finally:
            pm.close_all()


def test_search_procedures_exact_name_first():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_project(tmpdir)
        try:
            db = pm.get_db('p1')
            db.conn.execute(
                "INSERT INTO procedures (module_id, name, kind, name_cf) "
                "SELECT id, 'ЗначениеРеквизитаОбъектаАААА', 'Функция', "
                "'значениереквизитаобъектааааа' FROM modules LIMIT 1")
            db.conn.commit()
            out = execute_tool(pm, 'search_procedures', {'query': 'ЗначениеРеквизитаОбъекта'})
            first_hit = out.splitlines()[1]
            assert first_hit.rstrip().split('(')[0].endswith('.ЗначениеРеквизитаОбъекта'), out
        finally:
            pm.close_all()


def test_attribute_results_include_attribute_name():
    report = FIXTURES / 'report_en.txt'
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        pm.create_project('p1', 'p1')
        with open(report, 'rb') as f:
            pm.add_source('p1', 'main', 'Main', report_file=f)
        pm.reindex('p1')
        try:
            db = pm.get_db('p1')
            row = db.conn.execute(
                "SELECT a.name, m.full_name FROM attributes a "
                "JOIN metadata_objects m ON m.id = a.object_id "
                "WHERE a.type_desc <> '' LIMIT 1").fetchone()
            assert row, "fixture should contain typed attributes"
            out = execute_tool(pm, 'search_attributes', {'query': row['name'], 'limit': 50})
            assert f"{row['full_name']}.{row['name']}" in out, out
        finally:
            pm.close_all()
