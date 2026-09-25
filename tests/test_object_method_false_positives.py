"""
Regression test for the object-method false-positive class found via
Этап 7's diagnose_index on the real ERP corpus (IMPROVEMENT_PLAN.md).

D1's classification ("classify by fact") only treated a *lowercase*-initial
dotted prefix as "probably a local variable, skip it" — PascalCase local
variables (the standard 1C style, e.g. `Сообщение = Новый
СообщениеПользователю; ... Сообщение.Сообщить()`) fell through and were
recorded as unresolved common_module calls. `Сообщение.Сообщить` alone was
~12,700 bogus edges on the real ERP export.

Deliberately NOT fixed by adding method names like 'Сообщить' to
_OBJECT_METHODS — that set already has a comment explaining why 'Сообщить'
specifically is excluded (it collides with real common-module calls like
ОбщегоНазначения.Сообщить). Fixed instead by tracking, per procedure,
which variable names were assigned via "= Новый X(" — known by fact to be
object variables, not modules, regardless of method name or case.
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.bsl_parser import BSLParser
from src.core.db import Database
from src.core.indexer import Indexer
from src.core.xml_walker import XMLWalker


def test_pascalcase_constructed_variable_method_call_not_recorded_as_module_call():
    code = '''
Процедура УведомитьПользователя() Экспорт
	Сообщение = Новый СообщениеПользователю;
	Сообщение.Текст = "Привет";
	Сообщение.Сообщить();
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('Сообщение', 'Сообщить') not in call_names, (
        f"Сообщение.Сообщить() should be recognized as a variable method "
        f"call, not a module call — got: {call_names}")


def test_same_method_name_still_resolves_when_module_is_a_real_common_module():
    """The fix must be scoped to names actually constructed with Новый in
    THIS procedure — a genuine common-module call using the exact same
    method name elsewhere must be unaffected."""
    code = '''
Процедура Тест() Экспорт
	ОбщегоНазначения.Сообщить("текст");
КонецПроцедуры
'''
    parser = BSLParser(known_modules={'общегоназначения'})
    result = parser.parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ОбщегоНазначения', 'Сообщить') in call_names, (
        f"a real common-module call must still resolve — got: {call_names}")


def test_constructed_variable_scoped_to_its_own_procedure():
    """A name constructed in one procedure must not suppress a
    same-named, genuinely different dotted call in another procedure."""
    code = '''
Процедура Первая() Экспорт
	Отчет = Новый ТабличныйДокумент;
	Отчет.Вывести(Область);
КонецПроцедуры

Процедура Вторая() Экспорт
	Отчет.Сформировать();
КонецПроцедуры
'''
    parser = BSLParser(known_modules={'отчет'})
    result = parser.parse(code)
    by_proc = {}
    for c in result.calls:
        by_proc.setdefault(c.callee_name, []).append(c)
    # In Вторая(), "Отчет" was never constructed there — with "отчет" a
    # known common module, it should resolve normally.
    second_proc_calls = [c for c in result.calls if c.callee_proc == 'Сформировать']
    assert len(second_proc_calls) == 1
    assert second_proc_calls[0].callee_module == 'Отчет'


def test_chained_factory_call_result_also_treated_as_variable():
    """The extremely common ЭлементБлокировки = Блокировка.Добавить(...)
    pattern — DataLock.Add() returns a DataLockItem — a real ~1700-edge
    false positive on the bshp corpus."""
    code = '''
Процедура Тест() Экспорт
	Блокировка = Новый БлокировкаДанных();
	ЭлементБлокировки = Блокировка.Добавить("Справочник.Тест");
	ЭлементБлокировки.УстановитьЗначение("Идентификатор", 1);
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ЭлементБлокировки', 'УстановитьЗначение') not in call_names, (
        f"chained factory-call result should be tracked as a variable — got: {call_names}")


def test_procedure_parameter_treated_as_local_variable():
    """A parameter can never be a common module reference in 1C — only a
    variable — so ПечатнаяФорма.Вывести(...) where ПечатнаяФорма arrives
    as a parameter (never locally constructed) should also be recognized,
    the single biggest remaining false-positive category on the real ERP
    (~4500 edges)."""
    code = '''
Процедура ЗаполнитьДокумент(ПечатнаяФорма, Знач ДопПараметры = Неопределено) Экспорт
	ПечатнаяФорма.Вывести(Макет.ПолучитьОбласть("Шапка"));
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ПечатнаяФорма', 'Вывести') not in call_names, (
        f"a parameter name must be treated as a local variable — got: {call_names}")


def test_factory_function_return_value_tracked_across_module():
    """The remaining big false-positive category flagged in the previous
    round: an object obtained from a same-module "factory function"
    (returns a value it built itself) rather than Новый/a parameter/a
    chain — real example from the ERP export:
    Функция НовыйПустойЛист() ... Возврат ПечатнаяФорма; КонецФункции
    then elsewhere: ПечатнаяФорма = НовыйПустойЛист(); ПечатнаяФорма.Вывести(...)."""
    code = '''
Функция НовыйПустойЛист() Экспорт
	ПечатнаяФорма = Новый ТабличныйДокумент;
	ПечатнаяФорма.ОтображатьЗаголовки = Ложь;
	Возврат ПечатнаяФорма;
КонецФункции

Функция СформироватьЛист(Объект) Экспорт
	ПечатнаяФорма = НовыйПустойЛист();
	ПечатнаяФорма.Вывести(Объект.ПолучитьМакет("Шапка"));
	Возврат ПечатнаяФорма;
КонецФункции
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ПечатнаяФорма', 'Вывести') not in call_names, (
        f"factory-function result should be tracked as a variable — got: {call_names}")


def test_factory_function_returning_new_directly_also_tracked():
    code = '''
Функция СоздатьСообщение() Экспорт
	Возврат Новый СообщениеПользователю;
КонецФункции

Процедура Тест() Экспорт
	Сообщение = СоздатьСообщение();
	Сообщение.Сообщить();
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('Сообщение', 'Сообщить') not in call_names


def test_cross_module_factory_function_tracked_via_known_factory_functions():
    """The genuinely-cross-module case: ПечатнаяФорма arrives from
    ОбщегоНазначения.НовыйПустойЛист() (a DIFFERENT module's factory
    function) rather than a same-module bare call — the dominant real
    pattern behind the remaining ПечатнаяФорма.Вывести false positives on
    the real ERP export. known_factory_functions is what a project-wide
    pre-pass (Indexer) is expected to supply."""
    code = '''
Процедура СформироватьЛист(Объект) Экспорт
	ПечатнаяФорма = ОбщегоНазначения.НовыйПустойЛист();
	ПечатнаяФорма.Вывести(Объект.ПолучитьМакет("Шапка"));
КонецПроцедуры
'''
    parser = BSLParser(
        known_modules={'общегоназначения'},
        known_factory_functions={'общегоназначения': {'новыйпустойлист'}},
    )
    result = parser.parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    # The cross-module call itself must still be recorded normally.
    assert ('ОбщегоНазначения', 'НовыйПустойЛист') in call_names
    # But the result assigned to ПечатнаяФорма must not be misread as a
    # module — Вывести() on it should not appear as a call at all.
    assert ('ПечатнаяФорма', 'Вывести') not in call_names


def test_factory_function_returning_manager_call_also_tracked():
    """МакетСоставаПоказателей.Область on the real ERP: the underlying
    same-module factory function returns a 3-part manager call
    (Отчеты.ИмяОтчета.ПолучитьМакет(...)) rather than "Новый X" or a bare
    variable — _RE_RETURN_NEW/_RE_RETURN_BARE_VAR miss this shape."""
    code = '''
Функция ПолучитьМакетСоставаПоказателей() Экспорт
	Возврат Отчеты.РегламентированныйОтчетПрибыль.ПолучитьМакет("МакетСоставаПоказателей");
КонецФункции

Процедура Тест() Экспорт
	МакетСоставаПоказателей = ПолучитьМакетСоставаПоказателей();
	МакетСоставаПоказателей.Область("R1C1").Текст = "1";
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('МакетСоставаПоказателей', 'Область') not in call_names


def test_parse_result_exposes_own_exported_factory_functions():
    """ParseResult.factory_functions is what the indexer's project-wide
    pre-pass collects across every module — only exported functions
    matter (non-exported ones can't be called cross-module anyway)."""
    code = '''
Функция НовыйПустойЛист() Экспорт
	ПечатнаяФорма = Новый ТабличныйДокумент;
	Возврат ПечатнаяФорма;
КонецФункции

Функция ВнутренняяПомощница()
	Возврат Новый Массив;
КонецФункции

Функция ПростоЧисло() Экспорт
	Возврат 42;
КонецФункции
'''
    result = BSLParser().parse(code)
    assert result.factory_functions == {'новыйпустойлист'}, (
        "only the exported, self-constructing function should be exposed "
        f"— got {result.factory_functions}")


# ============================================
# Module aliasing (Этап 7, third round): resolution improvements, not
# suppression — a var assigned a module's own bare name, or via the BSP
# ОбщегоНазначения.ОбщийМодуль("X") idiom (~12,900 real occurrences on
# the ERP export), should resolve Var.Method() as the real module's call.
# ============================================

def test_bare_module_alias_resolves_as_the_real_module():
    code = '''
Функция НастройкиПолейФормы() Экспорт
	Финансы = ФинансоваяОтчетностьСервер;
	Финансы.НовыйОтбор(Условие, "Поле", Истина);
КонецФункции
'''
    parser = BSLParser(known_modules={'финансоваяотчетностьсервер'})
    result = parser.parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ФинансоваяОтчетностьСервер', 'НовыйОтбор') in call_pairs, (
        f"bare module alias should resolve to the real module — got {call_pairs}")
    assert not any(m == 'Финансы' for m, _ in call_pairs), (
        "the fictional alias name itself must not appear as a callee_module")


