import json
import shutil
from pathlib import Path

import pytest

from ouro.application.session_recovery import SessionRecoveryService
from ouro.core.approvals import ApprovalRecord
from ouro.core.base import utc_now
from ouro.core.errors import MigrationRequiredError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolInvocation
from ouro.sessions import LocalSessionStore


def test_local_file_store_round_trip(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Local test", current_model="faux")
    run = RunRecord(session_id=session.session_id, status="completed", model="faux")
    message = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="user",
        text="hello",
        sequence=1,
    )
    event = EventRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        event_type="run_started",
        sequence=1,
    )

    store.create_session(session)
    store.save_run(run)
    store.append_message(message)
    store.append_event(event)

    snapshot = store.load_session(session.session_id)

    assert snapshot.session.title == "Local test"
    assert snapshot.messages[0].text_content == "hello"
    assert snapshot.events[0].event_type == "run_started"
    assert snapshot.runs[0].status == "completed"
    assert store.list_sessions()[0].session_id == session.session_id


def test_runtime_snapshot_never_reads_replay_event_journal(monkeypatch, tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Runtime state")
    store.create_session(session)
    store.append_message(
        MessageRecord.text(session_id=session.session_id, role="user", text="hello")
    )
    store.append_ordered_event(EventRecord(session_id=session.session_id, event_type="run_started"))
    read_paths: list[str] = []
    original = store._read_jsonl

    def record_read(path):
        read_paths.append(path.name)
        return original(path)

    monkeypatch.setattr(store, "_read_jsonl", record_read)

    snapshot = store.load_runtime_session(session.session_id)

    assert snapshot.messages[0].text_content == "hello"
    assert "events.jsonl" not in read_paths


def test_latest_event_sequence_reads_only_ordered_journal_tail(monkeypatch, tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Event tail")
    store.create_session(session)
    store.append_ordered_event(EventRecord(session_id=session.session_id, event_type="run_started"))
    store.append_ordered_event(
        EventRecord(session_id=session.session_id, event_type="model_request_started")
    )
    reopened = LocalSessionStore(tmp_path)

    def reject_full_journal_read(path):
        raise AssertionError(f"unexpected full journal read: {path}")

    monkeypatch.setattr(reopened, "_read_jsonl", reject_full_journal_read)

    assert reopened.latest_event_sequence(session.session_id) == 2


def test_jsonl_pages_use_stable_byte_cursors_across_appends(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Paged")
    store.create_session(session)
    for sequence in range(1, 6):
        store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=f"message {sequence}",
                sequence=sequence,
            )
        )

    first, cursor = store.load_messages_page(session.session_id, limit=2)
    assert [message.text_content for message in first] == ["message 1", "message 2"]
    assert cursor is not None

    store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="message 6",
            sequence=6,
        )
    )
    remaining: list[MessageRecord] = []
    while cursor is not None:
        page, cursor = store.load_messages_page(
            session.session_id,
            cursor=cursor,
            limit=2,
        )
        remaining.extend(page)

    assert [message.text_content for message in remaining] == [
        "message 3",
        "message 4",
        "message 5",
        "message 6",
    ]


