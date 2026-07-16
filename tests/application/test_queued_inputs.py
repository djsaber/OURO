from concurrent.futures import ThreadPoolExecutor

from ouro.application.queued_inputs import QUEUED_INPUTS_KEY, QueuedInputQueue
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore


def test_queued_input_queue_persists_items_fifo_and_clears_when_empty(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queued inputs")
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, status="running"))
    queue = QueuedInputQueue(store)

    snapshot = store.load_session(session.session_id)
    session, _, _ = queue.enqueue(snapshot=snapshot, prompt="first", title=None)
    snapshot = store.load_session(session.session_id)
    session, _, size = queue.enqueue(snapshot=snapshot, prompt="second", title="Second")

    snapshot = store.load_session(session.session_id)
    assert size == 2
    assert snapshot.session.metadata[QUEUED_INPUTS_KEY][0]["prompt"] == "first"
    assert snapshot.session.metadata[QUEUED_INPUTS_KEY][1]["title"] == "Second"

    first = queue.peek(snapshot)
    assert first is not None
    queue.acknowledge(snapshot, first.queue_id)
    snapshot = store.load_session(session.session_id)
    second = queue.peek(snapshot)
    assert second is not None
    queue.acknowledge(snapshot, second.queue_id)
    snapshot = store.load_session(session.session_id)

    assert first.prompt == "first"
    assert first.title is None
    assert second.prompt == "second"
    assert second.title == "Second"
    assert QUEUED_INPUTS_KEY not in snapshot.session.metadata


def test_queued_input_queue_removes_invalid_item(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid input", metadata={QUEUED_INPUTS_KEY: [{}]})
    store.create_session(session)
    queue = QueuedInputQueue(store)

    item = queue.peek(store.load_session(session.session_id))
    snapshot = store.load_session(session.session_id)

    assert item is None
    assert QUEUED_INPUTS_KEY not in snapshot.session.metadata


def test_queued_input_queue_requires_explicit_acknowledgement(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    queued_session, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="second",
        title=None,
    )
    snapshot = store.load_session(queued_session.session_id)

    first = queue.peek(snapshot)
    repeated = queue.peek(snapshot)

    assert first is not None
    assert repeated is not None
    assert repeated.queue_id == first.queue_id
    queue.acknowledge(snapshot, first.queue_id)
    assert queue.peek(store.load_session(session.session_id)) is None


def test_queued_input_queue_merges_concurrent_stale_snapshot_enqueues(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Concurrent queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    stale = store.load_session(session.session_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                queue.enqueue,
                snapshot=stale,
                prompt=f"prompt-{index}",
                title=None,
            )
            for index in range(20)
        ]
        for future in futures:
            future.result(timeout=2)

    reloaded = store.load_session(session.session_id)
    prompts = [item["prompt"] for item in reloaded.session.metadata[QUEUED_INPUTS_KEY]]
    assert sorted(prompts) == sorted(f"prompt-{index}" for index in range(20))