def test_bare_alias_ignored_when_rhs_is_not_a_known_module():
    """A plain variable-to-variable copy (RHS not a known module) must
    not be mistaken for an alias — it's just noise, harmlessly ignored."""
    code = '''
Процедура Тест() Экспорт
	Копия = Оригинал;
	Копия.ЧтоТоДелает();
КонецПроцедуры
'''
    result = BSLParser(known_modules={'общегоназначения'}).parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert not any(m == 'ОбщегоНазначения' for m, _ in call_pairs), (
        f"an ordinary variable copy is not an alias — got {call_pairs}")
    # Общее правило «присвоено не-модуль»: Оригинал не в known_modules,
    # значит Копия — значение, и фантомного вызова модуля «Копия» тоже нет.
    assert ('Копия', 'ЧтоТоДелает') not in call_pairs, call_pairs
    # Без фактов Pass 1 (known_modules пуст) судить не о чем — вызов остаётся.
    no_facts = {(c.callee_module, c.callee_proc) for c in BSLParser().parse(code).calls}
    assert ('Копия', 'ЧтоТоДелает') in no_facts, no_facts


def test_bsp_dynamic_module_loader_resolves_as_the_real_module():
    code = '''
Процедура ПриСозданииНаСервере() Экспорт
	МодульУправлениеДоступом = ОбщегоНазначения.ОбщийМодуль("УправлениеДоступом");
	МодульУправлениеДоступом.ПриЧтенииНаСервере(ЭтотОбъект, ТекущийОбъект);
КонецПроцедуры
'''
    parser = BSLParser(known_modules={'общегоназначения', 'управлениедоступом'})
    result = parser.parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ОбщегоНазначения', 'ОбщийМодуль') in call_pairs, (
        "the loader call itself must still be recorded normally")
    assert ('УправлениеДоступом', 'ПриЧтенииНаСервере') in call_pairs, (
        f"the dynamically-loaded module's call should resolve — got {call_pairs}")
    assert not any(m == 'МодульУправлениеДоступом' for m, _ in call_pairs)


