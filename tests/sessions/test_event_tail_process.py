from __future__ import annotations

import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

from ouro.core.events import EventRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore


def _write_events(home: Path, session_id: str, connection: Connection) -> None:
    store = LocalSessionStore(home)
    try:
        for text in ("4,16 @@ patch " * 2000, "中文🙂\u2028\u2029" * 12000, "finished"):
            if connection.recv() != "append":
                return
            event = store.append_ordered_event(
                EventRecord(
                    session_id=session_id,
                    event_type="model_text_delta",
                    payload={"text": text},
                )
            )
            connection.send((event.sequence, len(text)))
    finally:
        connection.close()


def test_another_process_can_follow_a_large_record_at_each_live_boundary(tmp_path) -> None:
    observer = LocalSessionStore(tmp_path)
    observer.initialize()
    session = SessionRecord(title="Two processes")
    observer.create_session(session)
    assert observer.latest_event_sequence(session.session_id) == 0
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe()
    process = context.Process(target=_write_events, args=(tmp_path, session.session_id, writer))
    process.start()
    writer.close()
    try:
        for expected in range(1, 4):
            reader.send("append")
            assert reader.poll(10), "writer did not reach the next event boundary"
            sequence, text_length = reader.recv()
            assert sequence == expected
            assert observer.latest_event_sequence(session.session_id) == sequence
            page, truncated = observer.load_events_after(
                session.session_id, after_sequence=sequence - 1
            )
            assert not truncated
            assert [event.sequence for event in page] == [sequence]
            assert len(page[0].payload["text"]) == text_length
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            reader.send("stop")
            process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        reader.close()
        process.close()
