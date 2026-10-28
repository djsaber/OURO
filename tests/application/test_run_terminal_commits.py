from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs.lifecycle import (
    RunLifecycle,
    abort_run_state,
    request_run_abort_state,
    reserve_run,
)
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.results import RunResult
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.sqlite3" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


def active_run(store):
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
    return store.load_session_record(session.session_id), run


def lifecycle(store):
    return RunLifecycle(
        store=store,
        events=RuntimeEventRecorder(store=store, observers=ObserverRegistry()),
    )


def finish(store, session, run, operation, *, committed=False):
    common = dict(session=session, run=run, final_text="answer", had_tool_error=False)
    runs = lifecycle(store)
    if operation in {"complete", "fail"}:
        common.update(
            reasoning_content="reasoning",
            model_response=None,
            provider_continuation=None,
            turn_id="turn_finish",
        )
        if operation == "complete":
            return runs._complete(**common, assistant_already_committed=committed)
        return runs._fail(**common, error="provider interrupted")
    if operation == "pause":
        return runs._pause(**common, reason="context_blocked", next_model_round=2)
    if operation == "abort":
        return abort_run_state(
            store, session_id=session.session_id, run_id=run.run_id, reason="cancelled"
        )
    return request_run_abort_state(
        store, session_id=session.session_id, run_id=run.run_id, reason="cancelled"
    )


@pytest.mark.parametrize("operation", ["complete", "fail", "pause", "abort", "request_abort"])
def test_transition_uses_current_head_without_reading_history(store, monkeypatch, operation):
    session, run = active_run(store)
    store.append_message(
        MessageRecord.text(
            session_id=session.session_id, run_id=run.run_id, role="system", text="new boundary"
        )
    )
    current = store.load_session_record(session.session_id)

    def forbid_history(*args, **kwargs):
        raise AssertionError("a Run transition must not load historical message bodies")

    with monkeypatch.context() as patch:
        for name in (
            "load_session",
            "load_runtime_session",
            "load_run_messages",
            "load_messages_tail",
        ):
            patch.setattr(store, name, forbid_history)
        result = finish(store, session, run, operation)
    saved = store.load_session_record(session.session_id)
    assert result[0] == saved
    adds_message = operation in {"complete", "fail"}
    assert saved.message_count == current.message_count + adds_message
    if adds_message:
        message = store.load_messages_tail(session.session_id, limit=1)[0]
        assert message.text_content == "answer"
        assert message.reasoning_content == "reasoning"
        assert message.sequence == 3
        assert message.before_harness_version == current.harness_version
        assert saved.last_message_id == message.message_id
        assert bool(message.metadata.get("incomplete")) == (operation == "fail")
    else:
        assert saved.last_message_id == current.last_message_id
    assert saved.active_run_id == (run.run_id if operation == "request_abort" else None)


def test_completion_does_not_repeat_an_already_committed_response(store):
    session, run = active_run(store)
    response = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="assistant", text="answer"
    )
    store.append_message(response)
    finish(store, session, run, "complete", committed=True)
    saved = store.load_session_record(session.session_id)
    assert saved.message_count == 2
    assert saved.last_message_id == response.message_id


@pytest.mark.parametrize("operation", ["complete", "fail"])
@pytest.mark.parametrize("change", ["request_abort", "withdraw"])
def test_terminal_output_loses_to_an_earlier_cancel_or_withdrawal(store, operation, change):
    session, run = active_run(store)
    entered = Event()

    def completing():
        entered.set()
        return finish(store, session, run, operation)

    with ThreadPoolExecutor(max_workers=1) as executor:
        # The completing worker can start but cannot enter its mutation until
        # the owning thread commits the cancellation or history withdrawal.
        with store.session_mutation(session.session_id):
            future = executor.submit(completing)
            assert entered.wait(5), "completion worker did not start"
            if change == "request_abort":
                request_run_abort_state(
                    store, session_id=session.session_id, run_id=run.run_id, reason="cancelled"
                )
            else:
                store.truncate_messages(
                    session.session_id,
                    before_message_id=session.last_message_id,
                    expected_last_message_id=session.last_message_id,
                )
        if change == "withdraw":
            with pytest.raises(SessionCommitConflict, match="withdrawn"):
                future.result(timeout=5)
        else:
            result = future.result(timeout=5)
            assert isinstance(result, RunResult)
            assert result.run.abort_requested
    saved = store.load_session_record(session.session_id)
    assert saved.message_count == (1 if change == "request_abort" else 0)
    assert not any(
        message.role == "assistant" for message in store.load_messages_tail(session.session_id)
    )


