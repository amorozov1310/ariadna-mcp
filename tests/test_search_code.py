"""
Regression tests for IMPROVEMENT_PLAN Этап 3 (D6 — real full-text code search).

search_code used to grep only calls.context (recognized call sites), so an
assignment, a query's SELECT text, or a comment was invisible even though
the string is right there in the source. It now searches the full text of
every indexed module via module_text_fts.

Acceptance criterion: a query for text inside a 1C query ("SELECT"), an
attribute name in an assignment, or comment text finds the module and line.
"""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import SearchEngine

FIXTURES = Path(__file__).parent / 'fixtures'


def _build_engine(tmpdir: str, xml_dir: Path, report: Path | None = None) -> SearchEngine:
    db = Database(os.path.join(tmpdir, 'index.db'))
    db.connect()
    db.init_schema()
    indexer = Indexer(db)
    if report:
        indexer.index_report(str(report), source_id='main')
    known_modules, known_objects = Indexer.scan_known_names([str(xml_dir)])
    indexer.index_bsl(str(xml_dir), source_id='main',
                       known_modules=known_modules, known_objects=known_objects)
    return SearchEngine(db)


def test_finds_comment_not_just_call():
    """Real acceptance-criterion example: OTelSettings appears in comments
    and an assignment in AgentDataUtils, never as a recognized call target
    named exactly 'OTelSettings' — the old calls.context grep found nothing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_en')
        results = engine.search_code('OTelSettings')
        assert results, "Expected matches for OTelSettings"
        assert any('AgentDataUtils' in r['module_name'] for r in results)
        # Comment lines must be reachable, not just the assignment line
        comment_hits = [r for r in results if '//' in r['context']]
        assert comment_hits, f"Expected at least one comment-line match, got contexts: {[r['context'] for r in results]}"
        engine.db.close()


def test_finds_assignment_line():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_en')
        results = engine.search_code('SessionParameters')
        assert results
        assert any('OTelSettings = SessionParameters' in r['context'] for r in results), \
            f"Expected the assignment line, got: {[r['context'] for r in results]}"
        engine.db.close()


def test_finds_sql_query_text_ru():
    """bshp-style example from the checklist: text inside a 1C query
    ("ВЫБРАТЬ ПЕРВЫЕ") is findable, matching tests/fixtures/xml_ru's real
    ВидыИспользованияРабочегоВремени/Ext/ObjectModule.bsl."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_ru')
        results = engine.search_code('ВЫБРАТЬ ПЕРВЫЕ')
        assert results, "Expected a match for query text 'ВЫБРАТЬ ПЕРВЫЕ'"
        assert any('ВидыИспользованияРабочегоВремени' in r['module_name'] for r in results)
        engine.db.close()


def test_line_number_and_context_are_correct():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_en')
        results = engine.search_code('SessionParameters')
        hit = next(r for r in results if 'OTelSettings = SessionParameters' in r['context'])
        # Cross-check against the real file directly
        real_file = FIXTURES / 'xml_en' / 'CommonModules' / 'AgentDataUtils' / 'Ext' / 'Module.bsl'
        lines = real_file.read_text(encoding='utf-8-sig').replace('\r\n', '\n').split('\n')
        assert 'OTelSettings = SessionParameters' in lines[hit['line'] - 1], \
            f"line {hit['line']} in real file is: {lines[hit['line'] - 1]!r}"
        # ±1 context includes the matched line plus a neighbor
        assert len(hit['context'].split('\n')) >= 2
        engine.db.close()


def test_case_insensitive_query():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_en')
        lower = engine.search_code('otelsettings')
        upper = engine.search_code('OTELSETTINGS')
        assert lower and upper
        assert len(lower) == len(upper)
        engine.db.close()


def test_source_id_and_file_pattern_filters():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_en')
        by_source = engine.search_code('OTelSettings', source_id='main')
        assert by_source
        assert all(r['source_id'] == 'main' for r in by_source)

        by_source_none = engine.search_code('OTelSettings', source_id='nonexistent')
        assert by_source_none == []

        by_file = engine.search_code('OTelSettings', file_pattern='AgentDataUtils')
        assert by_file
        assert all('AgentDataUtils' in r['file_path'] for r in by_file)
        engine.db.close()


def test_no_results_for_absent_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_engine(tmpdir, FIXTURES / 'xml_en')
        assert engine.search_code('ThisStringDefinitelyDoesNotExistAnywhere123') == []
        engine.db.close()


def test_module_text_fts_removed_on_source_clear():
    """_clear_source_data must also drop module_text_fts rows (no FK
    cascade on FTS5 virtual tables) so a removed source's code doesn't
    keep showing up in search_code."""
    from src.core.project_manager import ProjectManager
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('proj', 'Test')
        import shutil
        src_dir = Path(tmpdir) / 'projects' / 'proj' / 'sources' / 'main' / 'xml'
        shutil.copytree(FIXTURES / 'xml_en', src_dir)
        pm.add_source('proj', 'main', 'Main')
        pm.reindex('proj')

        engine = pm.get_search('proj')
        assert engine.search_code('OTelSettings')

        pm.remove_source('proj', 'main')
        db = pm.get_db('proj')
        remaining = db.conn.execute("SELECT COUNT(*) FROM module_text_fts").fetchone()[0]
        assert remaining == 0, f"Expected module_text_fts to be emptied, found {remaining} rows"
        pm.close_all()


