from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from ouro.billing.local import LocalUsageLedger
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro_upgrade.engine import upgrade
from tests.upgrade.test_upgrade import hashes


@pytest.fixture
def valid_home(historical_home):
    home = historical_home
    if (home / "sessions.sqlite3").exists():
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            run = json.loads(connection.execute("SELECT payload FROM runs").fetchone()[0])
            run["active_tool_batch"] = None
            connection.execute("UPDATE runs SET payload=?", (json.dumps(run),))
            messages = [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM messages ORDER BY sequence")
            ]
            messages[-1]["tool_result"]["tool_call_id"] = messages[1]["tool_calls"][0]["id"]
            connection.execute(
                "UPDATE messages SET payload=? WHERE message_id=?",
                (json.dumps(messages[-1]), messages[-1]["message_id"]),
            )
            connection.commit()
    else:
        path = home / "sessions/ses_fixture/runs/run_fixture.json"
        run = json.loads(path.read_text())
        run["active_tool_batch"] = None
        path.write_text(json.dumps(run))
        path = home / "sessions/ses_fixture/messages.jsonl"
        messages = [json.loads(line) for line in path.read_text().splitlines()]
        messages[-1]["tool_result"]["tool_call_id"] = messages[1]["tool_calls"][0]["id"]
        path.write_text("\n".join(map(json.dumps, messages)))
    return home


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_native_session_and_usage_readers(valid_home):
    home = valid_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = dict(session_backend=backend, task_backend=backend)
    before = hashes(home)
    assert upgrade(home, check=True, **options)["status"] == "upgrade_required"
    assert hashes(home) == before
    assert upgrade(home, **options)["status"] == "upgraded"
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    snapshot = store.load_session("ses_fixture")
    assert snapshot.session.message_count == 3
    assert snapshot.messages[-1].text_content == "fixture tool output"
    assert snapshot.messages[1].tool_calls[0].arguments["schema_version"] == 999
    assert snapshot.messages[-1].tool_result.tool_call_id == snapshot.messages[1].tool_calls[0].id
    context = store.load_latest_context("ses_fixture")
    assert context.context_id == "ctx_fixture"
    ledger = LocalUsageLedger(home)
    assert ledger.get("usage_kept").input_tokens == 123
    assert ledger.get_raw("usage_kept").input_tokens == 100
    assert ledger.get_raw("usage_removed").input_tokens == 99
    assert ledger.run_usage("ses_fixture", "run_fixture").input_tokens == 199
    after = hashes(home)
    assert upgrade(home, **options)["status"] == "current"
    assert hashes(home) == after


def test_execution_binding_uses_owning_call_identity(historical_home):
    path = historical_home / "sessions/ses_fixture/runs/run_fixture.json"
    raw = json.loads(path.read_text())
    raw["active_tool_batch"]["calls"][0]["invocation"]["tool_call_id"] = "mismatch"
    path.write_text(json.dumps(raw))
    upgrade(historical_home)
    snapshot = LocalSessionStore(historical_home).load_session("ses_fixture")
    assert (
        snapshot.call_states[0].prepared_binding.tool_call_id
        == raw["active_tool_batch"]["calls"][0]["request"]["id"]
    )


def test_saved_execution_preserves_start_and_structured_outcome(historical_home):
    path = historical_home / "sessions/ses_fixture/runs/run_fixture.json"
    raw = json.loads(path.read_text())
    call = raw["active_tool_batch"]["calls"][0]
    call["result"] = {
        "tool_call_id": call["request"]["id"],
        "tool_name": "example",
        "is_error": False,
        "content": "fixture tool output",
        "structured": {"answer": 42},
        "resource_refs": [],
        "started_at": "2026-09-01T00:00:00Z",
        "ended_at": "2026-09-01T00:00:01Z",
        "exit_code": None,
        "metadata": {},
    }
    path.write_text(json.dumps(raw))
    upgrade(historical_home)
    snapshot = LocalSessionStore(historical_home).load_session("ses_fixture")
    assert len(snapshot.tool_facts.starts) == 1
    assert len(snapshot.tool_facts.outcomes) == 1
    assert snapshot.tool_facts.outcomes[0].value.structured == {"answer": 42}
    assert snapshot.tool_facts.starts[0].arguments_override is None


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_delegation_parent_query_uses_current_index(historical_home):
    from ouro.core.agents import AgentDefinition, DelegationRecord

    record = DelegationRecord(
        delegation_id="dlg_upgrade",
        idempotency_key="request/upgrade",
        root_session_id="ses_fixture",
        parent_session_id="ses_fixture",
        parent_run_id="run_fixture",
        agent_id="general",
        task_description="Inspect",
        prompt_digest="sha256:fixture",
        definition=AgentDefinition(agent_id="general", name="General", description="Inspect"),
    )
    raw = record.model_dump(mode="json")
    raw["schema_version"] = 1
    del raw["parent_batch_id"]
    home = historical_home
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    if backend == "files":
        path = home / "sessions/ses_fixture/delegations/dlg_upgrade.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(raw))
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            connection.execute(
                "INSERT INTO delegations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.delegation_id,
                    record.root_session_id,
                    record.parent_session_id,
                    None,
                    record.idempotency_key,
                    record.spawn_state,
                    None,
                    record.revision,
                    raw["created_at"],
                    json.dumps(raw),
                ),
            )
            connection.commit()
    upgrade(home, session_backend=backend, task_backend=backend)
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    found = store.list_delegations("ses_fixture", parent_run_id="run_fixture")
    assert [item.delegation_id for item in found] == ["dlg_upgrade"]
    assert found[0].schema_version == 2


def test_usage_schema_two_preserves_original_and_effective_totals(historical_home):
    path = historical_home / "billing/usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("ALTER TABLE entries ADD COLUMN plugin_id TEXT")
        for table in ("entry_records", "events"):
            for rowid, payload in connection.execute(
                f"SELECT rowid, payload FROM {table}"
            ).fetchall():
                raw = json.loads(payload)
                raw["schema_version"] = 2
                if table == "entry_records":
                    raw["plugin_id"] = None
                elif raw.get("entry") is not None:
                    raw["entry"].update(schema_version=2, plugin_id=None)
                connection.execute(
                    f"UPDATE {table} SET payload=? WHERE rowid=?", (json.dumps(raw), rowid)
                )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    upgrade(historical_home)
    ledger = LocalUsageLedger(historical_home)
    assert ledger.get("usage_kept").input_tokens == 123
    assert ledger.run_usage("ses_fixture", "run_fixture").input_tokens == 199


def test_consumed_queue_marker_is_discarded(historical_home):
    path = historical_home / "sessions/ses_fixture/messages.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["metadata"]["queued_input_id"] = "unknown-acceptance"
    path.write_text("\n".join(map(json.dumps, rows)))
    upgrade(historical_home)
    store = LocalSessionStore(historical_home)
    snapshot = store.load_session("ses_fixture")
    assert snapshot.messages[0].text_content == "fixture question"
    assert snapshot.messages[0].input_id is None
    assert "queued_input_id" not in snapshot.messages[0].metadata
    assert not snapshot.inputs
    assert not snapshot.input_states
