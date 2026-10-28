from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.application.runs.tool_batches import load_batch_call, load_tool_batch
from ouro.application.sessions.service import SessionService
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord, ToolMessageReference
from ouro.core.observers import ObserverRegistry
from ouro.core.run_drivers import DriverCheckpoint
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_facts import ToolCallRecord, ToolOutcomeRecord
from ouro.core.tools import ToolResult, ToolResultFeedback, ToolResultValue
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.portable import PortableSession, SessionBundle, populate_sessions
from tests.tool_batches import persist_tool_batch, resolved_tool_batch


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


def accepted_frame(store, *, unknown=False, feedback=False, structured_bytes=0, argument_bytes=0):
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session.active_run_id = run.run_id
    session.status = "running"
    store.create_session(session)
    results = [
        ToolResult(
            tool_call_id=f"provider/{index}",
            tool_name="Inspect",
            content=f"OBSERVED-{index}",
            structured={"value": f"STRUCTURED-{index}" + "x" * structured_bytes},
        )
        for index in range(2)
    ]
    batch = resolved_tool_batch(run, results)
    for index, call in enumerate(batch.calls):
        call.request.arguments = {"value": f"ORIGINAL-{index}" + "x" * argument_bytes}
        if unknown:
            call.status = "unknown_outcome"
            call.result = None
    run.driver_checkpoint = DriverCheckpoint(batch_id=batch.batch_id, payload="finish")
    run = persist_tool_batch(store, run, batch)
    assistant = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=batch.turn_id,
        role="assistant",
        call_refs=tuple(batch.head().identity(run.run_id, call.request.id) for call in batch.calls),
    )
    store.append_message(assistant)
    lifecycle = RunLifecycle(
        store=store, events=RuntimeEventRecorder(store=store, observers=ObserverRegistry())
    )
    if feedback:
        for call in batch.calls:
            call.feedback = ToolResultFeedback(
                error_type="ControlFailed", error_message=f"FEEDBACK-{call.request.source_index}"
            )
            call.result = call.feedback.materialize(call.result)
            call.status = "failed"
        session, run, batch, _ = lifecycle.save_batch_state(
            session=session, run=run, batch=batch, status="running", phase="tool_execution"
        )
    return session, run, batch, assistant, lifecycle


