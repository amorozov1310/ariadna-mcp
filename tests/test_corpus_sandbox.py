"""
Быстрая проверка песочницы медленных тестов (tests/corpus_sandbox.py) на
фикстурах — в облаке реальных корпусов нет.

«Реальный» data/ собирается из фикстур так же, как его заполняет Web UI:
реестр, выгрузки в projects/{id}/sources/{id}/xml, у одного источника
report.txt есть, у другого нет (его сгенерирует индексация). Затем проект
индексируется и опрашивается через песочницу, и проверяется, что в
«реальном» data/ не изменился ни один файл, а индексы, реестр и report.txt
оказались во временном каталоге.
"""

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core import report_generator
from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

from corpus_sandbox import assert_data_unchanged, data_snapshot, sandbox_project_manager

FIXTURES = Path(__file__).parent / 'fixtures'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


def _real_data(root: Path) -> Path:
    data = root / 'real-data'
    pm = ProjectManager(str(data))
    pm.create_project('corpus', 'Корпус')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source('corpus', 'main', 'Main', report_file=f,
                      xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
    pm.add_source('corpus', 'ext', 'Ext', source_type='extension',
                  xml_archive=_zip(FIXTURES / 'xml_en'), xml_archive_filename='x.zip')
    pm.close_all()
    return data


def _tree(root: Path) -> dict:
    """Все файлы с mtime и размером — полная картина «реального» data/."""
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob('*') if p.is_file()}


def test_sandbox_reads_corpus_and_writes_nothing_to_data(monkeypatch):
    generated = []

    def fake_generate(xml_path, report_dest):
        generated.append(Path(report_dest))
        Path(report_dest).parent.mkdir(parents=True, exist_ok=True)
        Path(report_dest).write_bytes((FIXTURES / 'report_en.txt').read_bytes())
        return True

    monkeypatch.setattr(report_generator, 'generate_report', fake_generate)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real = _real_data(root)
        tree_before = _tree(real)
        snapshot = data_snapshot(real, ['corpus'])

        work = root / 'work'
        pm = sandbox_project_manager(real, ['corpus', 'нет-такого'], work)
        try:
            # Источники указывают на реальные выгрузки абсолютными путями.
            xml = pm.get_source_xml_path('corpus', 'main')
            assert xml == (real / 'projects' / 'corpus' / 'sources' / 'main' / 'xml').resolve()
            assert pm.get_source_report_path('corpus', 'main').is_file()
            with pytest.raises(KeyError):
                pm.get_project('нет-такого')

            stats = pm.reindex('corpus')
            assert pm.get_project('corpus').status == 'ready'
            assert stats.total_procedures > 0 and stats.total_objects > 0

            # report.txt источника без отчёта сгенерирован в песочницу.
            assert generated == [work / 'data' / 'projects' / 'corpus' / 'sources' / 'ext' / 'report.txt']
            assert pm.get_source_report_path('corpus', 'ext') == generated[0]

            # Поиск, код процедуры и diagnose_index читают файлы корпуса.
            code = execute_tool(pm, 'get_procedure_code', {
                'project_id': 'corpus', 'module_name': 'ЦеныСервер', 'procedure_name': 'РассчитатьЦену'})
            assert 'Функция РассчитатьЦену' in code and 'изменён' not in code, code
            assert 'Found' in execute_tool(pm, 'search_code', {'project_id': 'corpus', 'query': 'Возврат'})
            assert 'BSL files scanned' in execute_tool(pm, 'diagnose_index', {'project_id': 'corpus'})

            # Индекс — во временном каталоге.
            assert (work / 'index' / 'corpus' / 'index.db').is_file()
        finally:
            pm.close_all()

        assert_data_unchanged(real, snapshot)
        assert _tree(real) == tree_before
        assert not list((real / 'projects').glob('*/index.db*'))


def _edit_registry(real: Path, edit) -> None:
    registry = real / 'projects.json'
    data = json.loads(registry.read_text(encoding='utf-8'))
    edit(data['projects'])
    registry.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _other_project(real: Path) -> None:
    """Проект, который тест в песочницу не берёт, — его правят в Web UI."""
    pm = ProjectManager(str(real))
    pm.create_project('other', 'Чужой')
    pm.close_all()


def test_snapshot_ignores_other_projects_in_registry():
    """Вживую: во время медленного теста в Web UI создали и удалили проект —
    размер projects.json тот же, mtime новый, и тест падал."""
    with tempfile.TemporaryDirectory() as tmp:
        real = _real_data(Path(tmp))
        _other_project(real)
        before = data_snapshot(real, ['corpus'])

        pm = ProjectManager(str(real))
        pm.update_project('other', status='ready', description='правка в Web UI')
        pm.create_project('created', 'Создан и удалён')
        pm.delete_project('created')
        pm.close_all()
        # Та же запись corpus, перезаписанная другим экземпляром (новый mtime).
        _edit_registry(real, lambda projects: None)

        assert_data_unchanged(real, before)


def _set_status(projects):
    projects['corpus']['status'] = 'indexing'


def _set_modules(projects):
    projects['corpus']['index_stats']['total_modules'] = 5


@pytest.mark.parametrize('edit, expected', [
    (_set_status, "проект corpus, поле status: 'empty' → 'indexing'"),
    (_set_modules, 'проект corpus, поле index_stats.total_modules: 0 → 5'),
])
def test_snapshot_catches_change_of_sandboxed_project(edit, expected):
    with tempfile.TemporaryDirectory() as tmp:
        real = _real_data(Path(tmp))
        _other_project(real)
        before = data_snapshot(real, ['corpus'])
        _edit_registry(real, edit)
        with pytest.raises(AssertionError) as exc:
            assert_data_unchanged(real, before)
        assert expected in str(exc.value), str(exc.value)


def test_snapshot_catches_new_index_and_changed_report():
    with tempfile.TemporaryDirectory() as tmp:
        real = _real_data(Path(tmp))
        before = data_snapshot(real, ['corpus'])
        (real / 'projects' / 'corpus' / 'index.db').write_bytes(b'')
        with pytest.raises(AssertionError) as exc:
            assert_data_unchanged(real, before)
        assert 'появились файлы индекса' in str(exc.value) and 'index.db' in str(exc.value)

        real2 = _real_data(Path(tmp) / 'second')
        before = data_snapshot(real2, ['corpus'])
        report = real2 / 'projects' / 'corpus' / 'sources' / 'main' / 'report.txt'
        report.write_bytes(report.read_bytes() + b'\n')
        with pytest.raises(AssertionError) as exc:
            assert_data_unchanged(real2, before)
        assert 'report.txt' in str(exc.value)
