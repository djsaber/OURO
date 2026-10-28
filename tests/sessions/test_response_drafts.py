from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

import ouro.sessions.backends.files.commits as file_commits
import ouro.sessions.backends.files.store as file_store
from ouro.core.base import utc_now
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.request_manifest import RequestManifest
from ouro.core.response_drafts import ResponseDraftChunk
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def drafting(tmp_path, request):
    store = request.param(
        tmp_path / ("sessions.db" if request.param is SQLiteSessionStore else "files")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    header = ModelRequestHeader(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        started_at=utc_now(),
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
    )
    run.current_request_id = header.context_id
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            after=session.model_copy(update={"active_run_id": run.run_id, "status": "running"}),
            runs=(run,),
            request_header=header,
            request_manifest=RequestManifestRecord(
                context_id=header.context_id, manifest=RequestManifest()
            ),
        )
    )
    return store, run, header


def test_draft_is_ordered_current_request_content_and_supports_incremental_reads(drafting):
    store, run, header = drafting
    chunks = [
        ResponseDraftChunk(channel="reasoning", content="想"),
        ResponseDraftChunk(channel="text", content="答"),
    ]
    for chunk in chunks:
        store.append_response_draft(run.session_id, header.context_id, chunk)
    page = store.load_response_draft(run.session_id, header.context_id, limit=1)
    assert [item.content for item in page.chunks] == [chunks[0].content]
    tail = store.load_response_draft(run.session_id, header.context_id, after_cursor=page.cursor)
    assert [item.content for item in tail.chunks] == [chunks[1].content]
    assert (
        store.load_response_draft(
            run.session_id, header.context_id, after_cursor=tail.cursor
        ).chunks
        == ()
    )
    assert store.load_response_draft(run.session_id, "ctx_other") is None
    with pytest.raises(PersistenceError, match="request"):
        store.append_response_draft(run.session_id, "ctx_other", chunks[0])


@pytest.mark.parametrize("partial", [False, True])
def test_response_commit_closes_draft_atomically_and_rejects_late_fragments(
    drafting, monkeypatch, partial
):
    store, run, header = drafting
    chunk = ResponseDraftChunk(channel="text", content="answer")
    store.append_response_draft(run.session_id, header.context_id, chunk)
    message = MessageRecord.text(
        session_id=run.session_id,
        run_id=run.run_id,
        turn_id=header.turn_id,
        role="assistant",
        text="answer",
    )
    message.metadata["incomplete"] = partial
    proposal = SessionCommit(
        before=store.load_session_record(run.session_id),
        steps=(SessionCommitStep(message=message),),
    )
    if isinstance(store, LocalSessionStore):

        def fail(connection, value):
            raise OSError("response interruption")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_header", fail)
            with pytest.raises(PersistenceError, match="response interruption"):
                store.commit_session(proposal)
        with pytest.raises(SessionRecoveryRequiredError):
            store.load_response_draft(run.session_id, header.context_id)
        with store.session_mutation(run.session_id):
            pass
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_response BEFORE UPDATE ON request_headers "
                "BEGIN SELECT RAISE(ABORT, 'response interruption'); END"
            )
        with pytest.raises(PersistenceError, match="response interruption"):
            store.commit_session(proposal)
        assert [
            item.content
            for item in store.load_response_draft(run.session_id, header.context_id).chunks
        ] == [chunk.content]
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER reject_response")
        store.commit_session(proposal)
    database = (
        store.sessions_dir / run.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM response_streams").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM response_drafts").fetchone()[0] == 0
    assert store.load_response_draft(run.session_id, header.context_id) is None
    assert (
        store.load_latest_context_header(run.session_id).response_message_id == message.message_id
    )
    with pytest.raises(PersistenceError, match="request"):
        store.append_response_draft(run.session_id, header.context_id, chunk)


def test_request_replacement_clears_old_draft_and_rejects_old_cursor_identity(drafting):
    store, run, header = drafting
    store.append_response_draft(
        run.session_id, header.context_id, ResponseDraftChunk(channel="text", content="old")
    )
    replacement = header.model_copy(update={"context_id": "ctx_new", "turn_id": "new"})
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(run.session_id),
            runs=(run.model_copy(update={"current_request_id": replacement.context_id}),),
            request_header=replacement,
            request_manifest=RequestManifestRecord(
                context_id=replacement.context_id, manifest=RequestManifest()
            ),
        )
    )
    assert store.load_response_draft(run.session_id, header.context_id) is None
    assert store.load_response_draft(run.session_id, replacement.context_id).chunks == ()
    with pytest.raises(PersistenceError, match="request"):
        store.append_response_draft(
            run.session_id, header.context_id, ResponseDraftChunk(channel="text", content="late")
        )


def test_start_opens_draft_and_settlement_keeps_the_accepted_prefix(drafting):
    store, run, previous = drafting
    header = previous.model_copy(update={"context_id": "ctx_prepared", "started_at": None})
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(run.session_id),
            request_header=header,
            request_manifest=RequestManifestRecord(
                context_id=header.context_id, manifest=RequestManifest()
            ),
        )
    )
    chunk = ResponseDraftChunk(channel="text", content="prefix")
    with pytest.raises(PersistenceError, match="request"):
        store.append_response_draft(run.session_id, header.context_id, chunk)
    header.started_at = utc_now()
    store.commit_session(
        SessionCommit(before=store.load_session_record(run.session_id), request_header=header)
    )
    cursor = store.append_response_draft(run.session_id, header.context_id, chunk)
    store.settle_request_header(
        run.session_id,
        header.context_id,
        settled_at=utc_now(),
        provider_done=True,
        usage_entry_id=None,
    )
    page = store.load_response_draft(run.session_id, header.context_id)
    assert page.cursor == cursor
    assert [item.content for item in page.chunks] == ["prefix"]


