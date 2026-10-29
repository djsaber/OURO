from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolResult
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.response_drafts import persist_response_draft
from tests.tool_batches import persist_tool_batch


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


def interrupted_run(store, *, response_committed=False):
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session = session.model_copy(
        update={"status": "running", "active_run_id": run.run_id, "last_run_id": run.run_id}
    )
    store.create_session(session)
    store.save_run(run)
    store.append_message(
        MessageRecord.text(session_id=session.session_id, run_id=run.run_id, role="user", text="go")
    )
    persist_response_draft(
        store, run, turn_id="turn_interrupted", text="partial", reasoning="thought"
    )
    if response_committed:
        store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                turn_id="turn_interrupted",
                role="assistant",
                text="complete answer",
            )
        )
    return store.load_session_record(session.session_id), run


@pytest.mark.parametrize("response_committed", [False, True])
def test_native_failure_keeps_recovered_response_state_and_event_together(
    store, monkeypatch, response_committed
):
    session, run = interrupted_run(store, response_committed=response_committed)
    before = store.load_session(session.session_id)
    if isinstance(store, LocalSessionStore):

        def fail_projection(connection, session):
            raise OSError("recovery projection failed")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_session", fail_projection)
            with pytest.raises(PersistenceError, match="recovery projection failed"):
                SessionRecoveryService(store).recover_runtime(session.session_id)
        cold = LocalSessionStore(store.root)
        cold.initialize()
        if not response_committed:
            for read in (
                lambda: cold.load_session_record(session.session_id),
                lambda: cold.load_runtime_session(session.session_id),
                lambda: cold.load_session(session.session_id),
                lambda: cold.load_runs(session.session_id),
                lambda: cold.load_pending_runs(session.session_id),
                lambda: cold.has_input_messages(session.session_id),
                lambda: cold.has_run_messages(session.session_id, run.run_id),
                lambda: cold.find_last_assistant_message(session.session_id, run.run_id),
                lambda: cold.load_run(session.session_id, run.run_id),
                lambda: cold.find_assistant_message(
                    session.session_id, run.run_id, "turn_interrupted"
                ),
            ):
                with pytest.raises(SessionRecoveryRequiredError):
                    read()
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_recovery AFTER UPDATE ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'recovery projection failed'); END"
            )
        with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="recovery projection"):
            SessionRecoveryService(store).recover_runtime(session.session_id)
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_recovery")

    if response_committed or isinstance(store, SQLiteSessionStore):
        after = cold.load_session(session.session_id)
        # Request interruption/usage settlement precedes the response transaction.
        # Its durable fact does not imply that partial output or Run recovery committed.
        assert len(after.request_headers) == 1
        header = after.request_headers[0]
        assert header.interrupted_at is not None
        assert header == before.request_headers[0].model_copy(
            update={"interrupted_at": header.interrupted_at}
        )
        assert after == before.model_copy(update={"request_headers": after.request_headers})

    recovery = SessionRecoveryService(cold)
    recovered = recovery.recover(session.session_id)
    assert recovered.session.status == "idle"
    assert recovered.session.active_run_id is None
    assert recovered.session.last_run_id == run.run_id
    assert recovered.session.message_count == 2
    assert recovered.runs[0].status == "recoverable"
    assert cold.load_pending_runs(session.session_id) == []
    response = recovered.messages[-1]
    assert response.sequence == 2
    assert cold.find_last_assistant_message(session.session_id, run.run_id) == response
    assert response.before_harness_version == session.harness_version
    assert recovered.session.last_message_id == response.message_id
    assert response.text_content == ("complete answer" if response_committed else "partial")
    assert response.reasoning_content == ("" if response_committed else "thought")
    if not response_committed:
        assert response.metadata["incomplete"] is True
        assert (
            recovered.runs[0].metadata["recovered_partial_assistant_message_id"]
            == response.message_id
        )
    revision = cold.observation_revision(session.session_id)
    assert recovery.recover(session.session_id) == recovered
    assert cold.observation_revision(session.session_id) == revision


def test_recovery_does_not_revive_withdrawn_output(store):
    session, run = interrupted_run(store)
    store.truncate_messages(
        session.session_id,
        before_message_id=session.last_message_id,
        expected_last_message_id=session.last_message_id,
    )
    before = store.load_session(session.session_id)
    assert before.session.history_epoch == 1
    assert SessionRecoveryService(store).recover(session.session_id) == before


def test_recovery_does_not_settle_a_live_stream(store):
    session, run = interrupted_run(store)
    before = store.load_session(session.session_id)
    lock = store.acquire_session_lock(session.session_id, run.run_id)
    assert lock is not None
    try:
        assert SessionRecoveryService(store).recover(session.session_id) == before
    finally:
        lock.release()


def test_recovery_does_not_read_completed_call_states(store, monkeypatch):
    session, run = interrupted_run(store)
    completed = RunRecord(session_id=session.session_id, status="completed", ended_at=utc_now())
    persist_tool_batch(
        store,
        completed,
        ToolCallBatchRecord(
            turn_id="turn_completed",
            assistant_message_id="msg_completed",
            phase="completed",
            calls=[
                ToolBatchCall(
                    request=ToolCallRequest(id="call_completed", name="Read"),
                    status="completed",
                    result=ToolResult(
                        tool_call_id="call_completed", tool_name="Read", content="old result"
                    ),
                )
            ],
        ),
    )

    def forbid_old_state(*args, **kwargs):
        pytest.fail("an interrupted stream cannot require terminal Run Call states")

    monkeypatch.setattr(store, "load_tool_call_state", forbid_old_state)
    recovered = SessionRecoveryService(store).recover_runtime(session.session_id)
    assert (
        next(item for item in recovered.runs if item.run_id == run.run_id).status == "recoverable"
    )
    assert completed.run_id not in {item.run_id for item in recovered.runs}
    assert store.load_run(session.session_id, completed.run_id).status == "completed"
