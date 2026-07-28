from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouro.application.session_recovery import SessionRecoveryService
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextBuilder
from ouro.core.context_snapshots import ContextSnapshotRecord, context_view_digest
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SessionStore, SQLiteSessionStore


@pytest.fixture(params=("files", "sqlite"))
def session_store(request: pytest.FixtureRequest, tmp_path) -> Iterator[SessionStore]:
    if request.param == "files":
        store = LocalSessionStore(tmp_path / "files")
    else:
        store = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    store.initialize()
    yield store


def test_session_store_round_trip_and_pages(session_store: SessionStore) -> None:
    session = SessionRecord(title="Contract")
    message = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="hello",
        sequence=1,
    )
    run = RunRecord(session_id=session.session_id, status="completed")
    compaction = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="summary",
        covered_message_ids=[message.message_id],
        source_digest="sha256:" + "0" * 64,
    )
    reference = SessionResourceReference(resource_id="res_" + "a" * 64)

    session_store.create_session_snapshot(session, [message], [reference])
    session_store.save_run(run)
    session_store.append_compaction(compaction)
    first = session_store.append_ordered_event(
        EventRecord(session_id=session.session_id, run_id=run.run_id, event_type="run_started")
    )
    second = session_store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_completed",
        )
    )

    snapshot = session_store.load_session(session.session_id)
    assert snapshot.messages == [message]
    assert snapshot.runs == [run]
    assert snapshot.compactions == [compaction]
    assert [event.sequence for event in snapshot.events] == [1, 2]
    assert (first.sequence, second.sequence) == (1, 2)
    assert session_store.latest_event_sequence(session.session_id) == 2
    assert session_store.load_resource_references(session.session_id) == [reference]

    first_page, cursor = session_store.load_events_page(session.session_id, limit=1)
    assert [event.event_type for event in first_page] == ["run_started"]
    assert cursor is not None
    second_page, final_cursor = session_store.load_events_page(
        session.session_id,
        cursor=cursor,
        limit=1,
    )
    assert [event.event_type for event in second_page] == ["run_completed"]
    assert final_cursor is None
    after_first, has_more = session_store.load_events_after(
        session.session_id,
        after_sequence=1,
        limit=1,
    )
    assert [event.event_type for event in after_first] == ["run_completed"]
    assert has_more is False
    first_after_zero, has_more = session_store.load_events_after(
        session.session_id,
        after_sequence=0,
        limit=1,
    )
    assert [event.event_type for event in first_after_zero] == ["run_started"]
    assert has_more is True


def test_session_store_preserves_unicode_line_separators_inside_messages(
    session_store: SessionStore,
) -> None:
    session = SessionRecord(title="Unicode separators")
    text = "first\u2028second\u2029third"
    message = MessageRecord.text(
        session_id=session.session_id,
        role="assistant",
        text=text,
        sequence=1,
    )

    session_store.create_session_snapshot(session, [message])

    assert session_store.load_runtime_session(session.session_id).messages == [message]
    assert session_store.load_messages_page(session.session_id) == ([message], None)


def test_session_store_replaces_latest_context_atomically(session_store: SessionStore) -> None:
    session = SessionRecord(title="Context")
    message = MessageRecord.text(session_id=session.session_id, role="user", text="hello")
    run = RunRecord(session_id=session.session_id, status="running")
    session_store.create_session_snapshot(session, [message])
    view = (
        ContextBuilder()
        .build(
            messages=[message],
            tools=[],
            context_window_tokens=10_000,
            reserved_output_tokens=1_000,
        )
        .model_copy(update={"context_id": "ctx_first"})
    )
    first = ContextSnapshotRecord(
        context_id="ctx_first",
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn_first",
        model_round=1,
        provider_id="test",
        model_id="test-model",
        system_prompt=view.system_prompt,
        source_messages=[message],
        tool_definitions=view.tool_definitions,
        metrics=view.metrics,
        context_metadata=view.context_metadata,
        context_digest=context_view_digest(view),
    )
    second = first.model_copy(
        update={
            "context_id": "ctx_second",
            "turn_id": "turn_second",
            "model_round": 2,
        }
    )

    assert session_store.load_latest_context(session.session_id) is None
    session_store.save_latest_context(first)
    assert session_store.load_latest_context(session.session_id) == first
    session_store.save_latest_context(second)
    assert session_store.load_latest_context(session.session_id) == second