@pytest.mark.parametrize("feedback", [False, True])
def test_fixed_messages_and_archive_keep_call_and_outcome_bodies_once(store, tmp_path, feedback):
    session, run, batch, assistant, lifecycle = accepted_frame(store, feedback=feedback)
    _, run, _, messages, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    loaded = store.load_message(session.session_id, assistant.message_id)
    assert [call.arguments for call in loaded.tool_calls] == [
        {"value": "ORIGINAL-0"},
        {"value": "ORIGINAL-1"},
    ]
    assert [message.text_content for message in messages] == [
        f"OBSERVED-{index}" + (f"\nFEEDBACK-{index}" if feedback else "") for index in range(2)
    ]
    if isinstance(store, LocalSessionStore):
        journal = store.sessions_dir / session.session_id / "messages.jsonl"
        payloads = [json.loads(line) for line in journal.read_bytes().splitlines()]
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            payloads = [
                json.loads(row[0]) for row in connection.execute("SELECT payload FROM messages")
            ]
    encoded_messages = json.dumps(payloads)
    assert "ORIGINAL-" not in encoded_messages
    assert "OBSERVED-" not in encoded_messages
    assert "STRUCTURED-" not in encoded_messages
    assert all(
        not payload.get("tool_calls") and payload.get("tool_result") is None for payload in payloads
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
    archive = SessionBundle(sessions=[PortableSession.from_snapshot(snapshot)])
    encoded = archive.model_dump_json()
    for marker in ("ORIGINAL-0", "OBSERVED-0", "STRUCTURED-0"):
        assert encoded.count(marker) == 1
    if feedback:
        assert encoded.count("FEEDBACK-0") == 1
    target = SQLiteSessionStore(tmp_path / "target.db")
    target.initialize()
    populate_sessions(target, SessionBundle.model_validate_json(encoded))
    assert target.load_session(session.session_id) == snapshot


def test_published_unknown_message_does_not_follow_a_late_outcome(store):
    session, run, batch, _, lifecycle = accepted_frame(store, unknown=True)
    for call in batch.calls:
        call.result = ToolResult(
            tool_call_id=call.request.id,
            tool_name=call.request.name,
            content="Execution outcome was unknown at recovery",
            is_error=True,
        )
    _, _, _, messages, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    assert all(message.outcome_ref.outcome_attempt is None for message in messages)
    identity = messages[0].outcome_ref.identity
    store.commit_tool_outcome(
        ToolOutcomeRecord(
            session_id=session.session_id,
            identity=identity,
            attempt=1,
            value=ToolResultValue(
                tool_call_id=identity.tool_call_id, tool_name="Inspect", content="LATE"
            ),
        )
    )
    assert store.load_message(session.session_id, messages[0].message_id) == messages[0]
    state = store.load_tool_call_state(session.session_id, identity)
    assert state.status == "unknown_outcome"
    assert state.outcome_attempt is None
    assert store.load_tool_outcome(session.session_id, identity, 1).value.content == "LATE"


def test_message_lookup_and_replay_use_the_fixed_batch_when_provider_ids_repeat(store, monkeypatch):
    import ouro.sessions.backends.sqlite.store as sqlite_store
    import ouro.sessions.sqlite_facts as native_facts

    session, run, original, _, lifecycle = accepted_frame(store)
    _, run, _, first, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=original)
    later = original.model_copy(deep=True, update={"batch_id": "batch_later"})
    for call in later.calls:
        call.result.content = "Different batch, same Provider call ID"
    run.driver_checkpoint = DriverCheckpoint(batch_id=later.batch_id, payload="later")
    run = persist_tool_batch(store, run, later)
    lifecycle.publish_tool_batch(session=session, run=run, batch=later)
    selected = []
    read = native_facts.read_fact

    def record(
        connection,
        session_id,
        identity,
        record_type,
        *,
        attempt=None,
        include_structured=True,
        include_arguments=True,
    ):
        selected.append((identity, record_type, attempt))
        return read(
            connection,
            session_id,
            identity,
            record_type,
            attempt=attempt,
            include_structured=include_structured,
            include_arguments=include_arguments,
        )

    monkeypatch.setattr(native_facts, "read_fact", record)
    monkeypatch.setattr(sqlite_store, "read_fact", record)
    assert store.load_message(session.session_id, first[0].message_id) == first[0]
    identity = first[0].outcome_ref.identity
    assert selected == [(identity, ToolCallRecord, None), (identity, ToolOutcomeRecord, 1)]
    selected.clear()
    assert store.load_message_fact(session.session_id, first[0].message_id) == first[0].as_fact()
    assert selected == []
    outcome_reads = []
    load_outcome = store.load_tool_outcome

    def observed_outcome(session_id, identity, attempt):
        outcome_reads.append((identity, attempt))
        return load_outcome(session_id, identity, attempt)

    monkeypatch.setattr(store, "load_tool_outcome", observed_outcome)
    assert load_batch_call(store, session.session_id, identity).result.content == "OBSERVED-0"
    assert outcome_reads == [(identity, 1)]
    _, _, _, replay, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=original)
    assert replay == first


@pytest.mark.parametrize("query", ["tail", "page", "run", "history"])
def test_tool_message_reads_bind_private_records_without_reconstructing_them(
    store, monkeypatch, query
):
    session, run, batch, _, lifecycle = accepted_frame(store, feedback=True)
    lifecycle.publish_tool_batch(session=session, run=run, batch=batch)

    def reject_reconstruction(*args, **kwargs):
        pytest.fail("decoded messages and typed source values must not be reconstructed")

    def read():
        match query:
            case "tail":
                return list(reversed(store.load_messages_tail(session.session_id)))
            case "page":
                return store.load_messages_page(session.session_id)[0]
            case "run":
                return store.load_run_messages(session.session_id, run.run_id)
            case "history":
                return store.load_session_observation(session.session_id).messages

    monkeypatch.setattr(MessageRecord, "model_validate", reject_reconstruction)
    messages = read()
    assert all(type(message) is MessageRecord for message in messages)
    assert messages[0].tool_calls[0].arguments == {"value": "ORIGINAL-0"}
    assert messages[1].text_content == "OBSERVED-0\nFEEDBACK-0"
    assert messages[1].tool_result.is_error
    messages[0].tool_calls[0].arguments["value"] = "changed view"
    messages[1].content[0].text = "changed view"
    again = read()
    assert again[0].tool_calls[0].arguments == {"value": "ORIGINAL-0"}
    assert again[1].text_content == "OBSERVED-0\nFEEDBACK-0"


@pytest.mark.parametrize("query", ["tail", "page", "run", "history"])
def test_files_selected_tool_messages_share_one_native_read(tmp_path, monkeypatch, query):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session, run, batch, _, lifecycle = accepted_frame(store)
    lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    import ouro.sessions.backends.files.store as file_store

    opens = 0
    connect_data = file_store.connect_data

    @contextmanager
    def count_open(directory, *, mode="ro", **kwargs):
        nonlocal opens
        opens += 1
        with connect_data(directory, mode=mode, **kwargs) as connection:
            assert connection.in_transaction
            yield connection

    with monkeypatch.context() as patch:
        patch.setattr(file_store, "connect_data", count_open)
        match query:
            case "tail":
                messages = store.load_messages_tail(session.session_id)
            case "page":
                messages, _ = store.load_messages_page(session.session_id)
            case "run":
                messages = store.load_run_messages(session.session_id, run.run_id)
            case "history":
                messages = store.load_session_observation(session.session_id).messages
    assert opens == 1
    assert len([item for item in messages if item.outcome_ref is not None]) == 2
    assert {item.text_content for item in messages if item.outcome_ref is not None} == {
        "OBSERVED-0",
        "OBSERVED-1",
    }


