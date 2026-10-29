from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ouro.application.events import RuntimeEventRecorder
from ouro.application.inputs.queue import QueuedInputQueue
from ouro.application.observations.activity import (
    project_runtime_activity_state,
    reduce_runtime_activity,
)
from ouro.application.observations.content import SessionContentProjection, restore_session_content
from ouro.application.sessions.service import SessionService
from ouro.core.approvals import ApprovalRecord
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolInputRequest, ToolInvocation
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.inputs import input_path
from tests.response_drafts import persist_response_draft
from tests.tool_batches import persist_tool_batch


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "home")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    result.initialize()
    return result


def test_current_snapshot_restores_draft_and_interactions_without_event_history(store, monkeypatch):
    session = SessionRecord(status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="current-model")
    session.active_run_id = run.run_id
    store.create_session(session)
    store.save_run(run)
    persist_response_draft(store, run, turn_id="turn_current", text="current", reasoning="think")

    service = SessionService(store)
    for limit in (None, 3):
        snapshot = service.observation_snapshot(session.session_id, history_limit=limit)
        assert all(event.stream is not None for event in snapshot.events)
        assert snapshot.position.revision == store.observation_revision(session.session_id)
        projection = restore_session_content(snapshot)
        assert projection.assistant_text == "current"
        assert projection.reasoning_text == "think"
        assert projection.model == "current-model"
        assert projection.activity.activity == "streaming_text"
    run = store.load_run(session.session_id, run.run_id)
    approval = ApprovalRecord(session_id=session.session_id, run_id=run.run_id, tool_call_id="edit")
    batch = ToolCallBatchRecord(
        turn_id="turn_wait",
        assistant_message_id="msg_wait",
        phase="awaiting_interaction",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(id="edit", name="Edit", source_index=0),
                status="awaiting_approval",
                approval=approval,
            ),
            ToolBatchCall(
                request=ToolCallRequest(id="ask", name="Ask", source_index=1),
                status="waiting_user",
                input_request=ToolInputRequest(data={"question": "Which target?"}),
                invocation=ToolInvocation(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    turn_id="turn_wait",
                    tool_call_id="ask",
                    tool_name="Ask",
                    canonical_tool_id="test/tools/ask",
                    plugin_id="test",
                    plugin_class="user",
                ),
            ),
        ],
    )
    run = persist_tool_batch(
        store,
        run.model_copy(update={"status": "waiting_approval", "phase": "human_interaction"}),
        batch,
    )
    snapshot = service.observation_snapshot(session.session_id, history_limit=3)
    projection = restore_session_content(snapshot)
    first, second = projection.pending_interactions
    assert (first.kind, first.request_id, first.tool_call_id) == (
        "approval",
        approval.approval_id,
        "edit",
    )
    assert second.kind == "question"
    assert second.request == {"question": "Which target?"}
    assert (second.canonical_tool_id, second.plugin_id, second.plugin_class) == (
        "test/tools/ask",
        "test",
        "user",
    )
    assert projection.activity.activity == "waiting_approval"
    store.save_run(run.model_copy(update={"abort_requested": True}))
    stopped = service.observation_snapshot(session.session_id, history_limit=3)
    assert not restore_session_content(stopped).pending_interactions


def test_observing_invalid_pending_input_does_not_repair_durable_state(store):
    session = SessionRecord()
    store.create_session(session)
    queue = QueuedInputQueue(store)
    session, item, _, _ = queue.enqueue(
        snapshot=store.load_runtime_session(session.session_id),
        prompt="accepted",
        title=None,
        role="user",
        scaffold=False,
    )
    if isinstance(store, LocalSessionStore):
        path = input_path(store.sessions_dir / session.session_id, item.queue_id)
        path.write_text("{}")
    else:
        with store.session_mutation(session.session_id):
            store._transaction_connection().execute(
                "UPDATE inputs SET payload = '{}' WHERE session_id = ? AND input_id = ?",
                (session.session_id, item.queue_id),
            )
    service = SessionService(store)

    with pytest.raises(PersistenceError):
        service.observation_snapshot(session.session_id)
    assert store.load_session_record(session.session_id) == session
    assert store.load_input_state(session.session_id, item.queue_id).status == "pending"


