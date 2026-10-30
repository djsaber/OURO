from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.session_commits import SessionCommit
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.layout import DATA_LAYOUT_VERSION
from ouro_upgrade import databases, session_records, workspace
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.units import UpgradeUnit
from tests.upgrade.test_isolation import confirmed
from tests.upgrade.test_upgrade import hashes


def database(home, backend):
    return (
        home / "sessions/ses_fixture/session-data.sqlite3"
        if backend == "files"
        else home / "sessions.sqlite3"
    )


def repository(home, backend):
    return (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(database(home, backend))
    )


def test_native_upgrade_preserves_payloads_and_only_reads_input_states(native_home, monkeypatch):
    home, backend = native_home
    path = database(home, backend)
    with closing(sqlite3.connect(path)) as connection:
        retained = {}
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table in {"events", "metadata", "sqlite_sequence"}:
                continue
            columns = ",".join(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
            retained[table] = (
                columns,
                connection.execute(f"SELECT {columns} FROM {table}").fetchall(),
            )

    read = Path.read_bytes

    def bounded_read(path):
        assert path.name != "messages.jsonl"
        assert "request-content" not in path.parts
        assert path.parent.name != "records"
        return read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", bounded_read)
        patch.setattr(
            session_records, "messages_and_runs", lambda *args: pytest.fail("rebuilt native facts")
        )
        report = upgrade(home, session_backend=backend)
    assert report["status"] == "upgraded"
    with closing(sqlite3.connect(path)) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone()
        for table, (columns, rows) in retained.items():
            assert connection.execute(f"SELECT {columns} FROM {table}").fetchall() == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None

    store = repository(home, backend)
    store.initialize()
    snapshot = store.load_session("ses_fixture")
    entry = store.read_catalog().sessions[0].model_dump(mode="json")
    assert entry["current_model"] == snapshot.session.current_model
    assert "current_provider" not in entry
    assert "current_endpoint" not in entry
    assert snapshot.messages[-1].text_content == "Accepted input input_consumed"
    assert store.load_input_state("ses_fixture", "input_consumed").message_ids == ("msg_input",)
    assert store.load_input_state("ses_fixture", "input_pending").status == "pending"
    draft = store.load_response_draft("ses_fixture", "ctx_fixture")
    assert [chunk.content for chunk in draft.chunks] == ["Draft reasoning", "Unfinished answer"]
    assert store.observation_revision("ses_fixture") == 0
    run = snapshot.runs[0].model_copy(update={"error": "updated after upgrade"})
    store.commit_session(SessionCommit(before=snapshot.session, runs=(run,)))
    changes = store.load_observation_changes(
        "ses_fixture",
        after_revision=0,
        through_revision=store.observation_revision("ses_fixture"),
        limit=128,
    )
    assert changes.runs == (run,)
    if backend == "files":
        assert not list((home / "sessions/ses_fixture/inputs/states").glob("*.json"))
        assert (home / "sessions/ses_fixture/inputs/records").is_dir()

    monkeypatch.setattr(
        databases.shutil, "copyfile", lambda *args: pytest.fail("copied current DB")
    )
    assert upgrade(home, session_backend=backend)["status"] == "current"


@pytest.mark.parametrize("native_home", ["files"], indirect=True)
def test_native_catalog_rejects_invalid_fields_without_exposing_values(native_home):
    home, backend = native_home
    path = home / "sessions/index.json"
    raw = json.loads(path.read_text("utf-8"))
    raw["sessions"][0]["status"] = "private-value-must-not-be-logged"
    path.write_text(json.dumps(raw), "utf-8")
    before = hashes(home)

    with pytest.raises(UpgradeError, match="invalid record used for catalog projection") as error:
        upgrade(home, session_backend=backend)

    assert "private-value" not in str(error.value)
    assert hashes(home) == before


def test_native_check_does_not_publish(native_home):
    home, backend = native_home
    before = hashes(home)
    assert upgrade(home, session_backend=backend, check=True)["status"] == "upgrade_required"
    assert hashes(home) == before


def test_native_wal_and_interrupted_publication_are_restorable(native_home, monkeypatch):
    home, backend = native_home
    path = database(home, backend)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); "
            "c.execute(\"UPDATE runs SET payload=json_set(payload,'$.error','committed WAL')\"); "
            "c.commit(); os._exit(0)",
            str(path),
        ],
        check=True,
    )
    before = hashes(home)
    install = workspace.install

    def interrupt(source, target, mode, **kwargs):
        install(source, target, mode, **kwargs)
        if target == path:
            raise OSError("interrupted native database publication")

    with monkeypatch.context() as patch:
        patch.setattr(workspace, "install", interrupt)
        with pytest.raises(UpgradeError):
            upgrade(home, session_backend=backend)
    assert upgrade(home, restore=True)["status"] == "restored"
    assert hashes(home) == before
    assert upgrade(home, session_backend=backend)["status"] == "upgraded"
    assert repository(home, backend).load_run("ses_fixture", "run_fixture").error == "committed WAL"