def test_selected_message_range_joins_only_its_referenced_facts(store, monkeypatch):
    import ouro.sessions.message_reads as reads

    session, run, batch, _, lifecycle = accepted_frame(store)
    lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    selected = []
    read = reads.read_selected

    def observe(connection, record_type, keys, **kwargs):
        selected.append((record_type, keys))
        return read(connection, record_type, keys, **kwargs)

    monkeypatch.setattr(reads, "read_selected", observe)
    messages = store.load_messages_tail(session.session_id, limit=1)
    identity = messages[0].outcome_ref.identity
    key = (session.session_id, identity.run_id, identity.batch_id, identity.tool_call_id)
    assert selected == [(ToolCallRecord, {key}), (ToolOutcomeRecord, {(*key, 1)})]
    selected.clear()
    assert len(store.load_messages_tail(session.session_id)) == 3
    assert [record_type for record_type, _ in selected] == [ToolCallRecord, ToolOutcomeRecord]
    assert [len(keys) for _, keys in selected] == [2, 2]


@pytest.mark.parametrize("argument_bytes", [1024, 1024 * 1024])
def test_result_queries_do_not_read_original_call_arguments(store, argument_bytes):
    session, run, batch, assistant, lifecycle = accepted_frame(store, argument_bytes=argument_bytes)
    _, _, _, results, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    with store.session_read(session.session_id) as reader:
        connection = reader.connection() if isinstance(store, LocalSessionStore) else reader

        def authorize(action, table, column, *args):
            if action == sqlite3.SQLITE_READ and table == "tool_calls" and column == "payload":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        try:
            assert store.load_message(session.session_id, results[0].message_id) == results[0]
            assert store.load_messages_tail(session.session_id, limit=2) == list(reversed(results))
        finally:
            connection.set_authorizer(None)
    expected = [call.request.arguments for call in batch.calls]
    assert [
        call.arguments
        for call in store.load_message(session.session_id, assistant.message_id).tool_calls
    ] == expected
    assert [
        call.arguments
        for call in store.load_session_observation(session.session_id).messages[0].tool_calls
    ] == expected
    snapshot = store.load_session(session.session_id)
    assert [call.request.arguments for call in snapshot.tool_facts.calls] == expected
    assert (
        store.load_tool_call(
            session.session_id, snapshot.tool_facts.calls[0].identity
        ).request.arguments
        == expected[0]
    )


