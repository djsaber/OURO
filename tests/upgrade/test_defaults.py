from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from ouro.core.agents import AgentDefinition, DelegationRecord
from ouro.core.inputs import validate_input_snapshot
from ouro.core.tool_batches import validate_call_states
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro_upgrade.engine import upgrade
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.test_isolation import clone_files


def edit(home, kind, change):
    if (home / "sessions.sqlite3").exists():
        table = {"run": "runs", "messages": "messages", "session": "sessions"}[kind]
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            for rowid, payload in connection.execute(
                f"SELECT rowid, payload FROM {table}"
            ).fetchall():
                raw = json.loads(payload)
                change(raw)
                connection.execute(
                    f"UPDATE {table} SET payload=? WHERE rowid=?", (json.dumps(raw), rowid)
                )
            connection.commit()
    else:
        path = (
            home
            / "sessions/ses_fixture"
            / {
                "run": "runs/run_fixture.json",
                "messages": "messages.jsonl",
                "session": "session.json",
            }[kind]
        )
        rows = (
            [json.loads(line) for line in path.read_text().splitlines()]
            if kind == "messages"
            else [json.loads(path.read_text())]
        )
        for raw in rows:
            change(raw)
        path.write_text("\n".join(map(json.dumps, rows)))


def upgraded(home):
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = dict(session_backend=backend, task_backend=backend)
    assert upgrade(home, **options)["status"] == "upgraded"
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    snapshot = store.load_session("ses_fixture")
    validate_call_states(snapshot.call_states, snapshot.tool_facts, session_id="ses_fixture")
    validate_input_snapshot(
        "ses_fixture",
        (),
        tuple(snapshot.inputs),
        tuple(snapshot.input_states),
        messages=snapshot.messages,
    )
    assert upgrade(home, **options)["status"] == "current"
    return store, snapshot


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
@pytest.mark.parametrize("saved_result", [False, True])
def test_running_calls_and_missing_bindings_upgrade(historical_home, saved_result):
    def change(raw):
        raw.update(status="running")
        batch = raw["active_tool_batch"]
        batch["phase"] = "executing"
        call = batch["calls"][0]
        call.update(status="running", invocation=None)
        if saved_result:
            call["result"] = dict(
                tool_call_id=call["request"]["id"],
                tool_name="example",
                content="saved result",
                structured={"answer": 42},
                is_error=False,
                harness_change={"obsolete_contract": True},
                session_control_transitions=[{"obsolete_contract": True}],
            )

    edit(historical_home, "run", change)
    _, snapshot = upgraded(historical_home)
    state = snapshot.call_states[0]
    if saved_result:
        assert state.status == "completed"
        assert state.proposals is None
        assert snapshot.tool_facts.outcomes[0].value.structured == {"answer": 42}
        assert (
            snapshot.tool_facts.starts[0].started_at == snapshot.tool_facts.outcomes[0].observed_at
        )
    else:
        assert state.status == "unknown_outcome"
        assert snapshot.runs[0].status == "recoverable"
        assert not snapshot.tool_facts.outcomes


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_unmodified_historical_tool_identity_is_accepted(historical_home):
    raw_run = json.loads(
        (FIXTURES / "files/sessions/ses_fixture/runs/run_fixture.json").read_text()
    )
    edit(historical_home, "run", lambda raw: (raw.clear(), raw.update(raw_run)))
    edit(
        historical_home,
        "messages",
        lambda raw: (
            raw["tool_result"].update(tool_call_id="tool_fixture")
            if raw.get("tool_result")
            else None
        ),
    )
    _, snapshot = upgraded(historical_home)
    assert snapshot.messages[-1].text_content == "fixture tool output"
    assert snapshot.messages[-1].tool_result.tool_call_id == "tool_fixture"
    orphan = next(call for call in snapshot.tool_facts.calls if call.request.id == "tool_fixture")
    assert orphan.request.arguments == {}


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_consumed_queue_marker_preserves_message_body(historical_home):
    edit(
        historical_home,
        "messages",
        lambda raw: (
            raw["metadata"].update(queued_input_id="queue/中文") if raw["role"] == "user" else None
        ),
    )
    store, snapshot = upgraded(historical_home)
    assert snapshot.messages[0].text_content == "fixture question"
    assert not snapshot.inputs
    assert "queued_input_id" not in snapshot.messages[0].metadata


