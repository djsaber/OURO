from __future__ import annotations

from ouro.application.contracts.sessions import SessionUndoResult
from ouro.application.inputs.queue import QueuedInputQueue
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.core.attachment_input import is_editable_user_input
from ouro.core.base import new_id
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.inputs import InputStateChange
from ouro.core.messages import ContentBlock, MessageRecord, ModelMessage, TextContent
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.state import ACTIVE_RUN_STATUSES, RESUMABLE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionStore


def continuable_run(
    store: SessionStore, snapshot: RuntimeSessionSnapshot, *, allow_completed: bool = False
) -> RunRecord:
    """Select the latest dialogue execution, independently of maintenance Runs."""

    with store.session_read(snapshot.session.session_id):
        if (
            store.load_session_record(snapshot.session.session_id).history_epoch
            != snapshot.session.history_epoch
        ):
            raise SessionCommitConflict("Continuation belongs to withdrawn Session history")
        if any(
            run.status in ACTIVE_RUN_STATUSES
            and run.history_epoch == snapshot.session.history_epoch
            for run in snapshot.runs
        ):
            raise SessionBusyError(
                "Finish or abort the active execution or pending interaction before continuing"
            )
        run = next(
            (
                run
                for run in reversed(store.load_runs(snapshot.session.session_id))
                if run.kind == "agent"
                and (
                    run.history_epoch == snapshot.session.history_epoch
                    or store.has_run_messages(snapshot.session.session_id, run.run_id)
                )
            ),
            None,
        )
        if run is None:
            raise ValueError("There is no retained agent input to continue")
        if (
            run.history_epoch == snapshot.session.history_epoch
            and run.status not in RESUMABLE_RUN_STATUSES
            and not (allow_completed and run.status == "completed")
        ):
            raise ValueError(
                f"The latest agent run is {run.status}; "
                "there is no unfinished execution to continue"
            )
        if not store.has_input_messages(snapshot.session.session_id):
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
                        input_changes=(
                            InputStateChange(input_id=pending.queue_id, status="cancelled"),
                        ),
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
                content.extend(pending.skill_activations)
                if pending.prompt:
                    content.append(TextContent(text=pending.prompt))
                return SessionUndoResult(
                    session=committed,
                    input=ModelMessage(role="user", content=content),
                    resource_refs=pending.resource_refs,
                    attachments=pending.attachments,
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
            attachment_id = selected.metadata.get("attachment_message_id")
            boundary = (
                store.load_message(session_id, attachment_id)
                if isinstance(attachment_id, str)
                else selected
            )
            after, cancelled = queue.after_withdrawal(session)
            committed = _commit_undo(
                store,
                SessionCommit(
                    before=session,
                    after=after,
                    before_message_id=boundary.message_id,
                    events=(
                        EventRecord(
                            session_id=session_id,
                            event_type="session_history_withdrawn",
                            payload={
                                "before_message_id": boundary.message_id,
                                "history_epoch": session.history_epoch + 1,
                                "cancelled_queue_ids": list(cancelled),
                            },
                        ),
                    ),
                ),
            )
        store.refresh_catalog(session_id)
        return SessionUndoResult(
            session=committed,
            input=ModelMessage(
                role="user",
                content=[] if selected.semantic_tag == "attachments" else selected.content,
            ),
            resource_refs=tuple(boundary.resource_refs),
            attachments=selected.input_attachments,
            message_id=selected.message_id,
            queue_id=selected.input_id,
            run_id=selected.run_id,
            removed_count=session.message_count - committed.message_count,
            cancelled_queue_ids=cancelled,
        )
    finally:
        lock.release()


def _direct_user_message(message: MessageRecord) -> bool:
    return is_editable_user_input(message) and (
        message.scaffold or message.metadata.get("session_input_source") == "user"
    )


def _commit_undo(store: SessionStore, request: SessionCommit) -> SessionRecord:
    try:
        return store.commit_session(request).session
    except OSError, PersistenceError:
        with store.session_mutation(request.before.session_id):
            current = store.load_session_record(request.before.session_id)
            if current.last_commit_id != request.operation_id:
                raise
            return current
