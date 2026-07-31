from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from ouro.application.session_models import SessionCatalogQuery
from ouro.application.sessions import SessionService
from ouro.core.agents import (
    AgentCatalogSnapshot,
    AgentDefinition,
    DelegationRecord,
    DelegationResult,
    ResultDelivery,
)
from ouro.core.context import ContextBuilder
from ouro.core.context_snapshots import ContextSnapshotRecord, context_view_digest
from ouro.core.errors import PersistenceError, SessionBranchError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.layout import DATA_LAYOUT_VERSION


def _service(tmp_path) -> tuple[LocalSessionStore, SessionService]:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    return store, SessionService(store)


def test_session_create_and_rename_persist_explicit_identity_and_title_state(tmp_path) -> None:
    store, sessions = _service(tmp_path)

    created = sessions.create(title="  First   task  ")
    renamed = sessions.rename(created.session.session_id, "User title")
    reloaded = store.load_session(created.session.session_id).session

    assert created.session.root_session_id == created.session.session_id
    assert created.session.title == "First task"
    assert created.session.title_source == "fallback"
    assert renamed.session.title == "User title"
    assert renamed.session.title_source == "user"
    assert renamed.session.title_locked is True
    assert reloaded.title == renamed.session.title
    assert reloaded.title_source == renamed.session.title_source
    assert reloaded.title_locked == renamed.session.title_locked


def test_session_run_result_projects_terminal_text_without_exposing_store(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Run result").session
    run = RunRecord(session_id=session.session_id, status="completed")
    store.save_run(run)
    store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text="done",
            sequence=1,
        )
    )

    result = sessions.run_result(session.session_id, run.run_id)

    assert result.run == run
    assert result.final_text == "done"


def test_session_context_rejects_content_that_does_not_match_digest(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Context digest").session
    message = MessageRecord.text(session_id=session.session_id, role="user", text="hello")
    view = (
        ContextBuilder()
        .build(messages=[message], tools=[])
        .model_copy(update={"context_id": "ctx_tampered"})
    )
    store.save_latest_context(
        ContextSnapshotRecord(
            context_id="ctx_tampered",
            session_id=session.session_id,
            run_id="run_1",
            turn_id="turn_1",
            model_round=1,
            provider_id="test",
            model_id="test-model",
            source_messages=[message],
            metrics=view.metrics,
            context_digest="sha256:" + "0" * 64,
        )
    )

    assert sessions.context_status(session.session_id) is not None
    with pytest.raises(PersistenceError, match="does not match its stored digest"):
        sessions.context(session.session_id)


def test_session_context_preserves_legacy_unwrapped_synthetic_snapshot_digest(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Legacy context digest").session
    message = MessageRecord.text(
        session_id=session.session_id,
        role="system_synthetic",
        text="legacy scaffold",
    )
    legacy_message = message.model_copy(update={"semantic_tag": None})
    view = (
        ContextBuilder()
        .build(messages=[legacy_message], tools=[])
        .model_copy(update={"context_id": "ctx_legacy"})
    )
    store.save_latest_context(
        ContextSnapshotRecord(
            context_id="ctx_legacy",
            session_id=session.session_id,
            run_id="run_legacy",
            turn_id="turn_legacy",
            model_round=1,
            provider_id="test",
            model_id="test-model",
            source_messages=[message],
            metrics=view.metrics,
            context_digest=context_view_digest(view),
        )
    )

    snapshot = sessions.context(session.session_id)

    assert snapshot is not None
    assert snapshot.provider_context.messages[0].text_content == "legacy scaffold"


def test_message_page_does_not_load_full_large_session(monkeypatch, tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = SessionRecord(title="Large", message_count=250)
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text=f"message {sequence}",
            sequence=sequence,
        )
        for sequence in range(1, 251)
    ]
    store.create_session_snapshot(session, messages)

    def fail_full_load(*args, **kwargs):
        del args, kwargs
        raise AssertionError("paged query must not load the full Session snapshot")

    monkeypatch.setattr(store, "load_session", fail_full_load)
    page = sessions.message_page(session.session_id, limit=25)

    assert len(page.messages) == 25
    assert page.messages[0].text_content == "message 1"
    assert page.messages[-1].text_content == "message 25"
    assert page.next_cursor is not None


def test_open_session_returns_bounded_detail_without_loading_history(
    monkeypatch,
    tmp_path,
) -> None:
    store, sessions = _service(tmp_path)
    session = SessionRecord(title="Bounded detail")
    run = RunRecord(session_id=session.session_id, status="completed")
    session = session.model_copy(update={"last_run_id": run.run_id})
    store.create_session_snapshot(
        session,
        [
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text="history",
                sequence=1,
            )
        ],
    )
    store.save_run(run)
    store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_completed",
        )
    )

    def reject_full_history(*_args, **_kwargs):
        raise AssertionError("open must not load complete Session history")

    monkeypatch.setattr(store, "load_session", reject_full_history)
    detail = sessions.open(session.session_id, recover=False)

    assert detail.session.session_id == session.session_id
    assert detail.recent_run == run


