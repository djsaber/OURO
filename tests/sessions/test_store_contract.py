from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

import ouro.sessions.backends.files.store as file_store
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.core.base import utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextBuilder, ContextMetrics, ProviderContext
from ouro.core.context_budget import ContextBudget
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError, ResourceNotFoundError, UnsupportedDataVersionError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import ReasoningRequest
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.resources.prepared import save_prepared_resources
from ouro.sessions import LocalSessionStore, SessionStore, SQLiteSessionStore
from tests.context_facts import save_context
from tests.harness_state import remove_harness_state, replace_harness_state
from tests.resource_fixtures import resource_record
from tests.session_storage import session_connection, write_session_payload


def test_session_read_preserves_callers_domain_errors(session_store: SessionStore) -> None:
    session = SessionRecord()
    session_store.create_session(session)
    failure = ValueError("invalid requested observation")
    with pytest.raises(ValueError) as raised:
        with session_store.session_read(session.session_id):
            session_store.load_session_record(session.session_id)
            raise failure
    assert raised.value is failure


def test_reserved_file_snapshot_retries_after_native_publication_failure(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    save_prepared_resources(store.root, session.session_id, [])
    message = MessageRecord.text(session_id=session.session_id, role="assistant", text="snapshot")
    database = store.sessions_dir / session.session_id / "session-data.sqlite3"
    publish = file_store.write_session

    def fail_after_head(connection, head):
        publish(connection, head)
        raise PersistenceError("injected snapshot publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(file_store, "write_session", fail_after_head)
        with pytest.raises(PersistenceError, match="injected snapshot"):
            store.create_session_snapshot(session, [message], prepared_resources=True)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM locations").fetchone() == (0,)
    store.create_session_snapshot(session, [message], prepared_resources=True)
    assert store.load_session_record(session.session_id).message_count == 1
    assert store.load_messages_tail(session.session_id)[0].message_id == message.message_id


@pytest.fixture(params=("files", "sqlite"))
def session_store(request: pytest.FixtureRequest, tmp_path) -> Iterator[SessionStore]:
    if request.param == "files":
        store = LocalSessionStore(tmp_path / "files")
    else:
        store = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    store.initialize()
    yield store


def test_sqlite_concurrent_initialization_preserves_all_sessions(tmp_path):
    barrier = Barrier(8)
    database = tmp_path / "ouro.db"

    def create(index):
        store = SQLiteSessionStore(database)
        barrier.wait(timeout=5)
        store.initialize()
        session = SessionRecord(title=f"Concurrent {index}")
        store.create_session(session)
        return session

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(create, range(8)))
    store = SQLiteSessionStore(database)
    store.initialize()
    assert {session.session_id for session in store.list_sessions()} == {
        session.session_id for session in created
    }


def test_session_store_round_trip_and_pages(session_store: SessionStore) -> None:
    session = SessionRecord(
        title="Contract",
        reasoning_request=ReasoningRequest(mode="enabled", effort="high"),
    )
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
        covered_message_count=1,
        covered_end_message_id=message.message_id,
        history_boundary_message_id=message.message_id,
        context_metrics_after=ContextBuilder().build(messages=[message], tools=[]).metrics,
    )
    session = session.model_copy(
        update={
            "context_selection": session.context_selection.model_copy(
                update={"compaction_id": compaction.compaction_id}
            )
        }
    )
    reference = resource_record(resource_id="res_" + "a" * 32)

    session_store.create_session_snapshot(
        session,
        [message],
        [reference],
        compactions=[compaction],
        resource_contents=((reference, b"fixture"),),
    )
    session_store.save_run(run)
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
    assert snapshot.session.reasoning_request == session.reasoning_request
    assert snapshot.messages == [message]
    assert snapshot.runs == [run]
    assert snapshot.compactions == [compaction]
    assert session_store.load_compactions(session.session_id) == [compaction]
    assert [event.sequence for event in snapshot.events] == [1, 2]
    assert (first.sequence, second.sequence) == (1, 2)
    assert session_store.latest_event_sequence(session.session_id) == 2
    assert session_store.load_resource_references(session.session_id) == [reference]
    assert session_store.load_message(session.session_id, message.message_id) == message
    with pytest.raises(PersistenceError, match="message not found"):
        session_store.load_message(session.session_id, "msg_missing")

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


