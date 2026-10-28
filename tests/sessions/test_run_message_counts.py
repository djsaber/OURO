from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.portable import PortableSession, SessionBundle
from tests.tool_batches import seed_tool_messages


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path)
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


def input_message(session_id, *, automatic=True):
    message = MessageRecord.text(
        session_id=session_id, run_id="run_counts", role="user", text="input"
    )
    message.metadata = {"session_input_mode": "follow_up"}
    if automatic:
        message.metadata["continuation_decision_id"] = "decision"
    return message


@pytest.mark.parametrize("old_messages", [4, 128])
def test_message_counts_read_and_append_do_not_read_old_bodies(store, monkeypatch, old_messages):
    session = SessionRecord()
    store.create_session(session)
    for _ in range(old_messages):
        message = input_message(session.session_id)
        message.content[0].text = "large historical body " * 1024
        store.append_message(message)
    original_open = Path.open

    def no_old_bodies(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.name == "messages.jsonl" and "r" in mode and "+" not in mode:
            raise AssertionError("message counts read old body")
        return original_open(path, *args, **kwargs)

    if isinstance(store, LocalSessionStore):
        monkeypatch.setattr(Path, "open", no_old_bodies)
    with store.session_read(session.session_id) as connection:
        if isinstance(store, SQLiteSessionStore):

            def authorize(action, table, column, *args):
                if action == sqlite3.SQLITE_READ and table == "messages" and column == "payload":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
        try:
            counts = store.load_run_message_counts(session.session_id, "run_counts")
            assert counts.input_count == old_messages
            assert counts.automatic_continuation is True
            assert counts.tool_call_count == 0
        finally:
            if isinstance(store, SQLiteSessionStore):
                connection.set_authorizer(None)
    store.append_message(input_message(session.session_id, automatic=False))
    counts = store.load_run_message_counts(session.session_id, "run_counts")
    assert counts.input_count == old_messages + 1
    assert counts.automatic_continuation is False


def test_tool_counts_follow_published_membership_and_transfer(store, tmp_path):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, run_id="run_counts", status="completed")
    store.save_run(run)
    first = input_message(session.session_id)
    assistant = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn",
        role="assistant",
        tool_calls=[ToolCallRequest(id="same", name="Read")],
    )
    result = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn",
        role="tool_result",
        tool_result=ToolResultReference(tool_call_id="same", tool_name="Read"),
    )
    seed_tool_messages(store, assistant, [result], run=run)
    store.append_messages([first, assistant, result])
    counts = store.load_run_message_counts(session.session_id, run.run_id)
    assert (counts.tool_call_count, counts.input_count, counts.automatic_continuation) == (
        1,
        1,
        True,
    )
    for target in (
        LocalSessionStore(tmp_path / "copy-files"),
        SQLiteSessionStore(tmp_path / "copy.db"),
    ):
        target.initialize()
        snapshot = store.load_session(session.session_id)
        target.import_bundle(
            SessionBundle(sessions=[PortableSession.from_snapshot(snapshot=snapshot)])
        )
        assert target.load_run_message_counts(session.session_id, run.run_id) == counts
    store.truncate_messages(
        session.session_id,
        before_message_id=assistant.message_id,
        expected_last_message_id=result.message_id,
    )
    counts = store.load_run_message_counts(session.session_id, run.run_id)
    assert (counts.tool_call_count, counts.input_count, counts.automatic_continuation) == (
        0,
        1,
        True,
    )
    store.truncate_messages(
        session.session_id,
        before_message_id=first.message_id,
        expected_last_message_id=first.message_id,
    )
    counts = store.load_run_message_counts(session.session_id, run.run_id)
    assert (counts.tool_call_count, counts.input_count, counts.automatic_continuation) == (
        0,
        0,
        None,
    )


