"""
Regression tests for IMPROVEMENT_PLAN Этап 1 (call graph correctness).

Each test maps directly to one of the five acceptance criteria listed at
the end of "Этап 1. Корректность графа вызовов (D1-D4, D9)":

  1. ДокументооборотИнтеграция.X() (a module name with a "banned" prefix
     under the old _VARIABLE_PREFIXES heuristic) is present in the graph — D1
  2. a local call to ПроверитьЗаполнение() is kept when the module declares
     its own procedure of that name, even though the name is also in the
     platform stoplist — D2
  3. the up-tree of one procedure does not pick up callers of an unrelated,
     same-named procedure in a different module — D3
  4. the down-tree of a qualified call resolves to its own module, not a
     same-named procedure elsewhere — D4
  5. &Вместо/&Around does not create a bogus "Вместо()"/"Around()" call
     node, and the interceptor shows up in the up-tree of the procedure
     it intercepts — D9

RU fixtures live in tests/fixtures/xml_ru(_ext), EN in xml_en(_ext) — see
IMPROVEMENT_PLAN Этап 0.3 for how they were assembled (real bshp/1idm2
code plus small clearly-marked synthetic additions where no real example
of a given pattern existed in either corpus).
"""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.call_analyzer import CallAnalyzer

FIXTURES = Path(__file__).parent / 'fixtures'


def _build_project_db(tmpdir: str, main_xml: Path, ext_xml: Path,
                       main_label: str = 'Main', ext_label: str = 'Ext') -> Database:
    """Two-pass index a (main, extension) source pair into a fresh DB,
    mirroring what ProjectManager.reindex() does for a real project."""
    db = Database(os.path.join(tmpdir, 'index.db'))
    db.connect()
    db.init_schema()
    indexer = Indexer(db)

    known_modules, known_objects = Indexer.scan_known_names([str(main_xml), str(ext_xml)])

    indexer.index_bsl(str(main_xml), source_id='main',
                       known_modules=known_modules, known_objects=known_objects,
                       source_label=main_label, source_type='main')
    indexer.index_bsl(str(ext_xml), source_id='ext',
                       known_modules=known_modules, known_objects=known_objects,
                       source_label=ext_label, source_type='extension')
    indexer.resolve_calls()
    return db


# ── D1: common-module calls with a "banned prefix" name stay in the graph ──

def test_d1_prefixed_module_name_call_is_resolved():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        ca = CallAnalyzer(db)
        tree = ca.build_call_tree('ОбработатьСобытие', module_name='ИнтеграционныйМодуль',
                                   direction='down', depth=2)
        assert tree is not None
        assert len(tree.children) == 1, f"Expected 1 call, got {tree.children}"
        child = tree.children[0]
        assert child.procedure_name == 'ЗарегистрироватьСобытие'
        assert child.module_name == 'ОбщийМодуль.ДокументооборотИнтеграция.Модуль', \
            f"Call into ДокументооборотИнтеграция should resolve, got {child.module_name}"
        assert child.resolved, "Call should resolve to the real procedure, not stay unresolved"
        db.close()


# ── D2: local call to a stoplisted name kept when declared in this module ──

def test_d2_local_call_shadowing_platform_stoplist_kept():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        ca = CallAnalyzer(db)
        tree = ca.build_call_tree('ОбработатьОбъект', module_name='ПроверкиОбщие',
                                   direction='down', depth=2)
        assert tree is not None
        names = [c.procedure_name for c in tree.children]
        assert 'ПроверитьЗаполнение' in names, \
            f"Local call to ПроверитьЗаполнение (stoplisted, but declared locally) should survive, got {names}"
        target = next(c for c in tree.children if c.procedure_name == 'ПроверитьЗаполнение')
        assert target.resolved and target.confidence == 'exact'
        db.close()


# ── D3: up-tree doesn't cross-pollute across same-named procedures ──

