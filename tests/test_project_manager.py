"""Tests for ProjectManager."""

import sys, os, shutil, json
from threading import Event, Thread
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


def test_registry_write_failure_rolls_back_create_and_delete(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    original = pm._write_registry_file
    monkeypatch.setattr(pm, '_write_registry_file', lambda _data: (_ for _ in ()).throw(OSError('held')))
    with pytest.raises(OSError):
        pm.create_project('retry', 'Retry')
    assert pm.list_projects() == []

    monkeypatch.setattr(pm, '_write_registry_file', original)
    pm.create_project('retry', 'Retry')
    monkeypatch.setattr(pm, '_write_registry_file', lambda _data: (_ for _ in ()).throw(OSError('held')))
    with pytest.raises(OSError):
        pm.delete_project('retry')
    assert pm.get_project('retry').id == 'retry'
    assert json.loads(pm.registry_path.read_text(encoding='utf-8'))['projects'].keys() == {'retry'}
    pm.close_all()


def test_protected_roots_include_quarantined_project_records(tmp_path):
    pm = ProjectManager(str(tmp_path), index_dir=str(tmp_path / 'index'))
    try:
        project = pm.create_project('Capital', 'Capital')
        data = {'projects': {
            'Capital': pm._project_to_dict(project),
            'capital': {**pm._project_to_dict(project), 'id': 'capital', 'name': 'capital'},
        }}
        pm.registry_path.write_text(json.dumps(data), encoding='utf-8')
        pm._registry = None
        pm.list_projects()
        roots = pm._protected_project_roots()
        assert pm.projects_dir / 'capital' in roots
        assert pm.index_dir / 'capital' in roots
    finally:
        pm.close_all()


def test_protected_roots_cover_legacy_service_project_ids(tmp_path):
    index_dir = tmp_path / 'index'
    pm = ProjectManager(str(tmp_path / 'data'), index_dir=str(index_dir))
    try:
        project = pm.create_project('demo', 'Demo')
        projects = {}
        for project_id in ('reports', 'platform-help'):
            projects[project_id] = {**pm._project_to_dict(project),
                                    'id': project_id, 'name': project_id}
        pm.registry_path.write_text(json.dumps({'projects': projects}), encoding='utf-8')
        pm._registry = None
        pm.list_projects()
        roots = pm._protected_project_roots()
        assert pm.projects_dir / 'reports' in roots
        assert index_dir / 'reports' / 'index.db' in roots
        assert index_dir / 'reports' not in roots
        assert pm.projects_dir / 'platform-help' in roots
        assert index_dir / 'platform-help' in roots
    finally:
        pm.close_all()


def test_registration_waits_until_project_file_cleanup_finishes(tmp_path, monkeypatch):
    from src.core import project_manager as project_manager_module

    pm = ProjectManager(str(tmp_path))
    pm.create_project('race', 'Old')
    project_dir = pm.projects_dir / 'race'
    entered_delete, finish_delete, create_started, create_done = (Event() for _ in range(4))
    create_errors = []
    original = project_manager_module.remove_path_safely

    def paused(path, *args, **kwargs):
        if Path(path) == project_dir and kwargs.get('recursive', True):
            entered_delete.set()
            assert finish_delete.wait(5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(project_manager_module, 'remove_path_safely', paused)
    deleting = Thread(target=pm.delete_project, args=('race',))
    deleting.start()
    assert entered_delete.wait(5)

    def recreate():
        create_started.set()
        try:
            pm.create_project('race', 'New')
        except ValueError as exc:
            create_errors.append(exc)
        create_done.set()

    creating = Thread(target=recreate)
    creating.start()
    assert create_started.wait(5)
    assert not create_done.wait(0.1)
    finish_delete.set()
    deleting.join(5)
    creating.join(5)
    assert not deleting.is_alive() and not creating.is_alive()
    assert create_errors
    pm.create_project('race', 'New')
    assert pm.get_project('race').name == 'New'
    assert project_dir.is_dir()
    pm.close_all()


def test_source_ids_are_safe_and_casefold_unique(tmp_path):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('demo', 'Demo')
    pm.add_source('demo', 'Main', 'Main')
    with pytest.raises(ValueError, match='already exists'):
        pm.add_source('demo', 'main', 'Duplicate')
    for invalid in ('../escape', 'a/b', 'a\\b', '', 'CON'):
        with pytest.raises(ValueError, match='source_id|ID источника'):
            pm.add_source('demo', invalid, 'Invalid')
    pm.close_all()


def test_async_reindex_refuses_failed_source_recovery(tmp_path, monkeypatch):
    from src.core import source_delta

    pm = ProjectManager(str(tmp_path))
    pm.create_project('demo', 'Demo')
    pm.add_source('demo', 'main', 'Main')
    monkeypatch.setattr(source_delta, 'recover', lambda *args, **kwargs: False)
    result = pm.reindex_async('demo', source_id='main')
    assert result['status'] == 'error'
    assert pm.get_project('demo').status == 'empty'
    pm.close_all()


def test_sync_reindex_refuses_failed_source_recovery(tmp_path, monkeypatch):
    from src.core import source_delta

    pm = ProjectManager(str(tmp_path))
    pm.create_project('demo', 'Demo')
    pm.add_source('demo', 'main', 'Main')
    monkeypatch.setattr(source_delta, 'recover', lambda *args, **kwargs: False)
    with pytest.raises(source_delta.DeltaError):
        pm.reindex('demo')
    assert not pm._index_path('demo').exists()
    pm.close_all()


def test_unregistered_project_delete_does_not_remove_service_report_directory(tmp_path):
    pm = ProjectManager(str(tmp_path), index_dir=str(tmp_path / 'index'))
    report_dir = pm.index_dir / '.ariadna-service' / 'reports' / 'missing'
    report_dir.mkdir(parents=True)
    marker = report_dir / 'platform-check.zip'
    marker.write_bytes(b'keep')
    assert pm.delete_project('missing') is False
    assert marker.read_bytes() == b'keep'
    pm.close_all()


def test_project_version_updates_move_report_selection(tmp_path):
    from src.core import platform_check

    pm = ProjectManager(str(tmp_path))
    pm.create_project('demo', 'Demo')
    pm.update_project('demo', platform_version='8.3.10.1')
    pm.update_project('demo', platform_version='8.3.11.1')
    directory = platform_check.report_directory(pm._index_path('demo')).resolve()
    assert platform_check._SELECTED_VERSIONS[directory] == '8.3.11.1'
    pm.close_all()


def test_help_migration_resumes_partial_copy_and_skips_report_archives(tmp_path):
    data, indexes = tmp_path / 'data', tmp_path / 'index'
    legacy = indexes / 'platform-help' / '8.3.27.1'
    legacy.mkdir(parents=True)
    (legacy / 'help.db').write_bytes(b'help')
    (legacy / 'platform-check.zip').write_bytes(b'project report')
    new_version = indexes / '.ariadna-service' / 'platform-help' / '8.3.27.1'
    new_version.mkdir(parents=True)
    (new_version / 'partial-copy').write_text('partial', encoding='utf-8')
    pm = ProjectManager(str(data), index_dir=str(indexes))
    assert (new_version / 'help.db').read_bytes() == b'help'
    assert (new_version / 'partial-copy').read_text(encoding='utf-8') == 'partial'
    assert not (new_version / 'platform-check.zip').exists()
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
    'COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³',
    'com¹.txt', 'LPT².log', 'COM³.', 'lpt¹ ',
])
def test_windows_reserved_project_ids_are_rejected(tmp_path, project_id):
    pm = ProjectManager(str(tmp_path))
    try:
        with pytest.raises(ValueError, match='reserved by Windows'):
            pm.create_project(project_id, project_id)
    finally:
        pm.close_all()


