"""
Отпечаток парсера (src/core/parser_fingerprint.py): правка кода разбора
должна сама инвалидировать результаты разбора.

Раньше reindex() пропускал файл при совпавшем хеше содержимого, и после
правки парсера повторная переиндексация выдавала прежние цифры — индекс
приходилось удалять руками (несколько раз это давало ложный вывод «правка
ничего не изменила»). Теперь по каждому источнику хранится отпечаток кода,
которым он разобран; при несовпадении BSL источника переразбирается целиком.

Признак «файл переразобран» — статистика самого Indexer.index_bsl
(files / files_skipped) по каждому источнику. Смена id процедур для этого
не годится: SQLite без AUTOINCREMENT выдаёт те же id, если удалены
последние строки таблицы.
"""

import io
import logging
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core import parser_fingerprint
from src.core.db import Database
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

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
    tmpdir = tempfile.mkdtemp()
    manager = ProjectManager(data_dir=tmpdir)
    manager.create_project('p', 'p')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        manager.add_source('p', 'main', 'Main', report_file=f,
                           xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
    manager.add_source('p', 'ext', 'Ext', source_type='extension',
                       xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')
    try:
        yield manager
    finally:
        manager.close_all()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def phases(monkeypatch):
    """Все progress_phase, которые reindex выставлял."""
    seen = []
    original = Database.update_state

    def spy(self, **kwargs):
        if 'progress_phase' in kwargs:
            seen.append(kwargs['progress_phase'])
        return original(self, **kwargs)

    monkeypatch.setattr(Database, 'update_state', spy)
    return seen


@pytest.fixture
def runs(monkeypatch):
    """[{source_id: (разобрано файлов, пропущено по хешу)}] — по записи на reindex."""
    from src.core.indexer import Indexer
    log: list[dict] = []
    original = Indexer.index_bsl

    def spy(self, xml_path, source_id='main', **kwargs):
        stats = original(self, xml_path, source_id=source_id, **kwargs)
        log[-1][source_id] = (stats['files'], stats['files_skipped'])
        return stats

    monkeypatch.setattr(Indexer, 'index_bsl', spy)

    def start():
        log.append({})
        return log[-1]
    return start


def _states(pm) -> dict[str, dict]:
    return pm.get_db('p').get_parser_states()


def _fake_fingerprint(monkeypatch, value: str):
    monkeypatch.setattr(parser_fingerprint, 'parser_fingerprint', lambda: value)


# ---------------------------------------------------------------------------
# Вычисление отпечатка
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_line_endings_and_location(monkeypatch):
    base = parser_fingerprint.compute_parser_fingerprint()
    assert len(base) == 16

    copy_dir = Path(tempfile.mkdtemp())
    try:
        for name in parser_fingerprint._SOURCE_FILES:
            text = (parser_fingerprint._HERE / name).read_bytes().decode('utf-8-sig')
            (copy_dir / name).write_bytes(
                '﻿'.encode() + text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8'))
        monkeypatch.setattr(parser_fingerprint, '_HERE', copy_dir)
        assert parser_fingerprint.compute_parser_fingerprint() == base, \
            "другой путь + CRLF + BOM не должны менять отпечаток"

        with open(copy_dir / 'bsl_reference.py', 'ab') as f:
            f.write(b"\r\nNEW_STOPWORD = {'x'}\r\n")
        assert parser_fingerprint.compute_parser_fingerprint() != base
    finally:
        shutil.rmtree(copy_dir)


def test_fingerprint_covers_parse_related_indexer_code_only():
    """Из indexer.py в отпечаток входят функции, формирующие сохранённый
    результат разбора, — но не resolve_calls/index_report (они и так
    пересчитываются при каждом reindex)."""
    assert 'index_bsl' in parser_fingerprint._INDEXER_FUNCTIONS
    assert 'collect_known_facts' in parser_fingerprint._INDEXER_FUNCTIONS
    assert 'resolve_calls' not in parser_fingerprint._INDEXER_FUNCTIONS
    assert 'index_report' not in parser_fingerprint._INDEXER_FUNCTIONS


def test_missing_sources_give_empty_fingerprint(monkeypatch):
    monkeypatch.setattr(parser_fingerprint, '_HERE', Path(tempfile.gettempdir()) / 'нет-такого')
    assert parser_fingerprint.compute_parser_fingerprint() == ''


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------

def _all_parsed(run: dict) -> bool:
    return all(files > 0 and skipped == 0 for files, skipped in run.values())


def _all_skipped(run: dict) -> bool:
    return all(files == 0 and skipped > 0 for files, skipped in run.values())


def test_a_unchanged_rerun_skips_files_and_records_fingerprint(pm, runs, phases):
    first = runs()
    pm.reindex('p')
    assert set(first) == {'main', 'ext'} and _all_parsed(first)
    current = parser_fingerprint.parser_fingerprint()
    states = _states(pm)
    assert {sid: st['fingerprint'] for sid, st in states.items()} == {'main': current, 'ext': current}
    # Первая индексация — это не «смена парсера».
    assert all(st['last_full_reparse_reason'] == '' for st in states.values())

    phases.clear()
    second = runs()
    pm.reindex('p')
    assert _all_skipped(second), second
    assert 'bsl_full' not in phases


def test_b_changed_fingerprint_forces_full_reparse(pm, runs, phases, monkeypatch, caplog):
    first = runs()
    pm.reindex('p')
    procs_before = pm.get_db('p').conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0]

    _fake_fingerprint(monkeypatch, 'другойпарсер0001')
    phases.clear()
    forced = runs()
    with caplog.at_level(logging.INFO, logger='ariadna'):
        pm.reindex('p')
    assert forced == first, "все файлы обоих источников переразобраны, как при первой индексации"
    assert 'bsl_full' in phases
    assert 'изменилась версия парсера' in caplog.text
    # Ничего не задвоилось и не потерялось.
    assert pm.get_db('p').conn.execute("SELECT COUNT(*) FROM procedures").fetchone()[0] == procs_before

    states = _states(pm)
    assert states['main']['fingerprint'] == 'другойпарсер0001'
    assert states['main']['last_full_reparse_reason'].startswith('изменилась версия парсера')

    # Следующий прогон с тем же отпечатком — снова инкрементальный.
    phases.clear()
    again = runs()
    pm.reindex('p')
    assert _all_skipped(again), again
    assert 'bsl_full' not in phases


def test_c_changed_file_still_reparses_only_that_file(pm, runs):
    first = runs()
    pm.reindex('p')

    target = (pm.projects_dir / 'p' / 'sources' / 'main' / 'xml'
              / 'CommonModules' / 'ЦеныСервер' / 'Ext' / 'Module.bsl')
    target.write_text(target.read_text(encoding='utf-8-sig')
                      + "\nФункция Новая() Экспорт\n\tВозврат 1;\nКонецФункции\n",
                      encoding='utf-8-sig')
    second = runs()
    pm.reindex('p')
    assert second == {'main': (1, first['main'][0] - 1), 'ext': (0, first['ext'][0])}
    names = {r['name'] for r in pm.get_db('p').conn.execute(
        "SELECT p.name FROM procedures p JOIN modules m ON m.id = p.module_id "
        "WHERE m.name = 'ОбщийМодуль.ЦеныСервер.Модуль'")}
    assert 'Новая' in names


def test_d_old_index_without_fingerprint_is_fully_reparsed(pm, runs, phases, caplog):
    """Индекс, построенный до появления parser_state: таблицы нет вовсе.
    Открытие не падает (init_schema её создаёт), следующий reindex полный."""
    first = runs()
    pm.reindex('p')
    db = pm.get_db('p')
    db.conn.execute("DROP TABLE parser_state")
    db.conn.commit()
    pm.close_all()

    reopened = ProjectManager(data_dir=str(pm.data_dir))
    try:
        phases.clear()
        forced = runs()
        with caplog.at_level(logging.INFO, logger='ariadna'):
            reopened.reindex('p')
        assert forced == first
        assert 'bsl_full' in phases
        assert 'нет отпечатка парсера' in caplog.text
        states = reopened.get_db('p').get_parser_states()
        assert states['main']['fingerprint'] == parser_fingerprint.parser_fingerprint()
    finally:
        reopened.close_all()


def test_e_single_source_reindex_does_not_mark_others_fresh(pm, runs, monkeypatch):
    first = runs()
    pm.reindex('p')
    old = parser_fingerprint.parser_fingerprint()

    _fake_fingerprint(monkeypatch, 'другойпарсер0002')
    only_ext = runs()
    pm.reindex('p', source_id='ext')
    assert only_ext == {'ext': first['ext']}
    states = _states(pm)
    assert states['ext']['fingerprint'] == 'другойпарсер0002'
    assert states['main']['fingerprint'] == old, "main не переразбирался — отпечаток прежний"

    text = execute_tool(pm, 'diagnose_index', {'project_id': 'p'})
    section = text.split('--- Parser fingerprint ---')[1].split('---')[0]
    assert f'main: {old} (устарел' in section
    assert 'ext: другойпарсер0002 (актуален)' in section

    # Полный reindex доводит main, а ext уже свежий — пропускается.
    full = runs()
    pm.reindex('p')
    assert full == {'main': first['main'], 'ext': (0, first['ext'][0])}


def test_unknown_fingerprint_keeps_old_behaviour(pm, runs, monkeypatch):
    runs()
    pm.reindex('p')
    _fake_fingerprint(monkeypatch, '')
    rerun = runs()
    pm.reindex('p')
    assert _all_skipped(rerun), rerun
