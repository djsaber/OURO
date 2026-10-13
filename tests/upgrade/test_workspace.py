from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from threading import current_thread, main_thread

import pytest

from ouro_upgrade import workspace
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.workspace import Workspace
from tests.upgrade.test_upgrade import hashes


def test_backup_move_failure_keeps_originals_and_allows_restore(historical_home: Path, monkeypatch):
    before = hashes(historical_home)
    original = workspace.replace_path_with_retry

    def fail_backup(source, target):
        if target.parent.name == "before":
            raise OSError("out of space")
        original(source, target)

    monkeypatch.setattr(workspace, "replace_path_with_retry", fail_backup)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    assert hashes(historical_home) == before
    assert (historical_home / ".upgrade/in-progress.json").exists()
    monkeypatch.setattr(workspace, "replace_path_with_retry", original)
    upgrade(historical_home, restore=True)
    assert hashes(historical_home) == before


def test_source_change_is_detected_before_publishing(historical_home: Path, monkeypatch):
    original = Workspace.publish
    path = historical_home / "sessions/ses_fixture/runs/run_fixture.json"

    def change_source(work, report):
        path.write_bytes(path.read_bytes() + b"\n")
        original(work, report)

    before = hashes(historical_home)
    monkeypatch.setattr(Workspace, "publish", change_source)
    with pytest.raises(UpgradeError, match="source changed"):
        upgrade(historical_home)
    after = hashes(historical_home)
    assert {key for key in after if after[key] != before[key]} == {
        "sessions/ses_fixture/runs/run_fixture.json"
    }
    assert not (historical_home / ".upgrade/in-progress.json").exists()


def test_competing_upgrader_is_rejected(historical_home: Path):
    with Workspace(historical_home):
        with pytest.raises(UpgradeError, match="another data upgrade"):
            upgrade(historical_home)


def test_hard_linked_record_is_rejected_before_any_write(historical_home: Path, tmp_path: Path):
    path = historical_home / "sessions/ses_fixture/session.json"
    alias = tmp_path / "alias.json"
    os.link(path, alias)
    before = hashes(historical_home)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    assert hashes(historical_home) == before


def test_restore_does_not_depend_on_backend_environment(historical_home: Path, monkeypatch):
    original = workspace.install

    def fail_write(*_, **__):
        raise OSError("interrupted")

    monkeypatch.setattr(workspace, "install", fail_write)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    monkeypatch.setattr(workspace, "install", original)
    monkeypatch.setenv("OURO_TASK_BACKEND", "invalid")
    monkeypatch.setenv("OURO_SESSION_BACKEND", "invalid")
    assert upgrade(historical_home, restore=True)["status"] == "restored"


def test_restore_rejects_manifest_path_outside_home(historical_home: Path, monkeypatch):
    def fail_write(*_, **__):
        raise OSError("interrupted")

    monkeypatch.setattr(workspace, "install", fail_write)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    marker = json.loads((historical_home / ".upgrade/in-progress.json").read_text("utf-8"))
    path = historical_home / ".upgrade" / marker["upgrade_id"] / "manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["changes"][0]["path"] = "../outside.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(UpgradeError, match="invalid relative"):
        upgrade(historical_home, restore=True)


def test_permission_failure_prevents_staging_sensitive_data(historical_home: Path, monkeypatch):
    def fail_permissions(*_, **__):
        raise PermissionError("denied")

    before = hashes(historical_home)
    monkeypatch.setattr(workspace, "restrict_to_user", fail_permissions)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    assert hashes(historical_home) == before
    assert list((historical_home / ".upgrade").iterdir()) == []


def test_check_does_not_require_durable_scratch_files(historical_home: Path, monkeypatch):
    def fail_sync(_):
        raise OSError("durability unavailable")

    before = hashes(historical_home)
    monkeypatch.setattr(workspace.os, "fsync", fail_sync)
    assert upgrade(historical_home, check=True)["status"] == "upgrade_required"
    assert hashes(historical_home) == before
    assert {path.name for path in (historical_home / ".upgrade").iterdir()} == {"lock"}


def test_staged_sync_failure_never_publishes(historical_home: Path, monkeypatch):
    def fail_sync(_):
        raise OSError("durability unavailable")

    before = hashes(historical_home)
    monkeypatch.setattr(workspace.os, "fsync", fail_sync)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    assert hashes(historical_home) == before
    assert not (historical_home / ".upgrade/in-progress.json").exists()


def test_original_sync_failure_keeps_business_paths_and_never_marks_publication(
    historical_home: Path, monkeypatch
):
    def fail_sync(_):
        raise OSError("original sync failed")

    def progress(event):
        if event.phase == "verify_source":
            monkeypatch.setattr(workspace.os, "fsync", fail_sync)

    before = hashes(historical_home)
    with pytest.raises(UpgradeError):
        upgrade(historical_home, progress=progress)
    assert hashes(historical_home) == before
    assert not (historical_home / ".upgrade/in-progress.json").exists()