@pytest.mark.parametrize("parent_source", ["batch", "messages", "missing"])
def test_file_delegation_resolves_cross_session_or_drops_missing_call(
    historical_home, parent_source
):
    home = historical_home
    if parent_source != "missing":
        parent = clone_files(home, "sessions/ses_fixture", "ses_parent")
        if parent_source == "messages":
            path = parent / "runs/run_fixture.json"
            raw = json.loads(path.read_text())
            raw["active_tool_batch"] = None
            path.write_text(json.dumps(raw))
    raw_run = json.loads((home / "sessions/ses_fixture/runs/run_fixture.json").read_text())
    call_id = raw_run["active_tool_batch"]["calls"][0]["request"]["id"]
    raw = DelegationRecord(
        delegation_id="dlg_cross",
        idempotency_key="cross",
        root_session_id="ses_fixture",
        parent_session_id="ses_parent",
        parent_run_id="run_fixture",
        parent_batch_id="batch_fixture",
        parent_tool_call_id=call_id,
        delivery_policy="resume_waiter",
        agent_id="general",
        task_description="Inspect",
        prompt_digest="sha256:fixture",
        definition=AgentDefinition(agent_id="general", name="General", description="Inspect"),
    ).model_dump(mode="json")
    raw["schema_version"] = 1
    del raw["parent_batch_id"]
    path = home / "sessions/ses_fixture/delegations/dlg_cross.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(raw))
    store, _ = upgraded(home)
    record = store.load_delegation("ses_fixture", "dlg_cross")
    if parent_source != "missing":
        assert record.parent_batch_id == (
            "batch_fixture" if parent_source == "batch" else "msg_assistant"
        )
    else:
        assert record.parent_batch_id is None
        assert record.parent_tool_call_id is None
        assert record.delivery_policy == "notify_only"
        assert record.parent_session_id == "ses_parent"


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_missing_structure_uses_defaults_and_actual_history(historical_home):
    def session(raw):
        for field in (
            "harness_version",
            "history_epoch",
            "created_at",
            "updated_at",
            "last_activity_at",
            "active_compaction_id",
        ):
            raw.pop(field)
        raw["message_count"] = 999

    def run(raw):
        for field in ("started_at", "history_epoch", "usage", "usage_entry_ids"):
            raw.pop(field)
        for field in ("created_at", "updated_at", "turn_id"):
            raw["active_tool_batch"].pop(field)

    edit(historical_home, "session", session)
    edit(historical_home, "run", run)
    edit(historical_home, "messages", lambda raw: raw.update(sequence=raw["sequence"] * 10))
    store, snapshot = upgraded(historical_home)
    assert snapshot.session.message_count == 3
    assert [item.sequence for item in snapshot.messages] == [1, 2, 3]
    assert snapshot.session.created_at.isoformat() == "2026-09-01T00:00:00+00:00"
    assert snapshot.runs[0].started_at == snapshot.session.created_at
    assert (
        store.load_harness("ses_fixture", "harness_upgrade").state.controls
        == snapshot.session.controls
    )


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_approved_arguments_survive_missing_invocation(historical_home):
    from ouro.core.approvals import ApprovalRecord

    def change(raw):
        call = raw["active_tool_batch"]["calls"][0]
        call["invocation"] = None
        call["approval"] = ApprovalRecord(
            session_id=raw["session_id"],
            run_id=raw["run_id"],
            tool_call_id=call["request"]["id"],
            status="modified",
            modified_arguments={"approved": True},
        ).model_dump(mode="json")

    edit(historical_home, "run", change)
    _, snapshot = upgraded(historical_home)
    state = snapshot.call_states[0]
    assert state.arguments_override == {"approved": True}
    assert state.approval.modified_arguments is None


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_noncanonical_queue_body_preserves_literal_content(historical_home):
    def change(raw):
        if raw["role"] == "user":
            raw["content"] = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
            raw["metadata"]["queued_input_id"] = "queue_literal"

    edit(historical_home, "messages", change)
    _, snapshot = upgraded(historical_home)
    message = snapshot.messages[0]
    assert [block.text for block in message.content] == ["first", "second"]
    assert "queued_input_id" not in message.metadata
    assert message.input_ref is None


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_skill_only_queue_preserves_content(historical_home):
    from ouro.core.messages import SkillActivationContent

    skill = SkillActivationContent(
        name="example",
        source="user",
        location="skills/example/SKILL.md",
        base_directory="skills/example",
        instructions="Example instructions",
        content_digest="sha256:" + "0" * 64,
    )

    def change(raw):
        if raw["role"] == "user":
            raw["content"] = [skill.model_dump(mode="json")]
            raw["metadata"]["queued_input_id"] = "queue_skill"

    edit(historical_home, "messages", change)
    store, snapshot = upgraded(historical_home)
    assert not snapshot.inputs
    assert snapshot.messages[0].content == [skill]
