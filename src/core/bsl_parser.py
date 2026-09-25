"""
BSL parser: extracts procedures, functions, directives, and calls from .bsl files.
Regex-based implementation (MVP). Architecture ready for tree-sitter swap.

Filtering strategy:
- Multi-line string literals (SQL queries) are tracked and skipped
- Platform built-in functions are excluded
- Object/variable method calls (Результат.Вставить) are excluded
- Only real cross-module calls (ОбщиеНаСервере.Метод) and local procedure calls are kept
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .bsl_reference import (
    CONSTRUCTOR_TYPES_RU,
    CONSTRUCTOR_TYPES_EN,
    SYSTEM_ENUMERATIONS_RU,
    SYSTEM_ENUMERATIONS_EN,
    PLATFORM_FUNCTIONS_EXTRA,
)


@dataclass
class ProcedureInfo:
    name: str = ''
    kind: str = 'Процедура'
    is_export: bool = False
    start_line: int = 0
    end_line: int = 0
    params: str = ''
    directive: str = ''
    signature: str = ''
    line_count: int = 0
    # Extension override target: name of the procedure this one intercepts,
    # from a &Вместо/&После/&Перед/&ИзменениеИКонтроль (or EN) directive.
    # Empty for ordinary procedures.
    intercepts: str = ''
    # D8: "Асинх Процедура X()" / "Async Function X()" (8.3.18+).
    is_async: bool = False


@dataclass
class CallInfo:
    callee_name: str = ''
    callee_module: str = ''
    callee_proc: str = ''
    line: int = 0
    context: str = ''
    # local | common_module | manager | override — set by the parser at
    # extraction time (it already knows which pattern matched); the
    # resolver (Indexer.resolve_calls) uses this to pick the right join.
    callee_kind: str = ''


@dataclass
class ParseResult:
    procedures: list[ProcedureInfo] = field(default_factory=list)
    calls: list[CallInfo] = field(default_factory=list)
    line_count: int = 0
    has_errors: bool = False
    # Raw file text (Этап 3/D6) — the indexer feeds this into module_text_fts
    # so search_code can grep the whole module, not just recognized calls.
    # Populated in parse(); reusing it here avoids re-reading the file.
    content: str = ''
    # Этап 7 (cross-module factory-function inference): lowercased names
    # of this module's own EXPORTED functions that return a value they
    # built themselves (see _RE_RETURN_NEW/_RE_RETURN_BARE_VAR). The
    # indexer collects this across every common module in a project-wide
    # pre-pass and feeds it back as BSLParser(known_factory_functions=...)
    # so `Var = ОбщегоНазначения.НовыйПустойЛист()` elsewhere in the
    # project is recognized as constructing an object too, not just the
    # same-module case this field is populated from.
    factory_functions: set[str] = field(default_factory=set)


# ============================================
# PATTERNS
# ============================================

_RE_DIRECTIVE = re.compile(
    r'^\s*&(НаСервереБезКонтекста|НаКлиентеНаСервереБезКонтекста|НаКлиентеНаСервере|'
    r'НаСервере|НаКлиенте|'
    r'AtServerNoContext|AtClientAtServerNoContext|AtClientAtServer|'
    r'AtServer|AtClient)\s*$',
    re.IGNORECASE
)

# Extension override directives: &Вместо("Имя") / &После("Имя") / &Перед("Имя") /
# &ИзменениеИКонтроль("Имя") and their EN equivalents &Around/&After/&Before/
# &ChangeAndValidate. Unlike _RE_DIRECTIVE these take a target-procedure
# argument and must NOT fall through to call extraction (D9: a bare
# "&Вместо(...)" line would otherwise regex-match as a call to "Вместо").
_RE_EXT_DIRECTIVE = re.compile(
    r'^\s*&(Вместо|После|Перед|ИзменениеИКонтроль|'
    r'Around|After|Before|ChangeAndValidate)'
    r'\s*\(\s*"([^"]+)"\s*\)\s*$',
    re.IGNORECASE
)

# D8: "Асинх Процедура X()" / "Async Function X()" — the async modifier
# comes before the Процедура/Функция keyword, not after.
_RE_PROC_START = re.compile(
    r'^\s*(?:(Асинх|Async)\s+)?(Процедура|Функция|Procedure|Function)\s+'
    r'([А-Яа-яA-Za-z0-9_]+)\s*'
    r'\(([^)]*)\)\s*'
    r'(Экспорт|Export)?\s*;?\s*$',
    re.IGNORECASE
)

# Module-level "Перем ИмяА, ИмяБ Экспорт;" declaration (BSL requires these
# before any Процедура/Функция) — only checked while current_proc is None,
# so a same-named local "Перем X;" inside a procedure body never matches.
_RE_MODULE_VAR_DECL = re.compile(
    r'^(?:Перем|Var)\s+(.+?)\s*;',
    re.IGNORECASE
)

# Multi-line signature start: has opening ( but no closing ) on same line
_RE_PROC_START_MULTILINE = re.compile(
    r'^\s*(?:(Асинх|Async)\s+)?(Процедура|Функция|Procedure|Function)\s+'
    r'([А-Яа-яA-Za-z0-9_]+)\s*'
    r'\([^)]*$',  # Opening paren, params, but NO closing paren
    re.IGNORECASE
)

_RE_PROC_END = re.compile(
    r'^\s*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\s*$',
    re.IGNORECASE
)

# Cross-module call: Идентификатор.Идентификатор(
_RE_CROSS_CALL = re.compile(
    r'(?<!["\'.А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\.'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*\('
)

# Manager module call: Справочники.Организации.СоздатьОрганизацию()
_RE_MANAGER_CALL = re.compile(
    r'(?<!["\'.А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'   # Справочники
    r'\.'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'   # Организации
    r'\.'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'   # МетодМенеджера
    r'\s*\('
)

# Metadata manager types: plural → singular (for resolving module names)
_METADATA_MANAGERS = {
    'справочники': 'Справочник', 'catalogs': 'Справочник',
    'документы': 'Документ', 'documents': 'Документ',
    'регистрысведений': 'РегистрСведений', 'informationregisters': 'РегистрСведений',
    'регистрынакопления': 'РегистрНакопления', 'accumulationregisters': 'РегистрНакопления',
    'регистрыбухгалтерии': 'РегистрБухгалтерии', 'accountingregisters': 'РегистрБухгалтерии',
    'отчеты': 'Отчет', 'reports': 'Отчет',
    'обработки': 'Обработка', 'dataprocessors': 'Обработка',
    'перечисления': 'Перечисление', 'enums': 'Перечисление',
    'регистрырасчета': 'РегистрРасчета', 'calculationregisters': 'РегистрРасчета',
    'планывидовхарактеристик': 'ПланВидовХарактеристик',
    'chartsofcharacteristictypes': 'ПланВидовХарактеристик',  # D11
    'планысчетов': 'ПланСчетов', 'chartsofaccounts': 'ПланСчетов',  # D11
    'планыобмена': 'ПланОбмена', 'exchangeplans': 'ПланОбмена',  # D11
    'бизнеспроцессы': 'БизнесПроцесс', 'businessprocesses': 'БизнесПроцесс',  # D11
    'задачи': 'Задача', 'tasks': 'Задача',  # D11
}

# Standard platform methods on manager objects — NOT user code
_PLATFORM_MANAGER_METHODS = {
    # Catalog/Document manager methods
    'найтипонаименованию', 'findbydescription',
    'найтипокоду', 'findbycode',
    'найтипореквизиту', 'findbyattribute',
    'создатьэлемент', 'createitem',
    'создатьгруппу', 'createfolder',
    'создатьдокумент', 'createdocument',
    'выбрать', 'select',
    'пустаяссылка', 'emptyref',
    'получитьмакет', 'gettemplate',
    'получитьформу', 'getform',
    'получитьполноеимя', 'getfullname',
    'получитьссылку', 'getref',
    # Register manager methods
    'создатьменеджерзаписи', 'createrecordmanager',
    'создатьнаборзаписей', 'createrecordset',
    'срезпоследних', 'slicelast',
    'срезпервых', 'slicefirst',
}

# Local call: Идентификатор(
_RE_LOCAL_CALL = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*\('
)

# Variable = Новый Тип(...) / Variable = New Type(...) — classifies the
# LHS as a local object variable *by fact* (a name can only appear here if
# it's being assigned a constructed value — never a common module), so
# Variable.AnyMethod(...) later in the same procedure is a variable method
# call, not a module call, regardless of case or method name (Этап 7:
# fixes the object-method false positives D1's lowercase-prefix heuristic
# missed for the very common PascalCase-variable style, e.g.
# `Сообщение = Новый СообщениеПользователю; ... Сообщение.Сообщить()`  —
# without guessing at method names like "Сообщить", which risk colliding
# with a genuine common-module export elsewhere, see _OBJECT_METHODS'
# own note on why it deliberately excludes 'Сообщить').
_RE_CONSTRUCTOR_ASSIGN = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*(?:Новый|New)\s+[А-Яа-яA-Za-z]',
    re.IGNORECASE
)

# Variable = Отчеты.ИмяОтчета.ПолучитьМакет(...) — a manager-style 3-part
# call used directly as an assignment's RHS (МакетСоставаПоказателей on
# the real ERP: found via a same-module factory function whose OWN return
# value comes from this shape rather than "Новый X"). Group(2) (the
# manager type, e.g. Отчеты) is checked against _METADATA_MANAGERS by the
# caller — sound regardless of which method is called, since a 3-part
# dotted call can never itself be a bare module reference.
_RE_MANAGER_CALL_ASSIGN = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\.[А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*'
    r'\.[А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*\s*\('
)

# Variable = Отчеты[ИмяОтчета].ПолучитьМакет(...) — same idea, but the
# manager-collection lookup uses dynamic "[...]" indexing instead of a
# literal object name. 1C has no array/structure literal syntax, so "["
# on an assignment's RHS is *always* an indexing operation into some
# collection — never a bare module reference (which can't be subscripted)
# — making this a sound, general "not a module" signal regardless of what
# follows the "[...]" (a bare index, or a chained ".Method(" like here).
_RE_INDEXED_ASSIGN = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*[^;]*\['
)

# Variable = OtherVariable.Method(...) — a chained factory call, e.g. the
# extremely common `ЭлементБлокировки = Блокировка.Добавить(Таблица)`
# (DataLock.Add() returns a DataLockItem). If OtherVariable is already
# known (by _RE_CONSTRUCTOR_ASSIGN or an earlier match of this same
# pattern) to be an object, not a module, then whatever its method
# returns is assigned to Variable — practically always another object,
# since modules are never returned from a method call, only referenced by
# their static name. Checked incrementally as lines are scanned in order,
# so multi-step chains (A=Новый X; B=A.M1(); C=B.M2()) all resolve.
_RE_CHAIN_ASSIGN = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\.([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)\s*\('
)

# One level of return-type inference: a function whose own body returns a
# value it built itself (either "Возврат Новый X" directly, or "Возврат
# Переменная" where Переменная is already known-constructed in that same
# function) is a "factory function" — 1C has no syntax to return a
# reference to a common module, only actual values, so treating its
# result as an object anywhere it's assigned is just as safe as the
# Новый-assignment case. Real example that motivated this (real ERP
# export): `Функция НовыйПустойЛист() Возврат ПечатнаяФорма; КонецФункции`
# where ПечатнаяФорма = Новый ТабличныйДокумент earlier in that function —
# callers doing `ПечатнаяФорма = НовыйПустойЛист()` were unrecognized.
_RE_RETURN_BARE_VAR = re.compile(
    r'^(?:Возврат|Return)\s+([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)\s*;?\s*$',
    re.IGNORECASE
)
_RE_RETURN_NEW = re.compile(
    r'^(?:Возврат|Return)\s+(?:Новый|New)\s+[А-Яа-яA-Za-z]',
    re.IGNORECASE
)

# Возврат Отчеты.ИмяОтчета.ПолучитьМакет(...) — a same-module factory
# function that builds its return value via a 3-part manager call (e.g.
# a report's own ПолучитьМакет) instead of "Новый X". Group(1) is checked
# against _METADATA_MANAGERS by the caller, same as _RE_MANAGER_CALL.
_RE_RETURN_MANAGER_CALL = re.compile(
    r'^(?:Возврат|Return)\s+'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)\.'
    r'[А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*\.'
    r'[А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*\s*\(',
    re.IGNORECASE
)

# Variable = FactoryFunctionName(...) — a bare (undotted) call to a
# same-module function already identified as a "factory function" above.
_RE_BARE_FUNC_CALL_ASSIGN = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*\('
)

# Module aliasing (Этап 7, second round, real ERP): unlike everything
# above — which all *suppress* a false "call" by recognizing the LHS as a
# value, never a module — these two patterns do the opposite: they
# recognize the LHS as a genuine ALIAS for a real common module, so a
# later Var.Method(...) resolves as ModuleName.Method(...) instead of
# staying unresolved as a fictional module named "Var". Both are sound
# because a bare identifier used as a value in 1C can *only* be a module
# reference (nothing else is a bare-identifier expression) — anything
# with an operator, dot, bracket or call is excluded by construction (the
# regexes below only match the one shape they're meant to).

# Variable = ИмяОбщегоМодуля; — the module's own literal name assigned
# with nothing else on the right (checked against known_modules by the
# caller; a plain variable-to-variable copy just doesn't match anything
# in known_modules and is silently ignored).
_RE_BARE_ALIAS_ASSIGN = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*;'
)

# Variable = AnyModule.ОбщийМодуль("ИмяМодуля") / .CommonModule("Name") —
# the standard BSP idiom (found ~12,900 times in one real ERP export) for
# looking up an optional subsystem's module by name, so calling into it
# doesn't create a hard compile-time dependency. AnyModule is typically
# ОбщегоНазначения/ОбщегоНазначенияКлиент/ОбщегоНазначенияКлиентСервер,
# but the method name alone (ОбщийМодуль/CommonModule is not a generic
# verb) is specific enough to key off of regardless of which module it's
# called on.
_RE_DYNAMIC_MODULE_LOAD = re.compile(
    r'(?<![.\"\x27А-Яа-яA-Za-z0-9_])'
    r'([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)'
    r'\s*=\s*'
    r'[А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*'
    r'\.(?:ОбщийМодуль|CommonModule)\s*\(\s*"([^"]+)"',
    re.IGNORECASE
)

# ============================================
# PLATFORM BUILT-IN FUNCTIONS (skip as calls)
# ============================================

_PLATFORM_FUNCTIONS = {
    # --- Control flow / keywords ---
    'если', 'if', 'тогда', 'then', 'иначе', 'else', 'иначеесли', 'elsif',
    'конецесли', 'endif', 'для', 'for', 'каждого', 'each', 'из', 'in',
    'по', 'to', 'цикл', 'do', 'конеццикла', 'enddo', 'пока', 'while',
    'попытка', 'try', 'исключение', 'except', 'конецпопытки', 'endtry',
    'возврат', 'return', 'продолжить', 'continue', 'прервать', 'break',
    'не', 'not', 'и', 'and', 'или', 'or',

    # --- Constructors / types ---
    'новый', 'new', 'тип', 'type', 'типзнч', 'typeof',
    'массив', 'array', 'структура', 'structure', 'соответствие', 'map',
    'таблицазначений', 'valuetable', 'деревозначений', 'valuetree',
    'запрос', 'query', 'списокзначений', 'valuelist',
    'менеджервременныхтаблиц', 'temptablesmanager',
    'текстовыйдокумент', 'textdocument', 'табличныйдокумент', 'spreadsheetdocument',
    'описаниеоповещения', 'notifydescription',
    'описаниетипов', 'typedescription',
    'квалификаторыстроки', 'stringqualifiers',
    'квалификаторычисла', 'numberqualifiers',
    'квалификаторыдаты', 'datequalifiers',
    'цвет', 'color', 'шрифт', 'font',
    'описаниепередаваемогофайла', 'filetransferredescription',
    'диалогвыборафайла', 'filedialog',

    # --- Literals / constants ---
    'строка', 'string', 'число', 'number', 'дата', 'date',
    'булево', 'boolean', 'неопределено', 'undefined', 'null',
    'истина', 'true', 'ложь', 'false',

    # --- String functions ---
    'лев', 'left', 'прав', 'right', 'сред', 'mid',
    'стрдлина', 'strlen', 'стрзаменить', 'strreplace',
    'стрнайти', 'strfind', 'стрчислостроки', 'strlinecount',
    'стрполучитьстроку', 'strgetline', 'стрчисловхождений', 'stroccurrencecount',
    'стрразделить', 'strsplit', 'стрсоединить', 'strconcat',
    'стрначинаетсяс', 'strstartswith', 'стрзаканчиваетсяна', 'strendswith',
    'врег', 'upper', 'нрег', 'lower', 'трег', 'title',
    'сокрл', 'triml', 'сокрп', 'trimr', 'сокрлп', 'trimall',
    'символ', 'char', 'кодсимвола', 'charcode',
    'пустаястрока', 'isblankstring',
    'стршаблон', 'strtemplate',

    # --- Numeric functions ---
    'цел', 'int', 'окр', 'round', 'pow', 'log', 'log10',
    'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'exp', 'sqrt',
    'макс', 'max', 'мин', 'min',

    # --- Date functions ---
    'год', 'year', 'месяц', 'month', 'день', 'day',
    'час', 'hour', 'минута', 'minute', 'секунда', 'second',
    'началогода', 'begofyear', 'началоквартала', 'begofquarter',
    'началомесяца', 'begofmonth', 'началонедели', 'begofweek',
    'началодня', 'begofday', 'началочаса', 'begofhour',
    'конецгода', 'endofyear', 'конецквартала', 'endofquarter',
    'конецмесяца', 'endofmonth', 'конецнедели', 'endofweek',
    'конецдня', 'endofday', 'конецчаса', 'endofhour',
    'добавитьмесяц', 'addmonth', 'текущаядата', 'currentdate',
    'текущаядатасеанса', 'currentsessiondate',

    # --- Type checking ---
    'значениезаполнено', 'valuefilled',
    'значениетипастрока', 'valueisstringtype',

    # --- Conversion ---
    'формат', 'format', 'числостроку', 'numbertostring',
    'строкачисло', 'stringtonumber',
    'xmlстрока', 'xmlstring', 'xmlзначение', 'xmlvalue',
    'xmlтип', 'xmltype', 'xmlтипзнч', 'xmltypeof',

    # --- Interaction ---
    'сообщить', 'message', 'предупреждение', 'warning',
    'вопрос', 'question', 'оповеститьобизменении', 'notifychanged',
    'оповестить', 'notify', 'обновитьинтерфейс', 'refreshinterface',
    'покажнитьоповещениепользователя', 'showusernoti',
    'состояние', 'status',

    # --- Global context ---
    'получитьформу', 'getform', 'открытьформу', 'openform',
    'получитьобщуюформу', 'getcommonform',
    'предопределенноезначение', 'predefinedvalue',
    'текущийпользователь', 'currentuser',
    'нстр', 'nstr',
    'заполнитьзначениясвойств', 'fillpropertyvalues',
    'копироватьданныеформы', 'copyformdata',
    'данныеформывзначение', 'formdatatovalue',
    'значениевданныеформы', 'valuetoformdata',
    'реквизитформывзначение', 'formattributetovalue',
    'обработканового', 'newhandler',
    'обработкапроведения', 'postinghandler',
    'подключитьобработчикожидания', 'attachidlehandler',
    'отключитьобработчикожидания', 'detachidlehandler',

    # --- Transaction / DB ---
    'началотранзакции', 'begintransaction',
    'зафиксироватьтранзакцию', 'committransaction',
    'отменитьтранзакцию', 'rollbacktransaction',
    'транзакцияактивна', 'transactionactive',

    # --- Collections (these are method-like but global) ---
    'вставить', 'insert', 'удалить', 'delete',
    'добавить', 'add', 'очистить', 'clear',
    'записать', 'write', 'прочитать', 'read',
    'выполнить', 'execute', 'вычислить', 'eval',
    'установитьпараметр', 'setparameter',
    'скопировать', 'copy', 'найти', 'find',
    'количество', 'count', 'получить', 'get',
    'установить', 'set', 'загрузить', 'load',
    'выгрузить', 'unload',
}

# Merge verified reference data (both Russian and English variants)
_PLATFORM_FUNCTIONS |= PLATFORM_FUNCTIONS_EXTRA
_PLATFORM_FUNCTIONS |= CONSTRUCTOR_TYPES_RU   # "Новый Структура(...)" → not a call
_PLATFORM_FUNCTIONS |= CONSTRUCTOR_TYPES_EN   # "New Structure(...)" → not a call
# System enumerations are used as <Enum>.<Value> — merging them here means
# the left-hand side is not mistaken for a module call
_PLATFORM_FUNCTIONS |= SYSTEM_ENUMERATIONS_RU
_PLATFORM_FUNCTIONS |= SYSTEM_ENUMERATIONS_EN

# ============================================
# OBJECT METHOD PATTERNS (Variable.Method — NOT cross-module calls)
# ============================================

# Common 1C object methods that appear as Obj.Method()
_OBJECT_METHODS = {
    # Collection/result methods
    'добавить', 'add', 'вставить', 'insert', 'удалить', 'delete',
    'очистить', 'clear', 'количество', 'count', 'найти', 'find',
    'получить', 'get', 'установить', 'set',
    'следующий', 'next', 'выбрать', 'select', 'выбратьсрез', 'selectslice',
    'итог', 'total', 'записать', 'write', 'прочитать', 'read',
    'загрузить', 'load', 'выгрузить', 'unload', 'скопировать', 'copy',
    'свойство', 'property', 'колонки', 'columns',
    'сортировать', 'sort', 'свернуть', 'groupby',
    'выполнить', 'execute', 'записывать', 'write',

    # Message/notification object methods
    # NOTE: 'Сообщить' / 'message' kept out — they are frequently called
    # on modules (e.g. ОбщегоНазначения.Сообщить) and as global functions.
    'установитьданные', 'setdata',

    # Form methods
    'элементы', 'items', 'модифицированность', 'modified',
    'закрыть', 'close', 'открыть', 'open',
    'обновитьотображение', 'refreshdisplay',

    # Query result methods
    'выбрать', 'select', 'пустой', 'isempty',

    # String/conversion
    'строка', 'string',

    # Common chained methods
    'добавитьколонку', 'addcolumn', 'индекс', 'index',
    'отбор', 'filter', 'параметры', 'parameters',
    'текст', 'text', 'установитьтекст', 'settext',
    'получитьтело', 'getbody', 'установитьтело', 'setbody',

    # ── Ref.X(), Obj.X() platform methods (RU + EN) ──
    # These are names that are EXCLUSIVELY platform methods and would
    # never be used as a user-defined procedure name in normal 1C code.
    #
    # Rule of thumb: if a name is short/generic (Execute, Get, Start, Show, Verify,
    # UpdateHash, Name, Attributes, Clear), DO NOT include it — users often
    # name their own procedures with those. False-positives there hide real
    # cross-module calls.
    #
    # Object retrieval from reference (unambiguous platform API)
    'getobject', 'получитьобъект',
    'getobjects', 'получитьобъекты',
    # CatalogManager / DocumentManager methods (unique to 1C platform)
    'createitem', 'создатьэлемент',
    'creategroup', 'создатьгруппу',
    'createdocument', 'создатьдокумент',
    'createnode', 'создатьузел',
    'createrecordmanager', 'создатьменеджерзаписи',
    'createrecordset', 'создатьнаборзаписей',
    'createrecordkey', 'создатьключзаписи',
    'getref', 'получитьссылку',
    'emptyref', 'пустаяссылка',
    'findbycode', 'найтипокоду',
    'findbydescription', 'найтипонаименованию',
    'findbyattribute', 'найтипореквизиту',
    'findbynumber', 'найтипономеру',
    'getobjectbyid', 'получитьобъектпоидентификатору',
    'selecthierarchically', 'выбратьиерархически',
    # JSON / XML writers/readers (specific platform I/O methods)
    'writejson', 'записатьjson', 'readjson', 'прочитатьjson',
    'writexml', 'записатьxml', 'readxml', 'прочитатьxml',
    'writejsonvalue', 'writejsonobject',
    # HTTP / HTTPService (specific platform names — CamelCase uniqueness)
    'callhttpmethod', 'вызватьhttpметод',
    'getbodyasstring', 'получитьтелокакстроку',
    'getbodyasbinarydata', 'получитьтелокакдвоичныеданные',
    'setbodyfromstring', 'установитьтелоизстроки',
    'setbodyfrombinarydata', 'установитьтелоиздвоичныхданных',
    'getheader', 'получитьзаголовок',
    # Value storage / query parameters — unique names
    'setparameter', 'установитьпараметр',
    'getparameter', 'получитьпараметр',
    # TabularSection / ValueTable methods that are unique to 1C
    'unloadcolumn', 'выгрузитьколонку',
    'loadcolumn', 'загрузитьколонку',
    'findrows', 'найтистроки',     # ValueTable.FindRows()
    'indexof',                       # Array/ValueList.IndexOf() — RU 'индекс' is in 'collection methods' above
    # NOTE: these are intentionally LEFT OUT because they're common
    # user-defined procedure names:
    #   execute/выполнить, start/запустить, cancel/отменить,
    #   show/показать, open/открыть, close/закрыть,
    #   verify/проверить, sign/подписать,
    #   updatehash/обновитьхеш, gethash/получитьхеш,
    #   name/имя, synonym/синоним, comment/комментарий,
    #   attributes/реквизиты, forms/формы, templates/макеты,
    #   lock/заблокировать, unlock/разблокировать,
    #   beforewrite/onwrite/afterwrite/beforedelete/filling/checkfilling
    #     (event handlers — users DO define these!),
    #   size, seek, get, put, patch, post, output, put, join,
    #   ref, metadata, uuid, tostring, presentation,
    #   readline, writeline, openfile, closefile
}


def read_bsl_text(file_path: str, encoding: str = 'utf-8-sig') -> str | None:
    """Read a .bsl file's text, trying likely 1C encodings in order.
    Returns None if the file is missing or none of them decode it.
    Shared by BSLParser.parse_file and search_code's on-disk re-read
    (Этап 3 — module_text_fts is contentless, so this is how a matched
    module's line/context actually gets recovered)."""
    path = Path(file_path)
    if not path.exists():
        return None
    for enc in [encoding, 'utf-8-sig', 'utf-8', 'cp1251']:
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


def _parse_param_names(params: str) -> set[str]:
    """Extract just the identifier names from a raw parameter-list string
    (e.g. "Знач ПечатнаяФорма, ДопПараметры = Неопределено" ->
    {"печатнаяформа", "допараметры"}) — lowercased, ready to seed a
    procedure's constructed_vars set. A parameter can only ever be a local
    variable in 1C (there's no way to pass a common module as an argument),
    so treating X.Method(...) as a variable call whenever X is one of this
    procedure's own parameters is exactly as safe as the Новый-assignment
    case (Этап 7: catches the `ПечатнаяФорма.Вывести(...)`-style false
    positives — ПечатнаяФорма arrives as a parameter, never constructed
    locally, so _RE_CONSTRUCTOR_ASSIGN alone can't see it)."""
    names = set()
    for part in params.split(','):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r'^(?:Знач|ByVal)\s+', '', part, flags=re.IGNORECASE)
        name = part.split('=')[0].strip()
        m = re.match(r'^([А-Яа-яA-Za-z][А-Яа-яA-Za-z0-9_]*)$', name)
        if m:
            names.add(m.group(1).lower())
    return names


