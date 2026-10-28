from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.core.base import utc_now
from ouro.core.context import ContextMetrics, ProviderContext
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import SessionRecord
from ouro.local_locks import FileMutationLock
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.context_facts import save_context


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


def request_for(session_id, *, text="prepared input", context_id="ctx_first"):
    return ContextSnapshotRecord(
        session_id=session_id,
        context_id=context_id,
        run_id="run_test",
        turn_id="turn_test",
        model_round=1,
        provider_id="test",
        model_id="test",
        provider_context=ProviderContext(system_prompt=text),
        metrics=ContextMetrics(estimated_input_tokens=10, estimated_fixed_tokens=10),
    )


@pytest.mark.parametrize("size", [64, 4 * 1024 * 1024])
def test_completion_never_reads_or_updates_request_input(store, monkeypatch, size):
    session = SessionRecord()
    store.create_session(session)
    request = request_for(session.session_id, text="x" * size)
    save_context(store, request)
    completed_at = utc_now()
    with monkeypatch.context() as patch:
        if isinstance(store, LocalSessionStore):
            original = Path.open

            def guarded_open(path, *args, **kwargs):
                if path.parent.name == "request-content":
                    pytest.fail("completion accessed request input")
                return original(path, *args, **kwargs)

            patch.setattr(Path, "open", guarded_open)
        else:
            connect = store._connect

            def guarded_connect():
                connection = connect()

                def authorize(action, table, column, database, source):
                    if table == "latest_contexts" and (
                        column == "payload" or action != sqlite3.SQLITE_READ
                    ):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(authorize)
                return connection

            patch.setattr(store, "_connect", guarded_connect)
        completed = store.settle_request_header(
            session.session_id,
            request.context_id,
            settled_at=completed_at,
            provider_done=True,
            usage_entry_id=request.expected_usage_entry_id,
        )
        assert completed.provider_done_at == completed_at
        assert completed.usage_entry_id == request.expected_usage_entry_id
        assert store.load_latest_context_header(session.session_id) == completed
        assert (
            store.settle_request_header(
                session.session_id,
                request.context_id,
                settled_at=utc_now(),
                provider_done=True,
                usage_entry_id=request.expected_usage_entry_id,
            )
            == completed
        )
        with pytest.raises(PersistenceError, match="conflicts"):
            store.settle_request_header(
                session.session_id,
                request.context_id,
                settled_at=utc_now(),
                provider_done=True,
                usage_entry_id=None,
            )
    full = store.load_latest_context(session.session_id)
    assert full.provider_context == request.provider_context
    assert full.header() == completed


def test_late_completion_keeps_original_identity_without_replacing_current_input(store):
    session = SessionRecord()
    store.create_session(session)
    first = request_for(session.session_id)
    second = request_for(session.session_id, context_id="ctx_second", text="new request")
    save_context(store, first)
    arrived, release = Event(), Event()

    def late_completion():
        arrived.set()
        assert release.wait(10)
        return store.settle_request_header(
            session.session_id,
            first.context_id,
            settled_at=utc_now(),
            provider_done=True,
            usage_entry_id=first.expected_usage_entry_id,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(late_completion)
        try:
            assert arrived.wait(10)
            save_context(store, second)
        finally:
            release.set()
        completed = pending.result(timeout=10)
        assert completed.usage_entry_id == first.expected_usage_entry_id
        assert store.load_request_header(session.session_id, first.context_id) == completed
    assert store.load_latest_context(session.session_id) == second


def test_preparation_cannot_overwrite_an_existing_request_identity(store):
    session = SessionRecord()
    store.create_session(session)
    first = request_for(session.session_id)
    save_context(store, first)
    with pytest.raises(PersistenceError, match="already published"):
        save_context(
            store,
            first.model_copy(
                update={"provider_context": ProviderContext(system_prompt="different input")}
            ),
        )
    assert store.load_latest_context(session.session_id) == first


def test_file_input_remains_protected_until_read_materialization(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    first = request_for(session.session_id)
    save_context(store, first)
    entered, release = Event(), Event()
    original = Path.open

    def paused_open(path, *args, **kwargs):
        if path.parent.name == "request-content":
            entered.set()
            assert release.wait(10)
        return original(path, *args, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as patch:
        patch.setattr(Path, "open", paused_open)
        pending = pool.submit(store.load_latest_context, session.session_id)
        try:
            assert entered.wait(10)
            lock = FileMutationLock(store._mutation_lock_path(session.session_id))
            try:
                # A nonblocking attempt tests ownership, not machine speed.
                assert not lock.acquire(timeout=0)
            finally:
                lock.release()
        finally:
            release.set()
        assert pending.result(timeout=10) == first
    save_context(store, request_for(session.session_id, context_id="ctx_second"))
    assert store.load_latest_context_header(session.session_id).context_id == "ctx_second"


def test_missing_usage_stays_unknown_and_withdrawal_removes_input(store):
    session = SessionRecord()
    store.create_session(session)
    request = request_for(session.session_id)
    save_context(store, request)
    header = store.settle_request_header(
        session.session_id,
        request.context_id,
        settled_at=utc_now(),
        provider_done=True,
        usage_entry_id=None,
    )
    assert header.provider_done_at is not None
    assert header.usage_entry_id is None
    appended = store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(
                    message=MessageRecord.text(
                        session_id=session.session_id, role="assistant", text="response"
                    )
                ),
            ),
        )
    )
    store.commit_session(
        SessionCommit(
            before=appended.session,
            before_message_id=appended.messages[0].message_id,
        )
    )
    assert store.load_latest_context(session.session_id) is None
    assert (
        store.settle_request_header(
            session.session_id,
            request.context_id,
            settled_at=utc_now(),
            provider_done=True,
            usage_entry_id=None,
        )
        == header
    )
    if isinstance(store, LocalSessionStore):
        assert list((store.sessions_dir / session.session_id / "request-content").iterdir()) == []
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            assert connection.execute("SELECT count(*) FROM latest_contexts").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM request_headers").fetchone() == (1,)


