from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ouro.core.errors import PersistenceError, ResourceNotFoundError
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files import index as files_index
from ouro.sessions.portable import PortableSession, SessionBundle
from tests.resource_fixtures import resource_record
from tests.session_storage import session_connection
from tests.sessions.test_tool_fact_storage import store as store


def counts(store, session_id):
    record = store.load_session_record(session_id)
    return record.run_count, record.resource_count


def test_counts_follow_native_membership_and_ignore_stale_session_values(store):
    session = SessionRecord()
    store.create_session(session)
    first = RunRecord(session_id=session.session_id, status="completed")
    store.save_run(first)
    store.save_run(first)
    second = RunRecord(session_id=session.session_id, status="completed")
    store.commit_session(
        SessionCommit(before=store.load_session_record(session.session_id), runs=(first, second))
    )
    reference = resource_record()
    store.attach_resource_reference(session.session_id, reference)
    store.attach_resource_reference(session.session_id, reference)
    store.save_session(
        session.model_copy(update={"title": "later title", "run_count": 999, "resource_count": 999})
    )
    assert counts(store, session.session_id) == (2, 1)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="withdraw")
    store.append_message(message)
    store.truncate_messages(
        session.session_id,
        before_message_id=message.message_id,
        expected_last_message_id=message.message_id,
    )
    assert counts(store, session.session_id) == (2, 1)
    store.remove_resource_reference(session.session_id, reference.resource_id)
    with pytest.raises(ResourceNotFoundError):
        store.remove_resource_reference(session.session_id, reference.resource_id)
    assert counts(store, session.session_id) == (2, 0)
    store.attach_resource_reference(session.session_id, reference)
    assert counts(store, session.session_id) == (2, 1)


@pytest.mark.parametrize("old_count", [4, 128])
def test_catalog_refresh_does_not_enumerate_old_runs_or_resources(store, monkeypatch, old_count):
    session = SessionRecord()
    store.create_session(session)
    for index in range(old_count):
        store.save_run(RunRecord(session_id=session.session_id, status="completed"))
        store.attach_resource_reference(
            session.session_id, resource_record(resource_id=f"res_{index:032x}")
        )
    message = MessageRecord.text(session_id=session.session_id, role="assistant", text="current")
    store.append_message(message)
    if isinstance(store, LocalSessionStore):
        scandir = os.scandir

        def no_old_directories(path):
            if {"runs", "resources"} & set(Path(path).parts):
                raise AssertionError("Catalog enumerated unrelated historical membership")
            return scandir(path)

        monkeypatch.setattr(os, "scandir", no_old_directories)
        assert store.refresh_catalog(session.session_id) is None
    else:
        with store.session_mutation(session.session_id):
            connection = store._transaction_connection()

            def authorize(action, table, column, *args):
                if action == sqlite3.SQLITE_FUNCTION and column == "count":
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "resource_references":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            try:
                assert store.refresh_catalog(session.session_id) is None
            finally:
                connection.set_authorizer(None)
    entry = store.read_catalog().sessions[0]
    assert (entry.message_count, entry.run_count, entry.resource_count) == (1, old_count, old_count)
    assert entry.last_message_preview == "current"


def test_snapshot_and_transfer_rebuild_counts_from_selected_membership(store, tmp_path):
    session = SessionRecord(run_count=999, resource_count=999)
    reference = resource_record()
    store.create_session_snapshot(
        session, [], [reference], resource_contents=((reference, b"fixture"),)
    )
    assert counts(store, session.session_id) == (0, 1)
    store.save_run(RunRecord(session_id=session.session_id, status="completed"))
    snapshot = store.load_session(session.session_id)
    bundle = SessionBundle(
        sessions=[PortableSession.from_snapshot(snapshot, resources=[reference])]
    )
    for target in (LocalSessionStore(tmp_path / "copy"), SQLiteSessionStore(tmp_path / "copy.db")):
        target.initialize()
        target.import_bundle(
            bundle, resource_contents={session.session_id: {reference.filename: b"fixture"}}
        )
        assert counts(target, session.session_id) == (1, 1)
        assert target.refresh_catalog(session.session_id) is None
        assert target.read_catalog().sessions[0].resource_count == 1


def test_files_catalog_head_and_latest_run_use_one_database_snapshot(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Before")
    run = RunRecord(session_id=session.session_id, run_id="run_catalog", status="running")
    store.create_session(session)
    store.save_run(run)
    session = store.load_session_record(session.session_id).model_copy(
        update={"last_run_id": "run_catalog"}
    )
    store.save_session(session)
    read_run = files_index.read_run

    def publish_then_read(connection, session_id, run_id):
        with session_connection(store, session_id) as writer:
            writer.execute(
                "UPDATE sessions SET payload = ? WHERE session_id = ?",
                (session.model_copy(update={"title": "After"}).model_dump_json(), session_id),
            )
            writer.execute(
                "UPDATE runs SET payload = ? WHERE session_id = ? AND run_id = ?",
                (
                    run.model_copy(update={"status": "completed"}).model_dump_json(),
                    session_id,
                    run_id,
                ),
            )
        return read_run(connection, session_id, run_id)

    monkeypatch.setattr(files_index, "read_run", publish_then_read)
    assert store.refresh_catalog(session.session_id) is None
    entry = store.read_catalog().sessions[0]
    assert (entry.title, entry.status) == ("Before", "active")
    assert store.load_session_record(session.session_id).title == "After"
    assert store.load_run(session.session_id, run.run_id).status == "completed"


@pytest.mark.parametrize("operation", ["run", "session_run", "attach", "remove"])
def test_counts_share_native_record_commit_and_recovery(store, operation):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="completed")
    reference = resource_record()
    if operation == "remove":
        store.attach_resource_reference(session.session_id, reference)
    before = counts(store, session.session_id)

    def publish(repository):
        if operation == "run":
            repository.save_run(run)
        elif operation == "session_run":
            repository.commit_session(
                SessionCommit(
                    before=repository.load_session_record(session.session_id), runs=(run,)
                )
            )
        elif operation == "attach":
            repository.attach_resource_reference(session.session_id, reference)
        else:
            repository.remove_resource_reference(session.session_id, reference.resource_id)

    table = "runs" if operation.endswith("run") else "resource_references"
    action = "DELETE" if operation == "remove" else "INSERT"
    with session_connection(store, session.session_id) as connection:
        connection.execute(
            f"CREATE TRIGGER fail_counter AFTER {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'counter publication interrupted'); END"
        )
    with pytest.raises(
        (PersistenceError, sqlite3.IntegrityError), match="counter publication interrupted"
    ):
        publish(store)
    cold = (
        LocalSessionStore(store.root)
        if isinstance(store, LocalSessionStore)
        else SQLiteSessionStore(store.database)
    )
    cold.initialize()
    assert counts(cold, session.session_id) == before
    with session_connection(store, session.session_id) as connection:
        connection.execute("DROP TRIGGER fail_counter")
    publish(cold)
    assert counts(cold, session.session_id) == (
        (1, 0) if operation.endswith("run") else (0, int(operation == "attach"))
    )
    assert cold.refresh_catalog(session.session_id) is None
    entry = cold.read_catalog().sessions[0]
    assert (entry.run_count, entry.resource_count) == counts(cold, session.session_id)
