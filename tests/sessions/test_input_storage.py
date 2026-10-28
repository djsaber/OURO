from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

import ouro.local_io as local_io
import ouro.sessions.backends.files.commits as file_commits
import ouro.sessions.backends.files.inputs as file_inputs
import ouro.sessions.backends.sqlite.inputs as sqlite_inputs
from ouro.application.sessions.editing import undo_user_input
from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.inputs import InputRecord, InputState, InputStateChange, QueuedInput
from ouro.core.messages import InputMessageReference, MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.inputs import input_key, input_path
from ouro.sessions.portable import PortableSession, SessionBundle, populate_sessions
from tests.input_facts import append_input_messages


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = repository(tmp_path, request.param)
    result.initialize()
    return result


def repository(path, backend):
    return (
        LocalSessionStore(path) if backend == "files" else SQLiteSessionStore(path / "session.db")
    )


def accepted(session, identity="input_selected", *, prompt=None):
    return InputRecord(
        session_id=session.session_id,
        item=QueuedInput(
            queue_id=identity,
            history_epoch=session.history_epoch,
            prompt=prompt if prompt is not None else f"original accepted content: {identity}",
            queued_at=utc_now(),
            source="user",
            mode="follow_up",
            role="user",
            scaffold=False,
            semantic_tag=None,
        ),
    )


def consume(session, record):
    message = MessageRecord.text(
        session_id=session.session_id, role="user", text=record.item.prompt
    ).model_copy(update={"input_ref": InputMessageReference(input_id=record.item.queue_id)})
    return SessionCommit(
        before=session,
        steps=(SessionCommitStep(message=message),),
        input_changes=(
            InputStateChange(
                input_id=record.item.queue_id, status="consumed", message_ids=(message.message_id,)
            ),
        ),
    )


def test_terminal_input_retains_acceptance_and_rejects_identity_reuse(store):
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    session = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    consumed = store.commit_session(consume(session, record))
    assert consumed.session.pending_input_ids == ()
    state = store.load_input_state(session.session_id, record.item.queue_id)
    assert state is not None and state.status == "consumed"
    assert state.message_ids == (consumed.messages[0].message_id,)
    withdrawn = store.commit_session(
        SessionCommit(before=consumed.session, before_message_id=consumed.messages[0].message_id)
    )
    assert withdrawn.session.message_count == 0
    assert store.load_input(session.session_id, record.item.queue_id) == record
    state = store.load_input_state(session.session_id, record.item.queue_id)
    assert state is not None and state.status == "withdrawn"
    assert state.message_ids == (consumed.messages[0].message_id,)
    with pytest.raises(SessionCommitConflict, match="already accepted"):
        store.commit_session(SessionCommit(before=withdrawn.session, inputs=(record,)))
    assert store.load_session_record(session.session_id) == withdrawn.session


def test_input_consumption_cannot_claim_another_inputs_message(store):
    session = SessionRecord()
    store.create_session(session)
    first, second = accepted(session, "first"), accepted(session, "second")
    session = store.commit_session(SessionCommit(before=session, inputs=(first, second))).session
    request = consume(session, second)
    request.input_changes = (
        request.input_changes[0].model_copy(update={"input_id": first.item.queue_id}),
    )
    with pytest.raises(SessionCommitConflict, match="Input message|another input"):
        store.commit_session(request)
    assert store.load_session_record(session.session_id) == session
    assert store.load_messages_page(session.session_id)[0] == []