@pytest.mark.parametrize("operation", ["complete", "fail", "pause", "abort", "request_abort"])
def test_native_failure_never_exposes_half_a_run_transition(store, monkeypatch, operation):
    session, run = active_run(store)
    if isinstance(store, LocalSessionStore):

        def fail_session(connection, session):
            raise OSError("terminal projection failure")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_session", fail_session)
            with pytest.raises(PersistenceError, match="terminal projection failure"):
                finish(store, session, run, operation)
        cold = LocalSessionStore(store.root)
        cold.initialize()
        if operation in {"complete", "fail"}:
            with pytest.raises(SessionRecoveryRequiredError):
                cold.load_session_record(session.session_id)
            with pytest.raises(SessionRecoveryRequiredError):
                cold.load_run(session.session_id, run.run_id)
            with cold.session_mutation(session.session_id):
                pass
        else:
            assert cold.load_run(session.session_id, run.run_id) == run
            assert cold.load_session_record(session.session_id) == session
            assert len(cold.load_messages_tail(session.session_id)) == 1
            assert cold.load_events_after(session.session_id) == ([], False)
            finish(cold, session, run, operation)
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_terminal AFTER UPDATE ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'terminal failed'); END"
            )
        with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="terminal failed"):
            finish(store, session, run, operation)
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        assert cold.load_run(session.session_id, run.run_id) == run
        assert cold.load_session_record(session.session_id) == session
        assert len(cold.load_messages_tail(session.session_id)) == 1
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_terminal")
        finish(cold, session, run, operation)
    saved = cold.load_session_record(session.session_id)
    saved_run = cold.load_run(session.session_id, run.run_id)
    expected_status = {
        "complete": "completed",
        "fail": "failed",
        "pause": "paused",
        "abort": "aborted",
        "request_abort": "running",
    }[operation]
    assert saved_run.status == expected_status
    events, more = cold.load_events_after(session.session_id)
    assert not more
    assert [event.event_type for event in events] == [
        "run_abort_requested" if operation == "request_abort" else f"run_{expected_status}"
    ]
    assert saved.status == (
        "failed" if operation == "fail" else "running" if operation == "request_abort" else "idle"
    )
    assert saved.message_count == (2 if operation in {"complete", "fail"} else 1)
    # Retrying an already settled operation cannot publish its output again.
    finish(cold, saved, saved_run, operation)
    assert cold.load_session_record(session.session_id).message_count == saved.message_count


def test_reservation_commits_configuration_and_run_together(store, monkeypatch):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id)

    def reserve(target):
        return reserve_run(
            target,
            run,
            configure_session=lambda value: value.model_copy(update={"title": "configured"}),
        )

    if isinstance(store, LocalSessionStore):

        def fail_session(connection, session):
            raise OSError("reservation projection failure")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_session", fail_session)
            with pytest.raises(PersistenceError, match="reservation projection failure"):
                reserve(store)
        cold = LocalSessionStore(store.root)
        cold.initialize()
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_reservation AFTER UPDATE ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'reservation failed'); END"
            )
        with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="reservation failed"):
            reserve(store)
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_reservation")
    assert cold.load_session_record(session.session_id) == session
    with pytest.raises(FileNotFoundError):
        cold.load_run(session.session_id, run.run_id)
    reserve(cold)
    saved = cold.load_session_record(session.session_id)
    assert saved.title == "configured"
    assert saved.active_run_id == run.run_id
    assert cold.load_run(session.session_id, run.run_id).status == "queued"