def test_session_store_ignores_unknown_record_fields(session_store: SessionStore) -> None:
    session = SessionRecord(title="Historical message")
    message = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="legacy",
        sequence=1,
    )
    session_store.create_session_snapshot(session, [message])
    payload = message.model_dump(mode="json")
    payload["synthetic"] = False

    if isinstance(session_store, LocalSessionStore):
        record = session_store.sessions_dir / session.session_id / "messages.jsonl"
        payload = json.loads(record.read_bytes())
        payload["synthetic"] = False
        record.write_bytes((json.dumps(payload) + "\n").encode("utf-8"))
    else:
        assert isinstance(session_store, SQLiteSessionStore)
        connection = sqlite3.connect(session_store.database)
        try:
            connection.execute(
                "UPDATE messages SET payload = ? WHERE session_id = ?",
                (json.dumps(payload), session.session_id),
            )
            connection.commit()
        finally:
            connection.close()

    messages, next_cursor = session_store.load_messages_page(session.session_id)

    assert [item.text_content for item in messages] == ["legacy"]
    assert "synthetic" not in messages[0].model_dump()
    assert next_cursor is None


def test_event_pages_filter_by_run_before_materializing_records(
    session_store: SessionStore,
) -> None:
    session = SessionRecord(title="Filtered events")
    session_store.create_session(session)
    selected = [
        EventRecord(
            session_id=session.session_id,
            run_id="run_selected",
            event_type="model_text_delta",
            payload={"text": str(index)},
        )
        for index in range(2)
    ]
    unrelated = EventRecord(
        session_id=session.session_id,
        run_id="run_unrelated",
        event_type="run_started",
        payload={"run_id": "run_selected"},
    )
    session_store.append_ordered_event(selected[0])
    session_store.append_ordered_event(unrelated)
    session_store.append_ordered_event(selected[1])

    first, cursor = session_store.load_events_page(
        session.session_id,
        run_id="run_selected",
        limit=1,
    )
    assert [event.event_id for event in first] == [selected[0].event_id]
    assert cursor is not None

    second, final_cursor = session_store.load_events_page(
        session.session_id,
        cursor=cursor,
        run_id="run_selected",
        limit=1,
    )
    assert [event.event_id for event in second] == [selected[1].event_id]
    assert final_cursor is None