@pytest.mark.parametrize("action", ["consume", "cancel", "accept"])
@pytest.mark.parametrize(("pending_count", "body_size"), [(4, 1024), (128, 1024), (4, 131072)])
def test_local_input_change_does_not_read_or_rewrite_accepted_bodies(
    store, monkeypatch, action, pending_count, body_size
):
    session = SessionRecord()
    store.create_session(session)
    records = tuple(
        accepted(session, f"input_{index}", prompt="x" * body_size)
        for index in range(pending_count)
    )
    session = store.commit_session(SessionCommit(before=session, inputs=records)).session
    if action == "consume":
        request = consume(session, records[0])
    elif action == "cancel":
        request = SessionCommit(
            before=session,
            input_changes=(
                InputStateChange(input_id=records[0].item.queue_id, status="cancelled"),
            ),
        )
    else:
        request = SessionCommit(before=session, inputs=(accepted(session, "new_input"),))
    with monkeypatch.context() as patch:
        if isinstance(store, LocalSessionStore):
            open_path = Path.open
            replace = local_io.replace_path_with_retry
            body_names = {f"{input_key(record.item.queue_id)}.json" for record in records}

            def guarded_open(path, *args, **kwargs):
                assert not (path.parent.name == "records" and path.name in body_names)
                return open_path(path, *args, **kwargs)

            def guarded_replace(source, target):
                assert not (target.parent.name == "records" and target.name in body_names)
                return replace(source, target)

            patch.setattr(Path, "open", guarded_open)
            patch.setattr(local_io, "replace_path_with_retry", guarded_replace)
        else:
            connect = store._connect

            def authorize(operation, table, column, _database, _source):
                if table == "inputs" and (
                    (operation == sqlite3.SQLITE_READ and column == "payload")
                    or operation in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            def guarded_connect():
                connection = connect()
                connection.set_authorizer(authorize)
                return connection

            patch.setattr(store, "_connect", guarded_connect)
        store.commit_session(request)
    assert [
        store.load_input(session.session_id, record.item.queue_id) for record in records
    ] == list(records)


@pytest.mark.parametrize("boundary", ["decision", "body", "state", "head", "cleanup"])
def test_file_input_acceptance_recovers_native_decision(tmp_path, monkeypatch, boundary):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    request = SessionCommit(before=session, inputs=(record,))
    directory = store.sessions_dir / session.session_id
    target = {
        "decision": directory / "commit.json",
        "body": input_path(directory, record.item.queue_id),
        "state": input_path(directory, record.item.queue_id, state=True),
    }.get(boundary)
    replace, unlink = local_io.replace_path_with_retry, Path.unlink
    reached = []
    write_head = file_commits.write_session

    def fail_head(connection, session):
        write_head(connection, session)
        if boundary == "head":
            reached.append(boundary)
            raise OSError("injected input acceptance interruption")

    def fail_replace(source, destination):
        replace(source, destination)
        if destination == target:
            reached.append(boundary)
            raise OSError("injected input acceptance interruption")

    def fail_unlink(path, *args, **kwargs):
        if boundary == "cleanup" and path == directory / "commit.json":
            reached.append(boundary)
            raise OSError("injected input acceptance interruption")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", fail_head)
        patch.setattr(local_io, "replace_path_with_retry", fail_replace)
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(PersistenceError):
            store.commit_session(request)
    assert reached == [boundary]
    reader = LocalSessionStore(tmp_path)
    reader.initialize()
    pending = (directory / "commit.json").read_bytes()
    for query in (
        lambda: reader.load_input(session.session_id, record.item.queue_id),
        lambda: reader.load_input_state(session.session_id, record.item.queue_id),
        lambda: reader.load_session_record(session.session_id),
    ):
        with pytest.raises(SessionRecoveryRequiredError):
            query()
        assert (directory / "commit.json").read_bytes() == pending
    with reader.session_mutation(session.session_id):
        pass
    assert reader.load_input(session.session_id, record.item.queue_id) == record
    state = reader.load_input_state(session.session_id, record.item.queue_id)
    assert state is not None and state.status == "pending"
    assert reader.load_session_record(session.session_id).pending_input_ids == (
        record.item.queue_id,
    )
    assert reader.load_session_record(session.session_id).last_commit_id == request.operation_id
    with pytest.raises(SessionCommitConflict, match="already applied"):
        reader.commit_session(request)
    assert not (directory / "commit.json").exists()


@pytest.mark.parametrize(
    ("action", "table", "operation"),
    [
        ("accept", "inputs", "INSERT"),
        ("accept", "input_states", "INSERT"),
        ("accept", "sessions", "UPDATE"),
        ("consume", "input_states", "UPDATE"),
        ("consume", "messages", "INSERT"),
        ("consume", "sessions", "UPDATE"),
        ("cancel", "input_states", "UPDATE"),
        ("cancel", "sessions", "UPDATE"),
    ],
)
def test_sqlite_input_fault_rolls_back_acceptance_state_and_history(
    tmp_path, action, table, operation
):
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    if action != "accept":
        session = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    if action == "accept":
        request = SessionCommit(before=session, inputs=(record,))
    elif action == "consume":
        request = consume(session, record)
    else:
        request = SessionCommit(
            before=session,
            input_changes=(InputStateChange(input_id=record.item.queue_id, status="cancelled"),),
        )
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER interrupt_input AFTER {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected input transaction fault'); END"
        )
    with pytest.raises(PersistenceError, match="injected input transaction fault"):
        store.commit_session(request)
    reader = SQLiteSessionStore(store.database)
    reader.initialize()
    assert reader.load_session_record(session.session_id) == session
    assert reader.load_messages_page(session.session_id)[0] == []
    state = reader.load_input_state(session.session_id, record.item.queue_id)
    assert (
        (state is None) if action == "accept" else (state is not None and state.status == "pending")
    )
    assert reader.load_input(session.session_id, record.item.queue_id) == (
        None if action == "accept" else record
    )
    assert reader.load_session_record(session.session_id).last_commit_id != request.operation_id
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute("DROP TRIGGER interrupt_input")
    reader.commit_session(request)
    state = reader.load_input_state(session.session_id, record.item.queue_id)
    assert state is not None
    assert (
        state.status == {"accept": "pending", "consume": "consumed", "cancel": "cancelled"}[action]
    )


def test_sqlite_undo_cannot_confirm_a_head_from_a_failed_transaction(tmp_path):
    store = SQLiteSessionStore(tmp_path / "session.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    before = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_undo AFTER INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'injected undo fault'); END"
        )
    with pytest.raises(PersistenceError):
        undo_user_input(store, session.session_id, queue_id=record.item.queue_id)
    assert store.load_session_record(session.session_id) == before
    assert store.load_input_state(session.session_id, record.item.queue_id).status == "pending"
    assert store.load_input(session.session_id, record.item.queue_id) == record


def test_cancel_and_consume_have_one_atomic_winner(store):
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    session = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    requests = (
        consume(session, record),
        SessionCommit(
            before=session,
            input_changes=(InputStateChange(input_id=record.item.queue_id, status="cancelled"),),
        ),
    )
    ready = Barrier(2)

    def commit(request):
        ready.wait(timeout=5)
        try:
            return store.commit_session(request)
        except SessionCommitConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(commit, requests))
    assert sum(result is not None for result in results) == 1
    state = store.load_input_state(session.session_id, record.item.queue_id)
    assert state is not None and state.status in {"consumed", "cancelled"}
    messages, _ = store.load_messages_page(session.session_id)
    assert len(messages) == (1 if state.status == "consumed" else 0)
    assert store.load_session_record(session.session_id).pending_input_ids == ()
    assert store.load_input(session.session_id, record.item.queue_id) == record