def test_event_page_filters_during_incremental_scan(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Event pages")
    store.create_session(session)
    for sequence, run_id in enumerate(["run_a", "run_b", "run_a", "run_b"], start=1):
        store.append_event(
            EventRecord(
                session_id=session.session_id,
                run_id=run_id,
                event_type="diagnostic",
                sequence=sequence,
            )
        )

    first, cursor = store.load_events_page(
        session.session_id,
        run_id="run_a",
        limit=1,
    )
    assert [event.sequence for event in first] == [1]
    assert cursor is not None
    second, cursor = store.load_events_page(
        session.session_id,
        run_id="run_a",
        cursor=cursor,
        limit=1,
    )
    assert [event.sequence for event in second] == [3]
    assert cursor is not None
    final, cursor = store.load_events_page(
        session.session_id,
        run_id="run_a",
        cursor=cursor,
        limit=1,
    )
    assert final == []
    assert cursor is None


def test_page_rejects_cursor_inside_json_record(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid cursor")
    store.create_session(session)
    store.append_message(
        MessageRecord.text(session_id=session.session_id, role="user", text="hello")
    )

    with pytest.raises(PersistenceError, match="not at a record boundary"):
        store.load_messages_page(session.session_id, cursor=1)


def test_stream_delta_events_group_commit_at_durable_boundary(monkeypatch, tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Event group commit")
    store.create_session(session)
    fsync_calls = 0
    original_fsync = __import__("os").fsync

    def count_fsync(descriptor):
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(descriptor)

    monkeypatch.setattr("ouro.sessions.local_files.os.fsync", count_fsync)
    store.append_ordered_event(
        EventRecord(session_id=session.session_id, event_type="model_text_delta")
    )
    store.append_ordered_event(
        EventRecord(session_id=session.session_id, event_type="model_reasoning_delta")
    )
    assert fsync_calls == 0

    store.append_ordered_event(
        EventRecord(session_id=session.session_id, event_type="model_message_completed")
    )
    assert fsync_calls == 1


def test_local_file_store_rebuilds_corrupt_index(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Indexed")
    store.create_session(session)
    (tmp_path / "sessions" / "index.json").write_text("{not-json", encoding="utf-8")

    repaired = LocalSessionStore(tmp_path)
    repaired.initialize()
    index = repaired.read_catalog()

    assert index.sessions[0].session_id == session.session_id


def test_index_initialization_failure_does_not_block_canonical_session_open(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Canonical remains available")
    store.create_session(session)
    (store.sessions_dir / "index.json").unlink()

    def fail_index_write(_path, _model) -> None:
        raise PersistenceError("index is read-only")

    monkeypatch.setattr("ouro.sessions.local_index.write_model_atomic", fail_index_write)
    reopened = LocalSessionStore(tmp_path)

    reopened.initialize()
    snapshot = reopened.load_session(session.session_id)

    assert snapshot.session.title == "Canonical remains available"
    assert reopened._index_dirty_path.exists()


def test_local_file_store_recovers_interrupted_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interrupted", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)

    recovered = SessionRecoveryService(LocalSessionStore(tmp_path)).recover(session.session_id)

    assert recovered.session.status == "idle"
    assert recovered.session.metadata["recovered_incomplete_runs"] is True
    assert recovered.runs[0].status == "recoverable"
    assert recovered.runs[0].metadata["recovered_from_status"] == "running"
    assert recovered.events[-1].event_type == "run_status_changed"
    assert recovered.events[-1].payload["to_status"] == "recoverable"

    reloaded = LocalSessionStore(tmp_path).load_session(session.session_id)
    recovery_events = [
        event for event in reloaded.events if event.payload.get("to_status") == "recoverable"
    ]
    assert len(recovery_events) == 1


def test_recovery_aligns_session_with_durable_waiting_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    request = ToolCallRequest(id="call_waiting", name="Protected")
    approval = ApprovalRecord(
        session_id="session_waiting",
        run_id="run_waiting",
        tool_call_id=request.id,
    )
    batch = ToolCallBatchRecord(
        turn_id="turn_waiting",
        assistant_message_id="msg_waiting",
        phase="awaiting_interaction",
        calls=[
            ToolBatchCall(
                request=request,
                status="awaiting_approval",
                approval=approval,
            )
        ],
    )
    run = RunRecord(
        run_id="run_waiting",
        session_id="session_waiting",
        status="waiting_approval",
        pending_approval_id=approval.approval_id,
        active_tool_batch=batch,
    )
    session = SessionRecord(
        session_id=run.session_id,
        title="Waiting mismatch",
        status="running",
        active_run_id=run.run_id,
        last_run_id=run.run_id,
    )
    store.create_session(session)
    store.save_run(run)

    recovered = SessionRecoveryService(store).recover(session.session_id)

    assert recovered.session.status == "waiting_approval"
    assert recovered.session.active_run_id == run.run_id
    assert recovered.runs[0].status == "waiting_approval"


def test_recovery_turns_invalid_waiting_state_into_recoverable_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    run = RunRecord(session_id="session_dead_wait", status="waiting_user")
    session = SessionRecord(
        session_id=run.session_id,
        title="Dead waiting state",
        status="waiting_user",
        active_run_id=run.run_id,
        last_run_id=run.run_id,
    )
    store.create_session(session)
    store.save_run(run)

    recovered = SessionRecoveryService(store).recover(session.session_id)

    assert recovered.session.status == "idle"
    assert recovered.session.active_run_id is None
    assert recovered.runs[0].status == "recoverable"
    assert recovered.events[-1].payload["reason"] == "invalid_interaction_state"


def test_recovery_clears_terminal_run_from_active_session_projection(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    run = RunRecord(
        session_id="session_completed",
        status="completed",
        ended_at=utc_now(),
    )
    session = SessionRecord(
        session_id=run.session_id,
        title="Terminal mismatch",
        status="running",
        active_run_id=run.run_id,
        last_run_id=run.run_id,
    )
    store.create_session(session)
    store.save_run(run)

    recovered = SessionRecoveryService(store).recover(session.session_id)

    assert recovered.session.status == "idle"
    assert recovered.session.active_run_id is None
    assert recovered.runs[0].status == "completed"


def test_recovery_clears_missing_active_run_reference(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(
        title="Missing active run",
        status="running",
        active_run_id="run_missing",
    )
    store.create_session(session)

    recovered = SessionRecoveryService(store).recover(session.session_id)

    assert recovered.session.status == "idle"
    assert recovered.session.active_run_id is None


def test_recovery_rejects_ambiguous_multiple_active_runs(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Ambiguous active runs", status="running")
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, status="waiting_user"))
    store.save_run(RunRecord(session_id=session.session_id, status="waiting_approval"))
    lock = store.acquire_session_lock(session.session_id, "run_live")
    assert lock is not None
    try:
        with pytest.raises(PersistenceError, match="multiple active runs"):
            SessionRecoveryService(store).recover(session.session_id)
    finally:
        lock.release()


def test_recovery_marks_in_flight_tool_outcome_unknown(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interrupted tool", status="running")
    run = RunRecord(session_id=session.session_id, status="running", phase="tool_execution")
    request = ToolCallRequest(id="call_1", name="Write")
    invocation = ToolInvocation(
        session_id=session.session_id,
        run_id=run.run_id,
        tool_call_id=request.id,
        tool_name=request.name,
    )
    batch = ToolCallBatchRecord(
        turn_id="turn_1",
        assistant_message_id="msg_1",
        phase="executing",
        calls=[ToolBatchCall(request=request, status="running", invocation=invocation)],
    )
    run = run.model_copy(update={"active_tool_batch": batch})
    store.create_session(session)
    store.save_run(run)

    recovered = SessionRecoveryService(LocalSessionStore(tmp_path)).recover(session.session_id)

    recovered_batch = recovered.runs[0].active_tool_batch
    assert recovered.runs[0].status == "recoverable"
    assert recovered_batch is not None
    assert recovered_batch.phase == "recoverable"
    assert recovered_batch.calls[0].status == "unknown_outcome"


def test_recovery_marks_in_flight_unattended_resume_outcome_unknown(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interrupted unattended resume", status="running")
    run = RunRecord(session_id=session.session_id, status="running", phase="human_interaction")
    request = ToolCallRequest(id="call_resume", name="Workflow")
    invocation = ToolInvocation(
        session_id=session.session_id,
        run_id=run.run_id,
        tool_call_id=request.id,
        tool_name=request.name,
    )
    batch = ToolCallBatchRecord(
        turn_id="turn_resume",
        assistant_message_id="msg_resume",
        phase="executing",
        calls=[
            ToolBatchCall(
                request=request,
                status="resolving_input",
                invocation=invocation,
            )
        ],
    )
    run = run.model_copy(update={"active_tool_batch": batch})
    store.create_session(session)
    store.save_run(run)

    recovered = SessionRecoveryService(LocalSessionStore(tmp_path)).recover(session.session_id)

    recovered_batch = recovered.runs[0].active_tool_batch
    assert recovered.runs[0].status == "recoverable"
    assert recovered_batch is not None
    assert recovered_batch.calls[0].status == "unknown_outcome"


def test_recovery_uses_only_stream_events_after_latest_durable_tool_round(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    collided_at = utc_now()
    session = SessionRecord(title="Tool recovery", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)
    user = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="user",
        text="use tool",
        sequence=1,
    )
    store.append_message(user)
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id="turn_1",
            event_type="model_request_started",
            sequence=1,
            created_at=collided_at,
            payload={"turn_id": "turn_1"},
        )
    )
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id="turn_1",
            event_type="model_text_delta",
            sequence=2,
            created_at=collided_at,
            payload={"text": "already durable", "turn_id": "turn_1"},
        )
    )
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="assistant",
        text="already durable",
        sequence=2,
        turn_id="turn_1",
    ).model_copy(
        update={
            "created_at": collided_at,
            "tool_calls": [ToolCallRequest(id="call_1", name="Read")],
        }
    )
    result = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="tool_result",
        text="result",
        sequence=3,
        turn_id="turn_1",
    ).model_copy(
        update={
            "created_at": collided_at,
            "tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read"),
        }
    )
    store.append_message(assistant)
    store.append_message(result)
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id="turn_1",
            event_type="tool_call_completed",
            sequence=3,
            created_at=collided_at,
            payload={"turn_id": "turn_1"},
        )
    )
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id="turn_2",
            event_type="model_request_started",
            sequence=4,
            created_at=collided_at,
            payload={"turn_id": "turn_2"},
        )
    )
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id="turn_2",
            event_type="model_text_delta",
            sequence=5,
            created_at=collided_at,
            payload={"text": "new partial", "turn_id": "turn_2"},
        )
    )

    recovered = SessionRecoveryService(store).recover(session.session_id)

    assert [message.text_content for message in recovered.messages] == [
        "use tool",
        "already durable",
        "result",
        "new partial",
    ]
    assert recovered.messages[-1].metadata["recovered_from_event_journal"] is True
    assert recovered.messages[-1].turn_id == "turn_2"


