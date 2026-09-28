"""
Методы менеджера без точки в его собственном модуле менеджера (Этап 7, остаток).

Реальный bshp (Catalogs/ИмпортируемаяПартияСАТУРН/Ext/ManagerModule.bsl):
`ПустаяСсылка().Метаданные()`. Контекст модуля менеджера — сам менеджер,
поэтому ПустаяСсылка() — его платформенный метод, а не вызов процедуры.
Источник факта — тип модуля (bsl_reference.module_context_methods), как
КомпоновщикНастроек в модуле объекта отчёта (test_object_module_context.py),
но набор имён другой и это голые вызовы, а не переменные.
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.bsl_parser import BSLParser
from src.core.bsl_reference import module_context_methods
from src.core.db import Database
from src.core.indexer import Indexer


def test_methods_depend_on_kind_and_module_type():
    catalog = module_context_methods('Справочник', 'МодульМенеджера')
    assert {'пустаяссылка', 'emptyref', 'выбрать', 'найтипокоду', 'создатьэлемент'} <= catalog
    document = module_context_methods('Документ', 'МодульМенеджера')
    assert 'найтипономеру' in document and 'найтипокоду' not in document
    assert module_context_methods('Перечисление', 'МодульМенеджера') == {'пустаяссылка', 'emptyref'}
    assert 'срезпоследних' in module_context_methods('РегистрСведений', 'МодульМенеджера')
    # Модуль объекта и прочие — другой тип модуля, набора нет.
    for kind, module_type in [('Справочник', 'МодульОбъекта'), ('Документ', 'МодульФормы'),
                              ('ОбщийМодуль', 'Модуль'), ('Отчет', 'МодульОбъекта')]:
        assert module_context_methods(kind, module_type) == set(), (kind, module_type)


def _local_calls(code: str, **kw) -> set[str]:
    result = BSLParser(known_modules={'общегоназначения'}).parse(code, **kw)
    return {c.callee_proc for c in result.calls if c.callee_kind == 'local'}


_MANAGER_CODE = '''
Функция ТипСсылки() Экспорт
	Возврат ПустаяСсылка().Метаданные();
КонецФункции

Функция Партии() Экспорт
	Выборка = Выбрать();
	Элемент = НайтиПоКоду("001");
	Ref = EmptyRef();
	Возврат ЗаполнитьКэш();
КонецФункции

Функция ЗаполнитьКэш()
	Возврат Неопределено;
КонецФункции
'''


def test_bare_manager_methods_are_not_procedure_calls():
    methods = module_context_methods('Справочник', 'МодульМенеджера')
    assert _local_calls(_MANAGER_CODE, context_methods=methods) == {'ЗаполнитьКэш'}
    # Без контекста (другой тип модуля) — как раньше.
    assert {'ПустаяСсылка', 'Выбрать', 'НайтиПоКоду', 'EmptyRef'} <= _local_calls(_MANAGER_CODE)


def test_procedure_declared_in_module_wins_over_manager_method():
    code = '''
Функция Выбрать(Отбор) Экспорт
	Возврат Неопределено;
КонецФункции

Процедура Тест()
	Выборка = Выбрать(Новый Структура);
КонецПроцедуры
'''
    methods = module_context_methods('Справочник', 'МодульМенеджера')
    assert 'Выбрать' in _local_calls(code, context_methods=methods)


def test_three_part_manager_calls_unchanged():
    code = '''
Процедура Тест()
	Справочники.Партии.ЗаполнитьКэш();
	Пустая = Справочники.Партии.ПустаяСсылка();
КонецПроцедуры
'''
    parser = BSLParser(known_objects={'Справочник': {'партии'}})
    methods = module_context_methods('Справочник', 'МодульМенеджера')
    with_ctx = {(c.callee_kind, c.callee_module, c.callee_proc)
                for c in parser.parse(code, context_methods=methods).calls}
    without = {(c.callee_kind, c.callee_module, c.callee_proc) for c in parser.parse(code).calls}
    assert with_ctx == without == {('manager', 'Справочник.Партии.МодульМенеджера', 'ЗаполнитьКэш')}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8-sig')


def test_indexer_passes_manager_module_context():
    same_call = '''
Процедура Тест() Экспорт
	Тип = ПустаяСсылка().Метаданные();
КонецПроцедуры
'''
    with tempfile.TemporaryDirectory() as tmpdir:
        xml_dir = Path(tmpdir) / 'xml'
        catalog = xml_dir / 'Catalogs' / 'ИмпортируемаяПартияСАТУРН'
        _write(catalog / 'Ext' / 'ManagerModule.bsl', _MANAGER_CODE)
        _write(catalog / 'Ext' / 'ObjectModule.bsl', same_call)
        _write(xml_dir / 'CommonModules' / 'Общий' / 'Ext' / 'Module.bsl', same_call + '''
Процедура Другая() Экспорт
	Справочники.ИмпортируемаяПартияСАТУРН.ЗаполнитьКэш();
КонецПроцедуры
''')
        # У документа НайтиПоНомеру — метод менеджера, НайтиПоКоду — нет.
        _write(xml_dir / 'Documents' / 'Поступление' / 'Ext' / 'ManagerModule.bsl', '''
Процедура Тест() Экспорт
	Док = НайтиПоНомеру("1");
	Элемент = НайтиПоКоду("1");
КонецПроцедуры
''')
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            known_modules, known_objects = Indexer.scan_known_names([str(xml_dir)])
            indexer = Indexer(db)
            indexer.index_bsl(str(xml_dir), source_id='main',
                              known_modules=known_modules, known_objects=known_objects)
            indexer.resolve_calls()
            rows = db.conn.execute("""
                SELECT m.name AS caller_module, c.callee_kind, c.callee_module, c.callee_proc,
                       cr.callee_id
                FROM calls c
                JOIN procedures p ON p.id = c.caller_id
                JOIN modules m ON m.id = p.module_id
                LEFT JOIN calls_resolved cr ON cr.call_id = c.id
            """).fetchall()
            local = {}
            for r in rows:
                if r['callee_kind'] == 'local':
                    local.setdefault(r['caller_module'], set()).add(r['callee_proc'])
            assert local == {
                'Справочник.ИмпортируемаяПартияСАТУРН.МодульМенеджера': {'ЗаполнитьКэш'},
                'Справочник.ИмпортируемаяПартияСАТУРН.МодульОбъекта': {'ПустаяСсылка'},
                'ОбщийМодуль.Общий.Модуль': {'ПустаяСсылка'},
                'Документ.Поступление.МодульМенеджера': {'НайтиПоКоду'},
            }, local
            resolved = {(r['callee_kind'], r['callee_proc']) for r in rows if r['callee_id']}
            assert ('local', 'ЗаполнитьКэш') in resolved
            assert ('manager', 'ЗаполнитьКэш') in resolved
        finally:
            db.close()