@pytest.mark.parametrize("structured_bytes", [1024, 1024 * 1024])
def test_message_queries_do_not_read_outcome_structured_results(store, structured_bytes):
    session, run, batch, _, lifecycle = accepted_frame(store, structured_bytes=structured_bytes)
    _, _, _, results, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    with store.session_read(session.session_id) as reader:
        connection = reader.connection() if isinstance(store, LocalSessionStore) else reader

        def authorize(action, table, column, *args):
            if (
                action == sqlite3.SQLITE_READ
                and table == "tool_outcomes"
                and column == "structured"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        try:
            assert (
                store.load_message(session.session_id, results[0].message_id).text_content
                == "OBSERVED-0"
            )
            assert len(store.load_messages_tail(session.session_id)) == 3
            assert len(store.load_run_messages(session.session_id, run.run_id)) == 3
            assert len(store.load_session_observation(session.session_id).messages) == 3
        finally:
            connection.set_authorizer(None)
    expected = {"value": "STRUCTURED-0" + "x" * structured_bytes}
    assert (
        store.load_tool_outcome(
            session.session_id, results[0].outcome_ref.identity, 1
        ).value.structured
        == expected
    )
    assert (
        store.load_session(session.session_id).tool_facts.outcomes[0].value.structured == expected
    )


def test_repeated_selected_facts_produce_independent_message_values(store):
    from ouro.core.message_content import decode_message_content
    from ouro.sessions.message_reads import resolve_messages

    session, run, batch, assistant, lifecycle = accepted_frame(store)
    _, _, _, results, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    facts = [assistant.as_fact(), results[0].as_fact()]

    def decoded():
        return (
            decode_message_content(fact.storage_json(), label="repeated reference")
            for fact in facts * 2
        )

    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection:
        messages = list(
            resolve_messages(decoded(), connection=connection, read_input=lambda *_: None)
        )
    messages[0].tool_calls[0].arguments["value"] = "changed"
    messages[1].tool_result.producer.kind = "driver"
    messages[1].content[0].text = "changed"
    assert messages[2].tool_calls[0].arguments == {"value": "ORIGINAL-0"}
    assert messages[3].tool_result.producer.kind == "model"
    assert messages[3].text_content == "OBSERVED-0"


@pytest.mark.parametrize("feedback", [False, True])
def test_branch_owns_referenced_facts_without_original_execution_state(store, feedback):
    session, run, batch, assistant, lifecycle = accepted_frame(store, feedback=feedback)
    _, run, _, _, _ = lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
    before = store.load_session_record(session.session_id)
    store.commit_session(
        SessionCommit(
            before=before,
            after=before.model_copy(update={"status": "idle", "active_run_id": None}),
            runs=(run.model_copy(update={"status": "completed"}),),
        )
    )
    branch = SessionService(store).branch(session.session_id, assistant.message_id)
    copied = store.load_session(branch.session.session_id)
    assert len(copied.messages) == 3
    assert not copied.runs and not copied.call_states
    assert (
        len(copied.tool_facts.calls)
        == len(copied.tool_facts.starts)
        == len(copied.tool_facts.outcomes)
        == 2
    )
    assert all(call.origin_session_id == session.session_id for call in copied.tool_facts.calls)
    assert all(call.session_id == branch.session.session_id for call in copied.tool_facts.calls)
    assert [message.text_content for message in copied.messages[1:]] == [
        f"OBSERVED-{index}" + (f"\nFEEDBACK-{index}" if feedback else "") for index in range(2)
    ]
    store.trash_session_tree(session.session_id, (session.session_id,))
    assert store.load_session(branch.session.session_id) == copied


@pytest.mark.parametrize("missing", ["call", "outcome"])
def test_missing_fixed_reference_rejects_the_entire_message_commit(store, missing):
    session, run, batch, _, _ = accepted_frame(store)
    identity = batch.head().identity(run.run_id, batch.calls[0].request.id)
    if missing == "call":
        identity = replace(identity, batch_id="absent")
    message = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=batch.turn_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=identity, outcome_attempt=99),
    )
    before = store.load_session_record(session.session_id)
    with pytest.raises(PersistenceError, match="missing"):
        store.commit_session(
            SessionCommit(before=before, steps=(SessionCommitStep(message=message),))
        )
    assert store.load_session_record(session.session_id) == before
    with pytest.raises(PersistenceError, match="message not found"):
        store.load_message(session.session_id, message.message_id)


def test_native_frame_failure_never_exposes_partial_history_or_driver_ready(store, monkeypatch):
    session, run, batch, assistant, lifecycle = accepted_frame(store)
    identity = batch.head().identity(run.run_id, batch.calls[0].request.id)
    reached = []
    if isinstance(store, LocalSessionStore):
        import ouro.sessions.backends.files.commits as file_commits

        write_state = file_commits.write_call_state

        def interrupt(connection, state):
            write_state(connection, state)
            if state.identity == identity:
                reached.append(True)
                raise OSError("interrupted after one published Call state")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_call_state", interrupt)
            with pytest.raises(PersistenceError):
                lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
        assert reached == [True]
        for query in (
            lambda: store.load_messages_tail(session.session_id),
            lambda: store.load_run(session.session_id, run.run_id),
            lambda: store.load_tool_call_state(session.session_id, identity),
        ):
            with pytest.raises(SessionRecoveryRequiredError):
                query()
        with store.session_mutation(session.session_id):
            pass
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_frame BEFORE INSERT ON messages BEGIN "
                "SELECT RAISE(ABORT, 'interrupted frame'); END"
            )
        with pytest.raises(PersistenceError):
            lifecycle.publish_tool_batch(session=session, run=run, batch=batch)
        assert [message.message_id for message in store.load_messages_tail(session.session_id)] == [
            assistant.message_id
        ]
        assert store.load_tool_call_state(session.session_id, identity).result_message_id is None
        assert store.load_run(session.session_id, run.run_id).driver_checkpoint.state == "pending"
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_frame")
    _, saved, completed, messages, _ = lifecycle.publish_tool_batch(
        session=session, run=run, batch=batch
    )
    events = [
        event
        for event in store.load_session(session.session_id).events
        if event.event_type == "tool_batch_completed"
    ]
    assert len(events) == 1
    assert events[0].payload["batch_id"] == batch.batch_id
    assert saved.driver_checkpoint.state == "ready"
    assert completed.phase == "completed"
    assert [call.result_message_id for call in completed.calls] == [
        message.message_id for message in messages
    ]
    assert len(store.load_messages_tail(session.session_id)) == 3
    assert [call.result.content for call in load_tool_batch(store, saved).calls] == [
        "OBSERVED-0",
        "OBSERVED-1",
    ]
