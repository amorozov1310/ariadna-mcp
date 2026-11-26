"""Проверки файловой разницы источника на временных каталогах."""

import hashlib
import io
import json
import os
import zipfile

import pytest

from src.core import source_delta


def _target(files):
    public = {p: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
              for p, data in sorted(files.items())}
    version = hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()
    return {"version": version, "files": public}


def _archive(files):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    result.seek(0)
    return result


def test_manifest_cache_and_delta_add_change_delete(tmp_path):
    root = tmp_path / "xml"
    cache = tmp_path / "project" / "source.json"
    (root / "CommonModules" / "Тест").mkdir(parents=True)
    (root / "CommonModules" / "Тест" / "Module.bsl").write_bytes("старое".encode())
    (root / "gone.xml").write_bytes(b"remove")
    (root / "Template.Form.xml").write_bytes(b"keep")
    old = source_delta.manifest(root, cache)
    assert "Template.Form.xml" not in old["files"]
    desired_files = {"CommonModules/Тест/Module.bsl": "новое".encode(), "new.xml": b"new"}
    target = _target(desired_files)
    result = source_delta.apply(root, _archive(desired_files), target, old["version"], old)
    assert result == {"applied": 2, "deleted": 1}
    assert (root / "CommonModules" / "Тест" / "Module.bsl").read_bytes() == "новое".encode()
    assert (root / "new.xml").read_bytes() == b"new"
    assert not (root / "gone.xml").exists()
    assert (root / "Template.Form.xml").read_bytes() == b"keep"


