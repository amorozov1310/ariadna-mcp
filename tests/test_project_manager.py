"""Tests for ProjectManager."""

import sys, os, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

FIXTURE = Path(__file__).parent / 'fixtures' / 'report_ru.txt'
TD = '/tmp/test_pm'


def _setup():
    if os.path.exists(TD):
        shutil.rmtree(TD)


def _make_project(pm, pid='proj'):
    pm.create_project(pid, f'Test {pid}')
    sd = Path(TD) / 'projects' / pid / 'sources' / 'main'
    sd.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, sd / 'report.txt')
    pm.add_source(pid, 'main', 'Main Config')


def test_create_project():
    _setup()
    pm = ProjectManager(TD)
    proj = pm.create_project('test1', 'Test Project', 'Описание')
    assert proj.id == 'test1'
    assert proj.name == 'Test Project'
    assert proj.status == 'empty'
    pm.close_all()


def test_list_projects():
    _setup()
    pm = ProjectManager(TD)
    pm.create_project('p1', 'P1')
    pm.create_project('p2', 'P2')
    assert len(pm.list_projects()) == 2
    pm.close_all()


def test_delete_project():
    _setup()
    pm = ProjectManager(TD)
    pm.create_project('del', 'Delete Me')
    assert len(pm.list_projects()) == 1
    pm.delete_project('del')
    assert len(pm.list_projects()) == 0
    assert not (Path(TD) / 'projects' / 'del').exists()
    pm.close_all()


def test_add_source_volume():
    _setup()
    pm = ProjectManager(TD)
    _make_project(pm)
    proj = pm.get_project('proj')
    assert len(proj.sources) == 1
    assert proj.sources[0].report_path == 'sources/main/report.txt'
    pm.close_all()


def test_reindex():
    _setup()
    pm = ProjectManager(TD)
    _make_project(pm)
    stats = pm.reindex('proj')
    assert stats.total_objects == 159
    assert pm.get_project('proj').status == 'ready'
    pm.close_all()


def test_multi_source():
    _setup()
    pm = ProjectManager(TD)
    pm.create_project('multi', 'Multi')
    for sid in ['main', 'ext1']:
        sd = Path(TD) / 'projects' / 'multi' / 'sources' / sid
        sd.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE, sd / 'report.txt')
        pm.add_source('multi', sid, f'Source {sid}',
                       source_type='main' if sid == 'main' else 'extension')
    stats = pm.reindex('multi')
    assert stats.total_objects > 159
    pm.close_all()


def test_project_isolation():
    _setup()
    pm = ProjectManager(TD)
    for pid in ['a', 'b']:
        _make_project(pm, pid)
        pm.reindex(pid)
    ra = pm.get_search('a').search_metadata('Семена')
    rb = pm.get_search('b').search_metadata('Семена')
    assert len(ra) == len(rb)
    pm.close_all()


def test_search_after_reindex():
    _setup()
    pm = ProjectManager(TD)
    _make_project(pm)
    pm.reindex('proj')
    engine = pm.get_search('proj')
    assert len(engine.search_metadata('Семена')) > 0
    pm.close_all()


def test_duplicate_project_raises():
    _setup()
    pm = ProjectManager(TD)
    pm.create_project('dup', 'First')
    try:
        pm.create_project('dup', 'Second')
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    pm.close_all()


def test_invalid_project_id():
    _setup()
    pm = ProjectManager(TD)
    try:
        pm.create_project('bad id!', 'Bad')
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    pm.close_all()


def test_get_nonexistent():
    _setup()
    pm = ProjectManager(TD)
    try:
        pm.get_project('nope')
        assert False, "Should have raised KeyError"
    except KeyError:
        pass
    pm.close_all()


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  ✅ {name}')
            except Exception as e:
                print(f'  ❌ {name}: {e}')
    _setup()  # cleanup
