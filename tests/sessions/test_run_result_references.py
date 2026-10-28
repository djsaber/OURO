from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord, ToolMessageReference
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.message_locations import FileMessageIndex
from ouro.sessions.portable import PortableSession, SessionBundle
from tests.sessions.test_tool_fact_storage import commit, facts
from tests.sessions.test_tool_fact_storage import store as store


def history(store, *, count=4):
    session, run, call, start, outcome = facts(store)
    outcome = outcome.model_copy(
        update={
            "value": outcome.value.model_copy(
                update={
                    "content": "observed " * 131072,
                    "resource_refs": ["resource_tool", "resource_shared"],
                }
            )
        }
    )
    commit(store, session, calls=(call,), starts=(start,))
    store.commit_tool_outcome(outcome)
    messages = [
        MessageRecord.text(
            session_id=session.session_id, run_id=run.run_id, role="user", text="input " * 8192
        )
        for _ in range(count)
    ]
    for index, message in enumerate(messages):
        message.metadata = {
            "session_input_source": "delegation" if index else "host",
            "delegation_id": "delegation_selected" if index != 1 else "other",
        }
        message.resource_refs = ["resource_input"]
    assistant = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="assistant", text="earlier " * 8192
    )
    assistant.resource_refs = ["resource_shared"]
    result = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=call.turn_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
    )
    # The publication reference is resolved from the immutable Outcome, not
    # copied from the caller's optional materialized message view.
    assert not result.resource_refs
    last = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=call.turn_id,
        role="assistant",
        call_refs=(call.identity,),
        resource_refs=["resource_last"],
    )
    messages.extend((assistant, result, last))
    store.append_messages(messages)
    return session, run, messages


def references(store, session, run):
    return store.load_run_result_references(
        session.session_id, run.run_id, delegation_id="delegation_selected"
    )


@pytest.mark.parametrize("old_messages", [4, 128])
def test_result_collection_reads_only_membership_at_both_scales(store, monkeypatch, old_messages):
    session, run, messages = history(store, count=old_messages)
    original_open = Path.open

    def no_bodies(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if "r" in mode and (
            path.name == "messages.jsonl" or "calls" in path.parts or "resources" in path.parts
        ):
            raise AssertionError("result collection opened canonical content")
        return original_open(path, *args, **kwargs)

    if isinstance(store, LocalSessionStore):
        monkeypatch.setattr(Path, "open", no_bodies)
    with store.session_read(session.session_id) as connection:
        if isinstance(store, SQLiteSessionStore):
            plans = connection.execute(
                "EXPLAIN QUERY PLAN SELECT message_id, role, delegation_input_id, resource_refs "
                "FROM messages WHERE session_id = ? AND run_id = ? ORDER BY row_id",
                (session.session_id, run.run_id),
            ).fetchall()
            assert any("COVERING INDEX messages_run_result_references" in row[3] for row in plans)

            def authorize(action, table, column, *args):
                if (
                    action == sqlite3.SQLITE_READ
                    and table
                    in {"messages", "tool_calls", "tool_outcomes", "inputs", "resource_references"}
                    and column == "payload"
                ):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
        try:
            found = references(store, session, run)
            assert found.initial_message_id == messages[2].message_id
            assert found.result_message_id == messages[-1].message_id
            assert found.resource_refs == ("resource_shared", "resource_tool", "resource_last")
        finally:
            if isinstance(store, SQLiteSessionStore):
                connection.set_authorizer(None)


def test_result_membership_survives_transfer_and_tracks_withdrawal(store, tmp_path):
    session, run, messages = history(store)
    expected = references(store, session, run)
    store.save_run(run.model_copy(update={"status": "completed"}))
    current = store.load_session_record(session.session_id)
    store.save_session(current.model_copy(update={"active_run_id": None, "status": "idle"}))
    bundle = SessionBundle(
        sessions=[PortableSession.from_snapshot(store.load_session(session.session_id))]
    )
    for target in (LocalSessionStore(tmp_path / "copy"), SQLiteSessionStore(tmp_path / "copy.db")):
        target.initialize()
        target.import_bundle(bundle)
        assert references(target, session, run) == expected
        target.truncate_messages(
            session.session_id,
            before_message_id=messages[-2].message_id,
            expected_last_message_id=messages[-1].message_id,
        )
        found = references(target, session, run)
        assert found.initial_message_id == expected.initial_message_id
        assert found.result_message_id == messages[-3].message_id
        assert found.resource_refs == ("resource_shared",)
        # The retained Outcome alone does not publish result resources.
        reference = messages[-2].outcome_ref
        assert target.load_tool_outcome(session.session_id, reference.identity, 1) is not None


def test_late_outcome_does_not_change_published_unknown_resource_membership(store):
    session, run, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    message = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id=call.turn_id,
        role="tool_result",
        resource_refs=["resource_feedback"],
        outcome_ref=ToolMessageReference(identity=call.identity, feedback_is_error=True),
    )
    store.append_message(message)
    before = references(store, session, run)
    store.commit_tool_outcome(
        outcome.model_copy(
            update={"value": outcome.value.model_copy(update={"resource_refs": ["resource_late"]})}
        )
    )
    assert references(store, session, run) == before
    assert before.resource_refs == ("resource_feedback",)


def test_result_membership_uses_native_publication_recovery(store, monkeypatch):
    session, run, messages = history(store)
    before = references(store, session, run)
    added = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="assistant", text="new result"
    )
    added.resource_refs = ["resource_new"]
    if isinstance(store, LocalSessionStore):
        apply_index = FileMessageIndex.apply

        def fail_after_lookup(owner, directory, references, removed, counts):
            apply_index(owner, directory, references, removed, counts)
            raise OSError("interrupted result membership")

        with monkeypatch.context() as patch:
            patch.setattr(FileMessageIndex, "apply", fail_after_lookup)
            with pytest.raises(PersistenceError, match="interrupted result membership"):
                store.append_message(added)
        cold = LocalSessionStore(store.root)
        cold.initialize()
        with pytest.raises(SessionRecoveryRequiredError):
            references(cold, session, run)
        with cold.session_mutation(session.session_id):
            pass
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_result AFTER INSERT ON messages "
                "BEGIN SELECT RAISE(ABORT, 'interrupted result membership'); END"
            )
        with pytest.raises(
            (PersistenceError, sqlite3.IntegrityError), match="interrupted result membership"
        ):
            store.append_message(added)
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        assert references(cold, session, run) == before
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_result")
        cold.append_message(added)
    found = references(cold, session, run)
    assert found.initial_message_id == messages[2].message_id
    assert found.result_message_id == added.message_id
    assert found.resource_refs == (*before.resource_refs, "resource_new")
