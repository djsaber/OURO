from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

from ouro.application.harness import capture_session_harness
from ouro.core.context import ContextBuilder
from ouro.core.context_budget import ContextBudget
from ouro.core.context_snapshots import ContextSnapshotRecord, context_view_digest
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import (
    SessionCommit,
    SessionCommitConflict,
    SessionCommitStep,
)
from ouro.core.session_controls import SessionControlActivation, SessionControlState
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore, file_commits, sqlite_commits


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


def state(session_id: str, value: object) -> HarnessState:
    return HarnessState(
        states=(
            SessionExtensionStateRecord(
                session_id=session_id,
                owner_id="example.state",
                state_schema_version=1,
                payload=value,
            ),
        )
    )


def message(session_id: str, text: str) -> MessageRecord:
    return MessageRecord.text(session_id=session_id, role="assistant", text=text)


@pytest.mark.parametrize("change", ["harness", "undo"])
def test_request_context_survives_harness_changes_but_not_history_withdrawal(store, change):
    session = SessionRecord()
    store.create_session(session)
    view = ContextBuilder().build(
        messages=[], tools=[], budget=ContextBudget(context_window=10000, max_output_tokens=1000)
    )
    context = ContextSnapshotRecord(
        session_id=session.session_id,
        run_id="run",
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        metrics=view.metrics,
        context_digest=context_view_digest(view),
    )
    store.save_latest_context(context)
    appended = store.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(message=message(session.session_id, "response")),),
        )
    )
    assert store.load_latest_context(session.session_id) == context
    request = (
        SessionCommit(
            before=appended.session,
            steps=(SessionCommitStep(harness=state(session.session_id, "changed")),),
        )
        if change == "harness"
        else SessionCommit(
            before=appended.session, before_message_id=appended.messages[0].message_id
        )
    )
    store.commit_session(request)
    assert store.load_latest_context(session.session_id) == (
        context if change == "harness" else None
    )


def test_file_session_enumeration_excludes_unpublished_creation(tmp_path, monkeypatch):
    import ouro.sessions.local_files as files

    store = LocalSessionStore(tmp_path)
    store.initialize()
    publish = files.replace_path_with_retry
    observed = []

    def inspect(source, destination):
        if source.parent == store.sessions_dir and source != destination:
            assert (source / "session.json").exists()
            observed.append(store.list_sessions())
        return publish(source, destination)

    monkeypatch.setattr(files, "replace_path_with_retry", inspect)
    session = SessionRecord()
    store.create_session(session)
    assert observed == [[]]
    assert store.list_sessions() == [store.load_session_record(session.session_id)]


def test_ordered_message_boundaries_restore_the_exact_before_state(store):
    session = SessionRecord()
    store.create_session(session)
    event = EventRecord(session_id=session.session_id, event_type="operation_finished")
    request = SessionCommit(
        before=store.load_session_record(session.session_id),
        steps=(
            SessionCommitStep(message=message(session.session_id, "input")),
            SessionCommitStep(
                message=message(session.session_id, "first"), harness=state(session.session_id, 1)
            ),
            SessionCommitStep(
                message=message(session.session_id, "second"), harness=state(session.session_id, 2)
            ),
        ),
        events=(event,),
    )
    committed = store.commit_session(request)
    first, second, third = committed.messages
    assert first.before_harness_version == session.harness_version
    assert second.before_harness_version == session.harness_version
    assert third.before_harness_version != session.harness_version
    assert store.load_harness(session.session_id).state.states[0].payload == 2
    assert store.load_session_record(session.session_id).message_count == 3

    undo = SessionCommit(before=committed.session, before_message_id=third.message_id)
    restored = store.commit_session(undo)
    assert store.load_harness(session.session_id).state.states[0].payload == 1
    assert restored.session.history_epoch == 1
    assert store.load_messages_tail(session.session_id) == [second, first]
    assert store.load_commit(session.session_id, undo.operation_id) == restored
    assert store.commit_session(undo) == restored
    assert store.load_session(session.session_id).events == list(committed.events)


