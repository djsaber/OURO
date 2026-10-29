from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import ouro.local_io as local_io
from ouro.core.errors import (
    PersistenceError,
    SessionRecoveryRequiredError,
    UnsupportedDataVersionError,
)
from ouro.core.messages import MessageRecord, TextContent, ToolMessageReference
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallState
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolCallRecord,
    ToolCallStart,
    ToolFactConflict,
    ToolOutcomeRecord,
)
from ouro.core.tools import (
    ToolCallProducer,
    ToolCallRequest,
    ToolInvocation,
    ToolResultFeedback,
    ToolResultValue,
)
from ouro.sessions import LocalSessionStore, SQLiteSessionStore, import_sessions_to_sqlite
from ouro.sessions.portable import PortableSession, SessionBundle, populate_sessions
from ouro.sessions.sqlite_facts import read_fact
from tests.session_storage import session_connection


def file_fact_payload(directory, record, replacement=None):
    table = {
        ToolCallRecord: "tool_calls",
        ToolCallStart: "tool_call_starts",
        ToolOutcomeRecord: "tool_outcomes",
    }[type(record)]
    identity = record.identity
    key = (record.session_id, identity.run_id, identity.batch_id, identity.tool_call_id)
    where = "session_id = ? AND run_id = ? AND batch_id = ? AND tool_call_id = ?"
    if isinstance(record, ToolCallStart | ToolOutcomeRecord):
        where += " AND attempt = ?"
        key += (record.attempt,)
    with closing(sqlite3.connect(directory / "session-data.sqlite3")) as connection, connection:
        if replacement is not None:
            connection.execute(f"UPDATE {table} SET payload = ? WHERE {where}", (replacement, *key))
            return replacement
        return connection.execute(f"SELECT payload FROM {table} WHERE {where}", key).fetchone()[0]


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path)
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


def facts(store, *, call_id="provider/调用", batch_id="batch_one"):
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session.active_run_id = run.run_id
    session.status = "running"
    store.create_session(session)
    call = ToolCallRecord(
        session_id=session.session_id,
        identity=ToolCallIdentity(run_id=run.run_id, batch_id=batch_id, tool_call_id=call_id),
        history_epoch=session.history_epoch,
        turn_id="turn_one",
        producer=ToolCallProducer(),
        request=ToolCallRequest(id=call_id, name="write", arguments={"text": "original"}),
    )
    invocation = ToolInvocation(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=call.turn_id,
        tool_call_id=call_id,
        tool_name="write",
        arguments=call.request.arguments,
    )
    start = ToolCallStart.from_invocation(call, invocation, attempt=1)
    outcome = ToolOutcomeRecord(
        session_id=session.session_id,
        identity=call.identity,
        attempt=1,
        value=ToolResultValue(
            tool_call_id=call_id,
            tool_name="write",
            content="Observed result body",
            structured={"distinct": "Structured result value"},
        ),
    )
    store.commit_session(SessionCommit(before=session, runs=(run,)))
    return session, run, call, start, outcome


def commit(store, session, *, call_states=(), **changes):
    return store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            tool_facts=ToolCallFacts(**changes),
            call_states=call_states,
        )
    )


@pytest.mark.parametrize("role", ["assistant", "tool_result"])
def test_reference_commit_does_not_project_owned_tool_content(store, monkeypatch, role):
    session, run, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    store.commit_tool_outcome(outcome)
    message = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=call.turn_id,
        role=role,
        call_refs=(call.identity,) if role == "assistant" else (),
        outcome_ref=(
            ToolMessageReference(identity=call.identity, outcome_attempt=1)
            if role == "tool_result"
            else None
        ),
        tool_feedback=(
            ToolResultFeedback(error_type="ControlFailed", error_message="State not applied")
            if role == "tool_result"
            else None
        ),
    )

    def unexpected_projection(*args, **kwargs):
        pytest.fail("committing fixed references must not expand their owned content")

    with monkeypatch.context() as patch:
        patch.setattr(ToolCallRequest, "model_dump", unexpected_projection)
        patch.setattr(TextContent, "model_dump", unexpected_projection)
        patch.setattr(ToolResultFeedback, "materialize", unexpected_projection)
        store.append_message(message)

    loaded = store.load_message(session.session_id, message.message_id)
    if role == "assistant":
        assert loaded.tool_calls == [call.request]
    else:
        assert loaded.text_content == "Observed result body\nState not applied"
        assert loaded.tool_result.is_error
    assert store.load_tool_outcome(session.session_id, call.identity, 1) == outcome


