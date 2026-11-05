"""Комментарий в конце строки объявления и конца процедуры.

`Функция Имя(…) Экспорт // Доработка` — объявление; без этого такие процедуры
пропадали из индекса целиком (их нет в поиске, графе вызовов, коде).
`КонецФункции // Имя()` — частый стиль типовых: без этого конец не распознавался, и
процедура тянулась до следующей, захватывая чужие строки.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.bsl_parser import BSLParser

CODE = '''
&НаСервере // доработка
Функция Наименование(Фамилия, Имя = "http://x") Экспорт   // Доработка
	Возврат Фамилия + Имя;
КонецФункции // Наименование()

// Разделитель между процедурами.

Процедура Тест() // без Экспорт
	Наименование("а");
КонецПроцедуры;  // Тест

&После("Записать") // перехват
Процедура Расш_Записать()
КонецПроцедуры

Function English(A) Export // comment
EndFunction // English
'''


def _procs():
    return {p.name: p for p in BSLParser().parse(CODE).procedures}


def test_declarations_with_trailing_comment():
    procs = _procs()
    assert set(procs) == {'Наименование', 'Тест', 'Расш_Записать', 'English'}
    name = procs['Наименование']
    assert name.is_export and name.kind == 'Функция' and name.directive.startswith('&НаСервере')
    assert name.params == 'Фамилия, Имя = "http://x"'
    assert name.signature == 'Функция Наименование(Фамилия, Имя = "http://x") Экспорт'
    assert procs['Тест'].signature == 'Процедура Тест()' and not procs['Тест'].is_export
    assert procs['English'].is_export
    assert procs['Расш_Записать'].intercepts


def test_end_with_trailing_comment_closes_procedure():
    procs = _procs()
    assert (procs['Наименование'].start_line, procs['Наименование'].end_line) == (3, 5)
    assert (procs['Тест'].start_line, procs['Тест'].end_line) == (9, 11)
    assert procs['English'].end_line == 18


def test_call_from_procedure_after_commented_end():
    calls = {(c.callee_proc, c.callee_kind) for c in BSLParser().parse(CODE).calls}
    assert ('Наименование', 'local') in calls
