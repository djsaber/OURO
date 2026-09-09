from __future__ import annotations

import json

import pytest

from ouro.core.errors import MigrationRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import HistoryBoundaryUnavailable, SessionCommit
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def legacy_records():
    session = SessionRecord(session_id="ses_legacy")
    message = MessageRecord.text(
        session_id=session.session_id, role="user", text="old input", sequence=1
    )
    session.last_message_id = message.message_id
    session.message_count = 1
    run = RunRecord(session_id=session.session_id, status="aborted")
    state = SessionExtensionStateRecord(
        session_id=session.session_id,
        owner_id="example.state",
        state_schema_version=1,
        payload={"value": "before migration"},
    )
    raw_session = session.model_dump(mode="json", exclude={"harness_version", "history_epoch"})
    raw_message = message.model_dump(mode="json", exclude={"before_harness_version"})
    raw_run = run.model_dump(mode="json", exclude={"history_epoch"})
    for raw in (raw_session, raw_message, raw_run):
        raw["schema_version"] = 1
    return session, message, run, state, raw_session, raw_message, raw_run


def legacy_files(root):
    session, message, run, state, raw_session, raw_message, raw_run = legacy_records()
    directory = root / "sessions" / session.session_id
    (directory / "runs").mkdir(parents=True)
    (directory / "extensions").mkdir()
    (root / "layout.json").write_text(json.dumps({"data_layout_version": 6}), encoding="utf-8")
    (directory / "session.json").write_text(json.dumps(raw_session), encoding="utf-8")
    (directory / "messages.jsonl").write_text(json.dumps(raw_message) + "\n", encoding="utf-8")
    (directory / "events.jsonl").touch()
    (directory / "compactions.jsonl").touch()
    (directory / "runs" / f"{run.run_id}.json").write_text(json.dumps(raw_run), encoding="utf-8")
    (directory / "extensions" / "index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "states": [state.model_dump(mode="json")],
            }
        ),
        encoding="utf-8",
    )
    return session, message, run, state


@pytest.fixture(params=["files", "sqlite"])
def migrated(tmp_path, request):
    if request.param == "files":
        session, message, run, state = legacy_files(tmp_path)
        store = LocalSessionStore(tmp_path)
    else:
        session, message, run, state, raw_session, raw_message, raw_run = legacy_records()
        store = SQLiteSessionStore(tmp_path / "sessions.db")
        store.initialize()
        store.create_session(session)
        with store.session_mutation(session.session_id):
            connection = store._transaction_connection()
            connection.execute(
                "CREATE TABLE session_extension_states "
                "(session_id TEXT, owner_id TEXT, payload TEXT)"
            )
            connection.execute(
                "UPDATE metadata SET value = '4' WHERE key = 'sqlite_schema_version'"
            )
            connection.execute(
                "UPDATE sessions SET payload = ? WHERE session_id = ?",
                (json.dumps(raw_session), session.session_id),
            )
            connection.execute(
                "INSERT INTO messages(session_id, message_id, sequence, payload) "
                "VALUES(?, ?, 1, ?)",
                (session.session_id, message.message_id, json.dumps(raw_message)),
            )
            connection.execute(
                "INSERT INTO runs(session_id, run_id, started_at, payload) VALUES(?, ?, ?, ?)",
                (session.session_id, run.run_id, str(run.started_at), json.dumps(raw_run)),
            )
            connection.execute(
                "INSERT INTO session_extension_states(session_id, owner_id, payload) "
                "VALUES(?, ?, ?)",
                (session.session_id, state.owner_id, state.model_dump_json()),
            )
    store.initialize()
    return store, session, message, run, state


def test_migration_preserves_current_state_without_fabricating_old_anchors(migrated):
    store, session, message, run, state = migrated
    current = store.load_session_record(session.session_id)
    assert current.harness_version == "harness_baseline"
    assert store.load_harness(session.session_id).state.states == (state,)
    assert store.load_run(session.session_id, run.run_id).history_epoch == 0
    assert store.load_message(session.session_id, message.message_id).before_harness_version is None
    with pytest.raises(HistoryBoundaryUnavailable, match="predates"):
        store.commit_session(SessionCommit(before=current, before_message_id=message.message_id))
    assert store.load_session_record(session.session_id) == current

    new = MessageRecord.text(session_id=session.session_id, role="user", text="new input")
    store.append_message(new)
    store.save_session_extension_state(state.model_copy(update={"payload": {"value": "later"}}))
    store.truncate_messages(
        session.session_id,
        before_message_id=new.message_id,
        expected_last_message_id=new.message_id,
    )
    assert store.load_harness(session.session_id).state.states == (state,)
    assert store.load_messages_tail(session.session_id)[0].message_id == message.message_id


