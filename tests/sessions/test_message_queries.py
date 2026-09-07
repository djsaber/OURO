from __future__ import annotations

import json

import pytest

from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("separate_reader", [False, True])
@pytest.mark.parametrize("append_more", [False, True])
def test_message_queries_detect_equal_size_replacement_with_identical_suffix(
    tmp_path, backend, separate_reader, append_more
):
    def store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )

    writer = store()
    writer.initialize()
    reader = store() if separate_reader else writer
    session = SessionRecord()
    writer.create_session(session)
    retained = MessageRecord.text(session_id=session.session_id, role="user", text="retained")
    original = MessageRecord.text(
        session_id=session.session_id, role="user", text="question", run_id="run_old"
    ).model_copy(update={"metadata": {"queued_input_id": "queue_old", "padding": "same" * 100}})
    replacement = original.model_copy(
        update={
            "message_id": original.message_id[:-3] + "new",
            "run_id": "run_new",
            "metadata": {"queued_input_id": "queue_new", "padding": "same" * 100},
        }
    )
    assert len(original.model_dump_json()) == len(replacement.model_dump_json())
    writer.append_messages([retained, original])
    assert reader.load_message(session.session_id, original.message_id) == original
    writer.truncate_messages(
        session.session_id,
        before_message_id=original.message_id,
        expected_last_message_id=original.message_id,
    )
    writer.append_message(replacement)
    if append_more:
        writer.append_message(
            MessageRecord.text(session_id=session.session_id, role="assistant", text="reply")
        )

    assert reader.load_message(session.session_id, replacement.message_id) == replacement
    assert reader.load_message(session.session_id, retained.message_id) == retained
    assert reader.load_run_messages(session.session_id, "run_old") == []
    assert reader.load_run_messages(session.session_id, "run_new") == [replacement]
    assert reader.find_queued_input_message(session.session_id, "queue_old") is None
    assert reader.find_queued_input_message(session.session_id, "queue_new") == replacement
    with pytest.raises(PersistenceError, match="message not found"):
        reader.load_message(session.session_id, original.message_id)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_message_queries_follow_other_writer_and_history_truncation(tmp_path, backend):
    def store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )

    writer, reader = store(), store()
    writer.initialize()
    reader.initialize()
    session = SessionRecord()
    writer.create_session(session)

    def message(run, queue, text):
        return MessageRecord.text(
            session_id=session.session_id,
            role="user",
            run_id=run,
            text=text,
        ).model_copy(update={"metadata": {"queued_input_id": queue}})

    first = message("run_one", "queue_one", "one")
    second = message("run_two", "queue_two", "two")
    writer.append_messages([first, second])
    assert reader.load_run_messages(session.session_id, "run_one") == [first]
    assert reader.find_queued_input_message(session.session_id, "queue_two") == second
    writer.truncate_messages(
        session.session_id,
        before_message_id=second.message_id,
        expected_last_message_id=second.message_id,
    )
    replacement = message("run_three", "queue_three", "replacement" * 200)
    writer.append_message(replacement)
    # The file is now larger than the cached size; size alone is not a revision.
    assert reader.find_queued_input_message(session.session_id, "queue_two") is None
    assert reader.load_run_messages(session.session_id, "run_two") == []
    assert reader.load_message(session.session_id, replacement.message_id) == replacement
    with pytest.raises(PersistenceError, match="message not found"):
        reader.load_message(session.session_id, second.message_id)


def test_repeated_local_queries_only_index_appended_records(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    history = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            run_id=f"run_{i}",
            text="history" * 100,
        )
        for i in range(200)
    ]
    store.append_messages(history)
    assert store.load_run_messages(session.session_id, "run_199") == history[-1:]
    original = json.loads
    indexed = []

    def count(value, *args, **kwargs):
        # Point reads validate the selected message through the same json
        # module; count bytes containing any retained *other* record.
        if history[-1].message_id.encode() not in value:
            indexed.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr("ouro.sessions.message_index.json.loads", count)
    for _ in range(20):
        assert store.find_queued_input_message(session.session_id, "absent") is None
        assert store.load_message(session.session_id, history[-1].message_id) == history[-1]
    assert len(indexed) == 0
    appended = MessageRecord.text(session_id=session.session_id, role="user", text="new")
    store.append_message(appended)
    assert store.load_message(session.session_id, appended.message_id) == appended
    # One new index entry and one validation of the selected point read.
    assert len(indexed) == 2