def test_bsp_dynamic_module_loader_en_variant():
    code = '''
Procedure OnServerCreate() Export
	AccessManagementModule = Common.CommonModule("AccessManagement");
	AccessManagementModule.OnReadAtServer(ThisObject, CurrentObject);
EndProcedure
'''
    parser = BSLParser(known_modules={'common', 'accessmanagement'})
    result = parser.parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('AccessManagement', 'OnReadAtServer') in call_pairs, (
        f"EN variant of the BSP idiom should resolve too — got {call_pairs}")


def test_multiple_constructed_variables_all_suppressed():
    code = '''
Процедура Тест() Экспорт
	ТабДок = Новый ТабличныйДокумент;
	Область = ТабДок.ПолучитьОбласть("R1C1");
	ЭлементБлокировки = Новый БлокировкаДанных;
	ЭлементБлокировки.УстановитьЗначение(Ссылка);
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ТабДок', 'ПолучитьОбласть') not in call_names
    assert ('ЭлементБлокировки', 'УстановитьЗначение') not in call_names


# ============================================
# Full-pipeline integration: Indexer's project-wide Pass 1.5
# ============================================

def _write_module(xml_dir: Path, module_name: str, code: str):
    module_dir = xml_dir / 'CommonModules' / module_name / 'Ext'
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / 'Module.bsl').write_text(code, encoding='utf-8-sig')


def test_indexer_project_wide_factory_pass_resolves_cross_module_calls():
    """End-to-end through the real Indexer/ProjectManager wiring (not just
    BSLParser called directly with a hand-fed known_factory_functions) —
    scan_known_factory_functions must discover ОбщегоНазначения's factory
    function from its own file, and index_bsl must use that to keep
    Модуль2's ПечатнаяФорма.Вывести() out of the graph while still
    recording ОбщегоНазначения.НовыйПустойЛист() as a normal, resolved
    call."""
    with tempfile.TemporaryDirectory() as tmpdir:
        xml_dir = Path(tmpdir) / 'xml'
        _write_module(xml_dir, 'ОбщегоНазначения', '''