def test_file_migration_is_reentrant_after_message_conversion(tmp_path, monkeypatch):
    from ouro.sessions import local_files

    session, message, _, state = legacy_files(tmp_path)
    convert = local_files.migrate_file_records

    def interrupted(directory):
        convert(directory)
        raise OSError("migration interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(local_files, "migrate_file_records", interrupted)
        with pytest.raises(OSError, match="migration interrupted"):
            LocalSessionStore(tmp_path).initialize()
    assert json.loads((tmp_path / "layout.json").read_text())["data_layout_version"] == 6
    store = LocalSessionStore(tmp_path)
    store.initialize()
    store.initialize()
    assert store.load_harness(session.session_id).state.states == (state,)
    assert store.load_message(session.session_id, message.message_id).before_harness_version is None


def test_file_migration_cannot_race_an_execution_owner(tmp_path):
    session, _, _, _ = legacy_files(tmp_path)
    store = LocalSessionStore(tmp_path)
    lock = store.acquire_session_lock(session.session_id, "run_live")
    assert lock is not None
    try:
        with pytest.raises(MigrationRequiredError, match="stop active"):
            store.initialize()
        assert json.loads((tmp_path / "layout.json").read_text())["data_layout_version"] == 6
    finally:
        lock.release()


def test_file_migration_finishes_the_preexisting_history_cut_first(tmp_path):
    session, _, _, state = legacy_files(tmp_path)
    directory = tmp_path / "sessions" / session.session_id
    raw = json.loads((directory / "session.json").read_text())
    raw.update(message_count=0, last_message_id=None)
    (directory / "history-cut.json").write_text(
        json.dumps(
            {
                "offset": 0,
                "session": raw,
                "compactions": None,
            }
        ),
        encoding="utf-8",
    )
    store = LocalSessionStore(tmp_path)
    store.initialize()
    assert store.load_messages_tail(session.session_id) == []
    assert store.load_harness(session.session_id).state.states == (state,)
    assert not (directory / "history-cut.json").exists()


def test_sqlite_migration_rejects_an_active_owner_and_keeps_old_format(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session, _, _, _, raw, _, _ = legacy_records()
    store.create_session(session)
    with store.session_mutation(session.session_id):
        connection = store._transaction_connection()
        connection.execute("UPDATE metadata SET value = '4' WHERE key = 'sqlite_schema_version'")
        connection.execute(
            "UPDATE sessions SET payload = ? WHERE session_id = ?",
            (json.dumps(raw), session.session_id),
        )
    lock = store.acquire_session_lock(session.session_id, "run_live")
    assert lock is not None
    try:
        with pytest.raises(MigrationRequiredError, match="stop active"):
            store.initialize()
        with store._read_connection() as connection:
            assert connection.execute(
                "SELECT value FROM metadata WHERE key = 'sqlite_schema_version'"
            ).fetchone() == ("4",)
            assert (
                json.loads(connection.execute("SELECT payload FROM sessions").fetchone()[0]) == raw
            )
    finally:
        lock.release()


def test_sqlite_migration_retains_all_owners_until_the_transaction_finishes(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    sessions = [SessionRecord(), SessionRecord()]
    for session in sessions:
        store.create_session(session)
    with store.session_mutation("migration"):
        store._transaction_connection().execute(
            "UPDATE metadata SET value = '4' WHERE key = 'sqlite_schema_version'"
        )
    rebuild = store._replace_catalog

    def before_commit(connection, catalog):
        assert all(store.is_session_locked(session.session_id) for session in sessions)
        rebuild(connection, catalog)

    monkeypatch.setattr(store, "_replace_catalog", before_commit)
    store.initialize()
    assert all(not store.is_session_locked(session.session_id) for session in sessions)