def test_recovery_does_not_duplicate_a_committed_current_tool_round(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Committed tool round", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)
    user = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="user",
        text="use tool",
        sequence=1,
    )
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="assistant",
        text="calling",
        sequence=2,
    ).model_copy(update={"tool_calls": [ToolCallRequest(id="call_1", name="Read")]})
    store.append_message(user)
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_request_started",
            sequence=1,
        )
    )
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_text_delta",
            sequence=2,
            payload={"text": "calling"},
        )
    )
    store.append_message(assistant)
    store.append_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_call_requested",
            sequence=3,
        )
    )

    recovered = SessionRecoveryService(store).recover(session.session_id)

    assert [message.message_id for message in recovered.messages] == [
        user.message_id,
        assistant.message_id,
    ]


def test_local_file_store_lock_prevents_active_run_recovery(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Locked", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)
    lock = store.acquire_session_lock(session.session_id, run.run_id)

    try:
        snapshot = SessionRecoveryService(store).recover(session.session_id)
    finally:
        assert lock is not None
        lock.release()

    assert snapshot.session.status == "running"
    assert snapshot.runs[0].status == "running"
    assert not any(event.event_type == "run_status_changed" for event in snapshot.events)
    assert not store.is_session_locked(session.session_id)


def test_released_lock_file_does_not_block_interrupted_run_recovery(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interrupted", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)
    lock = store.acquire_session_lock(session.session_id, run.run_id)
    assert lock is not None
    lock.release()

    recovered = SessionRecoveryService(LocalSessionStore(tmp_path)).recover(session.session_id)

    assert recovered.runs[0].status == "recoverable"


def test_store_rejects_path_like_record_ids(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()

    with pytest.raises(ValueError, match="invalid session id"):
        store.load_session("../outside")

    session = SessionRecord(title="Safe")
    store.create_session(session)
    unsafe_run = RunRecord(session_id=session.session_id, run_id="../outside")
    with pytest.raises(ValueError, match="invalid run id"):
        store.save_run(unsafe_run)


def test_store_reports_corrupt_session_as_persistence_error(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Corrupt")
    store.create_session(session)
    session_path = tmp_path / "sessions" / session.session_id / "session.json"
    session_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(PersistenceError, match="could not decode session record"):
        store.load_session(session.session_id)


def test_canonical_mutators_do_not_create_partial_unknown_sessions(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    missing = SessionRecord(title="Missing")

    with pytest.raises(FileNotFoundError):
        store.save_session(missing)
    with pytest.raises(FileNotFoundError):
        store.append_message(
            MessageRecord.text(
                session_id=missing.session_id,
                role="user",
                text="must not create a directory",
            )
        )
    with pytest.raises(FileNotFoundError):
        store.save_run(RunRecord(session_id=missing.session_id))

    assert not (store.sessions_dir / missing.session_id).exists()


def test_create_session_cleans_staging_when_publish_preparation_fails(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Atomic create")
    original = store._write_json_atomic

    def fail_resource_index(path, value) -> None:
        if path.name == "index.json" and path.parent.name == "resources":
            raise PersistenceError("injected resource index failure")
        original(path, value)

    monkeypatch.setattr(store, "_write_json_atomic", fail_resource_index)

    with pytest.raises(PersistenceError, match="injected"):
        store.create_session(session)

    assert not (store.sessions_dir / session.session_id).exists()
    assert list(store.sessions_dir.glob("*.staging")) == []


def test_trash_session_tree_rolls_back_when_staging_preparation_fails(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Keep canonical")
    store.create_session(session)
    original = store._write_json_atomic

    def fail_trash_manifest(path, value) -> None:
        if path.name == "trash.json":
            raise PersistenceError("injected trash failure")
        original(path, value)

    monkeypatch.setattr(store, "_write_json_atomic", fail_trash_manifest)

    with pytest.raises(PersistenceError, match="injected"):
        store.trash_session_tree(session.session_id, (session.session_id,))

    assert (store.sessions_dir / session.session_id / "session.json").exists()
    assert list(store.trash_sessions_dir.glob(".*.staging")) == []


def test_trash_session_tree_rolls_back_every_session_when_a_child_move_fails(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    root = SessionRecord(session_id="ses_rollback_root")
    child = SessionRecord(
        session_id="ses_rollback_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_rollback_child",
    )
    store.create_session(root)
    store.create_session(child)
    child_dir = store.sessions_dir / child.session_id
    original_replace = Path.replace

    def fail_child_move(path: Path, target: Path) -> Path:
        if path == child_dir and target.name == child.session_id:
            raise OSError("injected child move failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_child_move)

    with pytest.raises(PersistenceError, match="injected child move failure"):
        store.trash_session_tree(
            root.session_id,
            (root.session_id, child.session_id),
        )

    assert (store.sessions_dir / root.session_id / "session.json").exists()
    assert (store.sessions_dir / child.session_id / "session.json").exists()
    assert list(store.trash_sessions_dir.glob(".*.staging")) == []


def test_initialize_finishes_interrupted_session_tree_trash_publish(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    root = SessionRecord(session_id="ses_tree_root")
    child = SessionRecord(
        session_id="ses_tree_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_tree_child",
    )
    store.create_session(root)
    store.create_session(child)
    staging_dir = store.trash_sessions_dir / ".trash_tree_recovery.staging"
    staged_sessions = staging_dir / "sessions"
    staged_sessions.mkdir(parents=True)
    (store.sessions_dir / root.session_id).replace(staged_sessions / root.session_id)
    (staging_dir / "trash.json").write_text(
        json.dumps(
            {
                "trash_id": "trash_tree_recovery",
                "root_session_id": root.session_id,
                "session_ids": [root.session_id, child.session_id],
                "trashed_at": "2026-07-23T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    LocalSessionStore(tmp_path).initialize()

    target = store.trash_sessions_dir / "trash_tree_recovery" / "sessions"
    assert (target / root.session_id / "session.json").exists()
    assert (target / child.session_id / "session.json").exists()
    assert store.list_sessions() == []
    assert not staging_dir.exists()


def test_store_rejects_incompatible_data_layout_without_modifying_manifest(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        '{"data_layout_version":999,"created_at":"2026-01-01T00:00:00Z",'
        '"updated_at":"2026-01-01T00:00:00Z"}\n',
        encoding="utf-8",
    )
    before = layout_path.read_bytes()

    with pytest.raises(MigrationRequiredError, match="found 999, expected 6"):
        LocalSessionStore(tmp_path).initialize()

    assert layout_path.read_bytes() == before


def test_store_rejects_data_from_before_typed_tool_messages(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        '{"data_layout_version":2,"created_at":"2026-01-01T00:00:00Z",'
        '"updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    with pytest.raises(MigrationRequiredError, match="found 2, expected 6"):
        LocalSessionStore(tmp_path).initialize()


@pytest.mark.parametrize("source_version", (3, 4))
def test_store_migrates_extension_state_layout(
    tmp_path,
    source_version: int,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Existing Session")
    store.create_session(session)
    extension_dir = store.sessions_dir / session.session_id / "extensions"
    shutil.rmtree(extension_dir)
    (tmp_path / "layout.json").write_text(
        json.dumps(
            {
                "data_layout_version": source_version,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    migrated = LocalSessionStore(tmp_path)
    migrated.initialize()

    assert migrated.load_session_extension_states(session.session_id) == []
    assert (
        json.loads((tmp_path / "layout.json").read_text(encoding="utf-8"))["data_layout_version"]
        == 6
    )
    assert (extension_dir / "index.json").is_file()


def test_store_migrates_v5_by_discarding_transcript_catalog(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Bounded catalog")
    store.create_session(session)
    (tmp_path / "layout.json").write_text(
        json.dumps(
            {
                "data_layout_version": 5,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    store._catalog.index_path.write_text(
        '{"data_layout_version":5,"sessions":[{"search_text":"full transcript"}]}',
        encoding="utf-8",
    )

    migrated = LocalSessionStore(tmp_path)
    migrated.initialize()

    assert migrated.read_catalog().sessions[0].title == "Bounded catalog"
    assert "full transcript" not in migrated._catalog.index_path.read_text(encoding="utf-8")