def test_replacement_keeps_only_latest_input_and_materialized_read_survives(store):
    session = SessionRecord()
    store.create_session(session)
    first = request_for(session.session_id)
    save_context(store, first)
    retained_read = store.load_latest_context(session.session_id)
    for index in range(20):
        request = request_for(session.session_id, context_id=f"ctx_{index}", text=str(index))
        save_context(store, request)
    assert retained_read == first
    assert store.load_latest_context(session.session_id) == request
    if isinstance(store, LocalSessionStore):
        assert [
            p.name for p in (store.sessions_dir / session.session_id / "request-content").iterdir()
        ] == ["ctx_19.json"]
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            assert connection.execute("SELECT count(*) FROM latest_contexts").fetchone() == (1,)
            assert connection.execute("SELECT count(*) FROM request_headers").fetchone() == (21,)

    assert store.load_request_header(session.session_id, first.context_id) == first.header()


@pytest.mark.parametrize("failure", ["decision", "header", "cleanup", "finish"])
def test_file_replacement_recovers_only_its_local_decision(tmp_path, monkeypatch, failure):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    first = request_for(session.session_id)
    second = request_for(session.session_id, context_id="ctx_second")
    save_context(store, first)
    directory = store.sessions_dir / session.session_id
    write = file_commits.write_model_atomic
    write_header = file_commits.write_header
    unlink = Path.unlink

    def failed_write(path, model):
        if failure == "decision" and path.name == "commit.json":
            raise OSError("injected context failure")
        return write(path, model)

    def failed_header(connection, header):
        if failure == "header":
            raise OSError("injected context failure")
        return write_header(connection, header)

    def failed_unlink(path, *args, **kwargs):
        if (failure == "cleanup" and path.name == "ctx_first.json") or (
            failure == "finish" and path.name == "commit.json"
        ):
            raise OSError("injected context failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_model_atomic", failed_write)
        patch.setattr(file_commits, "write_header", failed_header)
        patch.setattr(Path, "unlink", failed_unlink)
        with pytest.raises(PersistenceError, match="injected"):
            save_context(store, second)
    if failure == "decision":
        assert store.load_latest_context(session.session_id) == first
        assert not (directory / "commit.json").exists()
        return  # The unpublished orphan is outside normal recovery's scan boundary.
    decision = (directory / "commit.json").read_bytes()
    for read in (store.load_latest_context, store.load_latest_context_header):
        with pytest.raises(SessionRecoveryRequiredError):
            read(session.session_id)
        assert (directory / "commit.json").read_bytes() == decision
    reopened = LocalSessionStore(tmp_path)
    with reopened.session_mutation(session.session_id):
        pass
    assert reopened.load_latest_context(session.session_id) == second
    assert not (directory / "commit.json").exists()
    assert [path.name for path in (directory / "request-content").iterdir()] == ["ctx_second.json"]


def test_sqlite_replacement_rolls_back_header_and_input_together(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    first = request_for(session.session_id)
    save_context(store, first)
    with closing(sqlite3.connect(store.database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_header BEFORE INSERT ON request_headers "
            "BEGIN SELECT RAISE(ABORT, 'injected context failure'); END"
        )
    with pytest.raises((sqlite3.DatabaseError, PersistenceError), match="injected"):
        save_context(store, request_for(session.session_id, context_id="ctx_second"))
    assert store.load_latest_context(session.session_id) == first