async def test_late_run_event_keeps_its_original_epoch_after_undo(store):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="completed")
    store.save_run(run)
    message = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="user", text="withdraw"
    )
    committed = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    withdrawn = store.commit_session(
        SessionCommit(
            before=committed.session,
            before_message_id=message.message_id,
            events=(
                EventRecord(session_id=session.session_id, event_type="session_history_withdrawn"),
            ),
        )
    )
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())
    try:
        late = await recorder.publish(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_text_delta",
            payload={"text": "old"},
        )
        assert withdrawn.events[0].history_epoch == 1
        assert late.history_epoch == 0
        activity = project_runtime_activity_state(withdrawn.session, None, sequence=0)
        assert reduce_runtime_activity(activity, late).activity == "idle"
        projection = SessionContentProjection(session.session_id, history_epoch=1)
        assert projection.apply(late) is None
        assert projection.assistant_text == ""
    finally:
        await recorder.close()


def test_event_message_read_distinguishes_history_change_from_missing_current_data(store):
    session = SessionRecord()
    store.create_session(session)
    service = SessionService(store)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="old")
    committed = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    selected = service.messages_at_history(
        session.session_id, (message.message_id,), history_epoch=0
    ).messages
    assert tuple(message.as_fact() for message in selected) == committed.messages
    store.commit_session(
        SessionCommit(before=committed.session, before_message_id=message.message_id)
    )
    stale = service.messages_at_history(session.session_id, (message.message_id,), history_epoch=0)
    assert stale.history_epoch == 1 and not stale.messages
    with pytest.raises(PersistenceError, match="message not found"):
        service.messages_at_history(session.session_id, (message.message_id,), history_epoch=1)


def test_history_reference_and_message_share_read_boundary_during_rewrite(store, monkeypatch):
    session = SessionRecord()
    store.create_session(session)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="original")
    committed = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    writer = (
        SQLiteSessionStore(store.database)
        if isinstance(store, SQLiteSessionStore)
        else LocalSessionStore(store.root)
    )
    entered = Event()

    def rewrite():
        entered.set()
        return writer.commit_session(
            SessionCommit(before=committed.session, before_message_id=message.message_id)
        )

    original = store.load_message
    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = None

        def read_during_rewrite(session_id, message_id):
            nonlocal writing
            writing = pool.submit(rewrite)
            assert entered.wait(5)
            if isinstance(store, SQLiteSessionStore):
                writing.result(timeout=5)
            return original(session_id, message_id)

        monkeypatch.setattr(store, "load_message", read_during_rewrite)
        page = SessionService(store).messages_at_history(
            session.session_id, (message.message_id,), history_epoch=0
        )
        assert writing is not None
        assert writing.result(timeout=5).session.history_epoch == 1
    assert page.history_epoch == 0
    assert tuple(message.as_fact() for message in page.messages) == committed.messages
    assert (
        SessionService(store)
        .messages_at_history(session.session_id, (message.message_id,), history_epoch=0)
        .history_epoch
        == 1
    )


def test_failed_read_releases_boundary_for_another_writer(store):
    session = SessionRecord()
    store.create_session(session)
    with pytest.raises(PersistenceError, match="message not found"):
        SessionService(store).messages_at_history(
            session.session_id, ("msg_missing",), history_epoch=0
        )
    # Use another thread so a leaked thread-reentrant file lock cannot mask the failure.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            store.save_session,
            session.model_copy(update={"title": "changed"}),
        )
        future.result(timeout=5)
    assert SessionService(store).observation_snapshot(session.session_id).session.title == "changed"