@pytest.mark.parametrize("operation", ["append", "withdraw"])
def test_message_count_native_failure_does_not_split_publication(store, monkeypatch, operation):
    session = SessionRecord()
    store.create_session(session)
    first = input_message(session.session_id)
    store.append_message(first)
    added = input_message(session.session_id, automatic=False)
    assistant = MessageRecord(
        session_id=session.session_id,
        run_id="run_counts",
        turn_id="turn",
        role="assistant",
        tool_calls=[ToolCallRequest(id="same", name="Read")],
    )
    result = MessageRecord(
        session_id=session.session_id,
        run_id="run_counts",
        turn_id="turn",
        role="tool_result",
        tool_result=ToolResultReference(tool_call_id="same", tool_name="Read"),
    )
    seed_tool_messages(store, assistant, [result])
    if operation == "withdraw":
        store.append_messages([added, assistant, result])
    before_counts = store.load_run_message_counts(session.session_id, "run_counts")
    before_ids = [
        item.message_id for item in store.load_run_messages(session.session_id, "run_counts")
    ]

    def publish(repository):
        if operation == "append":
            repository.append_messages([added, assistant, result])
        else:
            repository.truncate_messages(
                session.session_id,
                before_message_id=added.message_id,
                expected_last_message_id=result.message_id,
            )

    if isinstance(store, LocalSessionStore):
        database = store.sessions_dir / session.session_id / "session-data.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_counts BEFORE INSERT ON counts "
                "BEGIN SELECT RAISE(ABORT, 'counts installation interrupted'); END"
            )
        with pytest.raises(PersistenceError, match="counts installation interrupted"):
            publish(store)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_counts")
        cold = LocalSessionStore(store.root)
        cold.initialize()
        pending = cold.sessions_dir / session.session_id / "commit.json"
        accepted = pending.read_bytes()
        with pytest.raises(SessionRecoveryRequiredError):
            cold.load_run_message_counts(session.session_id, "run_counts")
        assert pending.read_bytes() == accepted
        with cold.session_mutation(session.session_id):
            pass
        assert not pending.exists()
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_counts AFTER UPDATE ON run_message_counts "
                "BEGIN SELECT RAISE(ABORT, 'counts transaction failed'); END"
            )
        with pytest.raises(
            (PersistenceError, sqlite3.IntegrityError), match="counts transaction failed"
        ):
            publish(store)
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        assert cold.load_run_message_counts(session.session_id, "run_counts") == before_counts
        assert [
            message.message_id
            for message in cold.load_run_messages(session.session_id, "run_counts")
        ] == before_ids
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_counts")
        publish(cold)
    with cold.session_mutation(session.session_id):
        pass
    counts = cold.load_run_message_counts(session.session_id, "run_counts")
    assert counts.input_count == (2 if operation == "append" else 1)
    assert counts.tool_call_count == (1 if operation == "append" else 0)
    assert counts.automatic_continuation is (False if operation == "append" else True)
    assert len(cold.load_run_messages(session.session_id, "run_counts")) == (
        4 if operation == "append" else 1
    )


@pytest.mark.parametrize("call_count", [4, 64])
def test_files_call_membership_size_does_not_repeat_the_batch(tmp_path, call_count):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    assistant = MessageRecord(
        session_id=session.session_id,
        run_id="run_counts",
        turn_id="turn",
        role="assistant",
        tool_calls=[
            ToolCallRequest(id=f"call_{index}", name="Read") for index in range(call_count)
        ],
    )
    seed_tool_messages(store, assistant, [])
    store.append_message(assistant)
    database = store.sessions_dir / session.session_id / "session-data.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        memberships = connection.execute(
            "SELECT call_key, position FROM call_memberships"
        ).fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(locations)")}
    assert len(memberships) == call_count
    assert {json.loads(key)[-1] for key, _ in memberships} == {
        f"call_{index}" for index in range(call_count)
    }
    assert {position for _, position in memberships} == {1}
    assert "call_keys" not in columns
    assert (
        store.load_run_message_counts(session.session_id, "run_counts").tool_call_count
        == call_count
    )


@pytest.mark.parametrize("publication", ["snapshot", "append"])
def test_sqlite_message_batch_updates_counts_once_per_run(tmp_path, monkeypatch, publication):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord()
    if publication == "append":
        store.create_session(session)
    messages = [input_message(session.session_id, automatic=index % 2 == 0) for index in range(128)]
    for index, message in enumerate(messages):
        message.run_id = f"run_{index // 64}"
    statements = []
    connect = store._connect

    def trace_connection():
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", trace_connection)
    if publication == "snapshot":
        store.create_session_snapshot(session, messages)
    else:
        store.append_messages(messages)
    updates = [sql for sql in statements if sql.startswith("UPDATE run_message_counts SET")]
    assert len(updates) == 2
    for run_id in ("run_0", "run_1"):
        counts = store.load_run_message_counts(session.session_id, run_id)
        assert counts.input_count == 64
        assert counts.automatic_input_count == 32
        assert counts.tool_call_count == 0


@pytest.mark.parametrize("call_count", [4, 64])
def test_sqlite_call_membership_publication_batches_one_message(tmp_path, monkeypatch, call_count):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    assistant = MessageRecord(
        session_id=session.session_id,
        run_id="run_counts",
        turn_id="turn",
        role="assistant",
        tool_calls=[
            ToolCallRequest(id=f"call_{index}", name="Read") for index in range(call_count)
        ],
    )
    results = [
        MessageRecord(
            session_id=session.session_id,
            run_id="run_counts",
            turn_id="turn",
            role="tool_result",
            tool_result=ToolResultReference(tool_call_id=call.id, tool_name=call.name),
        )
        for call in assistant.tool_calls
    ]
    seed_tool_messages(store, assistant, results)
    statements = []
    connect = store._connect

    def trace_connection():
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", trace_connection)
    store.append_message(assistant)
    membership_queries = [sql for sql in statements if "message_call_references" in sql]
    assert len(membership_queries) == 2
    assert (
        store.load_run_message_counts(session.session_id, "run_counts").tool_call_count
        == call_count
    )
    store.append_messages(results)
    assert (
        store.load_run_message_counts(session.session_id, "run_counts").tool_call_count
        == call_count
    )
