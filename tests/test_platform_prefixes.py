"""
Платформенные левые части двухчастного `Имя.Метод(`, которые не могут быть
общим модулем (Этап 7): менеджеры-коллекции (`Документы.ТипВсеСсылки()`),
свойства управляемой формы (`Элементы`, `Параметры`...) и объекты
глобального контекста (`ОбработкаОшибок`). Имя из known_modules — всегда
модуль: факт важнее списка.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.bsl_parser import BSLParser

KNOWN = {'общегоназначения'}


def _calls(code: str, known_modules=KNOWN) -> set[tuple[str, str, str]]:
    result = BSLParser(known_modules=known_modules,
                       known_objects={'Справочник': {'номенклатура'}}).parse(code)
    return {(c.callee_kind, c.callee_module, c.callee_proc) for c in result.calls}


def test_manager_collection_method_is_not_a_module_call():
    code = '''
Процедура Тест()
	Типы = Документы.ТипВсеСсылки();
	Типы = Справочники.ТипВсеСсылки();
	Типы = Catalogs.AllRefsType();
	Найдено = РегистрыСведений.НайтиПоИмени("X");
КонецПроцедуры
'''
    calls = _calls(code)
    assert not any(kind == 'common_module' for kind, _, _ in calls), calls


def test_three_part_manager_call_unchanged():
    code = '''
Процедура Тест()
	Справочники.Номенклатура.ЗаполнитьЦены(Ссылка);
	Catalogs.Номенклатура.ЗаполнитьЦены(Ссылка);
КонецПроцедуры
'''
    calls = _calls(code)
    assert ('manager', 'Справочник.Номенклатура.МодульМенеджера', 'ЗаполнитьЦены') in calls, calls


def test_form_context_identifiers_ru_and_en():
    code = '''
&НаКлиенте
Процедура Тест()
	Элементы.Переместить(Элемент, Группа);
	ВладелецФормы.ОбновитьДанныеФормы();
	Команды.Удалить(Команда);
	КоманднаяПанель.ПодчиненныеЭлементы.Очистить();
	КомандныйИнтерфейс.ПанельНавигации.Добавить(Ссылка);
	Параметры.ЗаполнитьПоУмолчанию();
	Items.Move(Item, Group);
	FormOwner.RefreshFormData();
	Commands.Delete(Command);
	Parameters.FillDefaults();
КонецПроцедуры
'''
    calls = _calls(code)
    assert not any(kind == 'common_module' for kind, _, _ in calls), calls


def test_error_processing_global_object():
    code = '''
Процедура Тест()
	Попытка
	Исключение
		Текст = ОбработкаОшибок.ПодробноеПредставлениеОшибки(ИнформацияОбОшибке());
		Text = ErrorProcessing.DetailErrorDescription(ErrorInfo());
	КонецПопытки;
КонецПроцедуры
'''
    calls = _calls(code)
    assert not any(kind == 'common_module' for kind, _, _ in calls), calls


def test_real_common_module_calls_unaffected():
    code = '''
Процедура Тест()
	ОбщегоНазначения.СообщитьПользователю("текст");
	Типы = Документы.ТипВсеСсылки();
КонецПроцедуры
'''
    calls = _calls(code)
    assert ('common_module', 'ОбщегоНазначения', 'СообщитьПользователю') in calls


def test_known_module_with_reserved_name_wins():
    """Если в конфигурации всё же есть общий модуль с таким именем — это
    факт, и вызов остаётся вызовом модуля."""
    code = '''
Процедура Тест()
	ОбработкаОшибок.ЗаписатьВЖурнал("текст");
	ErrorProcessing.WriteToLog("text");
КонецПроцедуры
'''
    calls = _calls(code, known_modules={'обработкаошибок', 'errorprocessing'})
    assert ('common_module', 'ОбработкаОшибок', 'ЗаписатьВЖурнал') in calls, calls
    assert ('common_module', 'ErrorProcessing', 'WriteToLog') in calls, calls


def test_constructor_pointintime_and_datahashing_recognized():
    """Этап 7 (восьмой заход, bshp): "Новый МоментВремени"/"Новый
    ХешированиеДанных" числились голыми функциями, хотя это конструкторы
    (подтверждено грепом — 278 и 139 раз соответственно). Заодно
    "ПотокВПамяти" — было "ПамятьПоток" (неверный порядок слов, не
    встречается в реальном коде)."""
    code = '''
Процедура Тест()
	Момент = Новый МоментВремени(Дата, Ссылка);
	Хеш = Новый ХешированиеДанных(ХешФункцияMD5);
	Поток = Новый ПотокВПамяти;
КонецПроцедуры
'''
    calls = _calls(code)
    assert not any(kind == 'local' for kind, _, _ in calls), calls


def test_bare_form_and_global_functions_recognized():
    """Этап 7 (восьмой заход, bshp): вызовы без точки, найденные в
    реальном коде — Активизировать()/ИзменитьРеквизиты() внутри модуля
    формы, ПолучитьФайл/ПолучитьДвоичныеДанныеИзСтроки/ВопросАсинх как
    обычные глобальные функции платформы."""
    code = '''
Процедура Тест()
	Активизировать();
	ИзменитьРеквизиты(ДобавляемыеРеквизиты);
	ПолучитьФайл(Адрес, Представление, Истина);
	Данные = ПолучитьДвоичныеДанныеИзСтроки(Текст, "windows-1251");
	Ответ = Ждать ВопросАсинх(Текст, РежимДиалогаВопрос.ДаНет);
КонецПроцедуры
'''
    calls = _calls(code)
    assert not any(kind == 'local' for kind, _, _ in calls), calls
