from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.resources import SessionResourceReference
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import loads
from tests.upgrade.test_upgrade import hashes


def test_upgrade_preserves_task_occurrences_without_auditing_them(historical_home):
    occurrence = next((historical_home / "tasks/records/task_fixture/occurrences").iterdir())
    occurrence.write_bytes(b"unrelated historical marker")
    report = upgrade(historical_home)
    assert occurrence.relative_to(historical_home).as_posix() not in report["files"]
    assert occurrence.read_bytes() == b"unrelated historical marker"


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_current_containers_skip_payload_reads_and_database_copies(historical_home, monkeypatch):
    home = historical_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = dict(session_backend=backend, task_backend=backend)
    upgrade(home, **options)
    original = Path.read_bytes

    def read(path):
        assert path.suffix != ".sqlite3"
        assert path.name != "messages.jsonl"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    from ouro_upgrade import databases

    monkeypatch.setattr(
        databases.shutil, "copyfile", lambda *args: pytest.fail("copied current database")
    )
    assert upgrade(home, **options)["status"] == "current"


def test_event_offsets_are_retired_with_the_historical_journal(historical_home):
    directory = historical_home / "sessions/ses_fixture"
    offset = directory / "event-index/sequences.bin"
    offset.parent.mkdir(exist_ok=True)
    offset.write_bytes(b"old offsets")
    report = upgrade(historical_home)
    assert not offset.exists()
    assert offset.relative_to(historical_home).as_posix() in report["files"]
    assert not (directory / "events.jsonl").exists()


def test_resource_migration_updates_metadata_without_reading_attachment_content(
    historical_home, monkeypatch
):
    home = historical_home
    directory = home / "sessions/ses_fixture/resources"
    resource = SessionResourceReference(
        filename="attachment.txt",
        digest="sha256:" + "0" * 64,
        media_type="text/plain",
        size=8,
        source={"name": "attachment.txt"},
    )
    index = directory / "index.json"
    index.write_text(
        json.dumps({"resources": [resource.model_dump(mode="json")]}), encoding="utf-8"
    )

    raw = resource.model_dump(mode="json")
    raw["schema_version"] = 2
    del raw["internal"]
    index.write_text(json.dumps({"resources": [raw]}), "utf-8")
    report = upgrade(home)
    assert index.relative_to(home).as_posix() in report["files"]
    with closing(sqlite3.connect(home / "sessions/ses_fixture/session-data.sqlite3")) as connection:
        from ouro.sessions.sqlite_resources import read_references

        result = read_references(connection, "ses_fixture")[0]
    assert result.digest == resource.digest
    assert result.internal is False
    assert not (directory / "files/attachment.txt").exists()


def test_unknown_event_version_is_rejected_even_without_registered_event_steps(historical_home):
    journal = historical_home / "sessions/ses_fixture/events.jsonl"
    journal.write_text('{"schema_version":999}\n', encoding="utf-8")
    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="unsupported event version"):
        upgrade(historical_home)
    assert hashes(historical_home) == before


def test_current_catalog_input_errors_do_not_expose_record_values(historical_home):
    path = historical_home / "sessions/ses_fixture/session.json"
    raw = loads(path.read_text("utf-8"))
    raw["status"] = "private-value-must-not-be-logged"
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="invalid SessionRecord") as error:
        upgrade(historical_home)
    assert "private-value" not in str(error.value)
    assert hashes(historical_home) == before


@pytest.mark.parametrize("field", ["created_at", "updated_at", "last_activity_at"])
def test_catalog_rebuild_does_not_fabricate_current_record_timestamps(historical_home, field):
    path = historical_home / "sessions/ses_fixture/session.json"
    raw = loads(path.read_text("utf-8"))
    del raw[field]
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="missing persisted SessionRecord"):
        upgrade(historical_home)
    assert hashes(historical_home) == before