@pytest.mark.parametrize(
    "arguments",
    [{}, {"text": "原始参数", "nested": {"empty": [], "none": None, "flag": False, "zero": 0}}],
)
def test_call_arguments_have_one_native_owner_and_round_trip(store, arguments):
    session, _, call, start, _ = facts(store)
    call.request.arguments = arguments
    commit(store, session, calls=(call,), starts=(start,))
    with session_connection(store, session.session_id) as connection:
        payload, projection = connection.execute(
            "SELECT payload, projection FROM tool_calls"
        ).fetchone()
    assert "arguments" not in json.loads(projection)["request"]
    assert json.loads(payload)["request"]["arguments"] == arguments
    assert store.load_tool_call(session.session_id, call.identity) == call
    assert store.load_session(session.session_id).tool_facts.calls == (call,)
    assert (
        store.load_tool_call_start(session.session_id, call.identity, 1).materialize(call).arguments
        == arguments
    )


def test_parameter_comparisons_still_read_the_accepted_arguments(store):
    session, _, call, start, _ = facts(store)
    commit(store, session, calls=(call,))
    duplicate = call.model_copy(
        update={"request": call.request.model_copy(update={"arguments": {}})}
    )
    with pytest.raises(ToolFactConflict, match="different accepted content"):
        commit(store, session, calls=(duplicate,))
    state = ToolCallState(
        session_id=session.session_id,
        identity=call.identity,
        prepared_binding=start.binding,
        arguments_override=call.request.arguments,
    )
    with pytest.raises(ToolFactConflict, match="unchanged prepared arguments"):
        commit(store, session, call_states=(state,))
    with pytest.raises(ToolFactConflict, match="unchanged execution arguments"):
        commit(
            store,
            session,
            starts=(start.model_copy(update={"arguments_override": call.request.arguments}),),
        )
    changed = state.model_copy(update={"arguments_override": {}})
    commit(store, session, call_states=(changed,))
    assert store.load_tool_call_state(session.session_id, call.identity) == changed
    assert store.load_tool_call(session.session_id, call.identity) == call


def test_call_start_and_outcome_are_distinct_immutable_facts(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,))
    assert store.load_tool_call(session.session_id, call.identity) == call
    assert store.load_tool_call_start(session.session_id, call.identity, 1) is None
    with pytest.raises(ToolFactConflict, match="no durable Call start"):
        commit(store, session, outcomes=(outcome,))
    commit(store, session, starts=(start,))
    assert store.load_tool_call_start(session.session_id, call.identity, 1) == start
    assert start.arguments_override is None
    store.commit_tool_outcome(outcome)
    assert store.load_tool_outcome(session.session_id, call.identity, 1) == outcome
    commit(store, session, calls=(call,), starts=(start,), outcomes=(outcome,))
    with pytest.raises(ToolFactConflict, match="cannot be replaced"):
        commit(
            store,
            session,
            outcomes=(
                outcome.model_copy(
                    update={"value": outcome.value.model_copy(update={"content": "rewritten"})}
                ),
            ),
        )
    assert store.load_tool_outcome(session.session_id, call.identity, 1) == outcome
    assert store.load_messages_tail(session.session_id) == []


def test_original_and_modified_arguments_keep_distinct_ownership(store):
    session, _, call, start, _ = facts(store)
    changed = start.model_copy(update={"arguments_override": {"text": ["approved"]}})
    commit(store, session, calls=(call,), starts=(changed,))
    loaded = store.load_tool_call_start(session.session_id, call.identity, 1)
    invocation = loaded.materialize(call)
    invocation.arguments["text"].append("mutated")
    assert loaded.arguments_override == {"text": ["approved"]}
    assert store.load_tool_call(session.session_id, call.identity).request.arguments == {
        "text": "original"
    }
    with pytest.raises(ToolFactConflict, match="different execution parameters"):
        commit(store, session, starts=(start,))


