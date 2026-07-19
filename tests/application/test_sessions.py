from __future__ import annotations

import pytest

from ouro.application.session_models import SessionCatalogQuery
from ouro.application.sessions import SessionService
from ouro.core.context import ContextBuilder
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError, SessionBranchError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.sessions import LocalSessionStore
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


def test_catalog_searches_all_user_assistant_text_but_not_reasoning(tmp_path) -> None:
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
    reasoning = sessions.catalog(SessionCatalogQuery(text="private-reasoning-needle"))

    assert [entry.session_id for entry in historical.entries] == [session.session_id]
    assert reasoning.entries == ()
    assert historical.entries[0].last_message_preview == "latest preview"
    assert "search_text" not in historical.entries[0].model_dump()


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


def test_trash_rejects_unfinished_session_and_moves_idle_session_atomically(tmp_path) -> None:
    store, sessions = _service(tmp_path)
    idle = sessions.create(title="Idle").session
    running = SessionRecord(title="Running", status="running", active_run_id="run_active")
    store.create_session(running)
    store.save_run(RunRecord(session_id=running.session_id, run_id="run_active", status="running"))

    with pytest.raises(SessionBusyError, match="unfinished work"):
        sessions.trash(running.session_id)

    result = sessions.trash(idle.session_id)

    assert not (store.sessions_dir / idle.session_id).exists()
    assert (store.trash_sessions_dir / result.trash_id / "session.json").exists()
