"""Регрессии восьмого раунда безопасной очистки и восстановления."""

import json
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.core import fs_safety, platform_check, project_manager as pm_module, source_delta
from src.core.db import Database
from src.core.fs_safety import RemovalResult
from src.core.project_manager import ProjectManager
from src.web import app as web
from tests.fixtures.generate_platform_help import context_book


def test_directory_is_unsafe_when_it_is_an_ancestor_of_another_project(tmp_path):
    projects = tmp_path / 'projects'
    parent = projects / 'one'
    protected = parent / 'nested'
    protected.mkdir(parents=True)
    assert not fs_safety.directory_is_safe(parent, projects, protected_roots=(protected,))


def test_startup_and_delete_pass_other_project_roots_to_report_cleanup(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('one', 'one')
    pm.create_project('two', 'two')
    original = platform_check.cleanup_report_temps
    calls = []

    def capture(directory, allowed_root=None, protected_roots=(), **kwargs):
        calls.append((directory, allowed_root, tuple(protected_roots)))
        return original(directory, allowed_root, protected_roots, **kwargs)

    monkeypatch.setattr(platform_check, 'cleanup_report_temps', capture)
    try:
        restarted = ProjectManager(str(tmp_path))
        assert any(tmp_path / 'projects' / 'two' in roots for _, _, roots in calls)
        calls.clear()
        restarted.delete_project('one')
        assert calls and all(tmp_path / 'projects' / 'two' in roots
                             for _, _, roots in calls)
        restarted.close_all()
    finally:
        pm.close_all()


def test_corrupt_registry_fallback_keeps_quarantined_records(tmp_path):
    pm = ProjectManager(str(tmp_path))
    try:
        project = pm.create_project('Capital', 'Capital')
        data = {'projects': {
            'Capital': pm._project_to_dict(project),
            'capital': {**pm._project_to_dict(project), 'id': 'capital'},
        }}
        pm.registry_path.write_text(json.dumps(data), encoding='utf-8')
        pm._registry = None
        pm.list_projects()
        pm.registry_path.write_text('{bad json', encoding='utf-8')
        pm._save_project_to_disk('Capital', project)
        saved = json.loads(pm.registry_path.read_text(encoding='utf-8'))['projects']
        assert set(saved) == {'Capital', 'capital'}
    finally:
        pm.close_all()


def test_utf8_project_id_limit_and_legacy_long_id_deletion(tmp_path):
    pm = ProjectManager(str(tmp_path))
    try:
        wide = chr(0x20000)
        with pytest.raises(ValueError, match='up to 64'):
            pm.create_project(wide * 64, 'too long in UTF-8')
        pm.create_project('old', 'old')
        data = json.loads(pm.registry_path.read_text(encoding='utf-8'))
        legacy_id = 'x' * 65
        item = data['projects'].pop('old')
        item['id'] = legacy_id
        data['projects'][legacy_id] = item
        pm.registry_path.write_text(json.dumps(data), encoding='utf-8')
        (pm.projects_dir / 'old').rename(pm.projects_dir / legacy_id)
        pm._registry = None
        assert pm.delete_project(legacy_id) is True
    finally:
        pm.close_all()


def test_post_delete_missing_project_returns_404(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    monkeypatch.setattr(web, '_pm', pm)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            response = client.post('/api/projects/missing/delete')
        assert response.status_code == 404
    finally:
        pm.close_all()


def test_project_creation_filesystem_error_is_a_clear_4xx(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    monkeypatch.setattr(web, '_pm', pm)
    original = type(pm.projects_dir).mkdir
    failed_dir = pm.projects_dir / 'disk_error'

    def fail_target(path, *args, **kwargs):
        if path == failed_dir:
            raise OSError('synthetic disk failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(pm.projects_dir), 'mkdir', fail_target)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            response = client.post('/api/projects', data={
                'project_id': 'disk_error', 'name': 'Disk error'})
        assert response.status_code == 400
        assert 'Не удалось создать каталог проекта' in response.text
    finally:
        pm.close_all()


def test_legacy_platform_help_migration_omits_project_index(tmp_path):
    service = tmp_path / 'index' / 'platform-help'
    version = service / '8.3.27.1'
    version.mkdir(parents=True)
    (version / 'help.db').write_bytes(b'help')
    (service / 'index.db').write_bytes(b'project index')
    pm = ProjectManager(str(tmp_path / 'data'), index_dir=str(tmp_path / 'index'))
    try:
        copied = pm.platform_help.root
        assert (copied / '8.3.27.1' / 'help.db').read_bytes() == b'help'
        assert not (copied / 'index.db').exists()
    finally:
        pm.close_all()


def test_forget_report_version_keeps_binding_when_archive_removal_fails(tmp_path, monkeypatch):
    directory = tmp_path / '.ariadna-service' / 'reports' / 'p'
    directory.mkdir(parents=True)
    archive = directory / ('platform-check-' + 'a' * 64 + '.zip')
    archive.write_bytes(b'archive')
    platform_check._record_report_archive(archive, '8.3.27.1')
    monkeypatch.setattr(platform_check, 'remove_path_safely',
                        lambda *args, **kwargs: RemovalResult(False, 'занят'))
    monkeypatch.setattr(platform_check, 'directory_is_safe', lambda *args, **kwargs: True)
    result = platform_check.forget_report_version('8.3.27.1', allowed_root=tmp_path)
    try:
        assert not result and result.reason == 'занят'
        assert platform_check._VERSION_ARCHIVES[directory.resolve()]['8.3.27.1'] == archive.name
    finally:
        platform_check.forget_report_project(tmp_path / 'p' / 'index.db')


def test_help_delete_passes_trusted_root_and_protected_projects(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('one', 'one')
    pm.create_project('two', 'two')
    pm.platform_help.build_from_bytes('8.3.27.1', context_book())
    monkeypatch.setattr(web, '_pm', pm)
    monkeypatch.setattr(pm, 'warm_platform_checks', lambda *args, **kwargs: None)
    seen = {}
    original = platform_check.forget_report_version

    def capture(version, allowed_root=None, protected_roots=()):
        seen.update(root=allowed_root, protected=tuple(protected_roots))
        return original(version, allowed_root, protected_roots)

    monkeypatch.setattr(platform_check, 'forget_report_version', capture)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            response = client.post('/api/admin/platform-help/8.3.27.1/delete')
        assert response.status_code == 200
        assert seen['root'] == pm.projects_dir
        assert pm.projects_dir / 'one' in seen['protected']
        assert pm.projects_dir / 'two' in seen['protected']
    finally:
        pm.close_all()


def test_report_save_checks_before_creating_temporary_zip(tmp_path, monkeypatch):
    from array import array

    directory = tmp_path / '.ariadna-service' / 'reports' / 'p'
    directory.mkdir(parents=True)
    path = directory / ('platform-check-' + 'a' * 64 + '.zip')
    report = platform_check._Report()
    report.count, report.starts, report.blocks = 0, [], []
    report.positions = {level: array('I') for level in platform_check.LEVELS}
    created = []
    real_temp = platform_check.NamedTemporaryFile

    def track_temp(*args, **kwargs):
        created.append(True)
        return real_temp(*args, **kwargs)

    monkeypatch.setattr(platform_check, 'directory_is_safe', lambda *args, **kwargs: False)
    monkeypatch.setattr(platform_check, 'NamedTemporaryFile', track_temp)
    assert report.save(path, 'key', before_replace=lambda: None,
                       allowed_root=tmp_path) is False
    assert not created
    assert not list(directory.glob('*.tmp'))


def test_corrupt_cold_registry_keeps_file_and_returns_empty_registry(tmp_path):
    registry = tmp_path / 'projects.json'
    registry.write_text('{broken json', encoding='utf-8')
    pm = ProjectManager(str(tmp_path))
    try:
        assert pm.list_projects() == []
        assert registry.read_text(encoding='utf-8') == '{broken json'
    finally:
        pm.close_all()


def test_help_migration_does_not_overwrite_or_resurrect_copied_files(tmp_path):
    legacy = tmp_path / 'platform-help'
    old_file = legacy / '8.3' / 'help.db'
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b'old')
    removed_file = legacy / '8.4' / 'help.db'
    removed_file.parent.mkdir(parents=True)
    removed_file.write_bytes(b'removed later')
    pm = ProjectManager(str(tmp_path))
    try:
        new_root = pm.platform_help.root
        assert (new_root / '8.3' / 'help.db').read_bytes() == b'old'
        (new_root / '8.3' / 'help.db').write_bytes(b'new')
        (new_root / '8.4' / 'help.db').unlink()
    finally:
        pm.close_all()

    restarted = ProjectManager(str(tmp_path))
    try:
        assert (restarted.platform_help.root / '8.3' / 'help.db').read_bytes() == b'new'
        assert not (restarted.platform_help.root / '8.4' / 'help.db').exists()
    finally:
        restarted.close_all()


def test_delete_finalization_preserves_another_process_pending_id(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('remove-me', 'remove-me')
    real = Database.forget_schema_lock

    def external_writer(index_path):
        data = json.loads(pm.registry_path.read_text(encoding='utf-8'))
        data['pending_deletions'] = ['remove-me', 'latest']
        pm._write_registry_file(data)
        return real(index_path)

    monkeypatch.setattr(Database, 'forget_schema_lock', staticmethod(external_writer))
    try:
        assert pm.delete_project('remove-me') is True
        data = json.loads(pm.registry_path.read_text(encoding='utf-8'))
        assert data['pending_deletions'] == ['latest']
    finally:
        pm.close_all()


def test_delete_alias_does_not_clear_reserved_canonical_directory(tmp_path):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('Straße', 'Straße')
    pm._pending_project_deletions.add('Straße')
    try:
        assert pm.delete_project('Strasse') is False
        assert (pm.projects_dir / 'Straße').exists()
        assert 'Straße' in pm._pending_project_deletions
        assert json.loads(pm.registry_path.read_text(encoding='utf-8'))['pending_deletions'] == []
    finally:
        pm.close_all()


def test_archive_trim_reports_directory_enumeration_failure(tmp_path, monkeypatch):
    directory = tmp_path / '.ariadna-service' / 'reports' / 'p'
    directory.mkdir(parents=True)
    published = directory / ('platform-check-' + 'a' * 64 + '.zip')
    published.write_bytes(b'zip')
    real_iterdir = type(directory).iterdir

    def fail_report_listing(path):
        if path == directory:
            raise PermissionError('listing denied')
        return real_iterdir(path)

    monkeypatch.setattr(type(directory), 'iterdir', fail_report_listing)
    result = platform_check._trim_archives(published, allowed_root=tmp_path)
    assert not result
    assert 'перечислить' in result.reason


def test_forget_help_version_refuses_archive_enumeration_failure(tmp_path, monkeypatch):
    directory = tmp_path / '.ariadna-service' / 'reports' / 'p'
    directory.mkdir(parents=True)
    archive = directory / ('platform-check-' + 'c' * 64 + '.zip')
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('report.json', json.dumps(['key', 0, [], '8.3.27.1']))
    platform_check._record_report_archive(archive, '8.3.27.1')
    real_iterdir = type(directory).iterdir

    def fail_report_listing(path):
        if path == directory:
            raise PermissionError('listing denied')
        return real_iterdir(path)

    monkeypatch.setattr(type(directory), 'iterdir', fail_report_listing)
    result = platform_check.forget_report_version('8.3.27.1', allowed_root=tmp_path)
    assert not result
    assert archive.exists()
    platform_check.forget_report_project(tmp_path / 'p' / 'index.db')


def test_help_delete_retries_report_cleanup_after_help_was_removed(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    version = '8.3.27.1'
    pm.platform_help.build_from_bytes(version, context_book())
    report_dir = platform_check.report_directory(pm._index_path('p'))
    report_dir.mkdir(parents=True, exist_ok=True)
    archive = report_dir / ('platform-check-' + 'a' * 64 + '.zip')
    archive.write_bytes(b'synthetic report')
    platform_check._record_report_archive(archive, version)
    monkeypatch.setattr(web, '_pm', pm)
    warmed = []
    monkeypatch.setattr(pm, 'warm_platform_checks', lambda *args, **kwargs: warmed.append((args, kwargs)))
    real_remove = platform_check.remove_path_safely
    attempts = []

    def fail_once(path, *args, **kwargs):
        if path == archive and not attempts:
            attempts.append(path)
            return RemovalResult(False, 'занят')
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(platform_check, 'remove_path_safely', fail_once)
    try:
        with TestClient(web.app, headers={'Host': '127.0.0.1'}) as client:
            first = client.post(f'/api/admin/platform-help/{version}/delete')
            assert archive.exists()
            assert not warmed
            second = client.post(f'/api/admin/platform-help/{version}/delete')
        assert 'занят' in first.text
        assert not archive.exists()
        assert 'занят' not in second.text
        assert len(warmed) == 1
    finally:
        pm.close_all()


def test_source_update_removes_manifest_cache_through_safe_helper(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('p', 'p')
    pm.add_source('p', 'main', 'main')
    cache = pm._source_manifest_cache_path('p', 'main')
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{}', encoding='utf-8')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('Configuration.xml', '<Configuration/>')
    archive.seek(0)
    original = pm_module.remove_path_safely
    seen = []

    def capture(path, *args, **kwargs):
        if path == cache:
            seen.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pm_module, 'remove_path_safely', capture)
    try:
        pm.update_source('p', 'main', xml_archive=archive, xml_archive_filename='source.zip')
        assert seen == [cache]
        assert not cache.exists()
    finally:
        pm.close_all()


def test_db_only_marks_schema_removed_when_all_removals_succeed(tmp_path, monkeypatch):
    db_path = tmp_path / 'p' / 'index.db'
    db_path.parent.mkdir()
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.executescript('CREATE TABLE calls (id INTEGER); CREATE TABLE index_state (id INTEGER);')
    monkeypatch.setattr('src.core.db.remove_path_safely',
                        lambda *args, **kwargs: RemovalResult(False, 'занят'))
    db = Database(str(db_path))
    db._recreate_if_stale_schema()
    assert not db.stale_schema_recreated
    assert db.rebuild_required
    assert not db.clear_rebuild_marker()


def test_delta_apply_stops_when_previous_recovery_fails(tmp_path, monkeypatch):
    import io
    import zipfile

    root = tmp_path / 'xml'
    root.mkdir()
    workspace = tmp_path / 'state'
    backup = workspace / 'rollback-held'
    backup.mkdir(parents=True)
    (backup / 'held.xml').write_bytes(b'old')
    journal = source_delta._journal_path(workspace)
    source_delta._atomic_json(journal, {
        'backup': str(backup), 'staging': '', 'old': [], 'new': [],
        'created_dirs': [], 'removed_dirs': [], 'committed': True,
    })
    current = source_delta.manifest(root, tmp_path / 'cache.json')
    target = {'version': current['version'], 'files': current['files']}
    monkeypatch.setattr(source_delta, 'remove_path_safely',
                        lambda *args, **kwargs: RemovalResult(False, 'занят'))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w'):
        pass
    archive.seek(0)
    with pytest.raises(source_delta.DeltaError, match='Не удалось восстановить'):
        source_delta.apply(root, archive, target, current['version'], current, workspace)
    assert journal.exists() and backup.exists()


def test_project_manager_delta_refuses_failed_recovery(tmp_path, monkeypatch):
    pm = ProjectManager(str(tmp_path))
    pm.create_project('p', 'p')
    pm.add_source('p', 'main', 'main')
    monkeypatch.setattr(pm, '_recover_source_delta', lambda *args, **kwargs: False)
    try:
        with pytest.raises(source_delta.DeltaError, match='Не удалось восстановить'):
            pm.apply_source_delta('p', 'main', io.BytesIO(b''), 'base', {'files': {}})
    finally:
        pm.close_all()


def test_orphan_cleanup_failure_is_reported_without_raising(tmp_path, monkeypatch):
    workspace = tmp_path / 'state'
    (workspace / 'rollback-held').mkdir(parents=True)
    monkeypatch.setattr(source_delta, 'remove_path_safely',
                        lambda *args, **kwargs: RemovalResult(False, 'занят'))
    assert source_delta.recover(tmp_path / 'xml', workspace) is False
