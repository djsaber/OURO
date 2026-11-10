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
from ouro_upgrade.records import convert
from ouro_upgrade.units import UpgradeUnit
from tests.upgrade.test_isolation import confirmed
from tests.upgrade.test_steps import historical_agent_catalog
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


def test_native_upgrade_converts_authority_and_preserves_other_facts(native_home, monkeypatch):
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
            actual = connection.execute(f"SELECT {columns} FROM {table}").fetchall()
            kind = {
                "sessions": "session",
                "runs": "run",
                "delegations": "delegation",
                "tool_call_states": "tool_state",
                "harness_versions": "harness",
            }.get(table)
            if kind is None and table != "session_catalog":
                assert actual == rows
            else:
                payload_index = columns.split(",").index("payload")
                for before, after in zip(rows, actual, strict=True):
                    assert before[:payload_index] == after[:payload_index]
                    assert before[payload_index + 1 :] == after[payload_index + 1 :]
                    expected = json.loads(before[payload_index])
                    if kind is not None:
                        expected = convert(kind, expected)
                    else:
                        expected.pop("workspace_trusted", None)
                        expected.pop("title_locked", None)
                    assert json.loads(after[payload_index]) == expected
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


def test_native_authority_upgrade_preserves_catalog_grants_from_existing_container(native_home):
    home, backend = native_home
    upgrade(home, session_backend=backend)
    with closing(sqlite3.connect(database(home, backend))) as connection:
        session = json.loads(connection.execute("SELECT payload FROM sessions").fetchone()[0])
        session.update(schema_version=5, workspace_trusted=True)
        connection.execute("UPDATE sessions SET payload=?", (json.dumps(session),))
        run = json.loads(connection.execute("SELECT payload FROM runs").fetchone()[0])
        run.update(schema_version=10, agent_catalog=historical_agent_catalog())
        connection.execute(
            "UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(run), run["run_id"])
        )
        if backend == "sqlite":
            connection.execute("UPDATE metadata SET value='27' WHERE key='sqlite_schema_version'")
            connection.execute(
                "UPDATE session_catalog SET payload=json_set(payload, '$.workspace_trusted', 1)"
            )
        connection.commit()
    if backend == "files":
        layout = home / "layout.json"
        raw = json.loads(layout.read_text("utf-8"))
        raw["data_layout_version"] = 42
        layout.write_text(json.dumps(raw), "utf-8")
        index = home / "sessions/index.json"
        raw_index = json.loads(index.read_text("utf-8"))
        for entry in raw_index["sessions"]:
            entry["workspace_trusted"] = True
        index.write_text(json.dumps(raw_index), "utf-8")
    assert upgrade(home, session_backend=backend)["status"] == "upgraded"
    store = repository(home, backend)
    current = store.load_run(session["session_id"], run["run_id"])
    reference = current.agent_catalog.resolve("reader").effective_tools[0]
    assert reference.public_name == "Read"
    assert reference.minimum_permission_mode == "approval"
    assert store.load_session_record(session["session_id"]).workspace == session["workspace"]
    if backend == "sqlite":
        with closing(sqlite3.connect(database(home, backend))) as connection:
            entries = [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM session_catalog")
            ]
    else:
        entries = json.loads(index.read_text("utf-8"))["sessions"]
    assert entries
    assert all("workspace_trusted" not in entry for entry in entries)
    assert upgrade(home, session_backend=backend)["status"] == "current"