def test_unchanged_business_value_reuses_checkpoint(store):
    session = SessionRecord()
    store.create_session(session)
    value = state(session.session_id, {"items": [1]})
    first = store.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(harness=value),),
        )
    )
    changed_timestamp = value.model_copy(deep=True)
    changed_timestamp.states[0].updated_at += timedelta(seconds=1)
    second = store.commit_session(
        SessionCommit(
            before=first.session,
            steps=(SessionCommitStep(harness=changed_timestamp),),
        )
    )
    assert second.session.harness_version == first.session.harness_version
    assert store.load_harness(session.session_id).state == value


def test_state_and_epoch_conflicts_do_not_append_effects(store):
    session = SessionRecord()
    store.create_session(session)
    result = store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(
                    message=message(session.session_id, "first"),
                    harness=state(session.session_id, 1),
                ),
            ),
        )
    )
    with pytest.raises(SessionCommitConflict, match="Harness state changed"):
        store.commit_session(
            SessionCommit(
                before=session, steps=(SessionCommitStep(harness=state(session.session_id, 2)),)
            )
        )
    restored = store.commit_session(
        SessionCommit(
            before=result.session,
            before_message_id=result.messages[0].message_id,
        )
    )
    assert restored.session.harness_version == session.harness_version
    with pytest.raises(SessionCommitConflict, match="history changed"):
        store.commit_session(
            SessionCommit(
                before=session,
                steps=(SessionCommitStep(message=message(session.session_id, "late")),),
            )
        )
    assert store.load_messages_tail(session.session_id) == []


def test_operation_identity_cannot_be_reused_for_another_proposal(store):
    session = SessionRecord()
    store.create_session(session)
    request = SessionCommit(
        before=session, steps=(SessionCommitStep(harness=state(session.session_id, 1)),)
    )
    store.commit_session(request)
    with pytest.raises(SessionCommitConflict, match="identity was reused"):
        store.commit_session(
            request.model_copy(
                update={"steps": (SessionCommitStep(harness=state(session.session_id, 2)),)}
            )
        )
    assert store.load_harness(session.session_id).state.states[0].payload == 1


def test_unrelated_session_fields_are_preserved_during_commit(store):
    session = SessionRecord()
    store.create_session(session)
    before = store.load_session_record(session.session_id)
    store.save_session(before.model_copy(update={"title": "renamed"}))
    result = store.commit_session(
        SessionCommit(
            before=before, steps=(SessionCommitStep(harness=state(session.session_id, 1)),)
        )
    )
    assert result.session.title == "renamed"


def test_run_event_queue_and_state_share_one_decision(store):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="waiting_user")
    after = session.model_copy(update={"metadata": {"pending_input": "input_1"}})
    result = store.commit_session(
        SessionCommit(
            before=session,
            after=after,
            runs=(run,),
            steps=(SessionCommitStep(harness=state(session.session_id, "waiting")),),
            events=(
                EventRecord(session_id=session.session_id, run_id=run.run_id, event_type="waiting"),
            ),
        )
    )
    assert store.load_run(session.session_id, run.run_id) == run
    assert store.load_session_record(session.session_id).metadata == after.metadata
    assert store.load_session(session.session_id).events == list(result.events)
    assert store.load_harness(session.session_id).state.states[0].payload == "waiting"


