from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.events import EventRecord
from ouro.core.resources import SessionResourceReference
from ouro_upgrade import records
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import loads
from ouro_upgrade.steps import UpgradeStep
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.test_upgrade import hashes


def test_upgrade_preserves_unrelated_history_without_auditing_it(historical_home: Path):
    home = historical_home
    event = EventRecord(session_id="ses_fixture", event_type="fixture").model_dump(mode="json")
    del event["sequence"]
    journal = home / "sessions/ses_fixture/events.jsonl"
    journal.write_text(json.dumps(event) + "\n", encoding="utf-8")
    occurrence = next((home / "tasks/records/task_fixture/occurrences").iterdir())
    occurrence.write_bytes(b"unrelated historical marker")
    before = hashes(home)
    report = upgrade(home)
    assert report["status"] == "upgraded"
    for path in (journal, occurrence):
        relative = path.relative_to(home).as_posix()
        assert relative not in report["files"]
        assert hashes(home)[relative] == before[relative]


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_context_only_upgrade_preserves_catalog_bytes(historical_home: Path):
    home = historical_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = {"session_backend": backend, "task_backend": backend}
    upgrade(home, **options)
    context = (FIXTURES / "records/context-v3.json").read_text("utf-8")
    if backend == "files":
        (home / "sessions/ses_fixture/context.json").write_text(context, encoding="utf-8")
        index = home / "sessions/index.json"
        before = index.read_bytes() + b"\n"
        index.write_bytes(before)
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            before = connection.execute("SELECT payload FROM session_catalog").fetchone()[0] + "\n"
            connection.execute("UPDATE session_catalog SET payload=?", (before,))
            connection.execute("UPDATE latest_contexts SET payload=?", (context,))
            connection.commit()
    report = upgrade(home, **options)
    assert report["status"] == "upgraded"
    if backend == "files":
        assert report["files"] == ["sessions/ses_fixture/context.json"]
        assert index.read_bytes() == before
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            assert connection.execute("SELECT payload FROM session_catalog").fetchone()[0] == before


def test_event_migration_invalidates_offsets_only_when_journal_changes(
    historical_home, monkeypatch
):
    home = historical_home
    directory = home / "sessions/ses_fixture"
    event = EventRecord(session_id="ses_fixture", event_type="fixture", sequence=1)
    (directory / "events.jsonl").write_text(event.model_dump_json() + "\n", encoding="utf-8")
    indexes = [
        directory / "event-index/runs/run_fixture.json",
        directory / "event-index/sequences.bin",
    ]
    for path in indexes:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing offset index")
    first = upgrade(home)
    assert all(
        path.exists() and path.relative_to(home).as_posix() not in first["files"]
        for path in indexes
    )

    class FutureEvent(EventRecord):
        schema_version: int = event.schema_version + 1
        category: str

    def categorize(value):
        value["category"] = "historical"

    monkeypatch.setitem(records.MODELS, "event", FutureEvent)
    monkeypatch.setitem(
        records.VERSION_STEPS,
        "event",
        {event.schema_version: UpgradeStep(event.schema_version + 1, categorize)},
    )
    report = upgrade(home)
    result = FutureEvent.model_validate_json((directory / "events.jsonl").read_text("utf-8"))
    assert result.category == "historical"
    assert all(
        not path.exists() and path.relative_to(home).as_posix() in report["files"]
        for path in indexes
    )
    assert upgrade(home)["status"] == "current"


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

    # An unavailable attachment does not prevent upgrading its descriptor.
    class FutureResource(SessionResourceReference):
        schema_version: int = resource.schema_version + 1
        label: str

    def label(value):
        value["label"] = value["filename"]

    monkeypatch.setitem(records.MODELS, "resource", FutureResource)
    monkeypatch.setitem(
        records.VERSION_STEPS,
        "resource",
        {resource.schema_version: UpgradeStep(resource.schema_version + 1, label)},
    )
    report = upgrade(home)
    assert index.relative_to(home).as_posix() in report["files"]
    result = FutureResource.model_validate(loads(index.read_text("utf-8"))["resources"][0])
    assert result.label == resource.filename
    assert result.digest == resource.digest
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
