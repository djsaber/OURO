from __future__ import annotations

from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.session_models import SessionUndoResult
from ouro.application.session_recovery import SessionRecoveryService
from ouro.core.base import new_id
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.messages import ContentBlock, MessageRecord, ModelMessage, TextContent
from ouro.core.session_commits import SessionCommit, SessionCommitResult
from ouro.core.state import ACTIVE_RUN_STATUSES, RESUMABLE_RUN_STATUSES, RunRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionStore


def continuable_run(snapshot: RuntimeSessionSnapshot) -> RunRecord:
    """Select the latest dialogue execution, independently of maintenance Runs."""

    if any(
        run.status in ACTIVE_RUN_STATUSES and run.history_epoch == snapshot.session.history_epoch
        for run in snapshot.runs
    ):
        raise SessionBusyError(
            "Finish or abort the active execution or pending interaction before continuing"
        )
    retained_runs = {message.run_id for message in snapshot.messages}
    run = next(
        (
            run
            for run in reversed(snapshot.runs)
            if run.kind == "agent"
            and (run.history_epoch == snapshot.session.history_epoch or run.run_id in retained_runs)
        ),
        None,
    )
    if run is None:
        raise ValueError("There is no retained agent input to continue")
    if (
        run.history_epoch == snapshot.session.history_epoch
        and run.status not in RESUMABLE_RUN_STATUSES
    ):
        raise ValueError(
            f"The latest agent run is {run.status}; there is no unfinished execution to continue"
        )
    if not any(message.role in {"user", "system"} for message in snapshot.messages):
        raise ValueError("The run has no retained input to continue")
    return run


def undo_user_input(
    store: SessionStore,
    session_id: str,
    *,
    message_id: str | None = None,
    queue_id: str | None = None,
) -> SessionUndoResult:
    """Select a direct input once, then cancel or withdraw that exact identity."""
    if message_id is not None and queue_id is not None:
        raise ValueError("Choose either message_id or queue_id")
    queue = QueuedInputQueue(store)
    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        if message_id is None:
            pending = (
                queue.find_session(session, queue_id)
                if queue_id is not None
                else next(
                    (
                        item
                        for item in reversed(queue.eligible_session(session))
                        if item.source == "user" and item.role == "user" and not item.scaffold
                    ),
                    None,
                )
            )
            if pending is not None:
                if pending.source != "user" or pending.role != "user" or pending.scaffold:
                    raise ValueError("Undo requires a directly submitted user input")
                committed = _commit_undo(
                    store,
                    SessionCommit(
                        before=session,
                        after=queue.without_inputs(session, (pending.queue_id,)),
                        events=(
                            EventRecord(
                                session_id=session_id,
                                run_id=session.active_run_id,
                                event_type="queued_input_cancelled",
                                payload={"queue_id": pending.queue_id, "source": "user"},
                            ),
                        ),
                    ),
                )
                content: list[ContentBlock] = []
                if pending.skill_activation is not None:
                    content.append(pending.skill_activation)
                if pending.prompt:
                    content.append(TextContent(text=pending.prompt))
                return SessionUndoResult(
                    session=committed.session,
                    input=ModelMessage(role="user", content=content),
                    resource_refs=pending.resource_refs,
                    queue_id=pending.queue_id,
                    cancelled_queue_ids=(pending.queue_id,),
                )
            if queue_id is not None:
                consumed = store.find_queued_input_message(session_id, queue_id)
                if consumed is None:
                    raise ValueError("The selected queue input is no longer available")
                message_id = consumed.message_id
    # Orphaned active work uses the ordinary recovery command before taking ownership.
    if session.active_run_id is not None and not store.is_session_locked(session_id):
        run = store.load_run(session_id, session.active_run_id)
        if run.status in {"queued", "running"}:
            SessionRecoveryService(store).recover_runtime(session_id)
    lock = store.acquire_session_lock(session_id, new_id("undo"))
    if lock is None:
        raise SessionBusyError("Abort the active run before undoing a message")
    try:
        with store.session_mutation(session_id):
            session = store.load_session_record(session_id)
            if session.active_run_id is not None:
                raise SessionBusyError("Abort the active run before undoing a message")
            selected: MessageRecord | None
            if message_id is not None:
                selected = store.load_message(session_id, message_id)
                if not _direct_user_message(selected):
                    raise ValueError("Undo requires a directly submitted user input")
            else:
                # Work is bounded by the selected suffix, not the retained conversation.
                limit = 32
                while True:
                    tail = store.load_messages_tail(session_id, limit=limit)
                    selected = next((m for m in tail if _direct_user_message(m)), None)
                    if selected is not None:
                        break
                    if len(tail) < limit:
                        raise ValueError("There is no user message to undo")
                    limit *= 2
            after, cancelled = queue.after_withdrawal(session)
            committed = _commit_undo(
                store,
                SessionCommit(
                    before=session,
                    after=after,
                    before_message_id=selected.message_id,
                    events=(
                        EventRecord(
                            session_id=session_id,
                            event_type="session_history_withdrawn",
                            payload={
                                "before_message_id": selected.message_id,
                                "history_epoch": session.history_epoch + 1,
                                "cancelled_queue_ids": list(cancelled),
                            },
                        ),
                    ),
                ),
            )
        store.refresh_catalog(session_id)
        assert committed.truncation is not None
        return SessionUndoResult(
            session=committed.session,
            input=ModelMessage(role="user", content=selected.content),
            resource_refs=tuple(selected.resource_refs),
            message_id=selected.message_id,
            queue_id=selected.metadata.get("queued_input_id"),
            run_id=selected.run_id,
            removed_count=committed.truncation.removed_count,
            cancelled_queue_ids=cancelled,
        )
    finally:
        lock.release()


def _direct_user_message(message: MessageRecord) -> bool:
    return (
        message.role == "user"
        and not message.scaffold
        and message.metadata.get("session_input_source") == "user"
    )


def _commit_undo(store: SessionStore, request: SessionCommit) -> SessionCommitResult:
    try:
        return store.commit_session(request)
    except (OSError, PersistenceError):
        committed = store.load_commit(request.before.session_id, request.operation_id)
        if committed is None:
            raise
        return committed
