"""
Golden (snapshot) tests for the call graph on the RU/EN fixtures — Этап 7
item 2 of IMPROVEMENT_PLAN.md.

Unlike test_call_graph_resolution.py (which asserts individual structural
properties per D1-D4/D9), these compare the *entire* rendered tree text
against a fixed snapshot — a broader regression net that also catches
formatting changes: confidence markers (the trailing '?'), @source labels,
line numbers, sort order, anything CallAnalyzer.format_tree_simple emits.

If a change intentionally alters tree output, regenerate a snapshot by
printing ca.format_tree_simple(tree) for the affected case and updating
the expected string here (don't just paste it blind — read it and confirm
it's still correct first).
"""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.call_analyzer import CallAnalyzer

# Формат дерева (см. CallAnalyzer.format_tree_simple): модуль и источник
# печатаются ТОЛЬКО когда меняются относительно родителя. Поэтому в
# ожиданиях ниже внутримодульные вызовы идут коротким именем, а полный
# путь и «@Источник» остаются там, где вызов действительно уходит в другой
# модуль или в расширение.

FIXTURES = Path(__file__).parent / 'fixtures'


def _build_project_db(tmpdir: str, main_xml: Path, ext_xml: Path,
                       main_label: str = 'Main', ext_label: str = 'Ext') -> Database:
    """Two-pass index a (main, extension) source pair into a fresh DB —
    same helper as test_call_graph_resolution.py's, duplicated here so
    this file stays self-contained."""
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


def test_golden_ru_down_tree_common_module_call():
    """D1: a call into a module with a "banned prefix" name
    (ДокументооборотИнтеграция) — full tree text, not just presence."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        try:
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree('ОбработатьСобытие', module_name='ИнтеграционныйМодуль',
                                       direction='down', depth=3)
            expected = (
                "ОбщийМодуль.ИнтеграционныйМодуль.Модуль.ОбработатьСобытие()  @Main\n"
                "└── ОбщийМодуль.ДокументооборотИнтеграция.Модуль.ЗарегистрироватьСобытие() [L4]"
            )
            assert ca.format_tree_simple(tree) == expected
        finally:
            db.close()


def test_golden_ru_up_tree_no_cross_pollution():
    """D3: up-tree of СкидкиСервер.РассчитатьЦену — must show only its
    real caller (ЗаказыСервер), never ЦеныСервер's."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        try:
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree('РассчитатьЦену', module_name='СкидкиСервер',
                                       direction='up', depth=2)
            expected = (
                "ОбщийМодуль.СкидкиСервер.Модуль.РассчитатьЦену()  @Main\n"
                "└── ОбщийМодуль.ЗаказыСервер.Модуль.ОформитьЗаказ() [L5]"
            )
            assert ca.format_tree_simple(tree) == expected
        finally:
            db.close()


def test_golden_ru_up_tree_double_override_intercept():
    """D9: both a &Вместо and a &После extension interceptor show up in
    the up-tree of the main-config procedure they target, alongside the
    real in-config caller — with confidence markers on the two override
    edges (name_only/override confidence isn't 'exact')."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_ru', FIXTURES / 'xml_ru_ext')
        try:
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree('ПриСозданииНаСервере',
                                       module_name='ДепонированиеЗарплатыФормыБазовый',
                                       direction='up', depth=2)
            expected = (
                "ОбщийМодуль.ДепонированиеЗарплатыФормыБазовый.Модуль.ПриСозданииНаСервере()  @Main\n"
                "├── РасширениеПример_ПриСозданииНаСервере() &Вместо:ПриСозданииНаСервере [L8]  @Ext  ?\n"
                "├── РасширениеПример_ПриСозданииНаСервереПосле() &После:ПриСозданииНаСервере [L17]  @Ext  ?\n"
                "└── ОбщийМодуль.ДепонированиеЗарплатыФормыВнутренний.Модуль.ПриСозданииНаСервере() [L4]"
            )
            assert ca.format_tree_simple(tree) == expected
        finally:
            db.close()


def test_golden_en_up_tree_around_intercept():
    """D9 (EN): &Around interceptor visible in the up-tree of the
    manager-module procedure it targets."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_en', FIXTURES / 'xml_en_ext')
        try:
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree('Search', module_name='Account', direction='up', depth=2)
            expected = (
                "Справочник.Account.МодульМенеджера.Search()  @Main\n"
                "└── sample_ext_Search() &Around:Search [L11]  @Ext  ?"
            )
            assert ca.format_tree_simple(tree) == expected
        finally:
            db.close()


def test_golden_en_down_tree_proceed_with_call_redirect():
    """D9 (EN): ProceedWithCall() inside the &Around interceptor redirects
    to the intercepted Search(), which itself calls on into ToJson() —
    exercises the ПродолжитьВызов/ProceedWithCall edge end to end."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _build_project_db(tmpdir, FIXTURES / 'xml_en', FIXTURES / 'xml_en_ext')
        try:
            ca = CallAnalyzer(db)
            tree = ca.build_call_tree('sample_ext_Search', module_name='Account',
                                       direction='down', source_id='ext', depth=2)
            expected = (
                "Справочник.Account.МодульМенеджера.sample_ext_Search() &Around:Search  @Ext\n"
                "└── Search() [L11]  @Main  ?\n"
                "    └── ToJson() [L328]"
            )
            assert ca.format_tree_simple(tree) == expected
        finally:
            db.close()