def test_session_store_loads_runs_without_reading_journals(session_store: SessionStore) -> None:
    session = SessionRecord(title="Runs")
    message = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="hello",
        sequence=1,
    )
    session_store.create_session_snapshot(session, [message])
    run = RunRecord(session_id=session.session_id, status="completed")
    session_store.save_run(run)

    # Run queries read run records only; a broken message journal must not matter.
    if isinstance(session_store, LocalSessionStore):
        journal = session_store.sessions_dir / session.session_id / "messages.jsonl"
        journal.write_text("{not json\n", encoding="utf-8")
    else:
        connection = sqlite3.connect(session_store.database)
        try:
            connection.execute(
                "UPDATE messages SET payload = '{not json' WHERE session_id = ?",
                (session.session_id,),
            )
            connection.commit()
        finally:
            connection.close()

    assert session_store.load_runs(session.session_id) == [run]
    with pytest.raises(FileNotFoundError):
        session_store.load_runs("ses_missing")


def test_session_store_resource_reference_ownership(session_store: SessionStore) -> None:
    first = SessionRecord(title="First")
    second = SessionRecord(title="Second")
    resource_id = "res_" + "b" * 64
    session_store.create_session(first)
    session_store.create_session(second)

    session_store.attach_resource_reference(
        first.session_id,
        SessionResourceReference(resource_id=resource_id),
    )
    session_store.attach_resource_reference(
        second.session_id,
        SessionResourceReference(
            resource_id=resource_id,
            copied_from_session_id=first.session_id,
        ),
    )
    assert session_store.is_resource_referenced(resource_id)

    session_store.remove_resource_reference(first.session_id, resource_id)
    assert session_store.is_resource_referenced(resource_id)
    session_store.remove_resource_reference(second.session_id, resource_id)
    assert not session_store.is_resource_referenced(resource_id)


def test_session_store_allocates_event_sequences_atomically(
    session_store: SessionStore,
) -> None:
    session = SessionRecord(title="Concurrent events")
    session_store.create_session(session)

    def append(index: int) -> int:
        event = session_store.append_ordered_event(
            EventRecord(
                session_id=session.session_id,
                event_type="concurrency_probe",
                payload={"index": index},
            )
        )
        return event.sequence

    with ThreadPoolExecutor(max_workers=8) as executor:
        allocated = list(executor.map(append, range(24)))

    assert sorted(allocated) == list(range(1, 25))
    persisted = session_store.load_session(session.session_id).events
    assert [event.sequence for event in persisted] == list(range(1, 25))


def test_session_store_read_is_pure_and_recovery_is_explicit(
    session_store: SessionStore,
) -> None:
    session = SessionRecord(title="Interrupted", status="running")
    run = RunRecord(session_id=session.session_id, status="running")
    session_store.create_session(session)
    session_store.save_run(run)

    unchanged = session_store.load_session(session.session_id)
    assert unchanged.session.status == "running"
    assert unchanged.runs[0].status == "running"

    recovered = SessionRecoveryService(session_store).recover(session.session_id)
    assert recovered.session.status == "idle"
    assert recovered.runs[0].status == "recoverable"
    assert session_store.load_session(session.session_id) == recovered


def test_session_store_exclusive_run_lock(session_store: SessionStore) -> None:
    session = SessionRecord(title="Lock")
    session_store.create_session(session)
    first = session_store.acquire_session_lock(session.session_id, "run_first")
    assert first is not None
    try:
        assert session_store.is_session_locked(session.session_id)
        assert session_store.acquire_session_lock(session.session_id, "run_second") is None
    finally:
        first.release()
    assert not session_store.is_session_locked(session.session_id)


def test_session_store_trashes_complete_tree_without_loading_domain_records(
    session_store: SessionStore,
) -> None:
    root = SessionRecord(session_id="ses_tree_root")
    child = SessionRecord(
        session_id="ses_tree_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_tree_child",
    )
    session_store.create_session(root)
    session_store.create_session(child)

    trash_id = session_store.trash_session_tree(
        root.session_id,
        (root.session_id, child.session_id),
    )

    assert trash_id.startswith("trash_")
    assert session_store.list_sessions() == []


def test_session_store_catalog_and_trash(session_store: SessionStore) -> None:
    session = SessionRecord(title="Catalog")
    session_store.create_session(session)
    assert session_store.refresh_catalog(session.session_id) is None
    catalog = session_store.read_catalog()
    assert [entry.session_id for entry in catalog.sessions] == [session.session_id]

    trash_id = session_store.trash_session_tree(
        session.session_id,
        (session.session_id,),
    )
    assert trash_id.startswith("trash_")
    assert session_store.list_sessions() == []
    with pytest.raises(FileNotFoundError):
        session_store.load_session(session.session_id)
