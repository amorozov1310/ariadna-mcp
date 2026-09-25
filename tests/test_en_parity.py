"""
Regression tests for IMPROVEMENT_PLAN Этап 2 (RU/EN parity + case-insensitive
search): async procedures (D8), missing directives (D10), EN metadata
managers (D11), kind aliases, and casefold search (D5).

Acceptance criteria (end of Этап 2):
  - all Этап 1 criteria hold on the EN fixture (covered in
    test_call_graph_resolution.py's test_d9_en_* — that file already runs
    the D3/D4/D9 checks against tests/fixtures/xml_en(_ext));
  - search_procedures('postdocument') finds PostDocument (case-insensitive);
  - search_metadata(kind='Catalog') is equivalent to kind='Справочник'.
"""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import SearchEngine
from src.core.bsl_parser import BSLParser
from src.core.report_parser import canonicalize_kind

FIXTURES = Path(__file__).parent / 'fixtures'


# ============================================
# D8: async procedures
# ============================================

def test_async_procedure_ru_and_en_detected():
    code = '''
Асинх Процедура ЗагрузитьДанные() Экспорт
	Ждать ВыполнитьАсинхронно();
КонецПроцедуры

Async Function GetData() Export
	Await DoSomethingAsync();
EndFunction

Процедура ОбычнаяПроцедура() Экспорт
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    by_name = {p.name: p for p in result.procedures}
    assert by_name['ЗагрузитьДанные'].is_async is True
    assert by_name['GetData'].is_async is True
    assert by_name['ОбычнаяПроцедура'].is_async is False


def test_await_does_not_block_call_extraction():
    """Ждать/Await before a call must not prevent it from being extracted
    (plan explicitly calls out checking the _RE_LOCAL_CALL lookbehind)."""
    code = '''
Асинх Процедура ЗагрузитьДанные() Экспорт
	Ждать ВыполнитьАсинхронно();
	Результат = Ждать ПолучитьРезультатАсинхронно(Парам1);
КонецПроцедуры

Async Function GetData() Export
	Await DoSomethingAsync();
EndFunction
'''
    result = BSLParser().parse(code)
    callees = {c.callee_proc for c in result.calls}
    assert 'ВыполнитьАсинхронно' in callees
    assert 'ПолучитьРезультатАсинхронно' in callees
    assert 'DoSomethingAsync' in callees
    # Ждать/Await themselves must not show up as calls
    assert 'Ждать' not in callees
    assert 'Await' not in callees


# ============================================
# D10: missing directives
# ============================================

def test_at_client_at_server_directives_recognized():
    code_ru = '''
&НаКлиентеНаСервере
Функция Ф1() Экспорт
КонецФункции

&НаКлиентеНаСервереБезКонтекста
Функция Ф2() Экспорт
КонецФункции
'''
    result = BSLParser().parse(code_ru)
    by_name = {p.name: p for p in result.procedures}
    assert by_name['Ф1'].directive == '&НаКлиентеНаСервере'
    assert by_name['Ф2'].directive == '&НаКлиентеНаСервереБезКонтекста'

    code_en = '''
&AtClientAtServer
Function F1() Export
EndFunction

&AtClientAtServerNoContext
Function F2() Export
EndFunction
'''
    result_en = BSLParser().parse(code_en)
    by_name_en = {p.name: p for p in result_en.procedures}
    assert by_name_en['F1'].directive == '&AtClientAtServer'
    assert by_name_en['F2'].directive == '&AtClientAtServerNoContext'


# ============================================
# D11: EN metadata managers
# ============================================

def test_en_metadata_manager_calls_resolve_kind():
    code = '''
Процедура Тест() Экспорт
	ChartsOfCharacteristicTypes.Kind1.DoSomething();
	ChartsOfAccounts.Acc1.DoSomething();
	ExchangePlans.Plan1.DoSomething();
	BusinessProcesses.Proc1.DoSomething();
	Tasks.Task1.DoSomething();
	CalculationRegisters.Reg1.DoSomething();
