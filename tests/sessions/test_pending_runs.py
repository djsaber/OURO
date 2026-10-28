from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunContinuationCheckpoint, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore, sqlite_runs


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


@pytest.mark.parametrize("metadata_size", [8, 100_000])
def test_budget_counts_do_not_read_run_bodies(store, monkeypatch, metadata_size):
    session = SessionRecord()
    store.create_session(session)
    for index in range(2):
        store.save_run(
            RunRecord(
                session_id=session.session_id,
                run_id=f"run_{index}",
                model_round_count=index + 1,
                summary_request_count=index,
                metadata={"large": "x" * metadata_size},
            )
        )
    with store.session_read(session.session_id) as reader:
        connection = reader.connection() if isinstance(store, LocalSessionStore) else reader
        connection.set_authorizer(
            lambda action, table, column, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "runs" and column == "payload"
                else sqlite3.SQLITE_OK
            )
        )
        try:
            counts = store.load_run_request_counts(session.session_id)
        finally:
            connection.set_authorizer(None)
    assert [
        (item.run_id, item.model_round_count, item.summary_request_count) for item in counts
    ] == [
        ("run_0", 1, 0),
        ("run_1", 2, 1),
    ]


@pytest.mark.parametrize("commit_kind", ["run", "session"])
def test_budget_counts_share_native_run_commit_and_recovery(store, monkeypatch, commit_kind):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, model_round_count=1)
    store.save_run(run)
    updated = run.model_copy(update={"model_round_count": 2, "summary_request_count": 1})

    def commit():
        if commit_kind == "run":
            store.save_run(updated)
        else:
            store.commit_session(SessionCommit(before=session, runs=(updated,)))

    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_run_counts AFTER UPDATE ON runs "
            "BEGIN SELECT RAISE(ABORT, 'Run counts failed'); END"
        )
    with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="Run counts failed"):
        commit()
    cold = (
        LocalSessionStore(store.root)
        if isinstance(store, LocalSessionStore)
        else SQLiteSessionStore(store.database)
    )
    cold.initialize()
    assert cold.load_run(session.session_id, run.run_id) == run
    assert cold.load_run_request_counts(session.session_id)[0].model_round_count == 1
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER fail_run_counts")
    commit()
    for _ in range(2):
        with cold.session_mutation(session.session_id):
            pass
        assert cold.load_run(session.session_id, run.run_id) == updated
        counts = cold.load_run_request_counts(session.session_id)
        assert len(counts) == 1
        assert (counts[0].model_round_count, counts[0].summary_request_count) == (2, 1)
    cold.save_run(updated.model_copy(update={"metadata": {"diagnostic": "changed"}}))
    assert cold.load_run_request_counts(session.session_id) == counts


@pytest.mark.parametrize("old_runs", [4, 256])
def test_execution_reads_only_heads_and_pending_runs(store, monkeypatch, old_runs):
    session = SessionRecord()
    active = RunRecord(session_id=session.session_id, status="running")
    terminal = RunRecord(session_id=session.session_id, status="completed")
    pending = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(),
    )
    session.active_run_id = active.run_id
    session.last_run_id = terminal.run_id
    store.create_session(session)
    unrelated = [
        RunRecord(session_id=session.session_id, status="completed") for _ in range(old_runs)
    ]
    for run in [*unrelated, active, terminal, pending]:
        store.save_run(run)
    allowed = {run.run_id for run in (active, terminal, pending)}
    reads = []
    original = sqlite_runs.decode_versioned_record_json

    def validate(model, payload, *, label):
        result = original(model, payload, label=label)
        if model is RunRecord:
            assert result.run_id in allowed
            reads.append(result.run_id)
        return result

    monkeypatch.setattr(sqlite_runs, "decode_versioned_record_json", validate)

    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1
        return 0

    with store.session_read(session.session_id) as reader:
        connection = reader.connection() if isinstance(store, LocalSessionStore) else reader
        connection.set_progress_handler(progress, 1)
        try:
            snapshot = store.load_runtime_session(session.session_id)
            assert {run.run_id for run in snapshot.runs} == allowed
            assert len(reads) == 3
            assert {run.run_id for run in store.load_pending_runs(session.session_id)} == {
                active.run_id,
                pending.run_id,
            }
        finally:
            connection.set_progress_handler(None, 0)
    assert instructions < 2000


