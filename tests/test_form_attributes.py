"""
Реквизиты формы как источник «известных не-модулей» (Этап 7, остаток).

После всех правок Этапа 7 почти весь остаток нерезолвленных вызовов на
реальной ERP — переменные, которые вообще не присваиваются в BSL: это
реквизиты формы, объявленные в Ext/Form.xml (`<Attribute name="...">`) и
видимые во всех процедурах модуля формы. `КомпоновщикНастроек.
ПолучитьНастройки()`, `ТабДок.ПолучитьОбласть()`, `РазделыОтчета.
ПолучитьЭлементы()` и т.п. записывались как вызовы несуществующих общих
модулей.

Имя реквизита формы по факту не может быть общим модулем в контексте
формы — реквизиты формы перекрывают глобальный контекст. Исключение —
процедуры &НаСервереБезКонтекста: там контекста формы нет, и то же имя
честно указывает на общий модуль.
"""

import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.xml_walker import XMLWalker, read_form_attributes

FIXTURES = Path(__file__).parent / 'fixtures'

_FORM_NS = ('xmlns="http://v8.1c.ru/8.3/xcf/logform" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema"')


def _form_xml(attributes: str, extra: str = '') -> str:
    return (f'﻿<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Form {_FORM_NS} version="2.20">\n{extra}'
            f'\t<Attributes>\n{attributes}\t</Attributes>\n</Form>\n')


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


# ---------------------------------------------------------------------------
# read_form_attributes / XMLWalker
# ---------------------------------------------------------------------------

def test_fixture_forms_ru_and_en_expose_their_attributes():
    walker = XMLWalker()
    ru = {m.form_name: read_form_attributes(m.form_xml_path)
          for m in walker.walk(FIXTURES / 'xml_ru') if m.form_xml_path}
    en = {m.form_name: read_form_attributes(m.form_xml_path)
          for m in walker.walk(FIXTURES / 'xml_en') if m.form_xml_path}
    assert ru['ФормаЭлемента'] == {'объект'}
    assert 'список' in ru['ФормаСписка']
    assert en['ItemForm'] == {'object', 'person'}


def test_non_form_modules_have_no_form_xml_path():
    for m in XMLWalker().walk(FIXTURES / 'xml_ru'):
        if m.module_type != 'МодульФормы':
            assert m.form_xml_path == '', m.full_name


def test_only_top_level_attributes_not_columns_or_parameters():
    tmp = Path(tempfile.mkdtemp())
    try:
        form = tmp / 'Form.xml'
        _write(form, _form_xml(
            '\t\t<Attribute name="ТабДок" id="1"/>\n'
            '\t\t<Attribute name="Товары" id="2">\n'
            '\t\t\t<Columns>\n'
            '\t\t\t\t<Column name="Номенклатура" id="1"/>\n'
            '\t\t\t</Columns>\n'
            '\t\t</Attribute>\n',
            extra='\t<Parameters>\n\t\t<Parameter name="Ключ"/>\n\t</Parameters>\n'))
        assert read_form_attributes(form) == {'табдок', 'товары'}
    finally:
        shutil.rmtree(tmp)


def test_borrowed_extension_form_includes_base_form_attributes():
    tmp = Path(tempfile.mkdtemp())
    try:
        form = tmp / 'Form.xml'
        _write(form, _form_xml(
            '\t\t<Attribute name="РасшРеквизит" id="1000001"/>\n',
            extra=('\t<BaseForm version="2.20">\n'
                   '\t\t<Attributes>\n'
                   '\t\t\t<Attribute name="КомпоновщикНастроек" id="1"/>\n'
                   '\t\t</Attributes>\n'
                   '\t</BaseForm>\n')))
        assert read_form_attributes(form) == {'расшреквизит', 'компоновщикнастроек'}
    finally:
        shutil.rmtree(tmp)


def test_missing_or_broken_form_xml_gives_empty_set():
    tmp = Path(tempfile.mkdtemp())
    try:
        broken = tmp / 'Form.xml'
        _write(broken, '<Form><Attributes><Attribute name="X"')
        assert read_form_attributes(broken) == set()
        assert read_form_attributes(tmp / 'nope.xml') == set()
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# BSLParser(form_attributes=...)
# ---------------------------------------------------------------------------

from src.core.bsl_parser import BSLParser  # noqa: E402


def _calls(result) -> set[tuple[str, str]]:
    return {(c.callee_module, c.callee_proc) for c in result.calls}


