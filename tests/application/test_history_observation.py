import pytest

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runtime_activity import (
    project_runtime_activity_state,
    reduce_runtime_activity,
)
from ouro.application.sessions import SessionService
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tui.state import TuiSessionProjection


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "home")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    result.initialize()
    return result


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
        assert late.sequence > withdrawn.events[0].sequence
        activity = project_runtime_activity_state(withdrawn.session, None, sequence=0)
        assert reduce_runtime_activity(activity, late).activity == "idle"
        projection = TuiSessionProjection(session.session_id, history_epoch=1)
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
    assert (
        service.messages_at_history(
            session.session_id, (message.message_id,), history_epoch=0
        ).messages
        == committed.messages
    )
    store.commit_session(
        SessionCommit(before=committed.session, before_message_id=message.message_id)
    )
    stale = service.messages_at_history(session.session_id, (message.message_id,), history_epoch=0)
    assert stale.history_epoch == 1 and not stale.messages
    with pytest.raises(PersistenceError, match="message not found"):
        service.messages_at_history(session.session_id, (message.message_id,), history_epoch=1)