@pytest.mark.parametrize("native_home", ["files"], indirect=True)
@pytest.mark.parametrize("damage", ["schema", "encoding", "duplicate"])
def test_partial_native_upgrade_keeps_layout_gated_and_skips_successful_units(
    native_home, monkeypatch, damage
):
    home, _ = native_home
    source = home / "sessions/ses_fixture"
    target = home / "sessions/ses_second"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.sqlite3"))
    for path in target.rglob("*.json*"):
        path.write_text(path.read_text("utf-8").replace("ses_fixture", "ses_second"), "utf-8")
    with closing(sqlite3.connect(source / "session-data.sqlite3")) as connection:
        dump = "\n".join(connection.iterdump()).replace("ses_fixture", "ses_second")
    with closing(sqlite3.connect(target / "session-data.sqlite3")) as connection:
        connection.executescript(dump)
    index = home / "sessions/index.json"
    raw = json.loads(index.read_text("utf-8"))
    raw["sessions"].append(
        json.loads(json.dumps(raw["sessions"][0]).replace("ses_fixture", "ses_second"))
    )
    index.write_text(json.dumps(raw), "utf-8")
    invalid, other = sorted((target / "inputs/states").glob("*.json"))
    content = invalid.read_bytes()
    invalid.write_bytes(
        {"schema": b'{"schema_version":999}', "encoding": b"\xff", "duplicate": other.read_bytes()}[
            damage
        ]
    )

    report = upgrade(home, isolate_failures=True)
    assert report["status"] == "partial"
    assert report["failures"][0]["unit"]["identity"] == "ses_second"
    assert report["failures"][0]["discardable"] is True
    assert json.loads((home / "layout.json").read_text("utf-8"))["data_layout_version"] == 41
    assert not list((source / "inputs/states").glob("*.json"))
    invalid.write_bytes(content)
    copy = databases.shutil.copyfile

    def copy_unfinished(path, destination):
        assert path != source / "session-data.sqlite3"
        return copy(path, destination)

    monkeypatch.setattr(databases.shutil, "copyfile", copy_unfinished)
    assert upgrade(home, isolate_failures=True)["status"] == "upgraded"
    assert (
        json.loads((home / "layout.json").read_text("utf-8"))["data_layout_version"]
        == DATA_LAYOUT_VERSION
    )
    assert (
        LocalSessionStore(home).load_input_state("ses_second", "input_pending").status == "pending"
    )


@pytest.mark.parametrize("native_home", ["files"], indirect=True)
def test_native_unfinished_commit_needs_recovery_or_confirmed_removal(native_home):
    home, _ = native_home
    (home / "sessions/ses_fixture/commit.json").write_text("{}", "utf-8")
    before = hashes(home)
    report = upgrade(home, isolate_failures=True)
    assert report["status"] == "partial"
    assert report["failures"][0]["discardable"] is True
    assert hashes(home) == before
    assert upgrade(home, isolate_failures=True, discard=confirmed(report))["status"] == "upgraded"
    assert not (home / "sessions/ses_fixture").exists()


def test_corrupt_native_database_can_be_removed_at_its_own_boundary(native_home):
    home, backend = native_home
    path = database(home, backend)
    path.write_bytes(b"corrupt native database")
    before = hashes(home)
    report = upgrade(home, session_backend=backend, isolate_failures=True)
    unit = (
        UpgradeUnit("session", "ses_fixture")
        if backend == "files"
        else UpgradeUnit("session_store", "sqlite")
    )
    assert confirmed(report) == {unit}
    assert hashes(home) == before
    result = upgrade(
        home, session_backend=backend, isolate_failures=True, discard=confirmed(report)
    )
    assert result["status"] == "upgraded"
    assert not path.exists()
    repository(home, backend).initialize()
