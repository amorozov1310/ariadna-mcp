"""Проверки файловой разницы источника на временных каталогах."""

import hashlib
import io
import json
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
