from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest

import ouro.sessions.backends.files.commits as file_commits
import ouro.sessions.backends.sqlite.commits as sqlite_commits
from ouro.application.harness import capture_session_harness
from ouro.core.context import ContextBuilder
from ouro.core.context_budget import ContextBudget
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
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
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.context_facts import save_context
from tests.session_storage import session_connection


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
        provider_context=view.provider_context,
    )
    save_context(store, context)
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
    import ouro.sessions.backends.files.store as files

    store = LocalSessionStore(tmp_path)
    store.initialize()
    publish = files.replace_path_with_retry
    observed = []

    def inspect(source, destination):
        if source.parent == store.sessions_dir and source != destination:
            assert (source / "session-data.sqlite3").exists()
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
    assert [message.as_fact() for message in store.load_messages_tail(session.session_id)] == [
        second,
        first,
    ]
    assert store.load_session_record(session.session_id).last_commit_id == undo.operation_id
    with pytest.raises(SessionCommitConflict, match="already applied"):
        store.commit_session(undo)
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


@pytest.mark.parametrize("metadata_only", [False, True])
def test_state_and_epoch_conflicts_do_not_append_effects(store, metadata_only):
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
                before=session,
                steps=()
                if metadata_only
                else (SessionCommitStep(harness=state(session.session_id, 2)),),
                events=(EventRecord(session_id=session.session_id, event_type="stale"),),
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
                steps=()
                if metadata_only
                else (SessionCommitStep(message=message(session.session_id, "late")),),
                events=(EventRecord(session_id=session.session_id, event_type="stale"),),
            )
        )
    assert store.load_messages_tail(session.session_id) == []
    assert store.load_events_after(session.session_id) == ([], False)


def test_current_commit_identity_cannot_be_reused_for_another_proposal(store):
    session = SessionRecord()
    store.create_session(session)
    request = SessionCommit(
        before=session, steps=(SessionCommitStep(harness=state(session.session_id, 1)),)
    )
    store.commit_session(request)
    with pytest.raises(SessionCommitConflict, match="already applied"):
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
    renamed = store.commit_session(
        SessionCommit(before=before, after=before.model_copy(update={"title": "renamed"}))
    )
    assert renamed.session.last_commit_id != before.last_commit_id
    result = store.commit_session(
        SessionCommit(
            before=before, steps=(SessionCommitStep(harness=state(session.session_id, 1)),)
        )
    )
    assert result.session.title == "renamed"


def test_finished_commit_keeps_only_message_owned_body(store):
    session = SessionRecord()
    store.create_session(session)
    body = "unique-assistant-content:" + "x" * 100_000
    result = store.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(message=message(session.session_id, body)),),
        )
    )
    assert (
        store.load_message(session.session_id, result.messages[0].message_id).text_content == body
    )
    if isinstance(store, LocalSessionStore):
        directory = store.sessions_dir / session.session_id
        assert not (directory / "operations").exists()
        assert not (directory / "commit.json").exists()
        owners = [
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and body.encode() in path.read_bytes()
        ]
        assert owners == ["messages.jsonl"]
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert ("session_commits",) not in tables
            owners = []
            for (table,) in tables:
                columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                if any(column[1] == "payload" for column in columns):
                    owners.extend(
                        table
                        for (payload,) in connection.execute(f'SELECT payload FROM "{table}"')
                        if body in payload
                    )
            assert owners == ["messages"]


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