@pytest.mark.parametrize("action", ["consume", "cancel"])
@pytest.mark.parametrize("boundary", ["state", "head", "cleanup"])
def test_file_terminal_input_recovery_preserves_original_body(
    tmp_path, monkeypatch, action, boundary
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    session = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    request = (
        consume(session, record)
        if action == "consume"
        else SessionCommit(
            before=session,
            input_changes=(InputStateChange(input_id=record.item.queue_id, status="cancelled"),),
        )
    )
    directory = store.sessions_dir / session.session_id
    body = input_path(directory, record.item.queue_id)
    original = body.read_bytes()
    target = {
        "state": input_path(directory, record.item.queue_id, state=True),
    }.get(boundary)
    replace, unlink = local_io.replace_path_with_retry, Path.unlink

    write_head = file_commits.write_session

    def interrupt_head(connection, session):
        write_head(connection, session)
        if boundary == "head":
            raise OSError("injected terminal input interruption")

    def interrupt_replace(source, destination):
        replace(source, destination)
        if destination == target:
            raise OSError("injected terminal input interruption")

    def interrupt_cleanup(path, *args, **kwargs):
        if boundary == "cleanup" and path == directory / "commit.json":
            raise OSError("injected terminal input interruption")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", interrupt_head)
        patch.setattr(local_io, "replace_path_with_retry", interrupt_replace)
        patch.setattr(Path, "unlink", interrupt_cleanup)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)
    assert body.read_bytes() == original
    reader = LocalSessionStore(tmp_path)
    reader.initialize()
    with pytest.raises(SessionRecoveryRequiredError):
        reader.load_input_state(session.session_id, record.item.queue_id)
    with reader.session_mutation(session.session_id):
        pass
    state = reader.load_input_state(session.session_id, record.item.queue_id)
    assert state is not None and state.status == (
        "consumed" if action == "consume" else "cancelled"
    )
    messages, _ = reader.load_messages_page(session.session_id)
    assert len(messages) == (1 if action == "consume" else 0)
    assert state.message_ids == tuple(message.message_id for message in messages)
    assert reader.load_session_record(session.session_id).pending_input_ids == ()
    assert body.read_bytes() == original