@pytest.mark.parametrize("missing", ["call", "start", "outcome"])
def test_call_state_cannot_reference_missing_execution_facts(store, missing):
    session, _, call, start, outcome = facts(store)
    state = ToolCallState(
        session_id=session.session_id,
        identity=call.identity,
        status="completed",
        attempt=1,
        outcome_attempt=1,
    )
    if missing != "call":
        commit(store, session, calls=(call,))
    if missing == "outcome":
        commit(store, session, starts=(start,))
    with pytest.raises(ToolFactConflict):
        commit(store, session, call_states=(state,))
    assert store.load_tool_call_state(session.session_id, call.identity) is None
    commit(
        store, session, calls=(call,), starts=(start,), outcomes=(outcome,), call_states=(state,)
    )
    assert store.load_tool_call_state(session.session_id, call.identity) == state


def test_late_outcome_does_not_rewrite_published_unknown_call_state(store):
    session, run, call, start, outcome = facts(store)
    feedback = MessageRecord.text(
        session_id=session.session_id, role="tool_result", text="Execution outcome unknown"
    )
    state = ToolCallState(
        session_id=session.session_id,
        identity=call.identity,
        status="unknown_outcome",
        attempt=1,
        result_message_id=feedback.message_id,
    )
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            steps=(SessionCommitStep(message=feedback),),
            tool_facts=ToolCallFacts(calls=(call,), starts=(start,)),
            call_states=(state,),
        )
    )
    store.save_run(run.model_copy(update={"status": "aborted"}))
    store.commit_tool_outcome(outcome)
    assert store.load_tool_call_state(session.session_id, call.identity) == state
    assert store.load_message(session.session_id, feedback.message_id).text_content == (
        "Execution outcome unknown"
    )
    assert store.load_tool_outcome(session.session_id, call.identity, 1) == outcome


def test_outcome_after_withdrawal_does_not_restore_run_or_publish_history(store):
    session, run, call, start, outcome = facts(store)
    message = MessageRecord.text(session_id=session.session_id, role="assistant", text="in flight")
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            steps=(SessionCommitStep(message=message),),
        )
    )
    commit(store, session, calls=(call,), starts=(start,))
    before = store.load_session_record(session.session_id)
    run.status = "aborted"
    # The storage boundary accepts a late observation without a current Run proposal.
    store.save_run(run)
    store.commit_session(SessionCommit(before=before, before_message_id=message.message_id))
    store.commit_tool_outcome(outcome)
    assert store.load_tool_outcome(session.session_id, call.identity, 1) == outcome
    assert store.load_run(session.session_id, run.run_id).status == "aborted"
    current = store.load_session_record(session.session_id)
    assert current.history_epoch == 1 and current.active_run_id is None
    assert store.load_messages_tail(session.session_id) == []
    with pytest.raises(ToolFactConflict, match="current active Run"):
        commit(store, session, starts=(start.model_copy(update={"attempt": 2}),))


def test_provider_call_id_is_scoped_by_batch_and_run(store):
    session, run, call, start, outcome = facts(store)
    second_identity = replace(call.identity, batch_id="other/批次")
    second = call.model_copy(update={"identity": second_identity, "turn_id": "turn_two"})
    commit(store, session, calls=(call, second), starts=(start,), outcomes=(outcome,))
    assert store.load_tool_call(session.session_id, second_identity) == second
    assert store.load_tool_outcome(session.session_id, second_identity, 1) is None
    absent = replace(call.identity, run_id="another_run")
    assert store.load_tool_call(session.session_id, absent) is None