@pytest.mark.asyncio
async def test_generated_title_is_sanitized_and_never_overwrites_user_title(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    generated_session = sessions.create(title="Fallback").session

    async def generator(_session, _messages) -> str:
        return "  Generated\n title  "

    generated = await sessions.generate_title(generated_session.session_id, generator)
    renamed = sessions.rename(generated_session.session_id, "User title")
    ignored = await sessions.generate_title(generated_session.session_id, generator)

    assert generated.session.title == "Generated title"
    assert generated.session.title_source == "generated"
    assert renamed.session.title_locked is True
    assert ignored.session.title == "User title"
    assert store.load_session(generated_session.session_id).session.title == "User title"


@pytest.mark.asyncio
async def test_title_generation_failure_keeps_fallback_and_returns_diagnostic(tmp_path) -> None:
    _, sessions = _service(tmp_path)
    session = sessions.create(title="Fallback").session

    async def failing_generator(_session, _messages) -> str:
        raise RuntimeError("title provider unavailable")

    result = await sessions.generate_title(session.session_id, failing_generator)

    assert result.session.title == "Fallback"
    assert result.session.title_source == "fallback"
    assert result.diagnostics[0].code == "session_title_generation_failed"


def test_session_mutation_succeeds_with_visible_diagnostic_when_index_refresh_fails(
    monkeypatch,
    tmp_path,
) -> None:
    store, sessions = _service(tmp_path)
    monkeypatch.setattr(
        store,
        "refresh_catalog",
        lambda _session_id: "index disk unavailable",
    )

    result = sessions.create(title="Canonical first")

    assert store.load_session(result.session.session_id).session.title == ("Canonical first")
    assert result.diagnostics[0].code == "session_index_refresh_failed"
    assert result.diagnostics[0].recoverable is True


def test_dirty_index_marker_forces_a_later_catalog_self_heal(monkeypatch, tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Before").session
    original_write_index = store._catalog._write

    def fail_write_index(_index):
        raise PersistenceError("injected index failure")

    monkeypatch.setattr(store._catalog, "_write", fail_write_index)
    renamed = sessions.rename(session.session_id, "After")
    assert renamed.diagnostics[0].code == "session_index_refresh_failed"
    assert store._index_dirty_path.exists()

    monkeypatch.setattr(store._catalog, "_write", original_write_index)
    catalog = sessions.catalog()

    assert catalog.entries[0].title == "After"
    assert not store._index_dirty_path.exists()


def test_session_update_uses_incremental_index_path(monkeypatch, tmp_path) -> None:
    store, sessions = _service(tmp_path)
    first = sessions.create(title="First").session
    second = sessions.create(title="Second").session

    def reject_full_scan():
        raise AssertionError("incremental update must not scan every session")

    monkeypatch.setattr(store._catalog, "scan", reject_full_scan)
    sessions.rename(first.session_id, "First renamed")

    by_id = {entry.session_id: entry for entry in store.read_catalog().sessions}
    assert by_id[first.session_id].title == "First renamed"
    assert by_id[second.session_id].title == "Second"


def test_catalog_filters_rebuildable_typed_entries(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    first = sessions.create(title="Alpha work", workspace="C:/work/alpha").session
    second = sessions.create(title="Beta work", workspace="C:/work/beta").session
    sessions.set_tags(first.session_id, ["important", "python"])
    sessions.set_archived(second.session_id, True)

    result = sessions.catalog(
        SessionCatalogQuery(text="alpha", archived=False, tags=frozenset({"python"}))
    )

    assert [entry.session_id for entry in result.entries] == [first.session_id]
    assert result.entries[0].root_session_id == first.session_id
    assert result.entries[0].health == "ok"
    assert store.read_catalog().data_layout_version == DATA_LAYOUT_VERSION


def test_catalog_searches_only_bounded_fields_and_latest_preview(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Search").session
    first = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="historical-needle",
        sequence=1,
    )
    second = MessageRecord.text(
        session_id=session.session_id,
        role="assistant",
        text="latest preview",
        sequence=2,
    ).model_copy(update={"reasoning_content": "private-reasoning-needle"})
    store.append_message(first)
    store.append_message(second)
    canonical = store.load_session(session.session_id).session
    store.save_session(
        canonical.model_copy(update={"message_count": 2, "last_message_id": second.message_id})
    )
    store.rebuild_catalog()

    historical = sessions.catalog(SessionCatalogQuery(text="historical-needle"))
    latest = sessions.catalog(SessionCatalogQuery(text="latest preview"))
    reasoning = sessions.catalog(SessionCatalogQuery(text="private-reasoning-needle"))

    assert historical.entries == ()
    assert [entry.session_id for entry in latest.entries] == [session.session_id]
    assert reasoning.entries == ()
    assert latest.entries[0].last_message_preview == "latest preview"
    assert "historical-needle" not in store._catalog.index_path.read_text(encoding="utf-8")


def test_catalog_keeps_valid_sessions_visible_when_another_session_is_corrupt(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    valid = sessions.create(title="Valid").session
    corrupt_dir = store.sessions_dir / "ses_corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "session.json").write_text("{broken", encoding="utf-8")
    store.rebuild_catalog()

    result = sessions.catalog()

    by_id = {entry.session_id: entry for entry in result.entries}
    assert by_id[valid.session_id].health == "ok"
    assert by_id["ses_corrupt"].health == "corrupt"


def test_catalog_marks_incompatible_session_record_as_migration_required(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Old record").session
    session_path = store.sessions_dir / session.session_id / "session.json"
    raw = json.loads(session_path.read_text("utf-8"))
    raw.pop("schema_version")
    session_path.write_text(json.dumps(raw), encoding="utf-8")

    store.rebuild_catalog()

    result = sessions.catalog()
    entry = next(item for item in result.entries if item.session_id == session.session_id)
    assert entry.health == "migration_required"


def test_branch_remaps_messages_and_expands_complete_tool_exchange(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Tool task").session
    user = MessageRecord.text(
        session_id=source.session_id,
        run_id="run_source",
        role="user",
        text="read it",
        sequence=1,
    )
    assistant = MessageRecord.text(
        session_id=source.session_id,
        run_id="run_source",
        role="assistant",
        text="",
        sequence=2,
    ).model_copy(update={"tool_calls": [ToolCallRequest(id="call_1", name="Read")]})
    result = MessageRecord.text(
        session_id=source.session_id,
        run_id="run_source",
        role="tool_result",
        text="contents",
        sequence=3,
    ).model_copy(
        update={"tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read")}
    )
    for message in (user, assistant, result):
        store.append_message(message)
    canonical = store.load_session(source.session_id).session
    store.save_session(
        canonical.model_copy(update={"message_count": 3, "last_message_id": result.message_id})
    )

    branch = sessions.branch(source.session_id, assistant.message_id)
    branch_snapshot = store.load_session(branch.session.session_id)
    source_snapshot = store.load_session(source.session_id)

    assert branch.session.parent_session_id == source.session_id
    assert branch.session.root_session_id == source.session_id
    assert branch.session.forked_from_message_id == assistant.message_id
    assert len(branch_snapshot.messages) == 3
    assert [message.sequence for message in branch_snapshot.messages] == [1, 2, 3]
    assert all(message.run_id is None for message in branch_snapshot.messages)
    assert branch_snapshot.messages[1].metadata["source_message_id"] == assistant.message_id
    assert branch.message_id_map[assistant.message_id] == branch_snapshot.messages[1].message_id
    assert [message.message_id for message in source_snapshot.messages] == [
        user.message_id,
        assistant.message_id,
        result.message_id,
    ]


def test_branch_rejects_incomplete_tool_exchange_and_reports_previous_boundary(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Incomplete").session
    user = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="do it",
        sequence=1,
    )
    assistant = MessageRecord.text(
        session_id=source.session_id,
        role="assistant",
        text="",
        sequence=2,
    ).model_copy(update={"tool_calls": [ToolCallRequest(id="call_missing", name="Write")]})
    store.append_message(user)
    store.append_message(assistant)

    with pytest.raises(SessionBranchError, match=user.message_id):
        sessions.branch(source.session_id, assistant.message_id)


def test_branch_applies_extension_copy_and_reset_policies(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Extension lifecycle").session
    first = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="define goal",
        sequence=1,
    )
    projection = MessageRecord.text(
        session_id=source.session_id,
        role="system_synthetic",
        semantic_tag="goal-contract",
        text="active goal",
        sequence=2,
    )
    branch_point = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="continue",
        sequence=3,
    )
    for message in (first, projection, branch_point):
        store.append_message(message)
    store.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=source.session_id,
            owner_id="dev.ouro.goal_mode",
            state_schema_version=2,
            branch_policy="reset",
            projection_semantic_tags=("goal-contract",),
            payload={"status": "active"},
        )
    )
    store.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=source.session_id,
            owner_id="dev.example.preferences",
            state_schema_version=1,
            branch_policy="copy",
            payload={"detail": "concise"},
        )
    )

    exported = sessions.export(source.session_id)
    assert {state.owner_id for state in exported.extension_states} == {
        "dev.ouro.goal_mode",
        "dev.example.preferences",
    }

    result = sessions.branch(source.session_id, branch_point.message_id)
    snapshot = store.load_session(result.session.session_id)

    assert [message.text_content for message in snapshot.messages] == [
        "define goal",
        "continue",
    ]
    assert projection.message_id not in result.message_id_map
    assert len(snapshot.extension_states) == 1
    assert snapshot.extension_states[0].owner_id == "dev.example.preferences"
    assert snapshot.extension_states[0].session_id == result.session.session_id


def test_branch_rejects_reset_projection_as_branch_point(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Projection boundary").session
    projection = MessageRecord.text(
        session_id=source.session_id,
        role="system_synthetic",
        semantic_tag="plan-context",
        text="current plan",
        sequence=1,
    )
    store.append_message(projection)
    store.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=source.session_id,
            owner_id="dev.ouro.plan_mode",
            state_schema_version=1,
            branch_policy="reset",
            projection_semantic_tags=("plan-context",),
            payload={"steps": []},
        )
    )

    with pytest.raises(SessionBranchError, match="must reset"):
        sessions.branch(source.session_id, projection.message_id)