Функция НовыйПустойЛист() Экспорт
	ПечатнаяФорма = Новый ТабличныйДокумент;
	Возврат ПечатнаяФорма;
КонецФункции
''')
        _write_module(xml_dir, 'Модуль2', '''
Процедура СформироватьЛист(Объект) Экспорт
	ПечатнаяФорма = ОбщегоНазначения.НовыйПустойЛист();
	ПечатнаяФорма.Вывести(Объект.ПолучитьМакет("Шапка"));
КонецПроцедуры
''')

        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            known_modules, known_objects = Indexer.scan_known_names([str(xml_dir)])
            common_module_files = [
                mf for mf in XMLWalker().walk(str(xml_dir)) if mf.object_kind == 'ОбщийМодуль']
            known_factory_functions = Indexer.scan_known_factory_functions(common_module_files)
            assert known_factory_functions.get('общегоназначения') == {'новыйпустойлист'}

            indexer = Indexer(db)
            indexer.index_bsl(str(xml_dir), source_id='main',
                               known_modules=known_modules, known_objects=known_objects,
                               known_factory_functions=known_factory_functions)
            indexer.resolve_calls()

            rows = db.conn.execute("""
                SELECT c.callee_module, c.callee_proc, cr.callee_id
                FROM calls c LEFT JOIN calls_resolved cr ON cr.call_id = c.id
            """).fetchall()
            call_pairs = {(r['callee_module'], r['callee_proc']) for r in rows}

            assert ('ПечатнаяФорма', 'Вывести') not in call_pairs, (
                f"cross-module factory result misread as a module call — got {call_pairs}")
            assert ('ОбщегоНазначения', 'НовыйПустойЛист') in call_pairs
            resolved = {(r['callee_module'], r['callee_proc']) for r in rows if r['callee_id']}
            assert ('ОбщегоНазначения', 'НовыйПустойЛист') in resolved, (
                "the real cross-module call must still resolve normally")
        finally:
            db.close()


def test_module_level_var_constructed_in_one_procedure_used_in_another():
    """A module-level "Перем" variable falls outside every per-procedure
    set built above by construction — it can be constructed in procedure A
    and only used via .Method(...) in procedure B, which per-procedure
    tracking alone can never see."""
    code = '''
Перем МакетСоставаПоказателей;

Процедура Инициализировать() Экспорт
	МакетСоставаПоказателей = Новый ТабличныйДокумент;
КонецПроцедуры

Процедура Заполнить() Экспорт
	МакетСоставаПоказателей.Область("R1C1").Текст = "1";
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('МакетСоставаПоказателей', 'Область') not in call_names