def test_outcome_retry_is_idempotent_without_rewriting_facts_or_session(store, monkeypatch):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    before = store.load_session_record(session.session_id)
    store.commit_tool_outcome(outcome)

    def unexpected_write(*args, **kwargs):
        pytest.fail("a confirmed Outcome retry must not write another decision or fact")

    with monkeypatch.context() as patch:
        if isinstance(store, LocalSessionStore):
            patch.setattr(local_io, "replace_path_with_retry", unexpected_write)
        else:
            import ouro.sessions.backends.sqlite.commits as sqlite_commits

            patch.setattr(sqlite_commits, "write_fact", unexpected_write)
        assert store.commit_tool_outcome(outcome) == outcome
    assert store.load_session_record(session.session_id) == before
    conflicting = outcome.model_copy(
        update={"value": outcome.value.model_copy(update={"content": "another result"})}
    )
    with pytest.raises(ToolFactConflict, match="cannot be replaced"):
        store.commit_tool_outcome(conflicting)
    assert store.load_tool_outcome(session.session_id, call.identity, 1) == outcome


def test_late_outcome_does_not_recreate_a_removed_session(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    store.trash_session_tree(session.session_id, (session.session_id,))
    with pytest.raises(FileNotFoundError):
        store.commit_tool_outcome(outcome)
    with pytest.raises(FileNotFoundError):
        store.load_session_record(session.session_id)


def test_preflight_rejection_cannot_replace_an_entered_tool(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    rejected = outcome.model_copy(
        update={"attempt": 0, "value": outcome.value.model_copy(update={"is_error": True})}
    )
    with pytest.raises(ToolFactConflict, match="started Call"):
        store.commit_tool_outcome(rejected)
    assert store.load_tool_outcome(session.session_id, call.identity, 0) is None


@pytest.mark.parametrize("record_type", [ToolCallRecord, ToolCallStart, ToolOutcomeRecord])
@pytest.mark.parametrize("fault", ["version", "identity"])
def test_point_and_snapshot_queries_reject_invalid_call_facts(store, record_type, fault):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,), outcomes=(outcome,))
    record = next(item for item in (call, start, outcome) if isinstance(item, record_type))
    payload = record.model_dump(mode="json")
    if fault == "version":
        payload.pop("schema_version")
    else:
        payload["identity"]["batch_id"] = "foreign_batch"
    if isinstance(store, LocalSessionStore):
        file_fact_payload(store.sessions_dir / session.session_id, record, json.dumps(payload))
    else:
        table = {
            ToolCallRecord: "tool_calls",
            ToolCallStart: "tool_call_starts",
            ToolOutcomeRecord: "tool_outcomes",
        }[record_type]
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(f"UPDATE {table} SET payload = ?", (json.dumps(payload),))
    query = {
        ToolCallRecord: lambda: store.load_tool_call(session.session_id, call.identity),
        ToolCallStart: lambda: store.load_tool_call_start(session.session_id, call.identity, 1),
        ToolOutcomeRecord: lambda: store.load_tool_outcome(session.session_id, call.identity, 1),
    }[record_type]
    expected = UnsupportedDataVersionError if fault == "version" else PersistenceError
    with pytest.raises(expected):
        query()
    with pytest.raises(expected):
        store.load_session(session.session_id)


@pytest.mark.parametrize("boundary", ["outcome", "data_committed"])
def test_files_outcome_transaction_preserves_session_and_can_be_retried(
    tmp_path, monkeypatch, boundary
):
    import ouro.sessions.backends.files.commits as file_commits
    import ouro.sessions.sqlite_facts as native_facts

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    before = store.load_session_record(session.session_id)
    write_fact = native_facts.write_fact
    connect_data = file_commits.connect_data
    reached = []

    def interrupted_fact(connection, record):
        write_fact(connection, record)
        if boundary == "outcome":
            reached.append(boundary)
            raise OSError("injected Outcome transaction failure")

    @contextmanager
    def interrupted_data(directory, *, mode="ro", **kwargs):
        with connect_data(directory, mode=mode, **kwargs) as connection:
            yield connection
        if mode == "rw" and boundary == "data_committed":
            reached.append(boundary)
            raise OSError("injected Outcome acknowledgement failure")

    with monkeypatch.context() as patch:
        patch.setattr(native_facts, "write_fact", interrupted_fact)
        patch.setattr(file_commits, "connect_data", interrupted_data)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_tool_outcome(outcome)
    assert reached == [boundary]
    reader = LocalSessionStore(tmp_path)
    assert reader.load_tool_outcome(session.session_id, call.identity, 1) == (
        outcome if boundary == "data_committed" else None
    )
    assert reader.load_session_record(session.session_id) == before
    assert reader.commit_tool_outcome(outcome) == outcome
    assert reader.load_tool_outcome(session.session_id, call.identity, 1) == outcome
    assert reader.load_session_record(session.session_id) == before
    assert not (store.sessions_dir / session.session_id / "commit.json").exists()