@pytest.mark.parametrize('project_id', ['COM0', 'LPT0', 'COM10', 'LPT10', 'COM⁴', 'LPT⁴'])
def test_non_reserved_windows_device_name_edges_remain_valid(tmp_path, project_id):
    pm = ProjectManager(str(tmp_path))
    try:
        pm.create_project(project_id, project_id)
        assert pm.delete_project(project_id) is True
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


def test_web_returns_clear_400_for_empty_project_id(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    pm = ProjectManager(str(tmp_path))
    monkeypatch.setattr(app_module, '_pm', pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        response = client.post('/api/projects', data={'project_id': '', 'name': 'Empty'})
        assert response.status_code == 400
        assert 'Invalid project_id' in response.json()['detail']
    finally:
        pm.close_all()


@pytest.mark.parametrize('project_id', ['NUL', 'COM1.', 'PROGRA~1'])
@pytest.mark.parametrize('method', ['delete', 'post'])
def test_web_project_deletion_id_validation_is_400(tmp_path, monkeypatch, project_id, method):
    from fastapi.testclient import TestClient
    from src.web import app as app_module

    pm = ProjectManager(str(tmp_path))
    monkeypatch.setattr(app_module, '_pm', pm)
    try:
        client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878'})
        response = getattr(client, method)(f'/api/projects/{project_id}' + ('/delete' if method == 'post' else ''))
        assert response.status_code == 400
        assert 'Invalid project_id' in response.json()['detail'] or 'reserved by Windows' in response.json()['detail']
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
