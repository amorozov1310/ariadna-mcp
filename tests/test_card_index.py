"""
Индекс карточек процедур (card_chunks + card_fts): строится в конце
reindex только для изменившихся модулей, ищется с рангом BM25
(src/core/cards.py: refresh_card_index, search_cards).
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core import cards
from src.core.cards import refresh_card_index, search_cards
from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'
FORM = 'Catalogs/Склады/Forms/ФормаЭлемента/Ext/Form/Module.bsl'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        with open(FIXTURES / 'report_ru.txt', 'rb') as f:
            manager.add_source('p1', 'main', 'Main', report_file=f,
                               xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        manager.reindex('p1')
        try:
            yield manager
        finally:
            manager.close_all()


def _src(pm) -> Path:
    return pm.projects_dir / 'p1' / 'sources' / 'main'


def _chunk_ids(pm) -> dict[str, set[int]]:
    """Модуль → id его фрагментов: новые id — модуль пересобран."""
    out: dict[str, set[int]] = {}
    for r in pm.get_db('p1').conn.execute(
            """SELECT m.name, c.id FROM card_chunks c JOIN procedures p ON p.id = c.procedure_id
               JOIN modules m ON m.id = p.module_id"""):
        out.setdefault(r[0], set()).add(r[1])
    return out


def _count(pm, sql: str) -> int:
    return pm.get_db('p1').conn.execute(sql).fetchone()[0]


def test_reindex_builds_cards_for_every_procedure(pm):
    assert _count(pm, """SELECT COUNT(*) FROM procedures p WHERE NOT EXISTS
                         (SELECT 1 FROM card_chunks c WHERE c.procedure_id = p.id)""") == 0
    chunks = _count(pm, 'SELECT COUNT(*) FROM card_chunks')
    assert chunks >= _count(pm, 'SELECT COUNT(*) FROM procedures') > 10
    assert _count(pm, 'SELECT COUNT(*) FROM card_fts') == chunks
    assert _count(pm, 'SELECT COUNT(*) FROM card_modules') == \
        _count(pm, 'SELECT COUNT(DISTINCT module_id) FROM procedures')


def test_search_cards_ranks_by_relevance(pm):
    engine = pm.get_search('p1')
    # Формы слов совпадают («склада» — «склад»), поэтому рядом и экспортная
    # СкладПоУмолчанию из модуля менеджера; искомая — в первой двойке.
    hits = search_cards(engine, 'тип склада по умолчанию')
    top = [(h['module_name'], h['procedure_name']) for h in hits[:2]]
    assert ('Справочник.Склады.МодульОбъекта', 'ТипСкладаПоУмолчанию') in top
    # Точное имя — первым.
    assert search_cards(engine, 'ТипСкладаПоУмолчанию')[0]['procedure_name'] == 'ТипСкладаПоУмолчанию'
    assert [h['rank'] for h in hits] == list(range(1, len(hits) + 1))
    assert len({h['procedure_id'] for h in hits}) == len(hits)   # по процедуре — одна строка

    # Синоним объекта есть только в заголовке карточки — по нему тоже находится.
    by_synonym = search_cards(engine, 'Склады', limit=50)
    assert {h['module_name'] for h in by_synonym} >= {
        'Справочник.Склады.МодульОбъекта', 'Справочник.Склады.Форма.ФормаЭлемента'}

    assert search_cards(engine, 'ВЫБРАТЬ ПЕРВЫЕ')[0]['procedure_name'] == 'ТипСкладаПоУмолчанию'
    assert search_cards(engine, '') == [] and search_cards(engine, '!!!') == []
    assert search_cards(engine, 'склад', source_id='нет') == []


def test_unchanged_reindex_rebuilds_nothing(pm):
    before = _chunk_ids(pm)
    pm.reindex('p1')
    assert _chunk_ids(pm) == before
    assert refresh_card_index(pm.get_search('p1')) == {'modules': 0, 'chunks': 0}


def test_changed_module_alone_is_rebuilt(pm):
    before = _chunk_ids(pm)
    path = _src(pm) / 'xml' / FORM
    path.write_text(path.read_text(encoding='utf-8-sig').replace('"Запись_Склады"', '"ЗаписьСклада"'),
                    encoding='utf-8-sig')
    pm.reindex('p1')
    after = _chunk_ids(pm)
    form = 'Справочник.Склады.Форма.ФормаЭлемента'
    assert after[form].isdisjoint(before[form])
    assert all(after[m] == ids for m, ids in before.items() if m != form)
    assert search_cards(pm.get_search('p1'), 'ЗаписьСклада')[0]['module_name'] == form


def test_synonym_change_rebuilds_modules_of_that_object(pm):
    before = _chunk_ids(pm)
    report = _src(pm) / 'report.txt'
    text = report.read_bytes().decode('utf-16')
    assert text.count('Синоним: "Склады"') == 1
    report.write_bytes(text.replace('Синоним: "Склады"', 'Синоним: "Места хранения"').encode('utf-16'))
    pm.reindex('p1')
    after = _chunk_ids(pm)
    changed = {m for m in before if after[m] != before[m]}
    assert changed == {m for m in before if m.startswith('Справочник.Склады.')}
    hits = search_cards(pm.get_search('p1'), 'места хранения')
    assert hits and all(h['module_name'].startswith('Справочник.Склады.') for h in hits)


def test_card_format_change_rebuilds_everything(pm, monkeypatch):
    before = _chunk_ids(pm)
    monkeypatch.setattr(cards, 'CARD_FORMAT', cards.CARD_FORMAT + 1)
    stats = refresh_card_index(pm.get_search('p1'))
    assert stats['modules'] == len(before)
    after = _chunk_ids(pm)
    assert all(after[m].isdisjoint(before[m]) for m in before)


def test_removed_source_leaves_no_cards(pm):
    pm.remove_source('p1', 'main')
    for table in ('card_chunks', 'card_fts', 'card_modules'):
        assert _count(pm, f'SELECT COUNT(*) FROM {table}') == 0, table


def test_word_forms_match_through_roots(pm):
    """«оформление» ↔ ОформитьЗаказ, «регистрация» ↔ ЗарегистрироватьСобытие
    (приставка «за-»): общий корень в заголовке карточки и в запросе."""
    engine = pm.get_search('p1')
    assert search_cards(engine, 'оформление заказа')[0]['procedure_name'] == 'ОформитьЗаказ'
    hits = search_cards(engine, 'регистрация события в документообороте')
    assert hits[0]['procedure_name'] == 'ЗарегистрироватьСобытие'


def test_exact_name_puts_most_called_procedure_first(tmp_path):
    """Одноимённые процедуры по точному имени — сначала та, в которую ведёт
    больше вызовов (исходный метод), а не обёртка, хотя по алфавиту
    «Обертка» раньше «Основной»."""
    modules = {
        'Основной': 'Функция Значение(Ссылка) Экспорт\n\tВозврат Ссылка;\nКонецФункции\n',
        'Обертка': 'Функция Значение(Ссылка) Экспорт\n\tВозврат Основной.Значение(Ссылка);\nКонецФункции\n',
        'Потребитель': ''.join(
            f'Процедура Шаг{i}(Ссылка) Экспорт\n\tА = Основной.Значение(Ссылка);\nКонецПроцедуры\n\n'
            for i in range(3)),
    }
    xml = tmp_path / 'xml'
    for name, code in modules.items():
        path = xml / 'CommonModules' / name / 'Ext' / 'Module.bsl'
        path.parent.mkdir(parents=True)
        path.write_text(code, encoding='utf-8-sig')
    manager = ProjectManager(str(tmp_path / 'data'))
    try:
        manager.create_project('p2', 'p2')
        manager.add_source('p2', 'main', 'Main', xml_archive=_zip(xml), xml_archive_filename='x.zip')
        manager.reindex('p2')
        hits = search_cards(manager.get_search('p2'), 'Значение')
        assert [h['module_name'] for h in hits[:2]] == [
            'ОбщийМодуль.Основной.Модуль', 'ОбщийМодуль.Обертка.Модуль']
    finally:
        manager.close_all()