def test_session_store_rejects_record_without_explicit_schema_version(
    session_store: SessionStore,
) -> None:
    session = SessionRecord(title="Version boundary")
    session_store.create_session(session)
    raw = session.model_dump(mode="json")
    raw.pop("schema_version")
    payload = json.dumps(raw)

    if isinstance(session_store, LocalSessionStore):
        write_session_payload(session_store, session.session_id, payload)
    else:
        connection = sqlite3.connect(session_store.database)
        try:
            connection.execute(
                "UPDATE sessions SET payload = ? WHERE session_id = ?",
                (payload, session.session_id),
            )
            connection.commit()
        finally:
            connection.close()

    with pytest.raises(UnsupportedDataVersionError, match="found missing, expected 5"):
        session_store.load_session_record(session.session_id)


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

    assert session_store.load_session(session.session_id).messages == [message]
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
            budget=ContextBudget(context_window=10000, max_output_tokens=1000),
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
        metrics=view.metrics,
        context_metadata=view.context_metadata,
        provider_context=view.provider_context,
    )
    second = first.model_copy(
        update={
            "context_id": "ctx_second",
            "turn_id": "turn_second",
            "model_round": 2,
        }
    )

    assert session_store.load_latest_context(session.session_id) is None
    assert session_store.load_latest_context_header(session.session_id) is None
    save_context(session_store, first)
    assert session_store.load_latest_context(session.session_id) == first
    assert session_store.load_latest_context_header(session.session_id) == first.header()
    save_context(session_store, second)
    assert session_store.load_latest_context(session.session_id) == second
    assert session_store.load_latest_context_header(session.session_id) == second.header()
    completed = second.model_copy(
        update={"provider_done_at": utc_now(), "usage_entry_id": second.expected_usage_entry_id}
    )
    session_store.settle_request_header(
        session.session_id,
        second.context_id,
        settled_at=completed.provider_done_at,
        provider_done=True,
        usage_entry_id=second.expected_usage_entry_id,
    )
    assert session_store.load_latest_context(session.session_id) == completed
    assert session_store.load_latest_context_header(session.session_id) == completed.header()
    if isinstance(session_store, LocalSessionStore):
        directory = session_store.sessions_dir / session.session_id
        assert not (directory / "context-index.json").exists()
        assert [path.name for path in (directory / "request-content").iterdir()] == [
            "ctx_second.json"
        ]
        with closing(sqlite3.connect(directory / "session-data.sqlite3")) as connection:
            header = json.loads(
                connection.execute(
                    "SELECT payload FROM request_headers WHERE context_id = 'ctx_second'"
                ).fetchone()[0]
            )
        assert "source_messages" not in header
        body = json.loads((directory / "request-content/ctx_second.json").read_text("utf-8"))
        assert "provider_done_at" not in body
        assert "usage_entry_id" not in body
        assert "provider_reported_input_tokens" not in body


def test_session_store_rejects_context_with_incompatible_capability_schema(
    session_store: SessionStore,
) -> None:
    session = SessionRecord(title="Context schema")
    session_store.create_session(session)
    context = ContextSnapshotRecord(
        session_id=session.session_id,
        run_id="run_media",
        turn_id="turn_media",
        model_round=1,
        provider_id="media",
        model_id="media",
        supports_image_input=True,
        metrics=ContextMetrics(estimated_input_tokens=0, estimated_fixed_tokens=0),
        provider_context=ProviderContext(),
    )
    save_context(session_store, context)
    raw = context.model_dump(mode="json")
    raw["schema_version"] = 3
    raw["supports_vision"] = raw.pop("supports_image_input")
    raw.pop("supports_audio_input")
    raw.pop("supports_video_input")
    payload = json.dumps(raw)
    database = (
        session_store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(session_store, LocalSessionStore)
        else session_store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE request_headers SET payload = ? WHERE session_id = ?",
            (payload, session.session_id),
        )

    with pytest.raises(UnsupportedDataVersionError, match="found 3, expected 11"):
        session_store.load_latest_context(session.session_id)
    with pytest.raises(UnsupportedDataVersionError, match="found 3, expected 11"):
        session_store.load_latest_context_header(session.session_id)


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
            # Deliberate on-disk corruption must bypass derived JSON indexes;
            # ordinary indexed writes reject malformed payloads before commit.
            connection.execute("DROP INDEX messages_session_run")
            connection.execute("DROP INDEX messages_session_queued_input")
            connection.execute("DROP INDEX messages_assistant_turn")
            connection.execute("DROP INDEX messages_context_delivery")
            connection.execute("DROP INDEX messages_assistant_run")
            connection.execute("DROP INDEX messages_input_history")
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
    resource_id = "res_" + "b" * 32
    session_store.create_session(first)
    session_store.create_session(second)

    session_store.attach_resource_reference(
        first.session_id,
        resource_record(resource_id=resource_id),
    )
    session_store.attach_resource_reference(
        second.session_id,
        resource_record(
            resource_id=resource_id,
            copied_from_session_id=first.session_id,
        ),
    )
    assert [
        item.resource_id for item in session_store.load_resource_references(first.session_id)
    ] == [resource_id]
    assert [
        item.resource_id for item in session_store.load_resource_references(second.session_id)
    ] == [resource_id]

    session_store.remove_resource_reference(first.session_id, resource_id)
    assert session_store.load_resource_references(first.session_id) == []
    assert [
        item.resource_id for item in session_store.load_resource_references(second.session_id)
    ] == [resource_id]
    session_store.remove_resource_reference(second.session_id, resource_id)
    assert session_store.load_resource_references(second.session_id) == []


