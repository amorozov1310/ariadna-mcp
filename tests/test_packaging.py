"""
Установка пакета (pip install .) и консольная команда ariadna-index.

Автопоиск setuptools принимал каталог src/ за src-раскладку: в колесе на
верхнем уровне оказывались core/, web/… без пакета src и без единого не-.py
файла (VERSION, шаблоны, статика, vendor/*.json), а ariadna-index указывала
на несуществующую src.core.indexer:main. Сборку колеса и запуск из чистого
окружения проверяет шаг CI (.github/workflows/tests.yml); здесь — быстрые
проверки без сборки.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.indexer import main as ariadna_index
from src.core.project_manager import ProjectManager

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / 'fixtures'


def test_pyproject_finds_src_package_explicitly():
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    assert 'include = ["src", "src.*"]' in pyproject
    assert '"ariadna-index" = "src.core.indexer:main"' in pyproject


def test_runtime_data_files_are_declared():
    """Каждый не-.py файл внутри src/, который код читает, попадает под
    package-data — сверка с деревом, чтобы новый шаблон не выпал из колеса."""
    import tomllib
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    package_data = data['tool']['setuptools']['package-data']
    covered = set()
    for package, patterns in package_data.items():
        base = ROOT.joinpath(*package.split('.'))
        for pattern in patterns:
            covered.update(p for p in base.glob(pattern) if p.is_file())
    runtime = {p for p in (ROOT / 'src').rglob('*')
               if p.is_file() and p.suffix not in ('.py', '.pyc')
               and '__pycache__' not in p.parts
               and not any(part.endswith('.egg-info') for part in p.parts)}
    assert runtime - covered == set(), sorted(str(p) for p in runtime - covered)


def test_ariadna_index_help_and_errors(capsys):
    with pytest.raises(SystemExit) as exc:
        ariadna_index(['--help'])
    assert exc.value.code == 0
    assert 'ariadna-index' in capsys.readouterr().out

    with tempfile.TemporaryDirectory() as tmpdir:
        assert ariadna_index(['nope', '--data-dir', tmpdir]) == 1
        assert 'nope' in capsys.readouterr().err
        assert ariadna_index(['p1', '--data-dir', str(Path(tmpdir) / 'нет')]) == 1


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


def test_ariadna_index_reindexes_project(capsys):
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as index_dir:
        pm = ProjectManager(data_dir)
        try:
            pm.create_project('p1', 'p1')
            with open(FIXTURES / 'report_ru.txt', 'rb') as f:
                pm.add_source('p1', 'main', 'Main', report_file=f,
                              xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        finally:
            pm.close_all()

        # Неизвестный источник — ошибка, статус проекта не трогается.
        assert ariadna_index(['p1', '--source', 'ghost', '--data-dir', data_dir]) == 1
        assert 'ghost' in capsys.readouterr().err

        assert ariadna_index(['p1', '--data-dir', data_dir, '--index-dir', index_dir]) == 0
        assert 'процедур' in capsys.readouterr().out
        assert (Path(index_dir) / 'p1' / 'index.db').is_file()
        pm = ProjectManager(data_dir, index_dir=index_dir)
        try:
            assert pm.get_project('p1').status == 'ready'
            assert pm.get_search('p1').search_procedures('РассчитатьЦену')
        finally:
            pm.close_all()


def test_starlette_1_7_required_everywhere():
    """Проверка Host в Web UI разрешает [::1]:порт только со Starlette >= 1.7
    (раньше TrustedHostMiddleware резал Host по первому «:»). Требование
    должно стоять и в пакете, и в офлайн-сборке образа."""
    import starlette
    major, minor = (int(x) for x in starlette.__version__.split('.')[:2])
    assert (major, minor) >= (1, 7), starlette.__version__
    assert '"starlette>=1.7"' in (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    assert '"starlette>=1.7"' in (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert '"starlette>=1.7"' in (ROOT / 'docker-wheels' / 'fetch.sh').read_text(encoding='utf-8')


def test_python_m_src_core_indexer_runs_cli():
    """`python -m src.core.indexer` — тот же CLI, что ariadna-index
    (раньше модуль без __main__ молча ничего не делал)."""
    import subprocess
    r = subprocess.run([sys.executable, '-m', 'src.core.indexer', '--help'],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert 'usage' in r.stdout