@pytest.mark.parametrize("failure_point", ["checkpoint", "decision", "receipt"])
def test_file_decision_recovers_without_reexecuting_operation(tmp_path, monkeypatch, failure_point):
    from ouro.sessions import file_commits

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title_source="placeholder")
    store.create_session(session)
    write = file_commits.write_model_atomic

    def fail(path, value):
        fail_here = (
            (failure_point == "checkpoint" and path.parent.name == "harness")
            or (failure_point == "decision" and path.name == "commit.json")
            or (failure_point == "receipt" and path.parent.name == "operations")
        )
        if fail_here:
            raise OSError("injected persistence failure")
        write(path, value)

    request = SessionCommit(
        before=session,
        steps=(
            SessionCommitStep(
                message=MessageRecord.text(
                    session_id=session.session_id, role="user", text="First topic"
                ),
                harness=state(session.session_id, 1),
            ),
        ),
        events=(EventRecord(session_id=session.session_id, event_type="committed"),),
    )
    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_model_atomic", fail)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)
    reopened = LocalSessionStore(tmp_path)
    reopened.initialize()
    snapshot = reopened.load_session(session.session_id)
    if failure_point == "receipt":
        assert snapshot.session.title == "First topic"
        assert snapshot.session.title_source == "fallback"
        assert len(snapshot.messages) == len(snapshot.events) == 1
        assert reopened.load_harness(session.session_id).state.states[0].payload == 1
        assert reopened.commit_session(request).messages == tuple(snapshot.messages)
    else:
        assert snapshot.session.title == "New conversation"
        assert snapshot.session.title_source == "placeholder"
        assert snapshot.messages == snapshot.events == []
        assert reopened.load_harness(session.session_id).state.states == ()


def test_sqlite_failure_rolls_back_the_entire_decision(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord(title_source="placeholder")
    store.create_session(session)
    existing = EventRecord(session_id=session.session_id, sequence=1, event_type="existing")
    store.append_event(existing)
    with pytest.raises(SessionCommitConflict):
        store.commit_session(
            SessionCommit(
                before=session,
                events=(existing,),
                steps=(
                    SessionCommitStep(
                        message=MessageRecord.text(
                            session_id=session.session_id, role="user", text="Rolled back topic"
                        ),
                        harness=state(session.session_id, 1),
                    ),
                ),
            )
        )
    assert store.load_messages_tail(session.session_id) == []
    assert store.load_harness(session.session_id).state.states == ()
    assert store.load_session_record(session.session_id).message_count == 0
    assert store.load_session_record(session.session_id).title == "New conversation"
    assert store.load_session_record(session.session_id).title_source == "placeholder"


@pytest.mark.parametrize("failure_point", ["messages", "session", "events", "completion"])
def test_file_recovery_finishes_a_partially_applied_decision(tmp_path, monkeypatch, failure_point):
    from pathlib import Path

    from ouro.sessions.file_commits import FileSessionCommits

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    prefix = message(session.session_id, "keep")
    store.append_message(prefix)
    reader = LocalSessionStore(tmp_path)
    assert reader.load_messages_tail(session.session_id) == [prefix]
    run = RunRecord(session_id=session.session_id, status="waiting_user")
    request = SessionCommit(
        before=store.load_session_record(session.session_id),
        steps=(
            SessionCommitStep(
                message=message(session.session_id, "result"), harness=state(session.session_id, 1)
            ),
        ),
        runs=(run,),
        events=(EventRecord(session_id=session.session_id, event_type="committed"),),
    )
    write_suffix = FileSessionCommits._write_suffix
    save_session = FileSessionCommits.save_session_record
    unlink = Path.unlink

    def interrupted_suffix(path, offset, records):
        write_suffix(path, offset, records)
        if path.stem == failure_point:
            raise OSError("injected after journal write")

    def interrupted_session(directory, value):
        save_session(directory, value)
        if failure_point == "session":
            raise OSError("injected after state switch")

    def interrupted_unlink(path, *args, **kwargs):
        if failure_point == "completion" and path.name == "commit.json":
            raise OSError("injected before completing decision")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(FileSessionCommits, "_write_suffix", staticmethod(interrupted_suffix))
        patch.setattr(FileSessionCommits, "save_session_record", staticmethod(interrupted_session))
        patch.setattr(Path, "unlink", interrupted_unlink)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)

    # A paired read must recover before exposing either part of the decision.
    current, harness = reader.load_session_harness(session.session_id)
    assert current.harness_version == harness.version_id
    assert harness.state.states[0].payload == 1
    # Other canonical entry points observe the same recovered state.
    assert reader.load_resource_references(session.session_id) == []
    assert not (store.sessions_dir / session.session_id / "commit.json").exists()
    snapshot = reader.load_session(session.session_id)
    assert len(snapshot.messages) == 2
    assert snapshot.messages[0] == prefix
    assert snapshot.session.message_count == 2
    assert snapshot.runs == [run]
    assert len(snapshot.events) == 1
    assert snapshot.extension_states[0].payload == 1
    assert reader.commit_session(request).messages == tuple(snapshot.messages[1:])
    assert reader.load_session(session.session_id) == snapshot