def test_empty_delta_still_returns_same_manifest_and_conflict(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    target = _target({})
    assert source_delta.apply(root, _archive({}), target, current["version"], current) == {
        "applied": 0, "deleted": 0}
    with pytest.raises(source_delta.DeltaConflict):
        source_delta.apply(root, _archive({}), target, "stale", current)


@pytest.mark.parametrize("name", ["../escape", "/rooted", "C:/drive", "a\\b", "Template.Form.xml"])
def test_manifest_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(source_delta.DeltaError):
        source_delta._path(name)


def test_zip_rejects_case_insensitive_duplicate_paths(tmp_path):
    root = tmp_path / "xml"
    current = source_delta.manifest(root, tmp_path / "cache.json")
    target = _target({"a.xml": b"new"})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.xml", b"new")
        zf.writestr("A.xml", b"new")
    archive.seek(0)
    with pytest.raises(source_delta.DeltaError, match="Повтор"):
        source_delta.apply(root, archive, target, current["version"], current)


def test_zip_rejects_symlink_entry(tmp_path):
    root = tmp_path / "xml"
    current = source_delta.manifest(root, tmp_path / "cache.json")
    target = _target({"link.xml": b"target"})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        item = zipfile.ZipInfo("link.xml")
        item.create_system = 3
        item.external_attr = (0o120777 << 16)
        zf.writestr(item, b"target")
    archive.seek(0)
    with pytest.raises(source_delta.DeltaError, match="тип файла"):
        source_delta.apply(root, archive, target, current["version"], current)


def test_target_rejects_case_mismatched_shared_parent(tmp_path):
    root = tmp_path / "xml"
    current = source_delta.manifest(root, tmp_path / "cache.json")
    files = {"A/one.xml": b"1", "a/two.xml": b"2"}
    target = _target(files)
    with pytest.raises(source_delta.DeltaError, match="регистр"):
        source_delta.apply(root, _archive(files), target, current["version"], current)


def test_empty_delta_preserves_empty_excluded_directories(tmp_path):
    root = tmp_path / "xml"
    (root / "Template.Отчет").mkdir(parents=True)
    (root / "logs").mkdir()
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    target = _target({})
    source_delta.apply(root, _archive({}), target, current["version"], current)
    assert (root / "Template.Отчет").is_dir()
    assert (root / "logs").is_dir()


def test_target_manifest_uses_same_exclusion_filter_as_manifest(tmp_path):
    root = tmp_path / "xml"
    current = source_delta.manifest(root, tmp_path / "cache.json")
    for path in ("logs/new.txt", "Template.Отчет.xml"):
        target = _target({path: b"ignored"})
        with pytest.raises(source_delta.DeltaError):
            source_delta.apply(root, _archive({path: b"ignored"}), target,
                               current["version"], current)


def test_delta_delete_large_majority_requires_full_transfer(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    for i in range(source_delta.SUSPICIOUS_DELETE_FLOOR + 1):
        (root / f"{i:04}.xml").write_bytes(b"x")
    current = source_delta.manifest(root, tmp_path / "cache.json")
    target = _target({})
    with pytest.raises(source_delta.DeltaError, match="полную передачу"):
        source_delta.apply(root, _archive({}), target, current["version"], current)
    assert len(list(root.glob("*.xml"))) == source_delta.SUSPICIOUS_DELETE_FLOOR + 1


def test_reparse_point_attributes_are_detected_on_python_311(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(source_delta.os, "lstat", lambda _path: SimpleNamespace(st_file_attributes=0x400))
    assert source_delta.is_reparse_point(tmp_path / "junction")


def test_recover_cleans_staging_and_created_empty_directories(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    workspace = tmp_path / "state"
    workspace.mkdir()
    staging = workspace / "staging-orphan"
    staging.mkdir()
    backup = workspace / "rollback"
    backup.mkdir()
    (root / "new-parent").mkdir()
    source_delta._atomic_json(source_delta._journal_path(workspace), {
        "backup": str(backup), "staging": str(staging), "old": [], "new": [],
        "created_dirs": ["new-parent"], "committed": False,
    })
    source_delta.recover(root, workspace)
    assert not staging.exists()
    assert not backup.exists()
    assert not (root / "new-parent").exists()


def test_directory_case_rename_uses_target_spelling(tmp_path):
    root = tmp_path / "xml"
    old_path = root / "OldParent" / "Nested" / "file.xml"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"old")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    wanted = {"newparent/Nested/file.xml": b"new"}
    target = _target(wanted)
    source_delta.apply(root, _archive(wanted), target, current["version"], current)
    assert source_delta.manifest(root, cache)["files"] == target["files"]
    assert (root / "newparent" / "Nested" / "file.xml").read_bytes() == b"new"


def test_nested_file_to_top_level_file_prunes_only_managed_ancestors(tmp_path):
    root = tmp_path / "xml"
    deep = root / "P" / "Q" / "R" / "a.xml"
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b"old")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    wanted = {"P": b"new"}
    target = _target(wanted)
    source_delta.apply(root, _archive(wanted), target, current["version"], current)
    assert (root / "P").is_file()
    assert (root / "P").read_bytes() == b"new"
    assert source_delta.manifest(root, cache)["files"] == target["files"]


def test_project_startup_recovers_source_without_xml_path(tmp_path):
    from src.core.project_manager import ProjectManager

    data = tmp_path / "data"
    pm = ProjectManager(data)
    pm.create_project("p", "p")
    pm.add_source("p", "s", "source")
    root = data / "projects" / "p" / "sources" / "s" / "xml"
    root.mkdir(parents=True)
    workspace = pm._source_delta_workspace("p", "s")
    workspace.mkdir(parents=True)
    backup = workspace / "rollback"
    backup.mkdir()
    (backup / "old.xml").write_bytes(b"old")
    (root / "new.xml").write_bytes(b"new")
    staging = workspace / "staging-leftover"
    staging.mkdir()
    source_delta._atomic_json(source_delta._journal_path(workspace), {
        "backup": str(backup), "staging": str(staging), "old": ["old.xml"],
        "new": ["new.xml"], "created_dirs": [], "committed": False,
    })
    pm.close_all()
    resumed = ProjectManager(data)
    try:
        assert (root / "old.xml").read_bytes() == b"old"
        assert not (root / "new.xml").exists()
        assert not source_delta._journal_path(workspace).exists()
        assert not staging.exists()
        assert resumed.get_project("p").sources[0].xml_path == ''
        manifest = resumed.source_file_manifest('p', 's')
        wanted = {'new.xml': b'new'}
        target = _target(wanted)
        archive = _archive(wanted)
        resumed.apply_source_delta('p', 's', archive, manifest['version'], target)
        assert (root / 'new.xml').read_bytes() == b'new'
        assert resumed.get_project('p').sources[0].xml_path == 'sources/s/xml'
    finally:
        resumed.close_all()


def test_project_startup_does_not_recover_external_corpus_journal(tmp_path):
    from src.core.project_manager import ProjectManager

    external = tmp_path / "corpus" / "xml"
    external.mkdir(parents=True)
    (external / "new.xml").write_bytes(b"new")
    journal = external.parent / ".xml.delta-journal.json"
    backup = external.parent / "rollback"
    backup.mkdir()
    (backup / "old.xml").write_bytes(b"old")
    journal.write_text(json.dumps({"backup": str(backup), "old": ["old.xml"],
                                   "new": ["new.xml"], "committed": False}), encoding="utf-8")
    data = tmp_path / "sandbox"
    pm = ProjectManager(data)
    pm.create_project("p", "p")
    pm.add_source("p", "s", "source")
    project = pm.get_project("p")
    project.sources[0].xml_path = str(external)
    pm._save_registry()
    pm.close_all()
    resumed = ProjectManager(data)
    try:
        assert journal.exists()
        assert (backup / "old.xml").read_bytes() == b"old"
        assert (external / "new.xml").read_bytes() == b"new"
    finally:
        resumed.close_all()


def test_build_delta_fails_when_walk_reports_permission_error(monkeypatch, tmp_path):
    from scripts import build_delta

    root = tmp_path / "dump"
    root.mkdir()
    baseline = tmp_path / "manifest.json"
    baseline.write_text(json.dumps({"version": "base", "files": {}}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["build_delta.py", str(root), str(baseline),
                                      str(tmp_path / "delta.zip"), str(tmp_path / "target.json")])

    def inaccessible(*_args, **kwargs):
        kwargs["onerror"](PermissionError("access denied"))
        return iter(())

    monkeypatch.setattr(build_delta.os, "walk", inaccessible)
    with pytest.raises(PermissionError, match="access denied"):
        build_delta.main()


def test_case_only_rename_and_file_directory_transition(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    (root / "Name.xml").write_bytes(b"old")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    target_files = {"name.xml/child.xml": b"new"}
    target = _target(target_files)
    source_delta.apply(root, _archive(target_files), target, current["version"], current)
    assert (root / "name.xml" / "child.xml").read_bytes() == b"new"
    assert (root / "name.xml").is_dir()
    assert not (root / "name.xml").is_file()


@pytest.mark.parametrize("point", list("abcdefg"))
def test_transaction_failure_recovers_to_complete_tree(tmp_path, monkeypatch, point):
    root = tmp_path / "xml"
    root.mkdir()
    (root / "old.xml").write_bytes(b"old")
    (root / "change.xml").write_bytes(b"before")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    wanted = {"change.xml": b"after", "added.xml": b"added"}
    target = _target(wanted)

    def fail_at(name):
        if name == point:
            raise RuntimeError(f"injected failure at {name}")

    monkeypatch.setattr(source_delta, "_checkpoint", fail_at)
    with pytest.raises(RuntimeError):
        source_delta.apply(root, _archive(wanted), target, current["version"], current)
    monkeypatch.setattr(source_delta, "_checkpoint", lambda _name: None)
    source_delta.recover(root)
    source_delta.recover(root)
    observed = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    expected_old = {"old.xml": b"old", "change.xml": b"before"}
    expected_new = {"change.xml": b"after", "added.xml": b"added"}
    assert observed in (expected_old, expected_new)