def test_sqlite_outcome_failure_leaves_started_call_and_session_unchanged(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    before = store.load_session_record(session.session_id)
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_outcome AFTER INSERT ON tool_outcomes "
            "BEGIN SELECT RAISE(ABORT, 'injected Outcome interruption'); END"
        )
    with pytest.raises(PersistenceError, match="injected Outcome interruption"):
        store.commit_tool_outcome(outcome)
    assert store.load_tool_call(session.session_id, call.identity) == call
    assert store.load_tool_call_start(session.session_id, call.identity, 1) == start
    assert store.load_tool_outcome(session.session_id, call.identity, 1) is None
    assert store.load_session_record(session.session_id) == before


@pytest.mark.parametrize(
    "boundary", ["call", "start", "outcome", "state", "data_committed", "head", "cleanup"]
)
def test_files_cross_file_decision_recovers_call_facts_without_query_writes(
    tmp_path, monkeypatch, boundary
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session, _, call, start, outcome = facts(store)
    state = ToolCallState(
        session_id=session.session_id,
        identity=call.identity,
        status="completed",
        attempt=1,
        outcome_attempt=1,
    )
    directory = store.sessions_dir / session.session_id
    unlink = Path.unlink
    reached = []

    import ouro.sessions.backends.files.commits as file_commits
    import ouro.sessions.sqlite_facts as native_facts

    write_fact = native_facts.write_fact
    write_state = file_commits.write_call_state
    connect_data = file_commits.connect_data

    def interrupted_fact(connection, record, **kwargs):
        write_fact(connection, record, **kwargs)
        kind = {ToolCallRecord: "call", ToolCallStart: "start", ToolOutcomeRecord: "outcome"}[
            type(record)
        ]
        if boundary == kind:
            reached.append(boundary)
            raise OSError("injected Call transaction interruption")

    def interrupted_state(connection, record, **kwargs):
        write_state(connection, record, **kwargs)
        if boundary == "state":
            reached.append(boundary)
            raise OSError("injected Call state interruption")

    @contextmanager
    def interrupted_data(directory, *, mode="ro", **kwargs):
        with connect_data(directory, mode=mode, **kwargs) as connection:
            yield connection
        if mode == "rw" and boundary == "data_committed":
            reached.append(boundary)
            raise OSError("injected Call data publication interruption")

    write_head = file_commits.write_session

    def interrupted_head(connection, session):
        write_head(connection, session)
        if boundary == "head":
            reached.append(boundary)
            raise OSError("injected Call publication interruption")

    def interrupted_unlink(path, *args, **kwargs):
        if boundary == "cleanup" and path == directory / "commit.json":
            reached.append(boundary)
            raise OSError("injected Call cleanup interruption")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", interrupted_head)
        patch.setattr(Path, "unlink", interrupted_unlink)
        patch.setattr(native_facts, "write_fact", interrupted_fact)
        patch.setattr(file_commits, "write_call_state", interrupted_state)
        patch.setattr(file_commits, "connect_data", interrupted_data)
        with pytest.raises(PersistenceError):
            store.commit_session(
                SessionCommit(
                    before=store.load_session_record(session.session_id),
                    steps=(
                        SessionCommitStep(
                            message=MessageRecord.text(
                                session_id=session.session_id,
                                role="user",
                                text="cross-file boundary",
                            )
                        ),
                    ),
                    tool_facts=ToolCallFacts(calls=(call,), starts=(start,), outcomes=(outcome,)),
                    call_states=(state,),
                )
            )
    assert reached == [boundary]
    decision = (directory / "commit.json").read_bytes()
    reader = LocalSessionStore(tmp_path)
    for query in (
        lambda: reader.load_tool_call(session.session_id, call.identity),
        lambda: reader.load_tool_call_start(session.session_id, call.identity, 1),
        lambda: reader.load_tool_outcome(session.session_id, call.identity, 1),
        lambda: reader.load_tool_call_state(session.session_id, call.identity),
    ):
        with pytest.raises(SessionRecoveryRequiredError):
            query()
        assert (directory / "commit.json").read_bytes() == decision
    with reader.session_mutation(session.session_id):
        pass
    assert reader.load_tool_call(session.session_id, call.identity) == call
    assert reader.load_tool_call_start(session.session_id, call.identity, 1) == start
    assert reader.load_tool_outcome(session.session_id, call.identity, 1) == outcome
    assert reader.load_tool_call_state(session.session_id, call.identity) == state
    assert not (directory / "commit.json").exists()


@pytest.mark.parametrize(
    "table", ["tool_calls", "tool_call_starts", "tool_outcomes", "tool_call_states", "sessions"]
)
def test_sqlite_native_fault_rolls_back_all_call_facts(tmp_path, table):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session, _, call, start, outcome = facts(store)
    state = ToolCallState(
        session_id=session.session_id,
        identity=call.identity,
        status="completed",
        attempt=1,
        outcome_attempt=1,
    )
    before = store.load_session_record(session.session_id)
    operation = "UPDATE" if table == "sessions" else "INSERT"
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER fail_call AFTER {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected Call interruption'); END"
        )
    with pytest.raises(PersistenceError):
        commit(
            store,
            session,
            calls=(call,),
            starts=(start,),
            outcomes=(outcome,),
            call_states=(state,),
        )
    assert store.load_session_record(session.session_id) == before
    assert store.load_tool_call(session.session_id, call.identity) is None
    assert store.load_tool_call_start(session.session_id, call.identity, 1) is None
    assert store.load_tool_outcome(session.session_id, call.identity, 1) is None
    assert store.load_tool_call_state(session.session_id, call.identity) is None


@pytest.mark.parametrize("target_kind", ["files_archive", "sqlite_archive", "sqlite_native"])
def test_transfer_preserves_call_closure_and_observed_content(store, tmp_path, target_kind):
    session, run, call, start, outcome = facts(store)
    state = ToolCallState(
        session_id=session.session_id,
        identity=call.identity,
        status="completed",
        attempt=1,
        outcome_attempt=1,
    )
    commit(
        store, session, calls=(call,), starts=(start,), outcomes=(outcome,), call_states=(state,)
    )
    before = store.load_session_record(session.session_id)
    store.commit_session(
        SessionCommit(
            before=before,
            after=before.model_copy(update={"active_run_id": None, "status": "idle"}),
            runs=(run.model_copy(update={"status": "completed"}),),
        )
    )
    snapshot = store.load_session(session.session_id)
    assert snapshot.call_states == (state,)
    assert snapshot.tool_facts == ToolCallFacts(calls=(call,), starts=(start,), outcomes=(outcome,))
    target = (
        LocalSessionStore(tmp_path / "target")
        if target_kind == "files_archive"
        else SQLiteSessionStore(tmp_path / "target.db")
    )
    target.initialize()
    if target_kind == "sqlite_native":
        import_sessions_to_sqlite(store, target)
    else:
        bundle = SessionBundle(sessions=[PortableSession.from_snapshot(snapshot)])
        encoded = bundle.model_dump_json()
        assert encoded.count("Observed result body") == 1
        assert encoded.count("Structured result value") == 1
        populate_sessions(target, SessionBundle.model_validate_json(encoded))
    assert target.load_session(session.session_id) == snapshot
    assert (
        target.load_tool_call_start(session.session_id, call.identity, 1)
        .materialize(call)
        .arguments
        == call.request.arguments
    )


def test_trash_keeps_the_original_call_and_outcome(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,), outcomes=(outcome,))
    trash_id = store.trash_session_tree(session.session_id, (session.session_id,))
    if isinstance(store, LocalSessionStore):
        directory = store.trash_sessions_dir / trash_id / "sessions" / session.session_id
        with closing(sqlite3.connect(directory / "session-data.sqlite3")) as connection:
            assert (
                read_fact(
                    connection, session.session_id, call.identity, ToolOutcomeRecord, attempt=1
                )
                == outcome
            )
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT payload FROM trashed_sessions WHERE trash_id = ?", (trash_id,)
                ).fetchone()[0]
            )
        records = payload["records"]["tool_outcomes"]
        row = records["rows"][0]
        value = json.loads(row[records["columns"].index("payload")])
        value["value"]["structured"] = json.loads(row[records["columns"].index("structured")])
        assert ToolOutcomeRecord.model_validate(value) == outcome
    with pytest.raises(FileNotFoundError):
        store.load_tool_outcome(session.session_id, call.identity, 1)


