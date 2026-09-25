"""
Regression test for a parser bug found via Этап 7's diagnose_index on the
real ERP corpus (IMPROVEMENT_PLAN.md): a bare `//` comment line nested
inside an open multi-line string (query text) — without the `|`
continuation prefix — was incorrectly treated as closing the string.

Real-world trigger: 1C's "//++ НЕ УТ" / "//-- НЕ УТ" merge-annotation
convention, used inside multi-line query text to mark ERP/UT-specific
fragments. Once misparsed as "string closed", everything after it until
the string's real closing quote got scanned as normal code — turning
query-language keywords (ЗНАЧЕНИЕ, ЕСТЬNULL, СУММА, ВЫРАЗИТЬ...) into
thousands of bogus unresolved "calls" (confirmed on the real ERP export:
ЗНАЧЕНИЕ alone was the single most frequent unresolved callee name,
~24000 occurrences).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.bsl_parser import BSLParser


def test_comment_without_pipe_inside_multiline_string_does_not_leak_calls():
    code = '''
Функция ТекстЗапроса() Экспорт
	Возврат
	"ВЫБРАТЬ
	|	Таблица.Поле КАК Поле,
	|	Таблица.Аналитика = ЗНАЧЕНИЕ(Справочник.Пустая.ПустаяСсылка)
	//++ НЕ УТ
	|	И ЗНАЧЕНИЕ(Справочник.Другой.ПустаяСсылка) = Таблица.Прочее
	//-- НЕ УТ
	|ИЗ
	|	Справочник.Товары КАК Таблица
	|СГРУППИРОВАТЬ ПО
	|	ЕСТЬNULL(Таблица.Поле, 0)";
КонецФункции

Процедура ОбычнаяПроцедура() Экспорт
	РеальныйВызов();
КонецПроцедуры
'''
    result = BSLParser().parse(code)

    call_names = {c.callee_proc for c in result.calls}
    assert 'ЗНАЧЕНИЕ' not in call_names, (
        "query-language ЗНАЧЕНИЕ(...) inside the string leaked out as a "
        f"bogus call — got calls: {call_names}")
    assert 'ЕСТЬNULL' not in call_names, (
        "query-language ЕСТЬNULL(...) after the // comment leaked out as "
        f"a bogus call — got calls: {call_names}")

    # The real code after the string is still parsed normally.
    assert 'РеальныйВызов' in call_names


def test_pipe_line_that_also_closes_the_string_does_not_stick_in_multiline_state():
    """A second real parser bug found via diagnose_index (Этап 7, third
    round, real ERP): the extremely common convention of closing a query
    string on a "|"-prefixed line (`|...";`) was matched by the "|"-prefix
    check FIRST, which always `continue`s without ever looking at that
    line's own quote count — so the string's actual close was missed and
    in_multiline_string stayed stuck True. Every line after that (even
    plain code with no "|" prefix) kept getting swallowed as "still
    inside the string" until one happened to reset the state by chance,
    and a SECOND string's own "|" lines were then exposed as normal code
    in the gap — turning query keywords into bogus calls, exactly like
    the first bug above but from the opposite direction (closing, not a
    nested comment)."""
    code = '''
Процедура ЗаполнитьТекст() Экспорт
	Текст =
	"ВЫБРАТЬ
	|	Таблица.Поле
	|ИЗ
	|	Справочник.Товары КАК Таблица";

	ПоляВЗапрос = "";
	Если Ложь Тогда
		ПоляВЗапрос = ПоляВЗапрос + ",
		|	ЗНАЧЕНИЕ(Справочник.Другой.ПустаяСсылка) КАК Поле2";
	КонецЕсли;

	РеальныйВызов();
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {c.callee_proc for c in result.calls}
    assert 'ЗНАЧЕНИЕ' not in call_names, (
        "query-language ЗНАЧЕНИЕ(...) inside the second string leaked out "
        f"as a bogus call after the first string's \"|\"-line close was "
        f"missed — got calls: {call_names}")
    # The real code between and after both strings must still be seen —
    # proves in_multiline_string didn't stay stuck True past the first
    # string's real close.
    assert 'РеальныйВызов' in call_names


def test_comment_without_pipe_outside_any_string_is_unaffected():
    """A plain // comment when we're NOT inside a multi-line string must
    keep behaving as a normal comment (no regression from the fix)."""
    code = '''
Процедура Тест() Экспорт
	//++ обычный комментарий
	РеальныйВызов();
	//-- обычный комментарий
КонецПроцедуры
'''
    result = BSLParser().parse(code)
    call_names = {c.callee_proc for c in result.calls}
    assert call_names == {'РеальныйВызов'}