def test_branch_honors_extension_reject_policy(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Unbranchable extension").session
    message = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="branch",
        sequence=1,
    )
    store.append_message(message)
    store.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=source.session_id,
            owner_id="dev.example.unbranchable",
            state_schema_version=1,
            branch_policy="reject",
            payload={"external_transaction": "open"},
        )
    )

    with pytest.raises(SessionBranchError, match="dev.example.unbranchable"):
        sessions.branch(source.session_id, message.message_id)


def test_sqlite_branch_applies_extension_reset_policy(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    store.initialize()
    sessions = SessionService(store)
    source = sessions.create(title="SQLite extension lifecycle").session
    projection = MessageRecord.text(
        session_id=source.session_id,
        role="system_synthetic",
        semantic_tag="goal-contract",
        text="goal",
        sequence=1,
    )
    branch_point = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="continue",
        sequence=2,
    )
    store.append_message(projection)
    store.append_message(branch_point)
    store.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=source.session_id,
            owner_id="dev.ouro.goal_mode",
            state_schema_version=2,
            branch_policy="reset",
            projection_semantic_tags=("goal-contract",),
            payload={"status": "active"},
        )
    )

    result = sessions.branch(source.session_id, branch_point.message_id)
    snapshot = store.load_session(result.session.session_id)

    assert [message.text_content for message in snapshot.messages] == ["continue"]
    assert snapshot.extension_states == []


