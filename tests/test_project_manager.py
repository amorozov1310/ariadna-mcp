"""Tests for ProjectManager."""

import sys, os, shutil
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager

FIXTURE = Path(__file__).parent / 'fixtures' / 'xml_meta_ru'
TD = '/tmp/test_pm'


def _setup():
    if os.path.exists(TD):
        shutil.rmtree(TD)


def _make_project(pm, pid='proj'):
    pm.create_project(pid, f'Test {pid}')
    sd = Path(TD) / 'projects' / pid / 'sources' / 'main'
    sd.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, sd / 'xml')
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
    assert proj.sources[0].xml_path == 'sources/main/xml'
    pm.close_all()


def test_reindex():
    _setup()
    pm = ProjectManager(TD)
    _make_project(pm)
    stats = pm.reindex('proj')
    assert stats.total_objects == 48
    assert pm.get_project('proj').status == 'ready'
    pm.close_all()


def test_multi_source():
    _setup()
    pm = ProjectManager(TD)
    pm.create_project('multi', 'Multi')
    for sid in ['main', 'ext1']:
        sd = Path(TD) / 'projects' / 'multi' / 'sources' / sid
        sd.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE, sd / 'xml')
        pm.add_source('multi', sid, f'Source {sid}',
                       source_type='main' if sid == 'main' else 'extension')
    stats = pm.reindex('multi')
    assert stats.total_objects == 96
    pm.close_all()


def test_project_isolation():
    _setup()
    pm = ProjectManager(TD)
    for pid in ['a', 'b']:
        _make_project(pm, pid)
        pm.reindex(pid)
    ra = pm.get_search('a').search_metadata('Склады')
    rb = pm.get_search('b').search_metadata('Склады')
    assert len(ra) == len(rb)
    pm.close_all()


def test_search_after_reindex():
    _setup()
    pm = ProjectManager(TD)
    _make_project(pm)
    pm.reindex('proj')
    engine = pm.get_search('proj')
    assert len(engine.search_metadata('Склады')) > 0
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


def test_project_ids_are_casefold_unique_and_bounded(tmp_path):
    pm = ProjectManager(str(tmp_path))
    try:
        pm.create_project('Capital', 'Capital')
        with pytest.raises(ValueError, match='case-insensitive'):
            pm.create_project('capital', 'capital')
        with pytest.raises(ValueError, match='up to 64'):
            pm.create_project('x' * 256, 'Too long')
        for reserved in ('reports', 'REPORTS', 'platform-help', 'PLATFORM-HELP'):
            with pytest.raises(ValueError, match='reserved'):
                pm.create_project(reserved, reserved)
    finally:
        pm.close_all()


@pytest.mark.parametrize('project_id', [
    'CON', 'prn', 'AUX', 'nul', 'com1', 'COM9', 'lpt1', 'LPT9',
    'con.txt', 'NUL.log', 'com1.anything', 'lpt9.', 'AUX ',
])
def test_windows_reserved_project_ids_are_rejected(tmp_path, project_id):
    pm = ProjectManager(str(tmp_path))
    try:
        with pytest.raises(ValueError, match='reserved by Windows'):
            pm.create_project(project_id, project_id)
    finally:
        pm.close_all()


def test_web_rejects_windows_reserved_project_id_as_bad_request(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    pm = ProjectManager(str(tmp_path))
    monkeypatch.setattr(app_module, '_pm', pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        response = client.post('/api/projects', data={
            'project_id': 'NUL', 'name': 'Reserved', 'description': ''})
        assert response.status_code == 400
        assert 'reserved by Windows' in response.json()['detail']
    finally:
        pm.close_all()


def test_casefold_conflicts_in_legacy_registry_are_logged_and_quarantined(tmp_path, caplog):
    import json

    pm = ProjectManager(str(tmp_path))
    try:
        first = pm.create_project('Capital', 'Capital')
        data = {'projects': {
            'Capital': pm._project_to_dict(first),
            'capital': {**pm._project_to_dict(first), 'id': 'capital', 'name': 'capital'},
        }}
        pm.registry_path.write_text(json.dumps(data), encoding='utf-8')
        pm._registry = None
        projects = pm.list_projects()
        assert [project.id for project in projects] == ['Capital']
        assert 'Capital' in caplog.text and 'capital' in caplog.text
        assert 'capital' in json.loads(pm.registry_path.read_text(encoding='utf-8'))['projects']
        with pytest.raises(ValueError, match='исправьте projects.json'):
            pm._index_path('capital')
        with pytest.raises(ValueError, match='conflict|конфликт'):
            pm.delete_project('Capital')
    finally:
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
