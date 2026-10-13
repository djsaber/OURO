from __future__ import annotations

import json
import os
from pathlib import Path
from threading import current_thread, main_thread

import pytest

from ouro_upgrade import workspace
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.workspace import Workspace
from tests.upgrade.test_upgrade import hashes


def test_backup_failure_never_publishes_or_changes_business_data(
    historical_home: Path, monkeypatch
):
    before = hashes(historical_home)
    original = workspace.copy_durable

    def fail_backup(source, target):
        if target.parent.name == "before":
            raise OSError("out of space")
        original(source, target)

    monkeypatch.setattr(workspace, "copy_durable", fail_backup)
    with pytest.raises(UpgradeError):
        upgrade(historical_home)
    assert hashes(historical_home) == before
    assert not (historical_home / ".upgrade/in-progress.json").exists()


def test_source_change_is_detected_before_publishing(historical_home: Path, monkeypatch):
    original = Workspace.publish
    path = historical_home / "sessions/ses_fixture/session.json"

    def change_source(work, report):
        path.write_bytes(path.read_bytes() + b"\n")
        original(work, report)

    before = hashes(historical_home)
    monkeypatch.setattr(Workspace, "publish", change_source)
    with pytest.raises(UpgradeError, match="source changed"):
        upgrade(historical_home)
    after = hashes(historical_home)
    assert {key for key in after if after[key] != before[key]} == {
        "sessions/ses_fixture/session.json"
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


def test_damaged_staged_file_is_rejected_before_publishing(historical_home: Path):
    def damage_staged(event):
        if event.phase == "backup" and event.completed == 0:
            staged = next((historical_home / ".upgrade").glob("*/new/0"))
            staged.write_bytes(b"damaged")

    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="staged data changed"):
        upgrade(historical_home, progress=damage_staged)
    assert hashes(historical_home) == before
    assert not (historical_home / ".upgrade/in-progress.json").exists()


def test_unchanged_source_is_rechecked_after_backup(historical_home: Path):
    relative = "sessions/ses_fixture/events.jsonl"
    path = historical_home / relative

    def change_after_backup(event):
        if event.phase == "backup" and event.completed == event.total:
            path.write_bytes(path.read_bytes() + b"\n")

    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="source changed"):
        upgrade(historical_home, progress=change_after_backup)
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
