"""
Модули общих форм (CommonForms).

У общей формы нет подкаталога Forms — модуль лежит прямо в
CommonForms/{Имя}/Ext/Form/Module.bsl, рядом Ext/Form.xml. Раньше обход
искал *.bsl только непосредственно в {Obj}/Ext/ и формы только в
{Obj}/Forms/..., поэтому модули общих форм не индексировались вовсе.

Имя модуля — `ОбщаяФорма.{Имя}`: так же report_parser называет сам объект
метаданных, по этому имени index_bsl находит object_id, а resolve_calls
джойнит override-вызовы по точному modules.name.
"""

import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.indexer import Indexer
from src.core.xml_walker import XMLWalker, read_form_attributes

_FORM_NS = ('xmlns="http://v8.1c.ru/8.3/xcf/logform" '
            'xmlns:v8="http://v8.1c.ru/8.1/data/core" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema"')


def _form_xml(attributes: str) -> str:
    return (f'﻿<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Form {_FORM_NS} version="2.20">\n'
            f'\t<Attributes>\n{attributes}\t</Attributes>\n</Form>\n')


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _build_tree(xml_dir: Path) -> None:
    """Мини-выгрузка: общий модуль, общая форма с модулем и реквизитами,
    общая форма с #U-кодированным именем и общая форма без модуля."""
    _write(xml_dir / 'CommonModules' / 'ОбщегоНазначения' / 'Ext' / 'Module.bsl', '''
Процедура СообщитьПользователю(Текст) Экспорт
КонецПроцедуры
''')
    form_ext = xml_dir / 'CommonForms' / 'ВопросПользователю' / 'Ext'
    _write(form_ext / 'Form.xml', _form_xml(
        '\t\t<Attribute name="ТабДок" id="1"/>\n'))
    _write(form_ext / 'Form' / 'Module.bsl', '''
&НаСервере
Процедура Сформировать()
	Область = ТабДок.ПолучитьОбласть("Шапка");
	ОбщегоНазначения.СообщитьПользователю("готово");
	Подготовить();
КонецПроцедуры

&НаСервере
Процедура Подготовить()
КонецПроцедуры
''')
    # «Выбор» в #U-кодировке — так 1С выгружает часть имён каталогов.
    encoded = ''.join(f'#U{ord(ch):04X}' for ch in 'Выбор')
    _write(xml_dir / 'CommonForms' / encoded / 'Ext' / 'Form' / 'Module.bsl', '''
&НаКлиенте
Процедура ПриОткрытии(Отказ)
КонецПроцедуры
''')
    # Общая форма без модуля (только Form.xml) — модуля нет, и строки быть не должно.
    _write(xml_dir / 'CommonForms' / 'БезМодуля' / 'Ext' / 'Form.xml',
           _form_xml('\t\t<Attribute name="Реквизит" id="1"/>\n'))


# ---------------------------------------------------------------------------
# XMLWalker
# ---------------------------------------------------------------------------

def test_walker_finds_common_form_modules():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        xml_dir = tmpdir / 'xml'
        _build_tree(xml_dir)
        forms = {m.full_name: m for m in XMLWalker().walk(xml_dir)
                 if m.object_kind == 'ОбщаяФорма'}
        assert set(forms) == {'ОбщаяФорма.ВопросПользователю', 'ОбщаяФорма.Выбор'}, forms

        m = forms['ОбщаяФорма.ВопросПользователю']
        assert m.module_type == 'МодульФормы'
        assert m.object_name == 'ВопросПользователю'
        assert m.form_name == 'ВопросПользователю'
        assert m.relative_path == 'CommonForms/ВопросПользователю/Ext/Form/Module.bsl'
        assert Path(m.form_xml_path) == (
            xml_dir / 'CommonForms' / 'ВопросПользователю' / 'Ext' / 'Form.xml')
        assert read_form_attributes(m.form_xml_path) == {'табдок'}

        # Нет Form.xml — пустой путь, как у обычных форм.
        assert forms['ОбщаяФорма.Выбор'].form_xml_path == ''
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Полный конвейер: XMLWalker → Indexer.index_bsl → resolve_calls
# ---------------------------------------------------------------------------

def _index(db: Database, xml_dir: Path) -> dict:
    known_modules, known_objects = Indexer.scan_known_names([str(xml_dir)])
    indexer = Indexer(db)
    stats = indexer.index_bsl(str(xml_dir), source_id='main',
                              known_modules=known_modules, known_objects=known_objects)
    indexer.resolve_calls()
    return stats


def test_common_form_module_is_indexed_with_form_attributes():
    with tempfile.TemporaryDirectory() as tmpdir:
        xml_dir = Path(tmpdir) / 'xml'
        _build_tree(xml_dir)
        db = Database(str(Path(tmpdir) / 'index.db'))
        db.connect()
        db.init_schema()
        try:
            stats = _index(db, xml_dir)
            assert stats['modules'] == 3, stats

            mod = db.conn.execute(
                "SELECT id, module_type, file_path FROM modules WHERE name=?",
                ('ОбщаяФорма.ВопросПользователю',)).fetchone()
            assert mod is not None, "модуль общей формы должен попасть в индекс"
            assert mod['module_type'] == 'МодульФормы'
            assert mod['file_path'] == 'CommonForms/ВопросПользователю/Ext/Form/Module.bsl'

            rows = db.conn.execute("""
                SELECT c.callee_module, c.callee_proc, c.callee_kind, cr.callee_id
                FROM calls c
                JOIN procedures p ON p.id = c.caller_id
                LEFT JOIN calls_resolved cr ON cr.call_id = c.id
                WHERE p.module_id = ?
            """, (mod['id'],)).fetchall()
            by_proc = {r['callee_proc']: r for r in rows}

            # Реквизит формы из Form.xml — не общий модуль.
            assert 'ПолучитьОбласть' not in by_proc, [dict(r) for r in rows]
            # Настоящий общий модуль и локальная процедура резолвятся.
            assert by_proc['СообщитьПользователю']['callee_id'], dict(by_proc['СообщитьПользователю'])
            assert by_proc['Подготовить']['callee_id'], dict(by_proc['Подготовить'])
        finally:
            db.close()