КонецПроцедуры
'''
    known_objects = {
        'ПланВидовХарактеристик': {'kind1'},
        'ПланСчетов': {'acc1'},
        'ПланОбмена': {'plan1'},
        'БизнесПроцесс': {'proc1'},
        'Задача': {'task1'},
        'РегистрРасчета': {'reg1'},
    }
    result = BSLParser(known_objects=known_objects).parse(code)
    modules = {c.callee_module for c in result.calls}
    assert 'ПланВидовХарактеристик.Kind1.МодульМенеджера' in modules
    assert 'ПланСчетов.Acc1.МодульМенеджера' in modules
    assert 'ПланОбмена.Plan1.МодульМенеджера' in modules
    assert 'БизнесПроцесс.Proc1.МодульМенеджера' in modules
    assert 'Задача.Task1.МодульМенеджера' in modules
    assert 'РегистрРасчета.Reg1.МодульМенеджера' in modules


# ============================================
# Kind aliases (EN -> RU canonical)
# ============================================

def test_canonicalize_kind_aliases():
    assert canonicalize_kind('Catalog') == 'Справочник'
    assert canonicalize_kind('catalog') == 'Справочник'
    assert canonicalize_kind('CATALOG') == 'Справочник'
    assert canonicalize_kind('справочник') == 'Справочник'
    assert canonicalize_kind('Справочник') == 'Справочник'
    assert canonicalize_kind('Document') == 'Документ'
    assert canonicalize_kind('CommonModule') == 'ОбщийМодуль'
    # Unrecognized values pass through unchanged rather than erroring
    assert canonicalize_kind('NotAKind') == 'NotAKind'


def _index_report_db(tmpdir: str, fixture: Path) -> SearchEngine:
    db = Database(os.path.join(tmpdir, 'index.db'))
    db.connect()
    db.init_schema()
    Indexer(db).index_report(str(fixture), source_id='main')
    return SearchEngine(db)


def test_search_metadata_kind_catalog_equals_spravochnik():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _index_report_db(tmpdir, FIXTURES / 'report_en.txt')
        by_en = engine.search_metadata('Account', kind='Catalog', limit=20)
        by_ru = engine.search_metadata('Account', kind='Справочник', limit=20)
        assert by_en, "kind='Catalog' should return results"
        names_en = sorted(r['full_name'] for r in by_en)
        names_ru = sorted(r['full_name'] for r in by_ru)
        assert names_en == names_ru, f"{names_en} != {names_ru}"
        engine.db.close()


def test_search_metadata_en_alias_case_insensitive_kind():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _index_report_db(tmpdir, FIXTURES / 'report_en.txt')
        results = engine.search_metadata('app', kind='Catalog', limit=20)
        assert results, "EN alias 'Catalog' (any case) should work"
        assert all(r['kind'] == 'Справочник' for r in results)
        engine.db.close()


def test_list_objects_kind_alias():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _index_report_db(tmpdir, FIXTURES / 'report_en.txt')
        by_en = engine.list_objects(kind='Catalog', limit=200)
        by_ru = engine.list_objects(kind='Справочник', limit=200)
        assert len(by_en) == len(by_ru) and len(by_en) > 0
        engine.db.close()


# ============================================
# D5: case-insensitive search (Cyrillic + EN)
# ============================================

def _build_project_db_with_bsl(tmpdir: str, xml_dir: Path, report: Path) -> SearchEngine:
    db = Database(os.path.join(tmpdir, 'index.db'))
    db.connect()
    db.init_schema()
    indexer = Indexer(db)
    indexer.index_report(str(report), source_id='main')
    known_modules, known_objects = Indexer.scan_known_names([str(xml_dir)])
    indexer.index_bsl(str(xml_dir), source_id='main',
                       known_modules=known_modules, known_objects=known_objects)
    return SearchEngine(db)


def test_search_procedures_lowercase_en_finds_mixed_case():
    """search_procedures(query='agentgetsettings') finds AgentGetSettings —
    a real exported function in tests/fixtures/xml_en/CommonModules/AgentDataUtils."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_project_db_with_bsl(tmpdir, FIXTURES / 'xml_en', FIXTURES / 'report_en.txt')
        results = engine.search_procedures('agentgetsettings')
        names = [r['name'] for r in results]
        assert 'AgentGetSettings' in names, f"Expected AgentGetSettings, got {names}"
        engine.db.close()


def test_search_procedures_postdocument_case_insensitive():
    """Literal acceptance line from IMPROVEMENT_PLAN Этап 2:
    search_procedures('postdocument') finds PostDocument."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'index.db'))
        db.connect()
        db.init_schema()
        conn = db.conn
        conn.execute("INSERT INTO sources (id, label, source_type) VALUES ('main','Main','main')")
        conn.execute("""INSERT INTO modules (source_id, name, module_type, name_cf)
                         VALUES ('main', 'DocumentServer', 'Модуль', ?)""",
                     ('documentserver',))
        module_id = conn.execute("SELECT id FROM modules").fetchone()['id']
        conn.execute("""INSERT INTO procedures (module_id, name, kind, is_export, name_cf)
                         VALUES (?, 'PostDocument', 'Процедура', 1, ?)""",
                     (module_id, 'postdocument'))
        conn.commit()
        engine = SearchEngine(db)
        results = engine.search_procedures('postdocument')
        names = [r['name'] for r in results]
        assert 'PostDocument' in names, f"Expected PostDocument, got {names}"
        engine.db.close()


def test_search_metadata_cyrillic_case_insensitive():
    """D5's original defect: search_procedures/search_metadata was
    case-sensitive on Cyrillic (SQLite LIKE only folds ASCII)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _index_report_db(tmpdir, FIXTURES / 'report_ru.txt')
        lower = engine.search_metadata('семена', limit=20)
        upper = engine.search_metadata('СЕМЕНА', limit=20)
        mixed = engine.search_metadata('Семена', limit=20)
        assert lower and upper and mixed
        assert {r['full_name'] for r in lower} == {r['full_name'] for r in upper} == \
               {r['full_name'] for r in mixed}
        engine.db.close()


def test_search_procedures_cyrillic_case_insensitive():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine = _build_project_db_with_bsl(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'report_ru.txt')
        lower = engine.search_procedures('присозданиинасервере')
        upper = engine.search_procedures('ПРИСОЗДАНИИНАСЕРВЕРЕ')
        assert lower and upper
        assert {r['name'] for r in lower} == {r['name'] for r in upper}
        engine.db.close()


if __name__ == '__main__':
    tests = [
        test_async_procedure_ru_and_en_detected,
        test_await_does_not_block_call_extraction,
        test_at_client_at_server_directives_recognized,
        test_en_metadata_manager_calls_resolve_kind,
        test_canonicalize_kind_aliases,
        test_search_metadata_kind_catalog_equals_spravochnik,
        test_search_metadata_en_alias_case_insensitive_kind,
        test_list_objects_kind_alias,
        test_search_procedures_lowercase_en_finds_mixed_case,
        test_search_procedures_postdocument_case_insensitive,
        test_search_metadata_cyrillic_case_insensitive,
        test_search_procedures_cyrillic_case_insensitive,
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