@pytest.mark.parametrize("failure_point", ["checkpoint", "decision"])
def test_file_decision_recovers_without_reexecuting_operation(tmp_path, monkeypatch, failure_point):
    import ouro.sessions.backends.files.commits as file_commits

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title_source="placeholder")
    store.create_session(session)
    write = file_commits.write_model_atomic

    def fail(path, value):
        fail_here = (failure_point == "checkpoint" and path.parent.name == "harness") or (
            failure_point == "decision" and path.name == "commit.json"
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

    from ouro.sessions.backends.files.commits import FileSessionCommits

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
    save_session = file_commits.write_session
    insert_events = file_commits.insert_events
    unlink = Path.unlink

    def interrupted_suffix(path, offset, records):
        write_suffix(path, offset, records)
        if path.stem == failure_point:
            raise OSError("injected after journal write")

    def interrupted_session(directory, value):
        save_session(directory, value)
        if failure_point == "session":
            raise OSError("injected after state switch")

    def interrupted_events(connection, events):
        insert_events(connection, events)
        if failure_point == "events":
            raise OSError("injected after event insertion")

    def interrupted_unlink(path, *args, **kwargs):
        if failure_point == "completion" and path.name == "commit.json":
            raise OSError("injected before completing decision")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(FileSessionCommits, "_write_suffix", staticmethod(interrupted_suffix))
        patch.setattr(file_commits, "write_session", interrupted_session)
        patch.setattr(file_commits, "insert_events", interrupted_events)
        patch.setattr(Path, "unlink", interrupted_unlink)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)

    # Queries expose neither part; the explicit mutation boundary finishes storage recovery.
    with pytest.raises(SessionRecoveryRequiredError):
        reader.load_session_harness(session.session_id)
    with reader.session_mutation(session.session_id):
        pass
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
    assert current.last_commit_id == request.operation_id
    with pytest.raises(SessionCommitConflict, match="already applied"):
        reader.commit_session(request)
    assert reader.load_session(session.session_id) == snapshot


def test_sqlite_session_snapshot_uses_one_harness_version(tmp_path, monkeypatch):
    import ouro.sessions.backends.sqlite.store as sqlite_store

    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    writer = SQLiteSessionStore(store.database)
    original = sqlite_store.read_session
    changed = False

    def advance_after_session_read(connection, session_id):
        nonlocal changed
        result = original(connection, session_id)
        if not changed:
            changed = True
            writer.commit_session(
                SessionCommit(
                    before=result,
                    steps=(SessionCommitStep(harness=state(session_id, 1)),),
                )
            )
        return result

    monkeypatch.setattr(sqlite_store, "read_session", advance_after_session_read)
    snapshot = store.load_session(session.session_id)
    assert snapshot.session.harness_version == session.harness_version
    assert snapshot.extension_states == []
    assert store.load_harness(session.session_id).state.states[0].payload == 1


def test_snapshot_head_controls_come_from_harness_without_loading_private_state(store, monkeypatch):
    session = SessionRecord()
    harness = state(session.session_id, {"private": "x" * (1024 * 1024)})
    harness.controls = SessionControlState(revision=7)
    version = HarnessVersion(
        session_id=session.session_id, version_id=session.harness_version, state=harness
    )
    store.create_session_snapshot(session, [], harness_versions=[version])

    def forbidden(*args, **kwargs):
        pytest.fail("Session head read expanded the Harness checkpoint")

    owner = (
        store._commits
        if isinstance(store, LocalSessionStore)
        else sqlite_commits.SQLiteSessionCommits
    )
    monkeypatch.setattr(owner, "load_harness", forbidden)
    head = store.load_session_record(session.session_id)
    assert head.controls == harness.controls
    assert head.harness_version == version.version_id
    assert session.controls.revision == 0
    with session_connection(store, session.session_id) as connection:
        size = connection.execute(
            "SELECT length(payload) FROM sessions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
    assert size < 4096


@pytest.mark.parametrize("query", ["record", "current_harness", "pair", "capture"])
def test_session_reads_decode_only_the_requested_checkpoint(store, monkeypatch, query):
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
    assert decoded == ([] if query == "record" else [session.harness_version])


@pytest.mark.parametrize("publish_run", [False, True])
def test_metadata_commit_preserves_harness_without_decoding_private_state(
    store, monkeypatch, publish_run
):
    session = SessionRecord()
    store.create_session(session)
    expected = state(session.session_id, {"private": "x" * 1048576})
    expected.controls = SessionControlState(revision=7)
    session = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(harness=expected),))
    ).session
    run = RunRecord(session_id=session.session_id)
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
        result = store.commit_session(
            SessionCommit(
                before=session,
                after=session.model_copy(
                    update={"title": "updated", "controls": SessionControlState(revision=99)}
                ),
                runs=(run,) if publish_run else (),
                events=(EventRecord(session_id=session.session_id, event_type="updated"),),
            )
        )
    assert decoded == []
    assert result.session.harness_version == session.harness_version
    assert result.session.controls == expected.controls
    assert result.session.title == "updated"
    assert store.load_harness(session.session_id).state == expected
    assert store.load_session_record(session.session_id) == result.session
    if publish_run:
        assert store.load_run(session.session_id, run.run_id) == run
    events, more = store.load_events_after(session.session_id)
    assert not more
    assert [event.event_type for event in events] == ["updated"]


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
    finally:
        replace_checkpoint(original)
    assert store.load_session_record(session.session_id) == session
    assert store.load_messages_tail(session.session_id) == []