def test_cross_backend_transfer_retains_pending_and_terminal_acceptances(store, tmp_path):
    session = SessionRecord()
    store.create_session(session)
    records = tuple(accepted(session, name) for name in ("consumed", "cancelled", "pending"))
    session = store.commit_session(SessionCommit(before=session, inputs=records)).session
    session = store.commit_session(consume(session, records[0])).session
    store.commit_session(
        SessionCommit(
            before=session,
            input_changes=(InputStateChange(input_id="cancelled", status="cancelled"),),
        )
    )
    original = store.load_session(session.session_id)
    target = repository(
        tmp_path / "target", "sqlite" if isinstance(store, LocalSessionStore) else "files"
    )
    target.initialize()
    populate_sessions(target, SessionBundle(sessions=[PortableSession.from_snapshot(original)]))
    assert target.load_session(session.session_id) == original
    assert target.load_input(session.session_id, "cancelled") == records[1]
    with pytest.raises(SessionCommitConflict, match="already accepted"):
        target.commit_session(SessionCommit(before=original.session, inputs=(records[1],)))


def test_branch_copies_selected_input_fact_without_pending_authorization(store):
    from ouro.application.sessions.service import SessionService

    session = SessionRecord()
    store.create_session(session)
    selected, pending = accepted(session, "selected"), accepted(session, "pending")
    session = store.commit_session(
        SessionCommit(before=session, inputs=(selected, pending))
    ).session
    result = store.commit_session(consume(session, selected))
    branch = SessionService(store).branch(session.session_id, result.messages[0].message_id)
    copied = store.load_session(branch.session.session_id)
    assert len(copied.inputs) == 1
    assert copied.inputs[0].session_id == branch.session.session_id
    assert copied.inputs[0].item == selected.item
    assert copied.session.pending_input_ids == ()
    assert copied.input_states[0].status == "consumed"
    assert copied.input_states[0].message_ids == (copied.messages[0].message_id,)
    assert copied.messages[0].message_id != result.messages[0].message_id
    assert store.load_input(branch.session.session_id, pending.item.queue_id) is None


@pytest.mark.parametrize("damage", ["message", "routing", "queue"])
def test_snapshot_rejects_invalid_input_references_before_publication(store, damage):
    session = SessionRecord()
    record = accepted(session)
    state = InputState.pending(record)
    if damage == "message":
        state = state.model_copy(update={"status": "consumed", "message_ids": ("foreign_message",)})
    elif damage == "routing":
        session.pending_input_ids = (record.item.queue_id,)
        state = state.model_copy(update={"mode": "steer"})
    with pytest.raises(ValueError, match="input|Input"):
        store.create_session_snapshot(session, [], inputs=(record,), input_states=(state,))
    with pytest.raises(FileNotFoundError):
        store.load_session_record(session.session_id)