def test_branch_refuses_to_publish_dangling_resource_references(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Resource").session
    message = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="with file",
        sequence=1,
    ).model_copy(update={"resource_refs": ["resource_1"]})
    store.append_message(message)

    with pytest.raises(SessionBranchError, match="ResourceService"):
        sessions.branch(source.session_id, message.message_id)


@pytest.mark.parametrize("status", ["running", "waiting_approval", "waiting_user"])
def test_branch_reads_durable_history_from_an_active_source(tmp_path, status) -> None:
    store, sessions = _service(tmp_path)
    source = SessionRecord(title="Active source", status=status, active_run_id="run_active")
    store.create_session(source)
    message = MessageRecord.text(
        session_id=source.session_id,
        run_id="run_active",
        role="user",
        text="durable point",
        sequence=1,
    )
    store.append_message(message)

    branch = sessions.branch(source.session_id, message.message_id)

    assert branch.session.parent_session_id == source.session_id
    assert store.load_session(source.session_id).session.status == status


def test_branch_publish_failure_leaves_source_unchanged_and_no_partial_branch(
    monkeypatch,
    tmp_path,
) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Atomic branch").session
    message = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="branch here",
        sequence=1,
    )
    store.append_message(message)
    store.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=source.session_id,
            owner_id="dev.example.copy",
            state_schema_version=1,
            branch_policy="copy",
            payload={"value": 1},
        )
    )
    before = store.load_session(source.session_id).model_dump()
    original = store._write_json_atomic

    def fail_resource_index(path, value) -> None:
        if path.name == "index.json" and path.parent.name == "resources":
            raise PersistenceError("injected branch publish failure")
        original(path, value)

    monkeypatch.setattr(store, "_write_json_atomic", fail_resource_index)

    with pytest.raises(PersistenceError, match="injected branch publish failure"):
        sessions.branch(source.session_id, message.message_id)

    assert store.load_session(source.session_id).model_dump() == before
    assert list(store.sessions_dir.glob("*.staging")) == []
    assert [path for path in store.sessions_dir.glob("*/session.json")] == [
        store.sessions_dir / source.session_id / "session.json"
    ]


