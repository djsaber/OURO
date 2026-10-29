from __future__ import annotations

import multiprocessing

import pytest

from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def open_store(path, backend):
    return (
        LocalSessionStore(path) if backend == "files" else SQLiteSessionStore(path / "sessions.db")
    )


def write_process(path, backend, session_id, pipe):
    store = open_store(path, backend)
    for text in ("first", "large" * 100000, "last"):
        store.append_message(MessageRecord.text(session_id=session_id, role="user", text=text))
        pipe.send(store.observation_revision(session_id))
        assert pipe.recv() == "read"
    pipe.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_other_process_discovers_committed_content_without_event_log(tmp_path, backend):
    store = open_store(tmp_path, backend)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.get_context("spawn").Process(
        target=write_process, args=(tmp_path, backend, session.session_id, child)
    )
    process.start()
    try:
        for sequence, text in enumerate(("first", "large" * 100000, "last"), 1):
            assert parent.poll(10)
            revision = parent.recv()
            assert store.observation_revision(session.session_id) == revision
            page, more = store.load_messages_after(
                session.session_id, after_sequence=sequence - 1, limit=1
            )
            assert not more
            assert len(page) == 1 and page[0].text_content == text
            parent.send("read")
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()
        parent.close()
        child.close()