if __name__ == '__main__':
    tests = [
        test_finds_comment_not_just_call,
        test_finds_assignment_line,
        test_finds_sql_query_text_ru,
        test_line_number_and_context_are_correct,
        test_case_insensitive_query,
        test_source_id_and_file_pattern_filters,
        test_no_results_for_absent_text,
        test_module_text_fts_removed_on_source_clear,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  ✅ {t.__name__}')
            passed += 1
        except Exception as e:
            import traceback
            print(f'  ❌ {t.__name__}: {e}')
            traceback.print_exc()
            failed += 1
    print(f'\n{passed} passed, {failed} failed')


# ============================================
# Пределы search_code: сработавший предел не выдаётся за полную выдачу
# ============================================

import io
import zipfile

from src.core import search as search_module
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool


def _pm_with_xml_en(tmpdir: str) -> ProjectManager:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_en').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_en'))
    buf.seek(0)
    pm = ProjectManager(data_dir=tmpdir)
    pm.create_project('p1', 'p1')
    with open(FIXTURES / 'report_en.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def test_per_module_limit_is_reported_not_shown_as_complete():
    """В CommonAtServer «EndProcedure» встречается больше 5 раз — предел
    вхождений на модуль срабатывает. Раньше последняя строка была
    «shown 5 of 5», хотя вхождений больше."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_en(tmpdir)
        try:
            text = execute_tool(pm, 'search_code', {'query': 'EndProcedure',
                                                    'file_pattern': 'CommonAtServer'})
            assert text.count('CommonAtServer') >= 1
            assert 'shown 5 of 5' not in text, text
            assert 'выдача ограничена' in text and 'file_pattern' in text, text
        finally:
            pm.close_all()


def test_module_limit_is_reported(monkeypatch):
    monkeypatch.setattr(search_module, 'SEARCH_CODE_FTS_MODULES', 1)
    monkeypatch.setattr(search_module, 'SEARCH_CODE_MAX_PER_MODULE', 1)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_en(tmpdir)
        try:
            text = execute_tool(pm, 'search_code', {'query': 'Procedure'})
            assert 'Found 1 match(es)' in text, text
            assert 'shown 1 of 1' not in text, text
            assert 'выдача ограничена' in text, text
        finally:
            pm.close_all()


def test_complete_result_still_says_shown_and_web_api_returns_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_en(tmpdir)
        try:
            text = execute_tool(pm, 'search_code', {'query': 'SessionParameters'})
            assert 'выдача ограничена' not in text, text
            assert text.splitlines()[-1].startswith('shown '), text
            # Web UI вызывает search_code и ждёт список — сигнатура та же.
            results = pm.get_search('p1').search_code('SessionParameters', limit=50)
            assert isinstance(results, list) and results
            assert execute_tool(pm, 'search_code', {'query': 'НетТакогоТекста123'}) == 'No code matches found.'
        finally:
            pm.close_all()


# ============================================
# Недоступные файлы выгрузки: не «ничего не найдено», а честная ошибка
# ============================================

import shutil


def _xml_dir(pm: ProjectManager) -> Path:
    return pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'


def test_unreadable_dump_is_an_error_not_no_matches():
    """Выгрузку убрали с диска после индексации (так на живом сервере с
    bshp) — раньше search_code отвечал «No code matches found»."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_en(tmpdir)
        try:
            shutil.move(str(_xml_dir(pm)), str(Path(tmpdir) / 'moved-away'))
            abs_paths = [r[0] for r in pm.get_db('p1').conn.execute(
                "SELECT abs_path FROM modules WHERE abs_path != ''")]
            assert abs_paths and not any(os.path.exists(p) for p in abs_paths), \
                "запасной путь abs_path тоже должен быть недоступен"

            text = execute_tool(pm, 'search_code', {'query': 'OTelSettings'})
            assert 'No code matches found' not in text, text
            assert text.startswith('Error'), text
            assert 'недоступны' in text and 'переиндексируйте' in text.lower(), text
        finally:
            pm.close_all()


def test_partially_unreadable_dump_marks_result_incomplete():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_en(tmpdir)
        try:
            (_xml_dir(pm) / 'CommonModules' / 'CommonAtServer' / 'Ext' / 'Module.bsl').unlink()
            text = execute_tool(pm, 'search_code', {'query': 'EndFunction'})
            assert not text.startswith('Error'), text
            assert 'AgentDataUtils' in text, text
            assert 'не прочитано модулей: 1' in text and 'выдача неполная' in text, text
            assert 'shown' not in text.splitlines()[-1] or ' of ' not in text.splitlines()[-1], \
                "«shown N of N» противоречит неполной выдаче"
        finally:
            pm.close_all()


def test_unreadable_and_limit_notes_agree(monkeypatch):
    monkeypatch.setattr(search_module, 'SEARCH_CODE_MAX_PER_MODULE', 1)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm_with_xml_en(tmpdir)
        try:
            (_xml_dir(pm) / 'CommonModules' / 'CommonAtServer' / 'Ext' / 'Module.bsl').unlink()
            text = execute_tool(pm, 'search_code', {'query': 'EndFunction'})
            last = text.splitlines()[-1]
            assert 'выдача ограничена' in last and 'не прочитано модулей: 1' in last, text
            assert ' of ' not in last, text
        finally:
            pm.close_all()
