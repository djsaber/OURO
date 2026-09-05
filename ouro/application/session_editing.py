from __future__ import annotations

from ouro.application.queued_inputs import QUEUED_INPUTS_KEY
from ouro.core.base import new_id
from ouro.core.errors import SessionBusyError
from ouro.core.messages import MessageRecord, TextContent
from ouro.core.state import ACTIVE_RUN_STATUSES, RESUMABLE_RUN_STATUSES, RunRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionStore


def continuable_run(snapshot: RuntimeSessionSnapshot) -> RunRecord:
    """Select the latest dialogue execution, independently of maintenance Runs."""

    if any(run.status in ACTIVE_RUN_STATUSES for run in snapshot.runs):
        raise SessionBusyError(
            "Finish or abort the active execution or pending interaction before continuing"
        )
    retained_runs = {message.run_id for message in snapshot.messages}
    run = next(
        (
            run
            for run in reversed(snapshot.runs)
            if run.kind == "agent" and run.run_id in retained_runs
        ),
        None,
    )
    if run is None:
        raise ValueError("There is no retained agent input to continue")
    if run.status not in RESUMABLE_RUN_STATUSES:
        raise ValueError(
            f"The latest agent run is {run.status}; there is no unfinished execution to continue"
        )
    if not any(
        message.run_id == run.run_id and message.role in {"user", "system"}
        for message in snapshot.messages
    ):
        raise ValueError("The run has no retained input to continue")
    return run


def undo_latest_user_message(store: SessionStore, session_id: str) -> MessageRecord:
    """Return editable input only after its canonical suffix has been removed."""

    lock = store.acquire_session_lock(session_id, new_id("undo"))
    if lock is None:
        raise SessionBusyError("Abort the active run before undoing a message")
    try:
        with store.session_mutation(session_id):
            session = store.load_session_record(session_id)
            latest_run = (
                store.load_run(session_id, session.last_run_id)
                if session.last_run_id is not None
                else None
            )
            if session.active_run_id is not None or (
                latest_run is not None and latest_run.status in ACTIVE_RUN_STATUSES
            ):
                raise SessionBusyError("Abort the active run before undoing a message")
            if session.controls.activations or store.load_session_extension_states(session_id):
                raise ValueError("Undo is unavailable while session extension state is present")
            if session.metadata.get(QUEUED_INPUTS_KEY):
                raise ValueError("Remove queued inputs before undoing a message")
            # Geometric tail reads bound work by the removed suffix. A long
            # retained conversation never needs to be loaded just to edit its tail.
            limit = 32
            while True:
                tail = store.load_messages_tail(session_id, limit=limit)
                selected = next((m for m in tail if m.role == "user" and not m.scaffold), None)
                if selected is not None:
                    break
                if len(tail) < limit:
                    raise ValueError("There is no user message to undo")
                limit *= 2
            if selected.resource_refs or any(
                not isinstance(block, TextContent) for block in selected.content
            ):
                raise ValueError("Undo currently requires a text-only user message")
            affected_runs = {
                message.run_id
                for message in tail[: tail.index(selected) + 1]
                if message.run_id is not None
            }
            if any(
                relation.parent_run_id in affected_runs
                for relation in store.list_delegations(
                    session.agent_root_session_id, parent_session_id=session_id
                )
            ):
                raise ValueError("Undo cannot remove a run that owns delegated agents")
            store.truncate_messages(
                session_id,
                before_message_id=selected.message_id,
                expected_last_message_id=tail[0].message_id,
            )
        store.refresh_catalog(session_id)
        return selected
    finally:
        lock.release()