def test_sqlite_session_snapshot_uses_one_harness_version(tmp_path, monkeypatch):
    from ouro.sessions.sqlite_commits import SQLiteSessionCommits

    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    writer = SQLiteSessionStore(store.database)
    original = SQLiteSessionCommits.load_session_harness
    changed = False

    def advance_after_session_read(commits, session_id):
        nonlocal changed
        result = original(commits, session_id)
        if not changed:
            changed = True
            writer.commit_session(
                SessionCommit(
                    before=result[0],
                    steps=(SessionCommitStep(harness=state(session_id, 1)),),
                )
            )
        return result

    monkeypatch.setattr(SQLiteSessionCommits, "load_session_harness", advance_after_session_read)
    snapshot = store.load_session(session.session_id)
    assert snapshot.session.harness_version == session.harness_version
    assert snapshot.extension_states == []
    assert store.load_harness(session.session_id).state.states[0].payload == 1


@pytest.mark.parametrize("query", ["record", "current_harness", "pair", "capture"])
def test_session_reads_decode_the_referenced_checkpoint_once(store, monkeypatch, query):
    session = SessionRecord()
    store.create_session(session)
    expected = state(session.session_id, {"items": [1]})
    session = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(harness=expected),))
    ).session
    module = file_commits if isinstance(store, LocalSessionStore) else sqlite_commits
    decode = module.decode_versioned_record_json
    decoded = []

    def count_checkpoint(model, payload, **kwargs):
        result = decode(model, payload, **kwargs)
        if model is HarnessVersion:
            decoded.append(result.version_id)
        return result

    monkeypatch.setattr(module, "decode_versioned_record_json", count_checkpoint)
    if query == "record":
        assert store.load_session_record(session.session_id) == session
    elif query == "current_harness":
        version = store.load_harness(session.session_id)
        assert version.version_id == session.harness_version
        assert version.state == expected
    elif query == "pair":
        current, version = store.load_session_harness(session.session_id)
        assert current == session
        assert version.version_id == session.harness_version
        assert version.state == expected
    else:
        current, snapshot = capture_session_harness(store, session.session_id)
        assert current == session
        assert snapshot.harness_version == session.harness_version
        assert snapshot.state == expected
    assert decoded == [session.harness_version]


def test_noop_commit_does_not_decode_checkpoint_twice_during_preparation(store, monkeypatch):
    session = SessionRecord()
    store.create_session(session)
    module = file_commits if isinstance(store, LocalSessionStore) else sqlite_commits
    decode = module.decode_versioned_record_json
    decoded = []

    def count_checkpoint(model, payload, **kwargs):
        result = decode(model, payload, **kwargs)
        if model is HarnessVersion:
            decoded.append(result.version_id)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module, "decode_versioned_record_json", count_checkpoint)
        result = store.commit_session(SessionCommit(before=session))
    # Files independently verify the durable checkpoint when finishing the decision.
    expected_reads = 2 if isinstance(store, LocalSessionStore) else 1
    assert decoded == [session.harness_version] * expected_reads
    assert result.session.harness_version == session.harness_version
    assert store.load_session(session.session_id).harness_versions == [
        store.load_harness(session.session_id)
    ]


