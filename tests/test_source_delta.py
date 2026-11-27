"""Проверки файловой разницы источника на временных каталогах."""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

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


@pytest.mark.parametrize("name", [
    "a:b.xml",            # двоеточие в любом компоненте — буква диска или поток NTFS
    "a/./b.xml",
    "a//b.xml",
    "//server/share.xml",
    "a/b\x01.xml",
    'a/b"c.xml',
    "a/b?.xml",
    "a/b*.xml",
    "a/b<c.xml",
    "a/b>c.xml",
    "a/b|c.xml",
    "a/b /c.xml",         # компонент с пробелом на конце Windows не создаёт
    "a/b.xml.",
])
def test_manifest_rejects_paths_invalid_for_windows(name):
    with pytest.raises(source_delta.DeltaError, match="Недопустимый путь"):
        source_delta._path(name)


def test_manifest_accepts_ordinary_nested_paths():
    assert source_delta._path("CommonModules/ЦеныСервер/Ext/Module.bsl") == \
        "CommonModules/ЦеныСервер/Ext/Module.bsl"


def test_archive_entry_cannot_escape_staging_through_symlink(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (staging / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("символические ссылки недоступны")
    with pytest.raises(source_delta.DeltaError, match="за каталог распаковки"):
        source_delta._staging_target(staging, "link/x.xml")
    assert source_delta._staging_target(staging, "ok/x.xml") == staging / "ok" / "x.xml"


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


def test_actual_paths_rejects_casefold_collision(monkeypatch, tmp_path):
    """Разные имена с одинаковым casefold нельзя склеивать в одну запись:
    иначе один из файлов «исчезал» из манифеста и считался удалённым."""
    root = tmp_path / "xml"
    root.mkdir()

    def fake_walk(top, *, followlinks=False, onerror=None):
        yield str(top), [], ["Straße.xml", "Strasse.xml"]

    monkeypatch.setattr(source_delta.os, "walk", fake_walk)
    with pytest.raises(source_delta.DeltaError, match="Коллизия"):
        source_delta._actual_paths(root)


@pytest.mark.skipif(os.name != "nt", reason="NTFS различает «ß» и «ss» в именах файлов")
def test_delta_rejects_casefold_collision_in_dump(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    (root / "Straße.xml").write_bytes(b"one")
    try:
        (root / "Strasse.xml").write_bytes(b"two")
    except OSError:
        pytest.skip("файловая система не различает эти имена")
    current = source_delta.manifest(root, tmp_path / "cache.json")
    target = _target({"Straße.xml": b"new"})
    with pytest.raises(source_delta.DeltaError, match="Коллизия"):
        source_delta.apply(root, _archive({"Straße.xml": b"new"}), target,
                           current["version"], current, tmp_path / "state")


def test_final_check_rejects_deleted_file_left_in_dump(monkeypatch, tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    (root / "gone.xml").write_bytes(b"old")
    current = source_delta.manifest(root, tmp_path / "cache.json")
    real_replace = os.replace

    def keep_gone(src, dst):
        if Path(src).name == "gone.xml" and Path(dst).parent.name.startswith("rollback"):
            return
        real_replace(src, dst)

    monkeypatch.setattr(source_delta.os, "replace", keep_gone)
    with pytest.raises(source_delta.DeltaError, match="остался"):
        source_delta.apply(root, _archive({}), _target({}), current["version"], current,
                           tmp_path / "state")
    assert (root / "gone.xml").read_bytes() == b"old"


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


def test_directory_case_rename_preserves_excluded_logs_and_rollback_spelling(monkeypatch, tmp_path):
    root = tmp_path / "xml"
    note = root / "Old" / "note.xml"
    note.parent.mkdir(parents=True)
    note.write_bytes(b"old")
    (note.parent / "logs").mkdir()
    (note.parent / "logs" / "keep.txt").write_bytes(b"keep")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    target = _target({"old/note.xml": b"new"})

    def fail_after_install(point):
        if point == "e":
            raise RuntimeError("stop")

    monkeypatch.setattr(source_delta, "_checkpoint", fail_after_install)
    with pytest.raises(RuntimeError):
        source_delta.apply(root, _archive({"old/note.xml": b"new"}), target,
                           current["version"], current, tmp_path / "state")
    assert (root / "Old" / "note.xml").read_bytes() == b"old"
    assert (root / "Old" / "logs" / "keep.txt").read_bytes() == b"keep"
    assert "Old" in {entry.name for entry in os.scandir(root)}
    assert "old" not in {entry.name for entry in os.scandir(root)}


def test_directory_case_rename_moves_excluded_logs_with_tree(tmp_path):
    root = tmp_path / "xml"
    note = root / "Old" / "note.xml"
    note.parent.mkdir(parents=True)
    note.write_bytes(b"old")
    (note.parent / "logs").mkdir()
    (note.parent / "logs" / "keep.txt").write_bytes(b"keep")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    target = _target({"old/note.xml": b"new"})
    source_delta.apply(root, _archive({"old/note.xml": b"new"}), target,
                       current["version"], current, tmp_path / "state")
    assert source_delta.manifest(root, cache)["files"] == target["files"]
    assert "old" in {entry.name for entry in os.scandir(root)}
    assert (root / "old" / "logs" / "keep.txt").read_bytes() == b"keep"


def test_nested_directory_case_renames_follow_parent_rename(tmp_path):
    root = tmp_path / "xml"
    tracked = root / "Old" / "Sub" / "item.xml"
    tracked.parent.mkdir(parents=True)
    tracked.write_bytes(b"old")
    logs = tracked.parent / "logs"
    logs.mkdir()
    (logs / "keep.txt").write_bytes(b"keep")
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    wanted = {"old/sub/item.xml": b"new"}
    target = _target(wanted)

    source_delta.apply(root, _archive(wanted), target, current["version"], current)

    assert source_delta.manifest(root, cache)["files"] == target["files"]
    assert (root / "old" / "sub" / "item.xml").read_bytes() == b"new"
    assert (root / "old" / "sub" / "logs" / "keep.txt").read_bytes() == b"keep"


def test_empty_directory_and_new_root_are_restored_after_rollback(monkeypatch, tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    (root / "empty").mkdir()
    cache = tmp_path / "cache.json"
    current = source_delta.manifest(root, cache)
    target = _target({"empty": b"file"})

    def fail_after_install(point):
        if point == "e":
            raise RuntimeError("stop")

    monkeypatch.setattr(source_delta, "_checkpoint", fail_after_install)
    with pytest.raises(RuntimeError):
        source_delta.apply(root, _archive({"empty": b"file"}), target,
                           current["version"], current, tmp_path / "state")
    assert (root / "empty").is_dir()
    assert not (root / "empty").is_file()


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


def test_recovery_skips_journal_owned_by_another_process(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    workspace = tmp_path / ".delta-state"
    backup = workspace / "rollback-test"
    backup.mkdir(parents=True)
    (backup / "item.xml").write_bytes(b"old")
    (root / "item.xml").write_bytes(b"new")
    source_delta._atomic_json(source_delta._journal_path(workspace), {
        "backup": str(backup), "staging": "", "old": ["item.xml"],
        "new": ["item.xml"], "created_dirs": [], "committed": False,
    })
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    code = (
        "import pathlib,time; from src.core.source_delta import transaction_lock; "
        f"w=pathlib.Path({str(workspace)!r}); "
        f"r=pathlib.Path({str(ready)!r}); x=pathlib.Path({str(release)!r}); "
        "ctx=transaction_lock(w); ctx.__enter__(); r.write_text('ready'); "
        "\nwhile not x.exists(): time.sleep(.02)\nctx.__exit__(None,None,None)"
    )
    child = subprocess.Popen([sys.executable, "-c", code], cwd=Path.cwd())
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        assert source_delta.recover(root, workspace) is False
        assert (root / "item.xml").read_bytes() == b"new"
    finally:
        release.touch()
        child.wait(timeout=10)


def test_manifest_walk_errors_are_not_silently_ignored(monkeypatch, tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    def failed_walk(*_args, onerror=None, **_kwargs):
        onerror(PermissionError("cannot enumerate"))
        return iter(())

    monkeypatch.setattr(source_delta.os, "walk", failed_walk)
    with pytest.raises(PermissionError, match="cannot enumerate"):
        source_delta.manifest(root, tmp_path / "manifest.json")


def test_manifest_cache_detects_replaced_file_with_same_size_and_mtime(tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    path = root / "item.xml"
    path.write_bytes(b"old")
    cache = tmp_path / "manifest.json"
    first = source_delta.manifest(root, cache)
    old_stat = path.stat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    os.replace(replacement, path)
    new_stat = path.stat()
    os.utime(path, ns=(new_stat.st_atime_ns, old_stat.st_mtime_ns))
    second = source_delta.manifest(root, cache)
    assert second["version"] != first["version"]
    assert second["files"]["item.xml"]["sha256"] == hashlib.sha256(b"new").hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="проверяет поведение кэша на Windows")
def test_manifest_rehashes_fresh_file_even_with_reused_windows_identity(monkeypatch, tmp_path):
    root = tmp_path / "xml"
    root.mkdir()
    path = root / "item.xml"
    path.write_bytes(b"old")
    cache = tmp_path / "manifest.json"
    first = source_delta.manifest(root, cache)
    old_stat = path.stat()
    path.write_bytes(b"new")

    # Windows может сохранить размер, время записи и file ID при быстрой
    # перезаписи. Смоделировать именно такой stat, оставив новые байты файла.
    real_stat = Path.stat
    def reused_identity(self, *args, **kwargs):
        actual = real_stat(self, *args, **kwargs)
        if self == path:
            return type("Stat", (), {
                "st_size": old_stat.st_size,
                "st_mtime_ns": old_stat.st_mtime_ns,
                "st_ctime_ns": old_stat.st_ctime_ns,
                "st_ino": old_stat.st_ino,
                "st_mode": actual.st_mode,
                "st_file_attributes": getattr(actual, "st_file_attributes", 0),
            })()
        return actual

    monkeypatch.setattr(Path, "stat", reused_identity)
    monkeypatch.setattr(source_delta.time, "time_ns", lambda: old_stat.st_mtime_ns + 1_000_000_000)
    second = source_delta.manifest(root, cache)
    assert second["version"] != first["version"]
    assert second["files"]["item.xml"]["sha256"] == hashlib.sha256(b"new").hexdigest()


def test_duplicate_json_keys_are_rejected():
    with pytest.raises(source_delta.DeltaError, match="Повтор ключа"):
        source_delta.parse_manifest_json('{"files":{"a":1,"a":2}}')
    if os.name == "nt":
        with pytest.raises(source_delta.DeltaError, match="Повтор ключа"):
            source_delta.parse_manifest_json('{"files":{"A":1,"a":2}}')


def test_client_and_server_share_logs_file_exclusion(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_delta", Path("scripts/build_delta.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder._managed("logs") == source_delta._managed("logs")
    assert builder._managed("Logs") == source_delta._managed("Logs")


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


def test_owned_external_source_journal_recovers_on_startup_and_manifest(tmp_path):
    from src.core.project_manager import ProjectManager

    external = tmp_path / "corpus" / "xml"
    external.mkdir(parents=True)
    (external / "new.xml").write_bytes(b"new")
    data = tmp_path / "sandbox"
    pm = ProjectManager(data)
    pm.create_project("p", "p")
    pm.add_source("p", "s", "source")
    pm.get_project("p").sources[0].xml_path = str(external)
    pm._save_registry()
    workspace = pm._source_delta_workspace("p", "s")
    backup = workspace / "rollback-pending"
    backup.mkdir(parents=True)
    (backup / "old.xml").write_bytes(b"old")
    source_delta._atomic_json(source_delta._journal_path(workspace), {
        "backup": str(backup), "staging": "", "old": ["old.xml"],
        "new": ["new.xml"], "created_dirs": [], "committed": False,
    })
    pm.close_all()
    resumed = ProjectManager(data)
    try:
        manifest = resumed.source_file_manifest("p", "s")
        assert manifest["files"] == {
            "old.xml": {"sha256": hashlib.sha256(b"old").hexdigest(), "size": 3}}
        assert not source_delta._journal_path(workspace).exists()
        assert not (external / "new.xml").exists()
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