def _form_context_vars(directive: str, form_attributes: set[str]) -> set[str]:
    """Реквизиты формы, видимые процедуре с этой директивой: все, кроме
    процедур БезКонтекста/NoContext (там контекста формы нет)."""
    if not form_attributes:
        return set()
    low = directive.lower()
    if 'безконтекста' in low or 'nocontext' in low:
        return set()
    return form_attributes


class BSLParser:
    """Parses .bsl file content into procedures and calls.

    known_modules/known_objects come from a project-wide Pass 1 (see
    Indexer.scan_known_names / index_bsl): a directory listing of every
    common module and metadata object in the project, gathered before any
    file is parsed for calls. Pass 2 (this class) uses that to classify
    "X.Y(" calls by fact — X is a real common module, a real metadata
    manager's object, or neither — instead of guessing from naming
    heuristics (D1). Unknown names fall back to a single last-resort
    signal: a lowercase-initial identifier reads as a local variable.
    """

    def __init__(self, known_modules: set[str] | None = None,
                 known_objects: dict[str, set[str]] | None = None,
                 known_factory_functions: dict[str, set[str]] | None = None):
        self.known_modules = known_modules or set()
        self.known_objects = known_objects or {}
        # {module_name_lower: {exported_factory_function_name_lower, ...}}
        # — project-wide pre-pass result, see ParseResult.factory_functions.
        self.known_factory_functions = known_factory_functions or {}

    def parse_file(self, file_path: str, encoding: str = 'utf-8-sig',
                   form_attributes: set[str] | None = None) -> ParseResult:
        """Parse a .bsl file."""
        content = read_bsl_text(file_path, encoding)
        if content is None:
            return ParseResult(has_errors=True)
        return self.parse(content, form_attributes=form_attributes)

    def parse(self, content: str, form_attributes: set[str] | None = None) -> ParseResult:
        """Parse BSL source code string.

        form_attributes — lower-case имена реквизитов формы из её Form.xml
        (см. xml_walker.read_form_attributes), только для модуля формы.
        Реквизит виден во всех процедурах с контекстом формы как обычная
        переменная, поэтому засевается в их множество известных
        переменных — как параметр процедуры. Реквизит формы перекрывает
        одноимённый общий модуль (так работает сама платформа: контекст
        формы приоритетнее глобального), поэтому в таких процедурах
        `Имя.Метод(` не станет вызовом модуля, даже если такой модуль есть
        в known_modules. В процедурах &НаСервереБезКонтекста контекста
        формы нет — там имя по-прежнему может быть только модулем.
        """
        form_attributes = form_attributes or set()
        lines = content.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        result = ParseResult(line_count=len(lines), content=content)

        procedures = []
        synthetic_calls = []   # override edges from &Вместо/&После/... directives
        pending = []            # (proc, stripped_line, line_num) — call extraction deferred
                                 # to a second pass, once every procedure in this module
        # id(proc) -> {lowercased variable names constructed via "= Новый X("
        # in that procedure} — see _RE_CONSTRUCTOR_ASSIGN above.
        constructed_vars: dict[int, set[str]] = {}
                                 # is known (D2: local calls need the full local name set)
        # id(proc) -> {lowercased alias var name: lowercased real module
        # name} — see _RE_BARE_ALIAS_ASSIGN / _RE_DYNAMIC_MODULE_LOAD above.
        module_aliases: dict[int, dict[str, str]] = {}

        # {lowercased name} declared via a module-level "Перем X;" (always
        # before any Процедура/Функция in valid BSL) — a var assigned in
        # ONE procedure and used via .Method(...) in ANOTHER falls outside
        # every per-procedure set above by construction, so it needs its
        # own module-wide tracking, merged into every procedure's view at
        # call-extraction time below.
        module_level_var_names: set[str] = set()
        module_level_constructed: set[str] = set()
        module_level_aliases: dict[str, str] = {}

        current_directive = ''
        current_intercept_keyword = ''
        current_intercept_target = ''
        current_proc: ProcedureInfo | None = None
        in_multiline_string = False

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            if not stripped:
                continue

            # Track multi-line string literals (SQL queries etc.)
            # In 1C, multi-line strings continue with | at start of line
            if in_multiline_string:
                if stripped.startswith('|'):
                    # A "|"-continuation line is always query text, never
                    # real BSL code — but it can ALSO be the line that
                    # closes the string (the extremely common
                    # `|...";` convention). Missing that closing quote here
                    # left in_multiline_string stuck True indefinitely,
                    # silently swallowing following code lines as "still
                    # inside the string" until some later line happened to
                    # reset it — and reopening on the NEXT real string
                    # start left query keywords on ITS "|" lines exposed as
                    # normal code (found via diagnose_index: ЗНАЧЕНИЕ,
                    # ЕСТЬNULL etc. turning into thousands of bogus calls).
                    if stripped.count('"') % 2 == 1:
                        in_multiline_string = False
                    continue  # Skip SQL query line entirely either way
                if stripped.startswith('//'):
                    # A bare comment line nested inside an open multi-line
                    # string (e.g. the "//++ НЕ УТ" / "//-- НЕ УТ" merge-
                    # annotation convention seen in real ERP query text) —
                    # real 1C tolerates these without closing the string.
                    # Found via Этап 7's diagnose_index: without this,
                    # everything after such a line was misparsed as normal
                    # code until the string's real closing quote, turning
                    # query keywords (ЗНАЧЕНИЕ, ЕСТЬNULL, СУММА...) into
                    # thousands of bogus unresolved "calls".
                    continue
                # Check if string closes on this line
                # Count quotes — odd means string is still open
                quote_count = stripped.count('"')
                if quote_count % 2 == 1:
                    in_multiline_string = False
                    continue  # This line closes the string, skip it
                elif quote_count == 0:
                    # No quotes, might be continuation or end
                    in_multiline_string = False
                    # Fall through to process normally
                else:
                    continue  # Even quotes, string still open/closed/reopened

            # Detect start of multi-line string
            # A line ending with unclosed quote (odd number of quotes after removing comments)
            clean_for_quote = self._remove_comments(stripped)
            if clean_for_quote.count('"') % 2 == 1:
                # Unclosed string — next lines may be continuations
                in_multiline_string = True
                # Still process this line for calls (the part before the string)

            # Skip full-line comments
            if stripped.startswith('//'):
                continue

            # Module-level "Перем X, Y Экспорт;" — only before the first
            # Процедура/Функция (current_proc is None); a same-shaped line
            # inside a procedure body declares an ordinary local instead.
            if current_proc is None:
                var_decl_match = _RE_MODULE_VAR_DECL.match(stripped)
                if var_decl_match:
                    for name in var_decl_match.group(1).split(','):
                        name = name.strip().split()[0].strip() if name.strip() else ''
                        if name and name.lower() not in ('экспорт', 'export'):
                            module_level_var_names.add(name.lower())
                    continue

            # Directive: &НаСервере / &НаКлиенте / &НаСервереБезКонтекста
            dir_match = _RE_DIRECTIVE.match(stripped)
            if dir_match:
                current_directive = f'&{dir_match.group(1)}'
                continue

            # Extension override directive: &Вместо("X") / &After("X") / ...
            # (D9) — must be checked before call extraction ever sees this
            # line, or "Вместо(" regex-matches as a bogus local call.
            ext_dir_match = _RE_EXT_DIRECTIVE.match(stripped)
            if ext_dir_match:
                current_intercept_keyword = ext_dir_match.group(1)
                current_intercept_target = ext_dir_match.group(2)
                continue

            # Procedure/Function start
            proc_match = _RE_PROC_START.match(stripped)
            if proc_match:
                if current_proc:
                    current_proc.end_line = line_num - 1
                    current_proc.line_count = current_proc.end_line - current_proc.start_line + 1
                    procedures.append(current_proc)
                    if current_proc.intercepts:
                        synthetic_calls.append(self._override_call(current_proc))

                is_async = bool(proc_match.group(1))
                kind_raw = proc_match.group(2)
                name = proc_match.group(3)
                params = proc_match.group(4).strip()
                is_export = bool(proc_match.group(5))
                kind = 'Функция' if kind_raw.lower() in ('функция', 'function') else 'Процедура'

                directive_str, intercepts = self._consume_directives(
                    current_directive, current_intercept_keyword, current_intercept_target)

                current_proc = ProcedureInfo(
                    name=name, kind=kind, is_export=is_export,
                    start_line=line_num, params=params,
                    directive=directive_str, signature=stripped,
                    intercepts=intercepts, is_async=is_async,
                )
                constructed_vars[id(current_proc)] = (
                    _parse_param_names(params) | _form_context_vars(directive_str, form_attributes))
                current_directive = ''
                current_intercept_keyword = ''
                current_intercept_target = ''
                continue

            # Multi-line signature: "Функция Name(Param1," without closing ")"
            if not proc_match:
                ml_match = _RE_PROC_START_MULTILINE.match(stripped)
                if ml_match:
                    # Accumulate lines until we find closing ")"
                    combined = stripped
                    peek = line_num
                    while peek < len(lines):
                        next_line = lines[peek].strip()
                        combined += ' ' + next_line
                        peek += 1
                        if ')' in next_line:
                            break

                    # Now try matching the combined signature
                    full_match = _RE_PROC_START.match(combined)
                    if full_match:
                        if current_proc:
                            current_proc.end_line = line_num - 1
                            current_proc.line_count = current_proc.end_line - current_proc.start_line + 1
                            procedures.append(current_proc)
                            if current_proc.intercepts:
                                synthetic_calls.append(self._override_call(current_proc))

                        is_async = bool(full_match.group(1))
                        kind_raw = full_match.group(2)
                        name = full_match.group(3)
                        params = full_match.group(4).strip()
                        is_export = bool(full_match.group(5))
                        kind = 'Функция' if kind_raw.lower() in ('функция', 'function') else 'Процедура'

                        directive_str, intercepts = self._consume_directives(
                            current_directive, current_intercept_keyword, current_intercept_target)

                        async_prefix = f'{full_match.group(1)} ' if is_async else ''
                        current_proc = ProcedureInfo(
                            name=name, kind=kind, is_export=is_export,
                            start_line=line_num, params=params,
                            directive=directive_str,
                            signature=f'{async_prefix}{kind_raw} {name}({params})' + (' Экспорт' if is_export else ''),
                            intercepts=intercepts, is_async=is_async,
                        )
                        constructed_vars[id(current_proc)] = (
                            _parse_param_names(params) | _form_context_vars(directive_str, form_attributes))
                        current_directive = ''
                        current_intercept_keyword = ''
                        current_intercept_target = ''
                        continue

            # Procedure/Function end
            end_match = _RE_PROC_END.match(stripped)
            if end_match:
                if current_proc:
                    current_proc.end_line = line_num
                    current_proc.line_count = current_proc.end_line - current_proc.start_line + 1
                    procedures.append(current_proc)
                    if current_proc.intercepts:
                        synthetic_calls.append(self._override_call(current_proc))
                    current_proc = None
                current_directive = ''
                current_intercept_keyword = ''
                current_intercept_target = ''
                continue

            # Body line — call extraction is deferred to the second pass
            # below, once the full set of this module's procedure names
            # is known (D2).
            if current_proc:
                pending.append((current_proc, stripped, line_num))
                clean_for_ctor = self._remove_strings_and_comments(stripped)
                proc_vars = constructed_vars.setdefault(id(current_proc), set())
                for ctor_m in _RE_CONSTRUCTOR_ASSIGN.finditer(clean_for_ctor):
                    var_name = ctor_m.group(1).lower()
                    proc_vars.add(var_name)
                    if var_name in module_level_var_names:
                        module_level_constructed.add(var_name)
                for chain_m in _RE_CHAIN_ASSIGN.finditer(clean_for_ctor):
                    if chain_m.group(2).lower() in proc_vars:
                        var_name = chain_m.group(1).lower()
                        proc_vars.add(var_name)
                        if var_name in module_level_var_names:
                            module_level_constructed.add(var_name)
                for mgr_assign_m in _RE_MANAGER_CALL_ASSIGN.finditer(clean_for_ctor):
                    if mgr_assign_m.group(2).lower() in _METADATA_MANAGERS:
                        var_name = mgr_assign_m.group(1).lower()
                        proc_vars.add(var_name)
                        if var_name in module_level_var_names:
                            module_level_constructed.add(var_name)
                for idx_assign_m in _RE_INDEXED_ASSIGN.finditer(clean_for_ctor):
                    var_name = idx_assign_m.group(1).lower()
                    proc_vars.add(var_name)
                    if var_name in module_level_var_names:
                        module_level_constructed.add(var_name)

                proc_aliases = module_aliases.setdefault(id(current_proc), {})
                # Unlike every other regex here, this one needs to read the
                # module-name STRING LITERAL's content ("УправлениеДоступом"),
                # so it must run against comments-only-stripped text — not
                # clean_for_ctor, which blanks string content to spaces.
                clean_comments_only = self._remove_comments(stripped)
                for load_m in _RE_DYNAMIC_MODULE_LOAD.finditer(clean_comments_only):
                    # Preserve the string literal's own casing — this is the
                    # real module name as it appears in the export, same as
                    # for a plain (non-aliased) Module.Method( call below.
                    var_name = load_m.group(1).lower()
                    proc_aliases[var_name] = load_m.group(2)
                    if var_name in module_level_var_names:
                        module_level_aliases[var_name] = load_m.group(2)
                for alias_m in _RE_BARE_ALIAS_ASSIGN.finditer(clean_for_ctor):
                    target = alias_m.group(2)
                    if target.lower() in self.known_modules:
                        var_name = alias_m.group(1).lower()
                        proc_aliases[var_name] = target
                        if var_name in module_level_var_names:
                            module_level_aliases[var_name] = target

        if current_proc:
            current_proc.end_line = len(lines)
            current_proc.line_count = current_proc.end_line - current_proc.start_line + 1
            procedures.append(current_proc)
            if current_proc.intercepts:
                synthetic_calls.append(self._override_call(current_proc))

        # Factory-function inference (one level — see _RE_RETURN_NEW's
        # docstring): find which of this module's own functions return a
        # value they built themselves, then mark every "Var =
        # FactoryFunction(...)" assignment as constructing Var too.
        lines_by_proc: dict[int, list[str]] = {}
        for p_proc, p_line, _ in pending:
            lines_by_proc.setdefault(id(p_proc), []).append(p_line)

        constructor_like_functions: set[str] = set()   # all — used for local propagation below
        exported_factory_functions: set[str] = set()   # exported-only — exposed to other modules
        for proc in procedures:
            if proc.kind != 'Функция':
                continue
            proc_vars = constructed_vars.get(id(proc)) or set()
            for p_line in lines_by_proc.get(id(proc), []):
                clean_ret = self._remove_strings_and_comments(p_line).strip()
                is_factory = bool(_RE_RETURN_NEW.match(clean_ret))
                if not is_factory:
                    ret_m = _RE_RETURN_BARE_VAR.match(clean_ret)
                    is_factory = bool(ret_m and ret_m.group(1).lower() in proc_vars)
                if not is_factory:
                    mgr_m = _RE_RETURN_MANAGER_CALL.match(clean_ret)
                    is_factory = bool(mgr_m and mgr_m.group(1).lower() in _METADATA_MANAGERS)
                if is_factory:
                    constructor_like_functions.add(proc.name.lower())
                    if proc.is_export:
                        exported_factory_functions.add(proc.name.lower())
                    break
        result.factory_functions = exported_factory_functions

        if constructor_like_functions or self.known_factory_functions:
            for proc, stripped_line, _ in pending:
                proc_vars = constructed_vars.setdefault(id(proc), set())
                clean_bare = self._remove_strings_and_comments(stripped_line)
                for bare_m in _RE_BARE_FUNC_CALL_ASSIGN.finditer(clean_bare):
                    func_name = bare_m.group(2).lower()
                    if func_name in constructor_like_functions:
                        var_name = bare_m.group(1).lower()
                        proc_vars.add(var_name)
                        if var_name in module_level_var_names:
                            module_level_constructed.add(var_name)
                # Re-check the chain pattern too, in case this same line
                # (or an earlier one in this procedure) just gained a
                # newly-discovered var above — same regex also catches
                # cross-module factory calls (Var = ОбщегоНазначения.
                # НовыйПустойЛист()), checked against the project-wide
                # known_factory_functions pre-pass result instead of a
                # locally-tracked variable.
                for chain_m in _RE_CHAIN_ASSIGN.finditer(clean_bare):
                    owner = chain_m.group(2).lower()
                    var_name = chain_m.group(1).lower()
                    if owner in proc_vars:
                        proc_vars.add(var_name)
                        if var_name in module_level_var_names:
                            module_level_constructed.add(var_name)
                    elif chain_m.group(3).lower() in self.known_factory_functions.get(owner, ()):
                        proc_vars.add(var_name)
                        if var_name in module_level_var_names:
                            module_level_constructed.add(var_name)

        # Second pass: now that every procedure declared in this module is
        # known, bare calls whose name collides with the platform stoplist
        # can be told apart from real local calls (D2).
        local_proc_names = {p.name.lower() for p in procedures}
        calls = list(synthetic_calls)
        for proc, stripped, line_num in pending:
            proc_vars = constructed_vars.get(id(proc)) or set()
            proc_aliases = module_aliases.get(id(proc)) or {}
            # Module-level Перем vars are visible in every procedure, not
            # just the one that happened to construct/alias them — merge
            # last so a per-procedure shadow (a local of the same name)
            # would still win, though that's rare in practice.
            if module_level_constructed:
                proc_vars = proc_vars | module_level_constructed
            if module_level_aliases:
                proc_aliases = {**module_level_aliases, **proc_aliases}
            calls.extend(self._extract_calls(stripped, line_num, proc, local_proc_names,
                                              proc_vars, proc_aliases))

        result.procedures = procedures
        result.calls = calls
        return result

    def _consume_directives(self, current_directive: str,
                             intercept_keyword: str, intercept_target: str) -> tuple[str, str]:
        """Combine a pending compilation directive (&НаСервере) and/or
        extension override directive (&Вместо("X")) into the procedure's
        stored directive string and intercept target."""
        parts = []
        if current_directive:
            parts.append(current_directive)
        if intercept_target:
            parts.append(f'&{intercept_keyword}:{intercept_target}')
        return ' '.join(parts), intercept_target

    def _override_call(self, proc: ProcedureInfo) -> CallInfo:
        """Synthetic edge for an extension interceptor procedure → the
        procedure it intercepts (D9/1.4). callee_module is left blank here;
        the indexer fills it in with this module's own dotted name, since
        an override always targets a same-named module in another source."""
        return CallInfo(
            callee_name=proc.intercepts,
            callee_module='',
            callee_proc=proc.intercepts,
            line=proc.start_line,
            context=proc.directive,
            callee_kind='override',
        )

    def _extract_calls(self, line: str, line_num: int,
                        current_proc: ProcedureInfo,
                        local_proc_names: set[str],
                        constructed_vars: set[str] | None = None,
                        module_aliases: dict[str, str] | None = None) -> list[CallInfo]:
        """Extract meaningful calls from a line (excluding noise).

        constructed_vars: lowercased names this procedure assigned via
        "= Новый X(" — a dotted call on one of them is a variable method
        call, not a module call, known by fact (see _RE_CONSTRUCTOR_ASSIGN).

        module_aliases: {lowercased var name: real module name, in its
        original casing} for this procedure — a var assigned the module's
        own bare name, or
        via the ОбщегоНазначения.ОбщийМодуль("X") BSP idiom (see
        _RE_BARE_ALIAS_ASSIGN / _RE_DYNAMIC_MODULE_LOAD). Substituted in
        before classification so Var.Method(...) resolves as if
        РеальныйМодуль.Method(...) had been written directly.
        """
        constructed_vars = constructed_vars or set()
        module_aliases = module_aliases or {}
        calls = []
        clean = self._remove_strings_and_comments(line)
        matched_positions = set()  # Track positions to avoid duplicates

        # 3-part manager calls: Справочники.Организации.МетодМенеджера(
        for m in _RE_MANAGER_CALL.finditer(clean):
            manager_type = m.group(1)   # Справочники
            obj_name = m.group(2)       # Организации
            method_name = m.group(3)    # СоздатьОрганизацию

            singular = _METADATA_MANAGERS.get(manager_type.lower())
            if not singular:
                continue

            # Skip standard platform manager methods (not user code)
            if method_name.lower() in _PLATFORM_MANAGER_METHODS:
                continue

            # Validate the object name against Pass 1's fact base, when we
            # actually have data for this kind — an unknown obj_name is
            # more likely a local variable that happens to look like
            # "Справочники.Something.Method(" than a real manager call.
            bucket = self.known_objects.get(singular)
            if bucket and obj_name.lower() not in bucket:
                continue

            # Build module name: Справочник.Организации.МодульМенеджера
            module_full = f'{singular}.{obj_name}.МодульМенеджера'
            calls.append(CallInfo(
                callee_name=f'{module_full}.{method_name}',
                callee_module=module_full,
                callee_proc=method_name,
                line=line_num,
                context=line.strip()[:120],
                callee_kind='manager',
            ))
            matched_positions.add(m.start())

        # 2-part cross-module calls: Module.Method(
        for m in _RE_CROSS_CALL.finditer(clean):
            # Skip if already captured as 3-part call
            if m.start() in matched_positions or any(m.start() > p and m.start() < p + 50 for p in matched_positions):
                continue

            module_name = m.group(1)
            method_name = m.group(2)

            # Resolve a module alias first (see module_aliases docstring
            # above) — takes priority over every other check below, since
            # it redirects to the real module rather than just suppressing.
            aliased = module_aliases.get(module_name.lower())
            if aliased:
                module_name = aliased

            # Skip if this exact name was constructed with "= Новый X(" in
            # this procedure — known by fact to be a variable, not a
            # module, so this is precise regardless of method name (see
            # _RE_CONSTRUCTOR_ASSIGN; checked first since it's a stronger
            # signal than the name-based heuristics below).
            if module_name.lower() in constructed_vars:
                continue

            # Skip if method is a known object method
            if method_name.lower() in _OBJECT_METHODS:
                continue

            # Skip system enumerations: ВидДвиженияНакопления.Приход
            if module_name.lower() in SYSTEM_ENUMERATIONS_RU:
                continue

            # Skip short names (< 3 chars) — likely abbreviations for local vars
            if len(module_name) <= 2:
                continue

            # Skip platform global objects used with dot notation
            if module_name.lower() in _PLATFORM_FUNCTIONS:
                continue

            # D1: classify by fact — a known common module is a real call
            # regardless of what its name starts with. An unknown name
            # falls back to the one heuristic the plan keeps: lowercase
            # first letter reads as a local variable, not a module.
            if module_name.lower() not in self.known_modules and module_name[0].islower():
                continue

            calls.append(CallInfo(
                callee_name=f'{module_name}.{method_name}',
                callee_module=module_name,
                callee_proc=method_name,
                line=line_num,
                context=line.strip()[:120],
                callee_kind='common_module',
            ))

        # Local calls: Method(
        for m in _RE_LOCAL_CALL.finditer(clean):
            func_name = m.group(1)
            low = func_name.lower()

            # ПродолжитьВызов()/ProceedWithCall() inside a &Вместо/&Around
            # interceptor — redirect to the intercepted procedure (D9/1.4)
            # instead of leaving a dangling call to a nonexistent name.
            if low in ('продолжитьвызов', 'proceedwithcall') and current_proc.intercepts:
                if not any(c.callee_proc == current_proc.intercepts and c.line == line_num
                           for c in calls):
                    calls.append(CallInfo(
                        callee_name=current_proc.intercepts,
                        callee_module='',
                        callee_proc=current_proc.intercepts,
                        line=line_num,
                        context=line.strip()[:120],
                        callee_kind='override',
                    ))
                continue

            # Skip if already captured as cross-module
            if any(c.callee_proc == func_name and c.line == line_num for c in calls):
                continue

            # Skip platform built-in functions — UNLESS this exact name is
            # also declared as a procedure in the current module (D2): a
            # stoplist name like ПроверитьЗаполнение is legitimately a
            # user-defined procedure in plenty of BSP/ERP modules.
            if low in _PLATFORM_FUNCTIONS and low not in local_proc_names:
                continue

            # Skip current procedure (self-call)
            if func_name == current_proc.name:
                continue

            # Skip very short names (likely SQL or noise)
            if len(func_name) <= 1:
                continue

            calls.append(CallInfo(
                callee_name=func_name,
                callee_module='',
                callee_proc=func_name,
                line=line_num,
                context=line.strip()[:120],
                callee_kind='local',
            ))

        return calls

    def _remove_comments(self, line: str) -> str:
        """Remove // comments from line."""
        in_str = False
        for i, ch in enumerate(line):
            if ch == '"':
                in_str = not in_str
            elif ch == '/' and i + 1 < len(line) and line[i + 1] == '/' and not in_str:
                return line[:i]
        return line

    def _remove_strings_and_comments(self, line: str) -> str:
        """Remove string literals and comments for call detection."""
        # Remove comments first
        line = self._remove_comments(line)

        # Remove string literals
        result = []
        in_str = False
        for ch in line:
            if ch == '"':
                in_str = not in_str
                result.append(' ')
            elif in_str:
                result.append(' ')
            else:
                result.append(ch)
        return ''.join(result)