def test_module_level_var_via_module_alias_used_in_another_procedure():
    code = '''
Перем ФинансовыйМодуль;

Процедура Инициализировать() Экспорт
	ФинансовыйМодуль = ФинансоваяОтчетностьСервер;
КонецПроцедуры

Процедура Заполнить() Экспорт
	ФинансовыйМодуль.НовыйОтбор(Условие, "Поле", Истина);
КонецПроцедуры
'''
    parser = BSLParser(known_modules={'финансоваяотчетностьсервер'})
    result = parser.parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('ФинансоваяОтчетностьСервер', 'НовыйОтбор') in call_pairs, (
        f"module-level alias should resolve across procedures — got {call_pairs}")
    assert not any(m == 'ФинансовыйМодуль' for m, _ in call_pairs)


def test_local_var_same_name_as_module_level_var_not_confused():
    """A procedure-local "Перем X;" (or a plain local assignment) that
    shadows a module-level var's name by coincidence must not spuriously
    suppress a real call on a DIFFERENT, unrelated local."""
    code = '''
Перем Кэш;

Процедура ПервичноеЗаполнение() Экспорт
	Кэш = Новый Массив;
КонецПроцедуры

Процедура ОтдельнаяОбработка() Экспорт
	Кэш = Новый Массив;
	// КэшДругой здесь не присваивается (иначе его пометило бы общее
	// правило «присвоено не-модуль», а не модульный трекинг).
	КэшДругой.ЧтоТоДелает();
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('КэшДругой', 'ЧтоТоДелает') in call_pairs, (
        f"an unrelated local must not be swept in by module-level tracking — got {call_pairs}")


def test_manager_call_assignment_result_tracked_as_object():
    """МакетСоставаПоказателей on the real ERP (variant 1): the value is
    built via a bare "Var = Отчеты.Х.ПолучитьМакет(...)" assignment (not a
    "Возврат ..." — that's covered separately by
    test_factory_function_returning_manager_call_also_tracked), then used
    in the SAME procedure."""
    code = '''
Процедура Тест() Экспорт
	МакетСоставаПоказателей = Отчеты.РегламентированныйОтчетПрибыль.ПолучитьМакет("Макет");
	КодПоказателя = МакетСоставаПоказателей.Область(1, 1).Текст;
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('МакетСоставаПоказателей', 'Область') not in call_pairs


def test_indexed_manager_assignment_result_tracked_as_object():
    """МакетСоставаПоказателей on the real ERP (variant 2/3): the report
    is looked up by a dynamic key — "Отчеты[ИмяОтчета].ПолучитьМакет(...)"
    — instead of a literal object name. 1C has no array/structure literal
    syntax, so a bare "[" on an assignment's RHS can only be an indexing
    operation, never a module reference — sound as a general "not a
    module" signal regardless of what's being indexed or chained after."""
    code = '''
Процедура Тест(ИмяОтчета) Экспорт
	МакетСоставаПоказателей = Отчеты[ИмяОтчета].ПолучитьМакет("Макет");
	КодПоказателя = МакетСоставаПоказателей.Область(1, 1).Текст;
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert ('МакетСоставаПоказателей', 'Область') not in call_pairs


def test_this_form_and_this_object_are_never_treated_as_modules():
    """ЭтаФорма/ЭтотОбъект (and EN ThisForm/ThisObject) are reserved
    context identifiers — they always refer to the current form/object,
    never a module, regardless of what method is called on them. Found
    via diagnose_index on the real ERP export (ЭтаФорма.Активизировать,
    ЭтотОбъект.ПолучитьМакет)."""
    code = '''
Процедура Тест() Экспорт
	ЭтаФорма.Активизировать();
	ЭтотОбъект.ПолучитьМакет("Макет");
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert not any(m in ('ЭтаФорма', 'ЭтотОбъект') for m, _ in call_pairs), (
        f"ЭтаФорма/ЭтотОбъект must never appear as an unresolved module — got {call_pairs}")


def test_this_form_and_this_object_en_variant():
    code = '''
Procedure Test() Export
	ThisForm.Activate();
	ThisObject.GetTemplate("Template");
EndProcedure
'''
    result = BSLParser().parse(code)
    call_pairs = {(c.callee_module, c.callee_proc) for c in result.calls}
    assert not any(m in ('ThisForm', 'ThisObject') for m, _ in call_pairs)