def test_d3_up_tree_does_not_leak_across_same_named_procedures():
    """Reproduces the exact scenario named in IMPROVEMENT_PLAN.md's D3 row:
    ЦеныСервер.РассчитатьЦену and СкидкиСервер.РассчитатьЦену are two
    different procedures that share a name; СкидкиСервер calls the former,
    ЗаказыСервер calls the latter. The up-tree of one must not show the
    other's caller."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        ca = CallAnalyzer(db)

        prices_up = ca.build_call_tree('РассчитатьЦену', module_name='ЦеныСервер',
                                        direction='up', depth=2)
        assert prices_up is not None
        prices_callers = [(c.module_name, c.procedure_name) for c in prices_up.children]
        assert len(prices_up.children) == 1, f"Expected exactly 1 caller, got {prices_callers}"
        assert 'СкидкиСервер' in prices_up.children[0].module_name
        assert prices_up.children[0].procedure_name == 'ПрименитьСкидку'
        assert not any('ЗаказыСервер' in m for m, _ in prices_callers), \
            f"Caller of СкидкиСервер.РассчитатьЦену leaked into ЦеныСервер's up-tree: {prices_callers}"

        discounts_up = ca.build_call_tree('РассчитатьЦену', module_name='СкидкиСервер',
                                           direction='up', depth=2)
        assert discounts_up is not None
        discounts_callers = [(c.module_name, c.procedure_name) for c in discounts_up.children]
        assert any('ЗаказыСервер' in m for m, _ in discounts_callers), \
            f"Expected ЗаказыСервер among СкидкиСервер.РассчитатьЦену's callers, got {discounts_callers}"
        assert not any(m == 'ОбщийМодуль.ЦеныСервер.Модуль' for m, _ in discounts_callers), \
            f"ЦеныСервер should not appear as a caller of СкидкиСервер.РассчитатьЦену: {discounts_callers}"
        db.close()


# ── D4: down-tree resolves a qualified call to its own module ──

def test_d4_down_tree_resolves_to_own_module():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        ca = CallAnalyzer(db)

        tree = ca.build_call_tree('ПриСозданииНаСервере',
                                   module_name='ДепонированиеЗарплатыФормыВнутренний',
                                   direction='down', depth=2)
        assert tree is not None
        assert len(tree.children) == 1
        child = tree.children[0]
        assert child.procedure_name == 'ПриСозданииНаСервере'
        assert 'ДепонированиеЗарплатыФормыБазовый' in child.module_name, \
            f"Qualified call must resolve into its own module, got {child.module_name}"
        assert child.resolved
        db.close()


# ── D9: override directives don't leak a bogus call node, and the ──
# ── interceptor is visible in the up-tree of the intercepted procedure ──

def test_d9_ru_override_directive_no_bogus_call_and_visible_in_up_tree():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        ca = CallAnalyzer(db)

        # The extension intercepts ДепонированиеЗарплатыФормыБазовый.ПриСозданииНаСервере
        # with &Вместо + ПродолжитьВызов, and a plain &После handler.
        down = ca.build_call_tree('РасширениеПример_ПриСозданииНаСервере',
                                   module_name='ДепонированиеЗарплатыФормыБазовый',
                                   direction='down', source_id='ext', depth=2)
        assert down is not None
        names = [c.procedure_name for c in down.children]
        assert 'Вместо' not in names, f"&Вместо(...) must not produce a call to 'Вместо', got {names}"

        up = ca.build_call_tree('ПриСозданииНаСервере',
                                 module_name='ДепонированиеЗарплатыФормыБазовый',
                                 direction='up', depth=2)
        assert up is not None
        interceptors = {c.procedure_name for c in up.children}
        assert 'РасширениеПример_ПриСозданииНаСервере' in interceptors, \
            f"&Вместо interceptor should show up in the target's up-tree, got {interceptors}"
        assert 'РасширениеПример_ПриСозданииНаСервереПосле' in interceptors, \
            f"&После interceptor should show up too, got {interceptors}"
        db.close()


def test_d9_en_override_directive_no_bogus_call_and_visible_in_up_tree():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_en', FIXTURES / 'xml_en_ext')
        ca = CallAnalyzer(db)

        down = ca.build_call_tree('sample_ext_Search', module_name='Account',
                                   direction='down', source_id='ext', depth=2)
        assert down is not None
        names = [c.procedure_name for c in down.children]
        assert 'Around' not in names, f"&Around(...) must not produce a call to 'Around', got {names}"

        up = ca.build_call_tree('Search', module_name='Account', direction='up', depth=2)
        assert up is not None
        interceptors = {c.procedure_name for c in up.children}
        assert 'sample_ext_Search' in interceptors, \
            f"&Around interceptor should show up in Search's up-tree, got {interceptors}"
        db.close()


if __name__ == '__main__':
    tests = [
        test_d1_prefixed_module_name_call_is_resolved,
        test_d2_local_call_shadowing_platform_stoplist_kept,
        test_d3_up_tree_does_not_leak_across_same_named_procedures,
        test_d4_down_tree_resolves_to_own_module,
        test_d9_ru_override_directive_no_bogus_call_and_visible_in_up_tree,
        test_d9_en_override_directive_no_bogus_call_and_visible_in_up_tree,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  ✅ {t.__name__}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {t.__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