_FORM_CODE = '''
&НаСервере
Процедура СформироватьНаСервере()
	Настройки = КомпоновщикНастроек.ПолучитьНастройки();
	Область = ТабДок.ПолучитьОбласть("Шапка");
	ОбщегоНазначения.СообщитьПользователю("готово");
КонецПроцедуры

&НаКлиенте
Процедура РазделыПриАктивизацииСтроки(Элемент)
	Строки = РазделыОтчета.ПолучитьЭлементы();
КонецПроцедуры
'''


def test_form_attribute_method_call_is_not_a_module_call():
    parser = BSLParser(known_modules={'общегоназначения'})
    calls = _calls(parser.parse(
        _FORM_CODE, form_attributes={'компоновщикнастроек', 'табдок', 'разделыотчета'}))
    assert ('КомпоновщикНастроек', 'ПолучитьНастройки') not in calls
    assert ('ТабДок', 'ПолучитьОбласть') not in calls
    assert ('РазделыОтчета', 'ПолучитьЭлементы') not in calls
    # Настоящий общий модуль в том же модуле формы по-прежнему даёт вызов.
    assert ('ОбщегоНазначения', 'СообщитьПользователю') in calls


def test_without_form_attributes_behaviour_is_unchanged():
    """Негативный контроль: без Form.xml (или для обычного модуля) те же
    строки дают прежние «фантомные» вызовы — поведение не менялось."""
    calls = _calls(BSLParser(known_modules={'общегоназначения'}).parse(_FORM_CODE))
    assert ('КомпоновщикНастроек', 'ПолучитьНастройки') in calls
    assert ('ТабДок', 'ПолучитьОбласть') in calls


def test_form_attribute_names_are_case_insensitive():
    code = '''
Процедура Тест()
	табдок.ПолучитьОбласть("Шапка");
	TABDOC.GetArea("Header");
КонецПроцедуры
'''
    calls = _calls(BSLParser(known_modules={'tabdoc'}).parse(
        code, form_attributes={'табдок', 'tabdoc'}))
    assert not any(m.lower() in ('табдок', 'tabdoc') for m, _ in calls), calls


def test_chain_from_form_attribute_is_a_variable_too():
    code = '''
&НаСервере
Процедура Тест()
	Настройки = КомпоновщикНастроек.ПолучитьНастройки();
	Настройки.Отбор.Элементы.Очистить();
	Структура = Настройки.ПолучитьСтруктуру();
КонецПроцедуры
'''
    calls = _calls(BSLParser().parse(code, form_attributes={'компоновщикнастроек'}))
    assert ('Настройки', 'ПолучитьСтруктуру') not in calls, calls


def test_form_attribute_shadows_same_named_common_module():
    """Реквизит формы с именем реального общего модуля: в процедуре с
    контекстом формы платформа видит реквизит, а не модуль, — вызова модуля
    нет (иначе ребро молча вело бы не туда). В &НаСервереБезКонтекста
    контекста формы нет — там это честный вызов общего модуля."""
    code = '''
&НаКлиенте
Процедура НаКлиенте()
	Печать.Вывести();
	Копия = Печать;
	Копия.Вывести();
КонецПроцедуры

&НаСервереБезКонтекста
Процедура БезКонтекста()
	Печать.Вывести();
КонецПроцедуры

&AtServerNoContext
Procedure NoContext()
	Печать.Вывести();
EndProcedure
'''
    result = BSLParser(known_modules={'печать'}).parse(code, form_attributes={'печать'})
    module_calls = sorted(c.line for c in result.calls
                          if c.callee_kind == 'common_module' and c.callee_module == 'Печать')
    no_context_lines = [i for i, l in enumerate(code.split('\n'), 1)
                        if l.strip() == 'Печать.Вывести();'][1:]
    assert module_calls == no_context_lines, (module_calls, no_context_lines)


def test_english_directives_keep_form_context():
    code = '''
&AtClient
Procedure OnOpen(Cancel)
	SettingsComposer.GetSettings();
EndProcedure

&AtServer
Procedure OnCreateAtServer(Cancel, StandardProcessing)
	SettingsComposer.LoadSettings(Settings);
EndProcedure
'''
    calls = _calls(BSLParser().parse(code, form_attributes={'settingscomposer'}))
    assert not any(m == 'SettingsComposer' for m, _ in calls), calls