def test_file_failure_before_acceptance_decision_leaves_no_input_fact(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    request = SessionCommit(before=session, inputs=(record,))
    directory = store.sessions_dir / session.session_id
    replace = local_io.replace_path_with_retry

    def interrupt(source, destination):
        if destination == directory / "commit.json":
            raise OSError("injected before input decision")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(local_io, "replace_path_with_retry", interrupt)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)
    reader = LocalSessionStore(tmp_path)
    assert reader.load_session_record(session.session_id) == session
    assert reader.load_input(session.session_id, record.item.queue_id) is None
    assert reader.load_input_state(session.session_id, record.item.queue_id) is None
    assert not (directory / "commit.json").exists()


@pytest.mark.parametrize("identity", ["../host/结果:1", "x" * 256])
def test_opaque_input_identity_is_preserved_without_becoming_a_path(store, identity):
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session, identity)
    session = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    result = store.commit_session(consume(session, record))
    assert store.load_input(session.session_id, identity) == record
    resolved = store.find_queued_input_message(session.session_id, identity)
    assert resolved is not None and resolved.as_fact() == result.messages[0]
    assert resolved.text_content == record.item.prompt
    state = store.load_input_state(session.session_id, identity)
    assert state is not None and state.message_ids == (result.messages[0].message_id,)


@pytest.mark.parametrize("query", ["point", "tail", "run", "input"])
def test_cold_message_queries_resolve_only_selected_input_bodies(store, monkeypatch, query):
    session = SessionRecord()
    store.create_session(session)
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text=f"accepted body {index}",
            run_id="run_selected" if index >= 28 else f"run_{index}",
        ).model_copy(update={"input_ref": InputMessageReference(input_id=f"input_{index}")})
        for index in range(30)
    ]
    append_input_messages(store, messages)
    is_files = isinstance(store, LocalSessionStore)
    module = file_inputs if is_files else sqlite_inputs
    decoded = []
    decode = module.decode_versioned_record_json

    def track(model, payload, **kwargs):
        value = decode(model, payload, **kwargs)
        if model is InputRecord:
            decoded.append(value.item.queue_id)
        return value

    monkeypatch.setattr(module, "decode_versioned_record_json", track)
    if is_files:
        reader = LocalSessionStore(store.sessions_dir.parent)
        open_path = Path.open
        allowed = {f"{input_key(f'input_{index}')}.json" for index in (28, 29)}

        def guarded_open(path, *args, **kwargs):
            if path.parent.name == "records" and path.parent.parent.name == "inputs":
                assert path.name in allowed, "a selected message read an unrelated Input"
            return open_path(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", guarded_open)
    else:
        reader = SQLiteSessionStore(store.database)
    if query == "point":
        found = [reader.load_message(session.session_id, messages[-1].message_id)]
        expected = messages[-1:]
    elif query == "input":
        found = [reader.find_queued_input_message(session.session_id, "input_29")]
        expected = messages[-1:]
    elif query == "run":
        found = reader.load_run_messages(session.session_id, "run_selected")
        expected = messages[-2:]
    else:
        found = reader.load_messages_tail(session.session_id, limit=2)
        expected = messages[-1:-3:-1]
    assert found == expected
    assert decoded == [message.input_id for message in expected]


@pytest.mark.parametrize("damage", ["unbound", "role", "scaffold", "tag"])
def test_message_input_binding_rejects_invalid_publication_atomically(store, damage):
    session = SessionRecord()
    store.create_session(session)
    record = accepted(session)
    session = store.commit_session(SessionCommit(before=session, inputs=(record,))).session
    request = consume(session, record)
    if damage == "unbound":
        request.input_changes = ()
    else:
        field, value = {
            "role": ("role", "system"),
            "scaffold": ("scaffold", True),
            "tag": ("semantic_tag", "attachments"),
        }[damage]
        message = request.steps[0].message.model_copy(update={field: value})
        request.steps = (SessionCommitStep(message=message),)
    with pytest.raises(SessionCommitConflict, match="Input"):
        store.commit_session(request)
    assert store.load_session_record(session.session_id) == session
    assert store.load_input_state(session.session_id, record.item.queue_id).status == "pending"
    assert store.load_messages_page(session.session_id)[0] == []
