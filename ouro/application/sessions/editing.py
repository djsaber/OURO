from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager

from ouro.application.contracts.sessions import SessionUndoResult
from ouro.application.inputs.queue import PendingInputs, QueuedInputQueue, queued_input_event
from ouro.application.inputs.requests import InputSessionSettings, UserMessageEdit
from ouro.application.sessions.history import input_resource_refs
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.usage import UsageService
from ouro.core.attachment_input import is_direct_user_input
from ouro.core.base import new_id
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.inputs import InputRecord, InputStateChange
from ouro.core.messages import ContentBlock, MessageRecord, ModelMessage, TextContent
from ouro.core.session_commits import (
    HistoryBoundaryUnavailable,
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
)
from ouro.core.session_ports import (
    PendingInputReader,
    RuntimeSessionSnapshot,
    SessionObservationReader,
    SessionRepository,
    SessionStore,
)
from ouro.core.state import ACTIVE_RUN_STATUSES, RESUMABLE_RUN_STATUSES, RunRecord, SessionRecord


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
                        if item.source == "user" and item.role == "user"
                    ),
                    None,
                )
            )
            if pending is not None:
                if pending.source != "user" or pending.role != "user":
                    raise ValueError("Undo requires a directly submitted user input")
                committed = _commit_history_change(
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
                    session=committed.session,
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
    with _idle_history(store, session) as session:
        if message_id is not None:
            selected = store.load_message(session_id, message_id)
        else:
            # The store iterator starts at the head and stops at the first direct input.
            with closing(store.iter_messages_before(session_id)) as messages:
                latest = next((m for _, m in messages if is_direct_user_input(m)), None)
            if latest is None:
                raise ValueError("There is no user message to undo")
            selected = latest
        boundary = _input_boundary(store, selected)
        event, cancelled = _withdrawal_event(store, session, boundary.message_id)
        committed = _commit_history_change(
            store,
            SessionCommit(before=session, before_message_id=boundary.message_id, events=(event,)),
        )
    store.refresh_catalog(session_id)
    return SessionUndoResult(
        session=committed.session,
        input=ModelMessage(
            role="user",
            content=[] if selected.semantic_tag == "attachments" else selected.content,
        ),
        resource_refs=input_resource_refs(selected) or tuple(boundary.resource_refs),
        attachments=selected.input_attachments,
        message_id=selected.message_id,
        queue_id=selected.input_id,
        run_id=selected.run_id,
        removed_count=session.message_count - committed.session.message_count,
        cancelled_queue_ids=cancelled,
    )


def read_edit_boundary(
    store: SessionRepository,
    session: SessionRecord,
    edit: UserMessageEdit,
    usage: UsageService | None,
) -> tuple[str, HarnessCallSnapshot]:
    """Resolve the complete submitted input and the policy state before it."""
    if session.session_kind != "primary":
        raise ValueError("Editing a user message requires a primary session")
    if session.active_run_id is not None:
        raise SessionBusyError("Abort the active run before editing a message")
    if (session.history_epoch, session.message_count) != (edit.history_epoch, edit.message_count):
        raise SessionCommitConflict("Session history changed. Select the message again to edit it.")
    with store.session_read(session.session_id):
        selected = store.load_message(session.session_id, edit.message_id)
        boundary = _input_boundary(store, selected)
        if boundary.before_harness_version is None:
            raise HistoryBoundaryUnavailable("Message has no recoverable Harness boundary")
        version = store.load_harness(session.session_id, boundary.before_harness_version)
        return boundary.message_id, HarnessCallSnapshot(
            session_id=session.session_id,
            harness_version=version.version_id,
            history_epoch=session.history_epoch + 1,
            last_message_id=(
                store.message_id_at(session.session_id, boundary.sequence - 1)
                if boundary.sequence > 1
                else None
            ),
            state=version.state,
            usage_tokens=usage.session_scope_tokens(session.session_id) if usage else None,
        )


def accept_message_edit(
    store: SessionStore,
    before: SessionRecord,
    boundary_id: str,
    replacement: InputRecord,
    settings: InputSessionSettings,
) -> SessionCommitResult:
    with _idle_history(store, before) as current:
        # The original head and Harness remain commit preconditions across policy evaluation.
        event, cancelled = _withdrawal_event(store, current, boundary_id)
        queued = queued_input_event(
            current, replacement.item, len(current.pending_input_ids) - len(cancelled) + 1
        )
        configured = settings.apply(before)
        result = _commit_history_change(
            store,
            SessionCommit(
                before=before,
                after=configured if configured is not before else None,
                before_message_id=boundary_id,
                inputs=(replacement,),
                prepend_inputs=True,
                events=(event, queued),
            ),
        )
    store.refresh_catalog(before.session_id)
    return result


def _input_boundary(store: SessionObservationReader, selected: MessageRecord) -> MessageRecord:
    if not is_direct_user_input(selected):
        raise ValueError("Editing requires a directly submitted user input")
    first_id = selected.metadata.get("input_context_message_id") or selected.metadata.get(
        "attachment_message_id"
    )
    if isinstance(first_id, str):
        return store.load_message(selected.session_id, first_id)
    return selected


@contextmanager
def _idle_history(store: SessionStore, session: SessionRecord) -> Iterator[SessionRecord]:
    session_id = session.session_id
    if session.active_run_id is not None and not store.is_session_locked(session_id):
        run = store.load_run(session_id, session.active_run_id)
        if run.status in {"queued", "running"}:
            SessionRecoveryService(store).recover_runtime(session_id)
    lock = store.acquire_session_lock(session_id, new_id("edit"))
    if lock is None:
        raise SessionBusyError("Abort the active run before editing a message")
    try:
        with store.session_mutation(session_id):
            session = store.load_session_record(session_id)
            if session.active_run_id is not None:
                raise SessionBusyError("Abort the active run before editing a message")
            yield session
    finally:
        lock.release()


def _withdrawal_event(
    store: PendingInputReader, session: SessionRecord, boundary_id: str
) -> tuple[EventRecord, tuple[str, ...]]:
    cancelled = PendingInputs(store).cancelled_by_withdrawal(session)
    event = EventRecord(
        session_id=session.session_id,
        event_type="session_history_withdrawn",
        payload={
            "before_message_id": boundary_id,
            "history_epoch": session.history_epoch + 1,
            "cancelled_queue_ids": list(cancelled),
        },
    )
    return event, cancelled


def _commit_history_change(store: SessionRepository, request: SessionCommit) -> SessionCommitResult:
    try:
        return store.commit_session(request)
    except SessionCommitConflict, HistoryBoundaryUnavailable:
        raise
    except OSError, PersistenceError:
        with store.session_mutation(request.before.session_id):
            current = store.load_session_record(request.before.session_id)
            if current.last_commit_id != request.operation_id:
                raise
            return SessionCommitResult(operation_id=request.operation_id, session=current)