def test_repeated_branch_requests_create_distinct_lineage_preserving_sessions(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    source = sessions.create(title="Repeat branch").session
    message = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="branch twice",
        sequence=1,
    )
    store.append_message(message)

    first = sessions.branch(source.session_id, message.message_id)
    second = sessions.branch(source.session_id, message.message_id)

    assert first.session.session_id != second.session.session_id
    assert first.session.forked_from_message_id == second.session.forked_from_message_id
    assert store.load_session(first.session.session_id).messages[0].message_id != (
        store.load_session(second.session.session_id).messages[0].message_id
    )


def test_replay_is_a_sorted_read_only_event_projection(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    session = sessions.create(title="Replay").session
    second = EventRecord(
        session_id=session.session_id,
        run_id="run_1",
        event_type="model_text_delta",
        sequence=2,
    )
    first = EventRecord(
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        sequence=1,
    )
    store.append_event(second)
    store.append_event(first)

    replay = sessions.replay(session.session_id, run_id="run_1")

    assert [frame.event.event_id for frame in replay.frames] == [first.event_id, second.event_id]
    assert [event.event_id for event in store.load_session(session.session_id).events] == [
        second.event_id,
        first.event_id,
    ]


def test_trash_discards_unfinished_durable_run_and_moves_session_tree_atomically(
    tmp_path,
) -> None:
    store, sessions = _service(tmp_path)
    running = SessionRecord(title="Running", status="running", active_run_id="run_active")
    store.create_session(running)
    store.save_run(RunRecord(session_id=running.session_id, run_id="run_active", status="running"))

    result = sessions.trash(running.session_id)

    assert result.session_ids == (running.session_id,)
    assert not (store.sessions_dir / running.session_id).exists()
    assert (
        store.trash_sessions_dir
        / result.trash_id
        / "sessions"
        / running.session_id
        / "session.json"
    ).exists()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_trash_accepts_session_with_incompatible_runtime_records(tmp_path, backend) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    sessions = SessionService(store)
    session = SessionRecord(title="Old runtime")
    run = RunRecord(
        session_id=session.session_id,
        run_id="run_old_contract",
        status="completed",
        agent_catalog=AgentCatalogSnapshot(),
    )
    store.create_session(session)
    store.save_run(run)
    payload = json.loads(run.model_dump_json())
    payload["agent_catalog"]["contract_hash"] = "sha256:" + "0" * 64
    if backend == "files":
        run_path = store.sessions_dir / session.session_id / "runs" / f"{run.run_id}.json"
        run_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            connection.execute(
                "UPDATE runs SET payload = ? WHERE session_id = ? AND run_id = ?",
                (json.dumps(payload), session.session_id, run.run_id),
            )
            connection.commit()

    with pytest.raises(PersistenceError, match="agent catalog contract hash"):
        store.load_runtime_session(session.session_id)

    result = sessions.trash(session.session_id)

    assert result.session_ids == (session.session_id,)
    with pytest.raises(FileNotFoundError):
        store.load_session_record(session.session_id)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_trash_accepts_root_with_incompatible_delegation_record(tmp_path, backend) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    sessions = SessionService(store)
    root = SessionRecord(session_id="ses_old_root")
    child = SessionRecord(
        session_id="ses_old_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_old_contract",
    )
    store.create_session(root)
    store.create_session(child)
    relation = store.create_delegation(
        DelegationRecord(
            delegation_id=child.delegation_id,
            idempotency_key="old/delegation",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            parent_run_id="run_old_parent",
            agent_id="general",
            task_description="Old child",
            prompt_digest="sha256:old-child",
            definition=AgentDefinition(
                agent_id="general",
                name="General",
                description="Old child",
            ),
        )
    )
    payload = relation.model_dump(mode="json")
    payload.update(
        {
            "parent_tool_call_id": "call_old",
            "execution_mode": "async",
            "delivery_policy": "notify_only",
        }
    )
    if backend == "files":
        relation_path = (
            store.sessions_dir / root.session_id / "delegations" / f"{relation.delegation_id}.json"
        )
        relation_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            connection.execute(
                "UPDATE delegations SET payload = ? WHERE delegation_id = ?",
                (json.dumps(payload), relation.delegation_id),
            )
            connection.commit()

    with pytest.raises(PersistenceError, match="async child requires next_boundary"):
        store.list_delegations(root.session_id)

    result = sessions.trash(root.session_id)

    with pytest.raises(FileNotFoundError):
        store.load_session_record(root.session_id)
    with pytest.raises(FileNotFoundError):
        store.load_session_record(child.session_id)
    assert set(result.session_ids) == {root.session_id, child.session_id}


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_root_archive_and_trash_preserve_agent_descendant_scope(tmp_path, backend) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    sessions = SessionService(store)
    root = SessionRecord(session_id="ses_agent_root", title="Agent root")
    parent = RunRecord(
        run_id="run_agent_parent",
        session_id=root.session_id,
        status="completed",
    )
    store.create_session(root)
    store.save_run(parent)
    definition = AgentDefinition(
        agent_id="general",
        name="General",
        description="General subagent.",
    )
    relation = store.create_delegation(
        DelegationRecord(
            delegation_id="dlg_agent_child",
            idempotency_key="host/agent-child",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id=definition.agent_id,
            task_description="Preserve descendant history",
            prompt_digest="sha256:agent-child",
            definition=definition,
        )
    )
    child = SessionRecord(
        session_id="ses_agent_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id=relation.delegation_id,
        status="running",
        active_run_id="run_agent_child",
        last_run_id="run_agent_child",
    )
    child_run = RunRecord(
        run_id="run_agent_child",
        session_id=child.session_id,
        parent_run_id=parent.run_id,
        status="running",
    )
    store.create_session(child)
    store.save_run(child_run)
    relation = store.save_delegation(
        relation.model_copy(
            update={
                "spawn_state": "submitted",
                "child_session_id": child.session_id,
                "current_child_run_id": child_run.run_id,
            }
        ),
        expected_revision=relation.revision,
    )

    relation = store.save_delegation(
        relation.model_copy(
            update={
                "result": DelegationResult(
                    child_session_id=child.session_id,
                    child_run_id=child_run.run_id,
                    terminal_status="completed",
                    delivery=ResultDelivery(status="skipped"),
                )
            }
        ),
        expected_revision=relation.revision,
    )
    with pytest.raises(SessionBusyError, match="active or unresolved Agent descendant"):
        sessions.set_archived(root.session_id, True)

    store.save_run(child_run.model_copy(update={"status": "completed"}))
    store.save_session(child.model_copy(update={"status": "idle", "active_run_id": None}))
    assert sessions.set_archived(root.session_id, True).session.archived is True

    result = sessions.trash(child.session_id)

    assert set(result.session_ids) == {root.session_id, child.session_id}
    with pytest.raises(FileNotFoundError):
        store.load_session_record(root.session_id)
    with pytest.raises(FileNotFoundError):
        store.load_session_record(child.session_id)


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_trash_tree_refuses_concurrent_live_owner_without_partial_removal(
    tmp_path,
    backend: str,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    sessions = SessionService(store)
    root = SessionRecord(session_id="ses_live_root")
    child = SessionRecord(
        session_id="ses_live_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_live_child",
    )
    store.create_session(root)
    store.create_session(child)
    live_lock = store.acquire_session_lock(child.session_id, "run_live")
    assert live_lock is not None

    try:
        with pytest.raises(SessionBusyError, match="live runtime owns"):
            sessions.trash(root.session_id)
    finally:
        live_lock.release()

    assert {record.session_id for record in store.list_sessions()} == {
        root.session_id,
        child.session_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_stop_and_trash_aborts_live_tree_before_atomic_removal(
    tmp_path,
    backend: str,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    sessions = SessionService(store)
    root = SessionRecord(
        session_id="ses_running_root",
        status="running",
        active_run_id="run_running_root",
        last_run_id="run_running_root",
    )
    run = RunRecord(
        run_id="run_running_root",
        session_id=root.session_id,
        status="running",
    )
    store.create_session(root)
    store.save_run(run)
    live_lock = store.acquire_session_lock(root.session_id, run.run_id)
    assert live_lock is not None

    async def release_after_abort() -> None:
        while store.load_run(root.session_id, run.run_id).status != "aborted":
            await asyncio.sleep(0.01)
        live_lock.release()

    owner = asyncio.create_task(release_after_abort())
    result = await sessions.stop_and_trash(root.session_id)
    await owner

    assert result.session_ids == (root.session_id,)
    with pytest.raises(FileNotFoundError):
        store.load_session_record(root.session_id)


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_trash_orphan_subagent_removes_its_complete_root_tree(tmp_path, backend: str) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    sessions = SessionService(store)
    root = SessionRecord(session_id="ses_orphan_root")
    child = SessionRecord(
        session_id="ses_orphan_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_missing",
    )
    store.create_session(root)
    store.create_session(child)

    result = sessions.trash(child.session_id)

    assert set(result.session_ids) == {root.session_id, child.session_id}
    assert store.list_sessions() == []