def test_resource_lookup_is_scoped_and_does_not_list_other_resources(session_store, monkeypatch):
    session = SessionRecord()
    other = SessionRecord()
    session_store.create_session(session)
    session_store.create_session(other)
    reference = resource_record()
    session_store.attach_resource_reference(session.session_id, reference)

    def forbid_list(_session_id):
        pytest.fail("point lookup must not list all Session resources")

    monkeypatch.setattr(session_store, "load_resource_references", forbid_list)
    assert (
        session_store.load_resource_reference(session.session_id, reference.resource_id)
        == reference
    )
    with pytest.raises(ResourceNotFoundError):
        session_store.load_resource_reference(other.session_id, reference.resource_id)
    with pytest.raises(ValueError):
        session_store.load_resource_reference(session.session_id, "../outside")


def test_resource_identity_cannot_be_rebound(session_store):
    session = SessionRecord()
    session_store.create_session(session)
    reference = resource_record()
    session_store.attach_resource_reference(session.session_id, reference)
    session_store.attach_resource_reference(session.session_id, reference)
    with pytest.raises(PersistenceError, match="identity"):
        session_store.attach_resource_reference(
            session.session_id, reference.model_copy(update={"filename": "different.txt"})
        )
    assert (
        session_store.load_resource_reference(session.session_id, reference.resource_id)
        == reference
    )


def test_unselected_resource_corruption_does_not_affect_point_read(session_store):
    session = SessionRecord()
    session_store.create_session(session)
    selected = resource_record()
    damaged = resource_record()
    session_store.attach_resource_reference(session.session_id, selected)
    session_store.attach_resource_reference(session.session_id, damaged)
    with session_connection(session_store, session.session_id) as connection:
        connection.execute(
            "UPDATE resource_references SET payload = ? WHERE session_id = ? AND resource_id = ?",
            ("invalid record", session.session_id, damaged.resource_id),
        )
    assert (
        session_store.load_resource_reference(session.session_id, selected.resource_id) == selected
    )
    with pytest.raises(PersistenceError):
        session_store.load_resource_reference(session.session_id, damaged.resource_id)


def test_session_store_owns_extension_state_lifecycle(session_store: SessionStore) -> None:
    session = SessionRecord(title="Extension state")
    session_store.create_session(session)
    state = SessionExtensionStateRecord(
        session_id=session.session_id,
        owner_id="dev.example.state",
        state_schema_version=2,
        branch_policy="reset",
        projection_semantic_tags=("example-projection",),
        payload={"status": "active"},
    )

    assert session_store.load_session_extension_states(session.session_id) == []
    replace_harness_state(session_store, state)
    assert session_store.load_session_extension_states(session.session_id) == [state]

    updated = state.model_copy(update={"payload": {"status": "complete"}})
    replace_harness_state(session_store, updated)
    assert session_store.load_session_extension_states(session.session_id) == [updated]
    assert session_store.load_session(session.session_id).extension_states == [updated]

    remove_harness_state(session_store, session.session_id, state.owner_id)
    assert session_store.load_session_extension_states(session.session_id) == []


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
    state = SessionExtensionStateRecord(
        session_id=child.session_id,
        owner_id="dev.example.lifecycle",
        state_schema_version=1,
        payload={"phase": "active"},
    )
    replace_harness_state(session_store, state)

    version_id = session_store.load_session_record(child.session_id).harness_version
    trash_id = session_store.trash_session_tree(
        root.session_id,
        (root.session_id, child.session_id),
    )

    assert trash_id.startswith("trash_")
    assert session_store.list_sessions() == []
    if isinstance(session_store, LocalSessionStore):
        extension_path = (
            session_store.trash_sessions_dir
            / trash_id
            / "sessions"
            / child.session_id
            / "harness"
            / f"{version_id}.json"
        )
        assert (
            state.model_dump(mode="json")
            in json.loads(extension_path.read_text(encoding="utf-8"))["state"]["states"]
        )
    else:
        with closing(sqlite3.connect(session_store.database)) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload FROM trashed_sessions WHERE trash_id = ?",
                    (trash_id,),
                ).fetchone()[0]
            )
        versions = payload["records"]["harness_versions"]
        records = [dict(zip(versions["columns"], row, strict=True)) for row in versions["rows"]]
        persisted = next(row for row in records if row["version_id"] == version_id)
        assert state.model_dump(mode="json") in json.loads(persisted["payload"])["state"]["states"]
        assert "session_commits" not in payload["records"]


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


