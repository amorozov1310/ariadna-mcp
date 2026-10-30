"""
Стенд оценки поиска (src/core/search_eval.py) на синтетическом наборе
tests/fixtures/eval_ru.json к фикстуре xml_ru + xml_ru_ext.

Кроме механики стенда закрепляет два свойства поиска по карточкам:
точные имена процедур находятся первыми (их нельзя «размыть» ранжированием),
и на вопросах своими словами карточки не хуже существующих инструментов.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.search_eval import (
    Case, CaseResult, evaluate, format_summary, load_cases, summarize, _main,
)

FIXTURES = Path(__file__).parent / 'fixtures'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture(scope='module')
def data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        with open(FIXTURES / 'report_ru.txt', 'rb') as f:
            pm.add_source('p1', 'main', 'Main', report_file=f,
                          xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        pm.add_source('p1', 'ext', 'Ext', source_type='extension',
                      xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')
        pm.reindex('p1')
        pm.close_all()
        yield tmpdir


@pytest.fixture(scope='module')
def summary(data_dir):
    _, cases = load_cases(FIXTURES / 'eval_ru.json')
    pm = ProjectManager(data_dir)
    try:
        return summarize(evaluate(pm.get_search('p1'), cases))
    finally:
        pm.close_all()


def test_exact_names_are_found_first_by_every_method(summary):
    for method in ('procedures', 'code', 'cards'):
        assert summary[method]['by_kind']['exact']['hit@5'] == 1.0, method
    assert summary['cards']['by_kind']['exact']['hit@1'] == 1.0


def test_cards_beat_existing_tools_on_paraphrases(summary):
    para = {m: summary[m]['by_kind']['paraphrase'] for m in summary}
    assert para['cards']['hit@10'] > max(para['procedures']['hit@10'], para['code']['hit@10'])
    assert para['cards']['mrr'] > max(para['procedures']['mrr'], para['code']['mrr'])
    # Порог-регрессия на синтетике (не обещание качества на реальных конфигурациях).
    assert summary['cards']['hit@10'] >= 0.9


def test_negatives_are_counted_separately(summary):
    for s in summary.values():
        assert s['negatives'] == 2 and s['n'] == 18


def test_load_cases_yaml_and_module_normalization(tmp_path):
    path = tmp_path / 'set.yaml'
    path.write_text(
        'project: demo\n'
        'cases:\n'
        '  - query: "ТипСкладаПоУмолчанию"\n'
        '    kind: exact\n'
        '    expect:\n'
        '      - module: "ОбщийМодуль.ЦеныСервер.Модуль"\n'
        '        procedure: "РассчитатьЦену"\n'
        '      - module: "Справочник.Склады.МодульОбъекта"\n'
        '  - query: "нет такого"\n'
        '    expect: []\n', encoding='utf-8')
    project, cases = load_cases(path)
    assert project == 'demo'
    assert cases[0].id == '1' and cases[0].kind == 'exact'
    assert cases[0].expect == [('общиймодуль.ценысервер', 'рассчитатьцену'),
                               ('справочник.склады.модульобъекта', '')]
    assert cases[1].expect == []


def test_summary_math():
    pos = Case('1', 'paraphrase', 'q', [('m', 'p')])
    neg = Case('2', 'negative', 'q', [])
    results = [
        CaseResult(pos, 'x', 1, 5, 10.0), CaseResult(pos, 'x', 4, 5, 20.0),
        CaseResult(pos, 'x', None, 0, 30.0), CaseResult(pos, 'x', 10, 10, 40.0),
        CaseResult(neg, 'x', None, 3, 50.0),
    ]
    s = summarize(results)['x']
    assert s['n'] == 4 and s['hit@1'] == 0.25 and s['hit@5'] == 0.5 and s['hit@10'] == 0.75
    assert s['mrr'] == pytest.approx((1 + 0.25 + 0.1) / 4)
    assert s['negatives'] == 1 and s['negatives_with_results'] == 1
    assert 'paraphrase' in format_summary({'x': s})


def test_cli_runs_against_a_data_dir(data_dir, capsys):
    assert _main([str(FIXTURES / 'eval_ru.json'), '--data-dir', data_dir, '--details']) == 0
    out = capsys.readouterr().out
    assert 'Проект p1, вопросов 20' in out and 'cards' in out and '[1 exact] cards: #1' in out
