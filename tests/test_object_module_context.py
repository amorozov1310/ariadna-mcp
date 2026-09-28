"""
Неявные свойства объекта в его собственном модуле (Этап 7, остаток).

Реальная ERP (Reports/ABCXYZАнализНоменклатуры/Ext/ObjectModule.bsl, 60
вызовов только в этом файле): `КомпоновщикНастроек.ПолучитьНастройки()` в
модуле ОБЪЕКТА отчёта. Формы здесь нет, реквизитом формы это быть не может;
это платформенное свойство ОтчетОбъект, доступное без «ЭтотОбъект.».
Источник факта — тип модуля (bsl_reference.module_context_vars), а не
метаданные. В модулях других типов имя ничем не выделяется.
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.bsl_parser import BSLParser
from src.core.bsl_reference import module_context_vars
from src.core.db import Database
from src.core.indexer import Indexer


def test_only_report_object_module_has_settings_composer():
    assert module_context_vars('Отчет', 'МодульОбъекта') == {'компоновщикнастроек', 'settingscomposer'}
    for kind, module_type in [('Отчет', 'МодульМенеджера'), ('Отчет', 'МодульФормы'),
                              ('Обработка', 'МодульОбъекта'), ('ОбщийМодуль', 'Модуль'),
                              ('Справочник', 'МодульОбъекта')]:
        assert module_context_vars(kind, module_type) == set(), (kind, module_type)


def test_parser_context_vars_ru_and_en():
    code = '''
Процедура ПриКомпоновкеРезультата(ДокументРезультат, ДанныеРасшифровки, СтандартнаяОбработка)
	НастройкиОтчета = КомпоновщикНастроек.ПолучитьНастройки();
	КомпоновщикНастроек.ЗагрузитьНастройки(НастройкиОтчета);
	Settings = SettingsComposer.GetSettings();
	ОбщегоНазначения.СообщитьПользователю("готово");
КонецПроцедуры
'''
    parser = BSLParser(known_modules={'общегоназначения'})
    with_ctx = {(c.callee_module, c.callee_proc) for c in parser.parse(
        code, context_vars={'компоновщикнастроек', 'settingscomposer'}).calls}
    assert not any(m in ('КомпоновщикНастроек', 'SettingsComposer') for m, _ in with_ctx), with_ctx
    assert ('ОбщегоНазначения', 'СообщитьПользователю') in with_ctx

    without = {(c.callee_module, c.callee_proc) for c in parser.parse(code).calls}
    assert ('КомпоновщикНастроек', 'ПолучитьНастройки') in without, "без контекста — как раньше"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8-sig')


_USES = '''
Процедура Сформировать() Экспорт
	НастройкиОтчета = КомпоновщикНастроек.ПолучитьНастройки();
	ОбщегоНазначения.СообщитьПользователю("готово");
КонецПроцедуры
'''


def test_indexer_passes_module_type_context():
    """Отчёт: модуль объекта — свойство, вызова модуля нет. То же имя в
    модуле формы отчёта, модуле объекта обработки и в общем модуле — вызов
    настоящего общего модуля КомпоновщикНастроек, резолвится как раньше."""
    with tempfile.TemporaryDirectory() as tmpdir:
        xml_dir = Path(tmpdir) / 'xml'
        _write(xml_dir / 'CommonModules' / 'ОбщегоНазначения' / 'Ext' / 'Module.bsl', '''
Процедура СообщитьПользователю(Текст) Экспорт
КонецПроцедуры
''')
        # Общий модуль-«тёзка», чтобы было видно: настоящие вызовы к нему целы.
        _write(xml_dir / 'CommonModules' / 'КомпоновщикНастроек' / 'Ext' / 'Module.bsl', '''
Функция ПолучитьНастройки() Экспорт
	Возврат Неопределено;
КонецФункции
''')
        _write(xml_dir / 'CommonModules' / 'Отчеты' / 'Ext' / 'Module.bsl', _USES)
        report = xml_dir / 'Reports' / 'ABCXYZАнализНоменклатуры'
        _write(report / 'Ext' / 'ObjectModule.bsl', _USES)
        _write(report / 'Forms' / 'ФормаОтчета' / 'Ext' / 'Form' / 'Module.bsl', _USES)
        _write(xml_dir / 'DataProcessors' / 'Загрузка' / 'Ext' / 'ObjectModule.bsl', _USES)

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
                SELECT m.name AS caller_module, c.callee_module, c.callee_proc, cr.callee_id
                FROM calls c
                JOIN procedures p ON p.id = c.caller_id
                JOIN modules m ON m.id = p.module_id
                LEFT JOIN calls_resolved cr ON cr.call_id = c.id
                WHERE c.callee_kind = 'common_module'
            """).fetchall()
            composer = {r['caller_module']: bool(r['callee_id']) for r in rows
                        if r['callee_module'] == 'КомпоновщикНастроек'}
            assert composer == {
                'ОбщийМодуль.Отчеты.Модуль': True,
                'Отчет.ABCXYZАнализНоменклатуры.Форма.ФормаОтчета': True,
                'Обработка.Загрузка.МодульОбъекта': True,
            }, composer
            # Настоящий общий модуль из модуля объекта отчёта не задет.
            report_calls = {(r['callee_module'], r['callee_proc'], bool(r['callee_id'])) for r in rows
                            if r['caller_module'] == 'Отчет.ABCXYZАнализНоменклатуры.МодульОбъекта'}
            assert report_calls == {('ОбщегоНазначения', 'СообщитьПользователю', True)}, report_calls
        finally:
            db.close()
