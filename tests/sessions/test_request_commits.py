from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.core.context import ContextMetrics
from ouro.core.context_selection import ContextSelection, MessageBoundary
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.request_manifest import RequestManifest
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def prepared(tmp_path, request):
    store = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    source = MessageRecord.text(session_id=session.session_id, role="user", text="input")
    store.append_message(source)
    session = store.load_session_record(session.session_id)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    header = ModelRequestHeader(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
    )
    selection = ContextSelection(
        media_elided_through=MessageBoundary(message_id=source.message_id, sequence=1)
    )
    proposal = SessionCommit(
        before=session,
        after=session.model_copy(update={"context_selection": selection}),
        runs=(run.model_copy(update={"current_request_id": header.context_id}),),
        request_header=header,
        request_manifest=RequestManifestRecord(
            context_id=header.context_id,
            manifest=RequestManifest(system_prompt="unique request literal" * 100_000),
        ),
    )
    return store, run, proposal


def test_request_run_and_selection_share_native_commit_and_recovery(prepared, monkeypatch):
    store, run, proposal = prepared
    session_id = run.session_id
    if isinstance(store, LocalSessionStore):

        def interrupt(connection, model):
            raise OSError("injected request commit interruption")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_header", interrupt)
            with pytest.raises(PersistenceError, match="injected"):
                store.commit_session(proposal)
        directory = store.sessions_dir / session_id
        decision = (directory / "commit.json").read_bytes()
        assert len(decision) < 12_000
        assert b"unique request literal" not in decision
        for read in (store.load_session_record, store.load_latest_context_header):
            with pytest.raises(SessionRecoveryRequiredError):
                read(session_id)
        with store.session_mutation(session_id):
            pass
        assert not (directory / "commit.json").exists()
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_request BEFORE INSERT ON request_headers "
                "BEGIN SELECT RAISE(ABORT, 'injected request interruption'); END"
            )
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(proposal)
        assert store.load_run(session_id, run.run_id).current_request_id is None
        assert store.load_latest_request(session_id) is None
        assert store.load_session_record(session_id).context_selection == ContextSelection()
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER reject_request")
        store.commit_session(proposal)
    assert (
        store.load_session_record(session_id).context_selection == proposal.after.context_selection
    )
    assert (
        store.load_run(session_id, run.run_id).current_request_id
        == proposal.request_header.context_id
    )
    assert store.load_latest_request(session_id) == (
        proposal.request_header,
        proposal.request_manifest,
    )
    assert store.load_run(session_id, run.run_id).model_round_count == 0


def test_stale_request_header_cannot_move_run_to_replaced_request(prepared):
    store, run, proposal = prepared
    store.commit_session(proposal)
    before = store.load_session_record(run.session_id)
    replacement = proposal.model_copy(
        update={
            "operation_id": "op_replacement",
            "before": before,
            "after": None,
            "request_header": proposal.request_header.model_copy(update={"context_id": "ctx_new"}),
            "request_manifest": proposal.request_manifest.model_copy(
                update={"context_id": "ctx_new"}
            ),
            "runs": (run.model_copy(update={"current_request_id": "ctx_new"}),),
        }
    )
    store.commit_session(replacement)
    with pytest.raises(PersistenceError, match="does not match"):
        store.commit_session(
            SessionCommit(
                before=store.load_session_record(run.session_id),
                request_header=proposal.request_header,
                runs=(run.model_copy(update={"model_round_count": 1}),),
            )
        )
    assert store.load_run(run.session_id, run.run_id).current_request_id == "ctx_new"
    assert store.load_run(run.session_id, run.run_id).model_round_count == 0


@pytest.mark.parametrize("partial", [False, True])
def test_response_message_and_request_header_share_commit(prepared, monkeypatch, partial):
    store, run, proposal = prepared
    store.commit_session(proposal)
    session_id = run.session_id
    message = MessageRecord.text(
        session_id=session_id, run_id=run.run_id, turn_id="turn", role="assistant", text="output"
    ).model_copy(update={"metadata": {"incomplete": partial}})
    response = SessionCommit(
        before=store.load_session_record(session_id),
        runs=(store.load_run(session_id, run.run_id).model_copy(update={"status": "completed"}),),
        steps=(SessionCommitStep(message=message),),
    )
    if isinstance(store, LocalSessionStore):

        def interrupt(connection, value):
            raise OSError("response header failure")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_header", interrupt)
            with pytest.raises(PersistenceError, match="response header failure"):
                store.commit_session(response)
        with pytest.raises(SessionRecoveryRequiredError):
            store.load_latest_context_header(session_id)
        with store.session_mutation(session_id):
            pass
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_response BEFORE UPDATE ON request_headers "
                "BEGIN SELECT RAISE(ABORT, 'response header failure'); END"
            )
        with pytest.raises(PersistenceError, match="response header failure"):
            store.commit_session(response)
        with pytest.raises(PersistenceError, match="message not found"):
            store.load_message(session_id, message.message_id)
        assert store.load_run(session_id, run.run_id).status == "running"
        assert store.load_latest_context_header(session_id).response_message_id is None
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER reject_response")
        store.commit_session(response)
    assert store.load_latest_context_header(session_id).response_message_id == message.message_id
    assert store.load_message(session_id, message.message_id).metadata["incomplete"] is partial
    assert store.load_run(session_id, run.run_id).status == "completed"
