import json
import sqlite3
from contextlib import closing

import pytest

from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def old_event(event):
    raw = event.model_dump(mode="json", exclude={"history_epoch"})
    raw["schema_version"] = 1
    return raw


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_event_migration_preserves_epochs_receipts_and_is_reentrant(tmp_path, backend):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="aborted")
    store.save_run(run)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="withdraw")
    added = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    cut = store.commit_session(
        SessionCommit(
            before=added.session,
            before_message_id=message.message_id,
            events=(
                EventRecord(
                    session_id=session.session_id,
                    event_type="session_history_withdrawn",
                    payload={"history_epoch": 1},
                ),
            ),
        )
    )
    late = store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_text_delta",
            payload={"text": "late"},
        )
    )
    current = store.append_ordered_event(
        EventRecord(session_id=session.session_id, history_epoch=1, event_type="diagnostic")
    )
    events = [cut.events[0], late, current]
    receipt = cut.model_dump(mode="json")
    receipt["events"] = [old_event(event) for event in cut.events]
    if backend == "files":
        directory = tmp_path / "sessions" / session.session_id
        (tmp_path / "layout.json").write_text(json.dumps({"data_layout_version": 7}))
        (directory / "events.jsonl").write_text(
            "".join(json.dumps(old_event(event)) + "\n" for event in events), encoding="utf-8"
        )
        (directory / "operations" / f"{cut.operation_id}.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("UPDATE metadata SET value='5' WHERE key='sqlite_schema_version'")
            connection.executemany(
                "UPDATE events SET payload=? WHERE event_id=?",
                [(json.dumps(old_event(event)), event.event_id) for event in events],
            )
            connection.execute(
                "UPDATE session_commits SET payload=? WHERE operation_id=?",
                (json.dumps(receipt), cut.operation_id),
            )
    store.initialize()
    restored = store.load_session(session.session_id)
    assert restored.events == events
    assert store.load_commit(session.session_id, cut.operation_id) == cut
    store.initialize()
    assert store.load_session(session.session_id) == restored


def test_file_migration_finishes_old_pending_decision_before_rewriting_offsets(
    tmp_path, monkeypatch
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    store.append_ordered_event(EventRecord(session_id=session.session_id, event_type="diagnostic"))
    finish = store._commits.finish

    def fail_after_decision(session_id):
        if (tmp_path / "sessions" / session_id / "commit.json").exists():
            raise OSError("interrupted apply")
        finish(session_id)

    monkeypatch.setattr(store._commits, "finish", fail_after_decision)
    request = SessionCommit(
        before=session,
        events=(
            EventRecord(
                session_id=session.session_id,
                event_type="diagnostic",
                payload={"stage": "committed"},
            ),
        ),
    )
    with pytest.raises(PersistenceError, match="interrupted apply"):
        store.commit_session(request)
    directory = tmp_path / "sessions" / session.session_id
    pending = directory / "commit.json"
    raw = json.loads(pending.read_text(encoding="utf-8"))
    journal = directory / "events.jsonl"
    prior = EventRecord.model_validate_json(journal.read_text(encoding="utf-8").strip())
    old_prefix = (json.dumps(old_event(prior)) + "\n").encode("utf-8")
    journal.write_bytes(old_prefix)
    raw["event_offset"] = len(old_prefix)
    for event in raw["result"]["events"]:
        event["schema_version"] = 1
        del event["history_epoch"]
    pending.write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "layout.json").write_text(json.dumps({"data_layout_version": 7}))
    restarted = LocalSessionStore(tmp_path)
    restarted.initialize()
    snapshot = restarted.load_session(session.session_id)
    assert [event.sequence for event in snapshot.events] == [1, 2]
    assert all(event.schema_version == 2 for event in snapshot.events)
    assert snapshot.events[-1].payload == {"stage": "committed"}
    assert restarted.load_commit(session.session_id, request.operation_id) is not None
    assert not pending.exists()