def test_file_paired_read_keeps_recovery_checkpoint_verification(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    request = SessionCommit(
        before=session,
        steps=(SessionCommitStep(message=message(session.session_id, "cross-file")),),
    )

    def interrupt(directory, value):
        raise OSError("interrupted before session publication")

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", interrupt)
        with pytest.raises(PersistenceError, match="interrupted"):
            store.commit_session(request)
    directory = store.sessions_dir / session.session_id
    checkpoint = directory / "harness" / f"{session.harness_version}.json"
    original = checkpoint.read_bytes()
    checkpoint.unlink()
    reader = LocalSessionStore(tmp_path)
    with pytest.raises(SessionRecoveryRequiredError):
        reader.load_session_harness(session.session_id)
    with pytest.raises(PersistenceError, match="checkpoint not found"):
        with reader.session_mutation(session.session_id):
            pass
    assert (directory / "commit.json").exists()
    checkpoint.write_bytes(original)
    with reader.session_mutation(session.session_id):
        pass
    current, version = reader.load_session_harness(session.session_id)
    assert current.harness_version == version.version_id == session.harness_version
    assert current.last_commit_id == request.operation_id
    with pytest.raises(SessionCommitConflict, match="already applied"):
        reader.commit_session(request)
    assert not (directory / "commit.json").exists()


def test_native_commit_rolls_back_head_run_and_event_without_file_recovery(store):
    session = SessionRecord(title="before")
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    before = store.load_session_record(session.session_id)
    completed = run.model_copy(update={"status": "completed"})
    request = SessionCommit(
        before=before,
        after=before.model_copy(update={"title": "after"}),
        runs=(completed,),
        events=(EventRecord(session_id=session.session_id, event_type="committed"),),
    )
    with session_connection(store, session.session_id) as connection:
        connection.execute(
            "CREATE TRIGGER reject_event BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'injected native failure'); END"
        )
    with pytest.raises(PersistenceError, match="injected native failure"):
        store.commit_session(request)
    assert store.load_session_record(session.session_id) == before
    assert store.load_run(session.session_id, run.run_id) == run
    assert store.load_events_page(session.session_id)[0] == []
    with session_connection(store, session.session_id) as connection:
        connection.execute("DROP TRIGGER reject_event")
    result = store.commit_session(request)
    assert store.load_session_record(session.session_id) == result.session
    assert store.load_run(session.session_id, run.run_id) == completed
    assert store.load_events_page(session.session_id)[0] == list(result.events)
    if isinstance(store, LocalSessionStore):
        directory = store.sessions_dir / session.session_id
        assert not (directory / "commit.json").exists()
        assert not (directory / "events.jsonl").exists()
        assert not (directory / "event-index").exists()