def test_sparse_event_storage_preserves_explicit_nested_values(session_store):
    session = SessionRecord()
    session_store.create_session(session)
    event = EventRecord(
        session_id=session.session_id,
        run_id="external/run",
        turn_id="turn",
        history_epoch=2,
        sequence=7,
        event_type="diagnostic",
        payload={"nested": {"none": None, "empty": [], "zero": 0}},
        metadata={"none": None, "empty": {}},
    )
    session_store.append_event(event)
    assert session_store.load_events_page(session.session_id) == ([event], None)
    assert session_store.load_events_after(session.session_id, after_sequence=6) == ([event], False)
    assert session_store.latest_event_sequence(session.session_id) == 7


def test_nested_native_reads_share_one_scoped_connection(session_store, monkeypatch):
    session = SessionRecord()
    session_store.create_session(session)
    run = RunRecord(session_id=session.session_id)
    session_store.save_run(run)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="scope")
    session_store.append_message(message)
    connect = sqlite3.connect
    connections = []

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    with session_store.session_read(session.session_id):
        session_store.load_session_record(session.session_id)
        assert session_store.load_message(session.session_id, message.message_id) == message
        assert session_store.load_run(session.session_id, run.run_id) == run
        assert session_store.load_latest_request(session.session_id) is None
        assert session_store.load_active_compaction(session.session_id) is None
        assert session_store.load_session_observation(session.session_id).messages == [message]
        assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_nested_file_reads_share_the_boundary_and_preserve_outer_lifetime(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    observed = []
    observe = store._observe_session_directory

    def observe_directory(session_id):
        observed.append(session_id)
        observe(session_id)

    monkeypatch.setattr(store, "_observe_session_directory", observe_directory)
    with pytest.raises(ValueError, match="outer read failed"):
        with store.session_read(session.session_id) as outer:
            connection = outer.connection()
            with pytest.raises(ValueError, match="nested read failed"):
                with store.session_read(session.session_id) as inner:
                    assert inner is outer
                    assert store.load_session_record(session.session_id) == session
                    raise ValueError("nested read failed")
            assert connection.execute("SELECT 1").fetchone()[0] == 1
            assert observed == [session.session_id]
            raise ValueError("outer read failed")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    # A new reader observes directory replacement again; no scope or lock leaked.
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(store.load_session_record, session.session_id).result() == session
    assert observed == [session.session_id, session.session_id]


def test_nested_sqlite_reads_share_only_the_current_session_resource_lock(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    first, second = SessionRecord(), SessionRecord()
    for session in (first, second):
        store.create_session(session)
    resource_lock = store._resource_lock
    locked = []

    def observe_lock(session_id):
        locked.append(session_id)
        return resource_lock(session_id)

    monkeypatch.setattr(store, "_resource_lock", observe_lock)
    with store.session_read(first.session_id) as connection:
        with store.session_read(first.session_id) as nested:
            assert nested is connection
        with pytest.raises(ValueError, match="nested read failed"):
            with store.session_read(second.session_id) as other:
                assert other is connection
                assert store.load_session_record(second.session_id) == second
                raise ValueError("nested read failed")
        with store.session_read(first.session_id) as restored:
            assert restored is connection
        assert locked == [first.session_id, second.session_id]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with store.session_read(first.session_id):
        assert store.load_session_record(first.session_id) == first
    assert locked == [first.session_id, second.session_id, first.session_id]