@pytest.mark.parametrize("drafting", [LocalSessionStore], indirect=True)
def test_files_stream_uses_request_owned_target_without_rereading_header(drafting, monkeypatch):
    store, run, header = drafting

    def forbid_header(*args):
        pytest.fail("stream IO reloaded the current request")

    monkeypatch.setattr(file_store, "read_latest_header", forbid_header)
    for _ in range(32):
        store.append_response_draft(
            run.session_id, header.context_id, ResponseDraftChunk(channel="text", content="part")
        )
    assert (
        "".join(
            item.content
            for item in store.load_response_draft(run.session_id, header.context_id).chunks
        )
        == "part" * 32
    )


@pytest.mark.parametrize("drafting", [LocalSessionStore], indirect=True)
def test_files_draft_reads_only_committed_fragments(drafting):
    store, run, header = drafting
    chunk = ResponseDraftChunk(channel="text", content="complete")
    store.append_response_draft(run.session_id, header.context_id, chunk)
    database = store.sessions_dir / run.session_id / "session-data.sqlite3"
    with closing(sqlite3.connect(database)) as writer:
        writer.execute(
            "INSERT INTO response_drafts VALUES (?, 2, 'text', 'uncommitted')", (run.session_id,)
        )
        assert [
            item.content
            for item in store.load_response_draft(run.session_id, header.context_id).chunks
        ] == [chunk.content]
        writer.rollback()
        writer.execute(
            "INSERT INTO response_drafts VALUES (?, 2, 'invalid', 'invalid')", (run.session_id,)
        )
        writer.commit()
    with pytest.raises(PersistenceError):
        store.load_response_draft(run.session_id, header.context_id)


def test_observation_snapshot_does_not_block_append_or_change_its_draft_prefix(drafting):
    store, run, header = drafting
    first = ResponseDraftChunk(channel="text", content="first")
    second = ResponseDraftChunk(channel="text", content="second")
    store.append_response_draft(run.session_id, header.context_id, first)
    entered, release = Event(), Event()

    def observe():
        with store.session_read(run.session_id):
            before = store.load_response_draft(run.session_id, header.context_id)
            entered.set()
            assert release.wait(timeout=5)
            after = store.load_response_draft(run.session_id, header.context_id)
            assert before == after
        return before

    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(observe)
        try:
            assert entered.wait(timeout=5)
            writer = workers.submit(
                store.append_response_draft, run.session_id, header.context_id, second
            )
            writer.result(timeout=5)
        finally:
            release.set()
        assert [item.content for item in reader.result().chunks] == [first.content]
    assert [
        item.content for item in store.load_response_draft(run.session_id, header.context_id).chunks
    ] == [first.content, second.content]


@pytest.mark.parametrize("drafting", [LocalSessionStore], indirect=True)
def test_files_draft_connections_end_with_execution_ownership(drafting, monkeypatch):
    store, run, header = drafting
    owner = store.acquire_session_lock(run.session_id, run.run_id)
    assert owner is not None
    opened = []
    connect = sqlite3.connect

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture)
    with owner:
        for _ in range(16):
            store.append_response_draft(
                run.session_id, header.context_id, ResponseDraftChunk(channel="text", content="x")
            )
            assert store.load_session_record(run.session_id).session_id == run.session_id
            assert store.load_run(run.session_id, run.run_id).run_id == run.run_id
        assert len(opened) == 1
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert not store._connections


def test_response_publication_reclaims_or_reuses_the_draft_allocation(drafting):
    store, run, header = drafting
    database = (
        store.sessions_dir / run.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    chunk = ResponseDraftChunk(channel="text", content="x" * 16384)
    owner = store.acquire_session_lock(run.session_id, run.run_id)
    assert owner is not None
    with owner:
        for _ in range(64):
            store.append_response_draft(run.session_id, header.context_id, chunk)
        with closing(sqlite3.connect(database)) as connection:
            active_pages = connection.execute("PRAGMA page_count").fetchone()[0]
        message = MessageRecord.text(
            session_id=run.session_id,
            run_id=run.run_id,
            turn_id=header.turn_id,
            role="assistant",
            text=chunk.content * 64,
        )
        store.commit_session(
            SessionCommit(
                before=store.load_session_record(run.session_id),
                steps=(SessionCommitStep(message=message),),
            )
        )
    with closing(sqlite3.connect(database)) as connection:
        completed_pages = connection.execute("PRAGMA page_count").fetchone()[0]
        if isinstance(store, LocalSessionStore):
            assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
            assert completed_pages < active_pages // 2
        else:
            # One MiB of final text reuses the retired allocation; only bounded
            # message/header/index metadata may grow beyond that high-water mark.
            assert completed_pages <= active_pages + 16
        assert connection.execute("SELECT COUNT(*) FROM response_drafts").fetchone()[0] == 0
    assert store.load_message(run.session_id, message.message_id).text_content == chunk.content * 64