def _frozen_catalog():
    import hashlib

    from ouro.core.agents import AgentCatalogEntry, AgentCatalogSnapshot, AgentDefinition

    catalog = AgentCatalogSnapshot(
        entries=(
            AgentCatalogEntry(
                definition=AgentDefinition(
                    agent_id="reader", name="Reader", description="Frozen prompt"
                ),
            ),
        )
    ).model_dump(mode="json")
    definition = catalog["entries"][0]["definition"]
    definition["can_spawn"] = False
    payload = {key: value for key, value in definition.items() if key != "contract_hash"}
    definition["contract_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    catalog["spawn_policy"]["allowed_lifecycle_bindings"] = ["bound"]
    payload = {
        key: catalog[key] for key in ("schema_version", "revision", "entries", "spawn_policy")
    }
    catalog["contract_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return catalog


@pytest.mark.parametrize("corrupt", [False, True])
def test_current_container_upgrades_frozen_agents_and_isolates_invalid_contracts(
    native_home, corrupt
):
    home, backend = native_home
    upgrade(home, session_backend=backend)
    catalog = _frozen_catalog()
    if corrupt:
        catalog["entries"][0]["definition"]["description"] = "tampered"
    path = database(home, backend)
    with closing(sqlite3.connect(path)) as connection:
        run = json.loads(connection.execute("SELECT payload FROM runs LIMIT 1").fetchone()[0])
        run["agent_catalog"] = catalog
        connection.execute(
            "UPDATE runs SET payload=? WHERE run_id=?", (json.dumps(run), run["run_id"])
        )
        connection.commit()
    before = hashes(home)
    report = upgrade(home, session_backend=backend, check=True, isolate_failures=True)
    assert hashes(home) == before
    if corrupt:
        assert report["status"] == "partial"
        assert "contract hash" in report["failures"][0]["error"]
        result = upgrade(
            home, session_backend=backend, isolate_failures=True, discard=confirmed(report)
        )
        assert result["status"] == "upgraded"
        assert result["discarded"]
        assert not path.exists()
        return
    assert report["status"] == "upgrade_required"
    assert upgrade(home, session_backend=backend)["status"] == "upgraded"
    loaded = repository(home, backend).load_run(run["session_id"], run["run_id"])
    assert loaded.agent_catalog.entries[0].definition.description == "Frozen prompt"
    assert (
        loaded.agent_catalog.created_at.isoformat().replace("+00:00", "Z") == catalog["created_at"]
    )
    assert loaded.agent_catalog.contract_hash != catalog["contract_hash"]
    assert upgrade(home, session_backend=backend)["status"] == "current"


def test_delegation_definition_uses_the_same_offline_conversion():
    from ouro.core.agents import AgentDefinition, DelegationRecord

    definition = _frozen_catalog()["entries"][0]["definition"]
    record = DelegationRecord(
        root_session_id="root",
        parent_session_id="root",
        parent_run_id="run",
        idempotency_key="request",
        task_description="Inspect",
        prompt_digest="sha256:prompt",
        definition=AgentDefinition(agent_id="reader", name="Reader", description="Frozen prompt"),
    ).model_dump(mode="json")
    record["definition"] = definition
    record.update(
        agent_id="reader", delivery_policy="notify_only", depth=1, lifecycle_binding="bound"
    )
    result = convert("delegation", record)
    restored = DelegationRecord.model_validate(result)
    assert restored.delegation_id == record["delegation_id"]
    assert restored.definition.description == definition["description"]
    assert "can_spawn" in record["definition"]
    assert convert("delegation", result) is result


@pytest.mark.parametrize("invalid_phase", [False, True])
def test_retired_control_and_wait_fields_upgrade_in_current_containers(native_home, invalid_phase):
    from tests.core.test_persistence import _activation

    home, backend = native_home
    upgrade(home, session_backend=backend)
    activation = _activation().model_dump(mode="json")
    activation.update(
        phase="broken" if invalid_phase else "active",
        revision=1,
        updated_at=activation["created_at"],
        error=None,
    )
    # Same-named fields in opaque extension data are not schema-owned.
    activation["data"] = {"phase": "business", "revision": 9}
    controls = {"revision": 4, "activations": [activation]}
    path = database(home, backend)
    with closing(sqlite3.connect(path)) as connection:
        session = json.loads(connection.execute("SELECT payload FROM sessions").fetchone()[0])
        session.update(controls=controls, title_locked=False)
        connection.execute("UPDATE sessions SET payload=?", (json.dumps(session),))
        state = json.loads(
            connection.execute("SELECT payload FROM tool_call_states LIMIT 1").fetchone()[0]
        )
        state["agent_wait"] = {
            "wait_id": "wait",
            "delegation_id": "delegation",
            "child_run_id": "child",
            "state": "resolved",
            "resolution": {
                "resolved_delegation_id": "delegation",
                "resolved_child_run_id": "child",
                "reason": "condition_met",
                "resolved_at": activation["created_at"],
            },
        }
        connection.execute("UPDATE tool_call_states SET payload=?", (json.dumps(state),))
        if backend == "sqlite":
            harness = json.loads(
                connection.execute("SELECT payload FROM harness_versions LIMIT 1").fetchone()[0]
            )
            harness["state"]["controls"] = controls
            connection.execute("UPDATE harness_versions SET payload=?", (json.dumps(harness),))
        connection.commit()
    if backend == "files":
        harness_path = next((home / "sessions/ses_fixture/harness").glob("*.json"))
        harness = json.loads(harness_path.read_text("utf-8"))
        harness["state"]["controls"] = controls
        harness_path.write_text(json.dumps(harness), "utf-8")
    original = hashes(home)
    report = upgrade(home, session_backend=backend, check=True, isolate_failures=True)
    assert hashes(home) == original
    if invalid_phase:
        assert report["status"] == "partial"
        assert "activation phase" in report["failures"][0]["error"]
        result = upgrade(
            home, session_backend=backend, isolate_failures=True, discard=confirmed(report)
        )
        assert result["discarded"] and not path.exists()
        return
    assert report["status"] == "upgrade_required"
    upgrade(home, session_backend=backend)
    with closing(sqlite3.connect(path)) as connection:
        after = json.loads(connection.execute("SELECT payload FROM sessions").fetchone()[0])
        assert "title_locked" not in after
        assert after["controls"]["revision"] == 4
        saved = after["controls"]["activations"][0]
        assert saved["data"] == activation["data"]
        assert not {"phase", "revision", "updated_at", "error"}.intersection(saved)
        after = json.loads(
            connection.execute("SELECT payload FROM tool_call_states LIMIT 1").fetchone()[0]
        )
        assert after["agent_wait"]["child_run_id"] == "child"
        assert "state" not in after["agent_wait"]
        assert set(after["agent_wait"]["resolution"]) == {"reason", "resolved_at"}
        if backend == "sqlite":
            harness = json.loads(
                connection.execute("SELECT payload FROM harness_versions LIMIT 1").fetchone()[0]
            )
    if backend == "files":
        harness = json.loads(harness_path.read_text("utf-8"))
    assert "phase" not in harness["state"]["controls"]["activations"][0]
    assert upgrade(home, session_backend=backend)["status"] == "current"
