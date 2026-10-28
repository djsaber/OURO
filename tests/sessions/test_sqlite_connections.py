import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ouro.core.events import EventRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.sqlite_connections import ExecutionConnections


def test_execution_owners_reuse_connections_and_release_the_last_handle(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    sessions = [SessionRecord(), SessionRecord()]
    for session in sessions:
        store.create_session(session)
    first = store.acquire_session_lock(sessions[0].session_id, "run_first")
    second = store.acquire_session_lock(sessions[1].session_id, "run_second")
    assert first is not None and second is not None
    opened = []
    connect = sqlite3.connect

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture)
    try:
        store.load_session(sessions[0].session_id)
        store.save_session(sessions[0])
        first.release()
        store.load_session(sessions[1].session_id)
        assert len(opened) == 1
        assert opened[0].execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        first.release()
        second.release()
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    store.load_session(sessions[0].session_id)
    assert len(opened) == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[1].execute("SELECT 1")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_last_observer_closes_wal_after_execution_owner_releases(tmp_path, backend):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "ouro.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    owner = store.acquire_session_lock(session.session_id, "run_writer")
    assert owner is not None
    reader = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "ouro.db")
    )
    wal = (
        tmp_path / "sessions" / session.session_id / "session-data.sqlite3-wal"
        if backend == "files"
        else tmp_path / "ouro.db-wal"
    )
    try:
        store.append_ordered_event(
            EventRecord(session_id=session.session_id, event_type="agent_closed")
        )
        with reader.session_read(session.session_id):
            assert reader.load_session_record(session.session_id) == session
            owner.release()
            assert wal.stat().st_size > 0
        assert not wal.exists()
    finally:
        owner.release()


def test_returned_connection_does_not_retain_an_uncommitted_transaction(tmp_path):
    connections = ExecutionConnections(tmp_path / "ouro.db")
    connections.retain()
    connection = connections.borrow()
    connection.execute("CREATE TABLE facts (value TEXT)")
    connection.execute("INSERT INTO facts VALUES ('uncommitted')")
    connections.return_connection(connection)
    reused = connections.borrow()
    assert reused is connection
    assert not reused.in_transaction
    assert reused.execute("SELECT * FROM facts").fetchall() == []
    connections.release()
    # A still-borrowed handle is closed on return, not beneath its reader.
    assert reused.execute("SELECT 1").fetchone() == (1,)
    connections.return_connection(reused)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reused.execute("SELECT 1")


@pytest.mark.parametrize("executing", [False, True])
def test_files_catalog_reads_share_one_connection_and_execution_owner(
    tmp_path, monkeypatch, executing
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, run_id="run_catalog"))
    session = store.load_session_record(session.session_id).model_copy(
        update={"last_run_id": "run_catalog"}
    )
    store.save_session(session)
    owner = store.acquire_session_lock(session.session_id, "run_catalog") if executing else None
    opened = []
    connect = sqlite3.connect

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture)
    try:
        for _ in range(2):
            assert store.refresh_catalog(session.session_id) is None
        assert len(opened) == (1 if executing else 2)
        entry = store.read_catalog().sessions[0]
        assert entry.status == "active" and entry.run_count == 1
    finally:
        if owner is not None:
            owner.release()
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_concurrent_borrows_are_exclusive_and_can_move_between_threads(tmp_path):
    connections = ExecutionConnections(tmp_path / "ouro.db")
    connections.retain()
    barrier = Barrier(2)

    def query():
        connection = connections.borrow()
        try:
            barrier.wait(timeout=5)
            assert connection.execute("SELECT 1").fetchone() == (1,)
            return connection
        finally:
            connections.return_connection(connection)

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            handles = list(workers.map(lambda _: query(), range(2)))
        assert handles[0] is not handles[1]
        borrowed = connections.borrow()
        assert borrowed in handles
        assert borrowed.execute("SELECT 1").fetchone() == (1,)
        connections.return_connection(borrowed)
    finally:
        connections.release()
    for connection in handles:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