def test_backup_directory_sync_failure_leaves_recoverable_originals(historical_home, monkeypatch):
    original = workspace.sync_directory

    def sync(path):
        if path.name == "before" and (path / "0").exists():
            raise OSError("directory sync failed")
        original(path)

    before = hashes(historical_home)
    monkeypatch.setattr(workspace, "sync_directory", sync)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    monkeypatch.setattr(workspace, "sync_directory", original)
    assert upgrade(historical_home, restore=True)["status"] == "restored"
    assert hashes(historical_home) == before


def test_changed_source_is_rechecked_after_preparation(historical_home: Path):
    relative = "sessions/ses_fixture/messages.jsonl"
    path = historical_home / relative

    def change_after_preparation(event):
        if event.phase == "prepare" and event.completed == event.total:
            path.write_bytes(path.read_bytes() + b"\n")

    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="source changed"):
        upgrade(historical_home, progress=change_after_preparation)
    after = hashes(historical_home)
    assert {key for key in after if after[key] != before[key]} == {relative}
    assert not (historical_home / ".upgrade/in-progress.json").exists()


def test_failed_conversion_joins_prefetched_reads_before_returning(historical_home, monkeypatch):
    directory = historical_home / "sessions/ses_fixture/operations"
    raw = json.loads((directory / "op_fixture.json").read_text("utf-8"))
    for index in range(12):
        raw["operation_id"] = f"op_{index:02}"
        (directory / f"op_{index:02}.json").write_text(json.dumps(raw), encoding="utf-8")
    raw["unknown_native_field"] = "invalid"
    (directory / "op_00.json").write_text(json.dumps(raw), encoding="utf-8")
    readers = []
    original = Path.read_bytes

    def read(path):
        if current_thread() is not main_thread():
            readers.append(current_thread())
        return original(path)

    before = hashes(historical_home)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(UpgradeError, match="invalid SessionCommitResult"):
        upgrade(historical_home)
    assert readers and all(not thread.is_alive() for thread in readers)
    assert hashes(historical_home) == before


@pytest.mark.parametrize(
    ("index", "boundary"),
    [
        (index, boundary)
        for index in range(4)
        for boundary in ("before_backup", "after_backup", "after_install")
        if index != 2 or boundary == "after_install"
    ],
)
def test_partial_publication_restores_updates_deletions_and_additions(
    tmp_path: Path, monkeypatch, index: int, boundary: str
):
    home = tmp_path / "home"
    home.mkdir()
    for name in ("a", "b", "c"):
        (home / name).write_bytes(name.encode())
    before = hashes(home)
    original_move = workspace.replace_path_with_retry
    original_install = workspace.install

    def move(source, target):
        selected = target.parent.name == "before" and target.name == str(index)
        if selected and boundary == "before_backup":
            raise OSError("interrupted before backup")
        original_move(source, target)
        if selected and boundary == "after_backup":
            raise OSError("interrupted after backup")

    def install(source, target, mode, **kwargs):
        original_install(source, target, mode, **kwargs)
        if boundary == "after_install" and target.name == ("a", "b", "new", "c")[index]:
            raise OSError("interrupted after install")

    monkeypatch.setattr(workspace, "replace_path_with_retry", move)
    monkeypatch.setattr(workspace, "install", install)
    with pytest.raises(OSError):
        with Workspace(home) as work:
            work.start()
            work.stage("a", b"updated-a")
            work.stage("b", None)
            work.stage("new", b"added")
            work.stage("c", b"updated-c")
            work.publish({})
    monkeypatch.setattr(workspace, "replace_path_with_retry", original_move)
    monkeypatch.setattr(workspace, "install", original_install)
    assert upgrade(home, restore=True)["status"] == "restored"
    assert hashes(home) == before
    assert not (home / ".upgrade/in-progress.json").exists()


@pytest.mark.parametrize("missing_backup", [False, True])
@pytest.mark.parametrize("version", [1, 2])
def test_legacy_manifests_restore_according_to_backup_contract(tmp_path, missing_backup, version):
    home = tmp_path / "home"
    home.mkdir()
    (home / "record").write_bytes(b"original")
    with Workspace(home) as work:
        work.start()
        work.stage("record", b"updated")
        if not missing_backup:
            shutil.copyfile(home / "record", work.run / "before/0")
            (home / "record").write_bytes(b"updated")
        workspace.write_json_atomic(
            work.run / "manifest.json",
            {
                "schema_version": version,
                "changes": [
                    {key: value for key, value in asdict(item).items() if key != "directory"}
                    for item in work.changes.values()
                ],
            },
        )
        workspace.write_json_atomic(work.marker, {"upgrade_id": work.identifier})
        work.published = True
    if missing_backup and version == 1:
        with pytest.raises(UpgradeError, match="missing or damaged backup"):
            upgrade(home, restore=True)
    else:
        assert upgrade(home, restore=True)["status"] == "restored"
    assert (home / "record").read_bytes() == b"original"
