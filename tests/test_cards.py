"""
Поисковые карточки процедур (src/core/cards.py) на синтетической фикстуре
xml_ru + report_ru.txt.
"""

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.cards import CARD_FORMAT, build_cards, chunk_body
from src.core.project_manager import ProjectManager

FIXTURES = Path(__file__).parent / 'fixtures'


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


def _cards(pm, **kw):
    return {(c.module_name, c.procedure_name): c for c in build_cards(pm.get_search('p1'), **kw)}


def test_every_procedure_gets_one_card(pm):
    cards = list(build_cards(pm.get_search('p1')))
    count = pm.get_db('p1').conn.execute('SELECT COUNT(*) FROM procedures').fetchone()[0]
    assert len(cards) == count > 10
    assert len({c.procedure_id for c in cards}) == count
    assert all(c.body_available and c.chunks for c in cards)


def test_form_procedure_card(pm):
    card = _cards(pm)[('Справочник.Склады.Форма.ФормаЭлемента', 'ПослеЗаписи')]
    assert card.object_full_name == 'Справочник.Склады'
    assert card.object_synonym == 'Склады'          # из метаданных отчёта
    assert card.region == 'ОбработчикиСобытийФормы'
    assert card.relative_path == 'Catalogs/Склады/Forms/ФормаЭлемента/Ext/Form/Module.bsl'
    assert card.chunks[0].start_line == card.start_line
    assert card.chunks[-1].end_line == card.end_line
    assert 'Оповестить("Запись_Склады"' in card.chunks[0].text
    head = card.header()
    assert 'Модуль: Справочник.Склады.Форма.ФормаЭлемента' in head
    assert 'Объект: Справочник.Склады — Склады' in head
    assert card.chunk_texts()[0].startswith(head)


def test_doc_comment_and_nested_region(pm):
    card = _cards(pm)[('ОбщийМодуль.ФормыДокументовБазовый.Модуль', 'ПриСозданииНаСервере')]
    assert card.region == 'СлужебныйПрограммныйИнтерфейс'
    assert card.doc_comment.startswith('Обработчик события ПриСозданииНаСервере формы документа.')
    assert 'Форма - ФормаКлиентскогоПриложения' in card.doc_comment
    assert card.is_export


def test_query_text_is_in_the_body(pm):
    card = _cards(pm)[('Справочник.Склады.МодульОбъекта', 'ТипСкладаПоУмолчанию')]
    assert card.region == 'СлужебныеПроцедурыИФункции'
    assert 'ВЫБРАТЬ ПЕРВЫЕ 1' in '\n'.join(c.text for c in card.chunks)


def test_module_filter(pm):
    cards = _cards(pm, module_name='общиймодуль.ценысервер.модуль')
    assert list(cards) == [('ОбщийМодуль.ЦеныСервер.Модуль', 'РассчитатьЦену')]


def test_hash_is_stable_and_tracks_code_and_metadata(pm):
    key = ('Справочник.Склады.Форма.ФормаЭлемента', 'ПослеЗаписи')
    before = _cards(pm)[key].content_hash
    assert before.startswith('sha256:') and _cards(pm)[key].content_hash == before

    # Синоним объекта — часть карточки: меняется он, меняется отпечаток.
    conn = pm.get_db('p1').conn
    conn.execute("UPDATE metadata_objects SET synonym='Места хранения' WHERE full_name='Справочник.Склады'")
    conn.commit()
    renamed = _cards(pm)[key]
    assert 'Объект: Справочник.Склады — Места хранения' in renamed.header()
    assert renamed.content_hash != before

    # Код другой процедуры того же модуля на карточку не влияет.
    other = _cards(pm)[('Справочник.Склады.Форма.ФормаЭлемента', 'ПриСозданииНаСервере')].content_hash
    xml = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    form = xml / 'Catalogs' / 'Склады' / 'Forms' / 'ФормаЭлемента' / 'Ext' / 'Form' / 'Module.bsl'
    text = form.read_text(encoding='utf-8-sig')
    form.write_text(text.replace('Оповестить("Запись_Склады"', 'Оповестить("Запись_Склад"'),
                    encoding='utf-8-sig')
    after = _cards(pm)
    assert after[key].content_hash != renamed.content_hash
    assert after[('Справочник.Склады.Форма.ФормаЭлемента', 'ПриСозданииНаСервере')].content_hash == other


def test_unreadable_module_gives_header_only_cards(pm):
    xml = pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml'
    shutil.rmtree(xml / 'CommonModules' / 'ЦеныСервер')
    card = _cards(pm)[('ОбщийМодуль.ЦеныСервер.Модуль', 'РассчитатьЦену')]
    assert not card.body_available and card.chunks == []
    assert card.chunk_texts() == [card.header()]
    assert 'Функция РассчитатьЦену' in card.header()


def test_chunk_body_keeps_every_line_within_limit():
    lines = [f'\tСтрока{i} = {i};' for i in range(1, 301)]
    lines[149] = '\t' + 'Х' * 3700          # одна очень длинная строка
    chunks = chunk_body(lines, 10, 250, limit=500)
    assert all(len(c.text) <= 500 for c in chunks)
    assert chunks[0].start_line == 10 and chunks[-1].end_line == 250
    assert [c.chunk_no for c in chunks] == list(range(len(chunks)))
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start_line in (prev.end_line, prev.end_line + 1)
    # Ничего не потеряно: склеенные фрагменты — это ровно тело процедуры.
    assert ''.join(c.text.replace('\n', '') for c in chunks) == ''.join(lines[9:250])


def test_card_format_is_part_of_the_hash():
    assert isinstance(CARD_FORMAT, int) and CARD_FORMAT >= 1


@pytest.mark.parametrize('name, words', [
    ('ТипСкладаПоУмолчанию', 'тип склада по умолчанию'),
    ('НДСЗаписиКнигиПродаж', 'ндс записи книги продаж'),
    ('ЗаписатьВЖурнал2', 'записать в журнал 2'),
    ('GetIntegrationSettings', 'get integration settings'),
    ('XMLПакет', 'xml пакет'),
])
def test_split_identifier(name, words):
    from src.core.cards import split_identifier
    assert split_identifier(name) == words


@pytest.mark.parametrize('word, roots', [
    ('отправление', {'отпра', 'правл'}),
    ('зарезервировать', {'зарез', 'резер'}),
    ('резервирование', {'резер'}),
    ('склад', set()),                 # короткое — без корня
    ('GetSettings', set()),           # не кириллица
])
def test_word_roots(word, roots):
    from src.core.cards import word_roots
    assert word_roots(word) == roots