def test_pending_membership_follows_tool_settlement_continuation_and_epoch(store):
    session = SessionRecord()
    store.create_session(session)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="task")
    store.append_message(message)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    assert store.load_pending_runs(session.session_id) == [run]
    batch = ToolCallBatchRecord(
        turn_id="turn_failed", assistant_message_id="msg_failed", phase="recoverable"
    ).head()
    run = run.model_copy(update={"status": "failed", "active_tool_batch": batch})
    store.save_run(run)
    assert store.load_pending_runs(session.session_id) == [run]
    run = run.model_copy(
        update={"active_tool_batch": batch.model_copy(update={"phase": "completed"})}
    )
    store.save_run(run)
    assert store.load_pending_runs(session.session_id) == []
    checkpoint = RunContinuationCheckpoint()
    run = run.model_copy(update={"continuation": checkpoint})
    store.save_run(run)
    assert store.load_pending_runs(session.session_id) == [run]
    run = run.model_copy(
        update={"continuation": checkpoint.model_copy(update={"state": "resolved"})}
    )
    store.save_run(run)
    assert store.load_pending_runs(session.session_id) == []
    run = run.model_copy(update={"status": "running"})
    store.save_run(run)
    store.truncate_messages(
        session.session_id,
        before_message_id=message.message_id,
        expected_last_message_id=message.message_id,
    )
    assert store.load_pending_runs(session.session_id) == []
    assert store.load_runtime_session(session.session_id).runs == []
    assert store.load_run(session.session_id, run.run_id) == run
    # Updating old audit metadata cannot recreate current pending membership.
    store.save_run(run)
    assert store.load_pending_runs(session.session_id) == []
    if isinstance(store, LocalSessionStore):
        directory = store.sessions_dir / session.session_id
        assert not any(
            (directory / name).exists()
            for name in ("session.json", "runs", "run-counts", "pending-runs")
        )


@pytest.mark.parametrize("failure_table", ["runs", "sessions"])
def test_run_save_commits_pending_membership_and_session_count(store, failure_table):
    session = SessionRecord()
    store.create_session(session)
    before = store.load_session_record(session.session_id)
    run = RunRecord(session_id=session.session_id, status="running")
    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER fail_run AFTER {'INSERT' if failure_table == 'runs' else 'UPDATE'} "
            f"ON {failure_table} "
            "BEGIN SELECT RAISE(ABORT, 'Run transaction failed'); END"
        )
    with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="Run transaction failed"):
        store.save_run(run)
    cold = (
        LocalSessionStore(store.root)
        if isinstance(store, LocalSessionStore)
        else SQLiteSessionStore(store.database)
    )
    cold.initialize()
    assert cold.load_pending_runs(session.session_id) == []
    with pytest.raises(FileNotFoundError):
        cold.load_run(session.session_id, run.run_id)
    assert cold.load_session_record(session.session_id) == before
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER fail_run")
    cold.save_run(run)

    assert cold.load_run(session.session_id, run.run_id) == run
    assert cold.load_pending_runs(session.session_id) == [run]
    assert cold.load_session_record(session.session_id) == before.model_copy(
        update={"run_count": before.run_count + 1}
    )


def test_withdrawal_settles_pending_continuation_in_native_commit(store, monkeypatch):
    session = SessionRecord()
    store.create_session(session)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="withdraw")
    store.append_message(message)
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(),
    )
    store.save_run(run)
    before = store.load_session_record(session.session_id)

    def withdraw(repository):
        return repository.truncate_messages(
            session.session_id,
            before_message_id=message.message_id,
            expected_last_message_id=message.message_id,
        )

    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_withdrawal AFTER UPDATE ON runs "
            "BEGIN SELECT RAISE(ABORT, 'withdrawal Run transaction failed'); END"
        )
    with pytest.raises(
        (PersistenceError, sqlite3.IntegrityError), match="withdrawal Run transaction failed"
    ):
        withdraw(store)
    cold = (
        LocalSessionStore(store.root)
        if isinstance(store, LocalSessionStore)
        else SQLiteSessionStore(store.database)
    )
    cold.initialize()
    if isinstance(cold, LocalSessionStore):
        with pytest.raises(SessionRecoveryRequiredError):
            cold.load_runtime_session(session.session_id)
    else:
        assert cold.load_session_record(session.session_id) == before
        assert cold.load_pending_runs(session.session_id) == [run]
        assert cold.load_message(session.session_id, message.message_id) is not None
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER fail_withdrawal")
    if isinstance(cold, LocalSessionStore):
        with cold.session_mutation(session.session_id):
            pass
    else:
        withdraw(cold)

    settled = cold.load_run(session.session_id, run.run_id)
    assert settled.status == "completed"
    assert settled.continuation.state == "resolved"
    assert settled.continuation.action == "pause"
    assert settled.continuation.reason == "session_history_withdrawn"
    assert settled.continuation.resolved_at is not None
    assert cold.load_session_record(session.session_id).history_epoch == 1
    assert cold.load_pending_runs(session.session_id) == []
    assert cold.load_runtime_session(session.session_id).runs == []
    with cold.session_mutation(session.session_id):
        pass
    assert cold.load_run(session.session_id, run.run_id) == settled