@pytest.mark.parametrize("capture", [False, True])
def test_paired_session_controls_are_isolated_from_harness_and_future_reads(store, capture):
    session = SessionRecord()
    store.create_session(session)
    expected = state(session.session_id, {"items": [1]})
    expected.controls = SessionControlState(
        revision=1,
        activations=[
            SessionControlActivation(
                controller_id="example.state/session_controllers/mode",
                plugin_id="example.state",
                controller_local_id="mode",
                plugin_class="user",
                release_id="release",
                generation_id="generation",
                execution_contract_hash="contract",
                data={"items": [1]},
            )
        ],
    )
    session = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(harness=expected),))
    ).session
    current, snapshot = (
        capture_session_harness(store, session.session_id)
        if capture
        else store.load_session_harness(session.session_id)
    )
    assert current.controls == snapshot.state.controls == expected.controls
    current.controls.activations[0].data["items"].append(2)
    assert snapshot.state.controls == expected.controls
    snapshot.state.controls.revision = 7
    snapshot.state.states[0].payload["items"].append(3)
    assert current.controls.revision == 1
    fresh, version = store.load_session_harness(session.session_id)
    assert fresh == session
    assert version.state == expected


@pytest.mark.parametrize("damage", ["missing", "foreign_session", "foreign_version", "malformed"])
def test_paired_read_and_commit_reject_invalid_checkpoint_without_effects(store, damage):
    session = SessionRecord()
    store.create_session(session)
    version = store.load_harness(session.session_id)
    original = version.model_dump_json()
    payload = version.model_dump(mode="json")
    if damage == "foreign_session":
        payload["session_id"] = "another_session"
    elif damage == "foreign_version":
        payload["version_id"] = "another_version"
    corrupted = (
        None if damage == "missing" else "{" if damage == "malformed" else json.dumps(payload)
    )

    def replace_checkpoint(value):
        if isinstance(store, LocalSessionStore):
            path = (
                store.sessions_dir / session.session_id / "harness" / f"{version.version_id}.json"
            )
            if value is None:
                path.unlink()
            else:
                path.write_text(value, encoding="utf-8")
        else:
            with closing(sqlite3.connect(store.database)) as connection:
                connection.execute(
                    "DELETE FROM harness_versions WHERE session_id = ? AND version_id = ?",
                    (session.session_id, version.version_id),
                )
                if value is not None:
                    connection.execute(
                        "INSERT INTO harness_versions(session_id, version_id, payload) "
                        "VALUES(?, ?, ?)",
                        (session.session_id, version.version_id, value),
                    )
                connection.commit()

    request = SessionCommit(
        before=session, steps=(SessionCommitStep(message=message(session.session_id, "rejected")),)
    )
    replace_checkpoint(corrupted)
    try:
        with pytest.raises(PersistenceError):
            store.load_session_harness(session.session_id)
        with pytest.raises(PersistenceError):
            capture_session_harness(store, session.session_id)
        with pytest.raises(PersistenceError):
            store.commit_session(request)
        assert store.load_commit(session.session_id, request.operation_id) is None
    finally:
        replace_checkpoint(original)
    assert store.load_session_record(session.session_id) == session
    assert store.load_messages_tail(session.session_id) == []


def test_file_paired_read_keeps_recovery_checkpoint_verification(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    request = SessionCommit(before=session)

    def interrupt(directory, value):
        raise OSError("interrupted before session publication")

    with monkeypatch.context() as patch:
        patch.setattr(store._commits, "save_session_record", interrupt)
        with pytest.raises(PersistenceError, match="interrupted"):
            store.commit_session(request)
    directory = store.sessions_dir / session.session_id
    checkpoint = directory / "harness" / f"{session.harness_version}.json"
    original = checkpoint.read_bytes()
    checkpoint.unlink()
    reader = LocalSessionStore(tmp_path)
    with pytest.raises(PersistenceError, match="checkpoint not found"):
        reader.load_session_harness(session.session_id)
    assert (directory / "commit.json").exists()
    checkpoint.write_bytes(original)
    current, version = reader.load_session_harness(session.session_id)
    assert current.harness_version == version.version_id == session.harness_version
    assert reader.commit_session(request).session == current
    assert not (directory / "commit.json").exists()