@pytest.mark.parametrize(("previous_count", "body_size"), [(4, 1024), (128, 1024), (4, 131072)])
def test_a_new_outcome_reads_and_writes_only_its_call_facts(
    store, monkeypatch, previous_count, body_size
):
    import ouro.sessions.sqlite_facts as sqlite_facts

    session, _, original, original_start, original_outcome = facts(store)
    calls, starts, outcomes = [], [], []
    for index in range(previous_count + 1):
        identity = replace(original.identity, tool_call_id=f"call_{index}")
        call = original.model_copy(
            update={
                "identity": identity,
                "request": original.request.model_copy(
                    update={"id": identity.tool_call_id, "source_index": index}
                ),
            }
        )
        start = ToolCallStart.from_invocation(
            call,
            original_start.materialize(original).model_copy(
                update={"tool_call_id": identity.tool_call_id}
            ),
            attempt=1,
        )
        outcome = original_outcome.model_copy(
            update={
                "identity": identity,
                "value": original_outcome.value.model_copy(
                    update={
                        "tool_call_id": identity.tool_call_id,
                        "content": f"body {index}:" + "x" * body_size,
                    }
                ),
            }
        )
        calls.append(call)
        starts.append(start)
        outcomes.append(outcome)
    commit(store, session, calls=tuple(calls), starts=tuple(starts), outcomes=tuple(outcomes[:-1]))
    selected = calls[-1].identity
    before = store.load_session_record(session.session_id)
    reads = []
    read = sqlite_facts.read_fact

    def selected_read(source, session_id, identity, record_type, **kwargs):
        assert identity == selected
        if record_type is ToolCallRecord:
            assert not kwargs["include_arguments"]
        reads.append(record_type)
        return read(source, session_id, identity, record_type, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_facts, "read_fact", selected_read)
        if isinstance(store, LocalSessionStore):
            open_path = Path.open

            def guarded_open(path, *args, **kwargs):
                assert path.name not in {"events.jsonl", "messages.jsonl"}
                assert "runs" not in path.parts and "harness" not in path.parts
                return open_path(path, *args, **kwargs)

            patch.setattr(Path, "open", guarded_open)
        else:
            connect = store._connect

            def guarded_connect():
                connection = connect()

                def authorize(operation, table, column, _database, _source):
                    if operation == sqlite3.SQLITE_READ and (
                        table in {"runs", "harness_versions", "messages", "events"}
                        or (table == "sessions" and column == "payload")
                        or (table == "tool_calls" and column == "payload")
                    ):
                        return sqlite3.SQLITE_DENY
                    if table in {
                        "tool_calls",
                        "tool_call_starts",
                        "tool_outcomes",
                        "sessions",
                    } and operation in {
                        sqlite3.SQLITE_UPDATE,
                        sqlite3.SQLITE_DELETE,
                    }:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(authorize)
                return connection

            patch.setattr(store, "_connect", guarded_connect)
        assert store.commit_tool_outcome(outcomes[-1]) == outcomes[-1]
    assert reads == (
        [ToolOutcomeRecord, ToolCallRecord, ToolCallStart, ToolOutcomeRecord]
        if isinstance(store, LocalSessionStore)
        else [ToolCallRecord, ToolCallStart, ToolOutcomeRecord]
    )
    assert store.load_session_record(session.session_id) == before
    assert store.load_tool_outcome(session.session_id, selected, 1) == outcomes[-1]
