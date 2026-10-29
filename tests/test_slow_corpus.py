"""
Медленные тесты на реальных конфигурациях из локального data/ — своих у
каждого пользователя.

Какие проекты проверять, задаётся переменной окружения ARIADNA_CORPUS — id
проектов из data/projects.json через запятую (как в list_projects или Web
UI). Без неё тесты пропускаются: в репозитории нет ни одной реальной
конфигурации, и привязываться к чьим-то конкретным проектам тесты не
должны.

    ARIADNA_CORPUS=demo,myconf python -m pytest tests -q -m slow

Для каждого проекта — полная переиндексация с нуля и общие для любой
конфигурации проверки: статус ready, есть модули и процедуры, при
генерации отчёта из XML — метаданные, строится дерево вызовов, работает
поиск, время прогона правдоподобное.

В data/ тесты ничего не пишут: выгрузки только читаются, а реестр,
индексы и сгенерированный report.txt — во временном каталоге
(tests/corpus_sandbox.py: собственный projects.json с теми же проектами и
абсолютными путями к реальным источникам). После каждого теста
проверяется, что записи проверяемых проектов в data/projects.json (по
содержимому), index.db* в data/projects/*/ и report.txt их источников не
изменились. Правки других проектов в реестре (Web UI во время прогона) не
мешают, поэтому контейнер на время прогона останавливать не нужно.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.call_analyzer import CallAnalyzer

sys.path.insert(0, str(Path(__file__).parent))
from corpus_sandbox import assert_data_unchanged, data_snapshot, sandbox_project_manager

DATA_DIR = Path(__file__).parent.parent / 'data'
CORPUS_ENV = 'ARIADNA_CORPUS'
CORPUS_PROJECTS = [p.strip() for p in os.environ.get(CORPUS_ENV, '').split(',') if p.strip()]

pytestmark = pytest.mark.slow


def _real_sources(project_id: str) -> list[dict]:
    """Источники проекта из реального реестра (только чтение)."""
    registry = DATA_DIR / 'projects.json'
    if not registry.exists():
        return []
    entry = json.loads(registry.read_text(encoding='utf-8')).get('projects', {}).get(project_id)
    return (entry or {}).get('sources', [])


def _has_dump(project_id: str, source: dict) -> bool:
    """В каталоге источника лежит XML-выгрузка, а не просто существует
    каталог: источник бывает симлинком на выгрузку, которую перенесли или
    удалили, — тогда тест должен пропуститься, а не падать на «0 модулей»."""
    xml_path = source.get('xml_path')
    if not xml_path:
        return False
    path = Path(xml_path)
    if not path.is_absolute():
        path = DATA_DIR / 'projects' / project_id / path
    return (path / 'Configuration.xml').exists()


@pytest.fixture(autouse=True)
def data_untouched():
    """После теста в data/ не изменилось ничего (см. докстринг модуля)."""
    if not CORPUS_PROJECTS or not (DATA_DIR / 'projects.json').exists():
        yield
        return
    before = data_snapshot(DATA_DIR, CORPUS_PROJECTS)
    yield
    assert_data_unchanged(DATA_DIR, before)


@pytest.fixture
def work_dir():
    # Не tmp_path: pytest хранит три последних basetemp, а здесь гигабайты индексов.
    with tempfile.TemporaryDirectory(prefix='ariadna-slow-') as tmp:
        yield Path(tmp)


@pytest.mark.parametrize('project_id', CORPUS_PROJECTS or [None])
def test_corpus_full_reindex(project_id, work_dir):
    """Проект из локального data/ индексируется с нуля, и по индексу можно работать."""
    if project_id is None:
        pytest.skip(f"не задан {CORPUS_ENV}: id проектов из data/projects.json через запятую, "
                    f"например {CORPUS_ENV}=demo,myconf")
    sources = _real_sources(project_id)
    if not sources:
        pytest.skip(f"проекта {project_id} нет в data/projects.json или у него нет источников")
    with_dump = [s for s in sources if _has_dump(project_id, s)]
    if not with_dump:
        pytest.skip(f"у проекта {project_id} ни в одном источнике нет XML-выгрузки "
                    f"(Configuration.xml) — выгрузку перенесли или удалили?")

    pm = sandbox_project_manager(DATA_DIR, [project_id], work_dir)
    try:
        t0 = time.time()
        stats = pm.reindex(project_id)
        elapsed = time.time() - t0

        proj = pm.get_project(project_id)
        assert proj.status == 'ready'
        assert stats.total_modules > 0, "в выгрузке есть Configuration.xml, а модулей не проиндексировано"
        assert stats.total_procedures > 0
        # D15: затенённая переменная таймера однажды давала duration_sec ≈ время эпохи.
        assert 0 < stats.duration_sec < 3600, f"duration_sec похоже на время эпохи: {stats.duration_sec}"

        # Метаданные — из Configuration.xml той же выгрузки.
        assert stats.total_objects > 0, "в выгрузке есть Configuration.xml, а метаданных нет"

        db = pm.get_db(project_id)
        conn = db.read_conn()
        # Дерево вызовов: процедура, у которой есть разрешённые вызовы.
        row = conn.execute(
            """SELECT p.name, m.name AS module_name FROM calls_resolved cr
               JOIN calls c ON c.id = cr.call_id
               JOIN procedures p ON p.id = c.caller_id
               JOIN modules m ON m.id = p.module_id LIMIT 1""").fetchone()
        if stats.total_calls:
            assert row is not None, "вызовы есть, а ни один не разрешён"
            tree = CallAnalyzer(db).build_call_tree(
                row['name'], module_name=row['module_name'], direction='down', depth=2)
            assert tree is not None and tree.children, \
                f"дерево вызовов {row['module_name']}.{row['name']} пустое"

        # Поиск: процедура и объект метаданных находятся по своему имени.
        engine = pm.get_search(project_id)
        proc = conn.execute("SELECT name FROM procedures LIMIT 1").fetchone()
        assert engine.search_procedures(proc['name']), f"search_procedures не нашёл {proc['name']}"
        if stats.total_objects:
            obj = conn.execute("SELECT name FROM metadata_objects LIMIT 1").fetchone()
            assert engine.search_metadata(obj['name']), f"search_metadata не нашёл {obj['name']}"

        print(f"\n[{project_id}] objects={stats.total_objects} modules={stats.total_modules} "
              f"procedures={stats.total_procedures} calls={stats.total_calls} "
              f"duration={elapsed:.1f}s")
    finally:
        pm.close_all()
