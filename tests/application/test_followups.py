from ouro.application.followups import FOLLOW_UP_QUEUE_KEY, FollowUpQueue
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore


def test_follow_up_queue_persists_items_fifo_and_clears_when_empty(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Follow ups")
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, status="running"))
    queue = FollowUpQueue(store)

    snapshot = store.load_session(session.session_id, recover=False)
    session, size = queue.enqueue(snapshot=snapshot, prompt="first", title=None)
    snapshot = store.load_session(session.session_id, recover=False)
    session, size = queue.enqueue(snapshot=snapshot, prompt="second", title="Second")

    snapshot = store.load_session(session.session_id, recover=False)
    assert size == 2
    assert snapshot.session.metadata[FOLLOW_UP_QUEUE_KEY][0]["prompt"] == "first"
    assert snapshot.session.metadata[FOLLOW_UP_QUEUE_KEY][1]["title"] == "Second"

    first = queue.pop(snapshot)
    snapshot = store.load_session(session.session_id, recover=False)
    second = queue.pop(snapshot)
    snapshot = store.load_session(session.session_id, recover=False)

    assert first is not None
    assert second is not None
    assert first.prompt == "first"
    assert first.title is None
    assert second.prompt == "second"
    assert second.title == "Second"
    assert FOLLOW_UP_QUEUE_KEY not in snapshot.session.metadata


def test_follow_up_queue_removes_invalid_item_and_reports_no_prompt(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid follow up", metadata={FOLLOW_UP_QUEUE_KEY: [{}]})
    store.create_session(session)
    queue = FollowUpQueue(store)

    popped = queue.pop(store.load_session(session.session_id, recover=False))
    snapshot = store.load_session(session.session_id, recover=False)

    assert popped is None
    assert FOLLOW_UP_QUEUE_KEY not in snapshot.session.metadata
