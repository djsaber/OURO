from __future__ import annotations

from typing import Any

from ouro.application.events import RuntimeEventRecorder
from ouro.core.base import utc_now
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.provider import Usage
from ouro.core.results import RunResult
from ouro.core.state import RunPhase, RunRecord, RunStatus, SessionRecord
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolResult
from ouro.sessions import SessionStore

ACTIVE_RUN_STATUSES = {"queued", "running", "waiting_user", "waiting_approval"}


class RunLifecycle:
    """Apply and persist run/session state transitions at durable boundaries."""

    def __init__(self, *, store: SessionStore, events: RuntimeEventRecorder) -> None:
        self.store = store
        self.events = events

    def append_user_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        prompt: str,
        sequence: int,
        metadata: dict[str, Any] | None = None,
    ) -> MessageRecord:
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="user",
            text=prompt,
            sequence=sequence,
        )
        if metadata:
            message = message.model_copy(update={"metadata": dict(metadata)})
        self._append_message(session=session, run=run, message=message)
        return message

    def append_assistant_tool_batch_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        tool_calls: list[ToolCallRequest],
        text: str,
        sequence: int,
        turn_id: str,
        reasoning_content: str = "",
    ) -> MessageRecord:
        existing = next(
            (
                message
                for message in self.store.load_session(session.session_id, recover=False).messages
                if message.run_id == run.run_id
                and message.turn_id == turn_id
                and message.role == "assistant"
                and message.tool_calls
            ),
            None,
        )
        if existing is not None:
            return existing
        metadata: dict[str, Any] = {"reasoning_content": reasoning_content}
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=sequence,
            turn_id=turn_id,
        ).model_copy(update={"metadata": metadata, "tool_calls": list(tool_calls)})
        self._append_message(session=session, run=run, message=message)
        return message

    def append_tool_result_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
        sequence: int,
        turn_id: str | None = None,
    ) -> MessageRecord:
        existing = next(
            (
                message
                for message in self.store.load_session(session.session_id, recover=False).messages
                if message.run_id == run.run_id
                and message.role == "tool_result"
                and message.tool_result is not None
                and message.tool_result.tool_call_id == result.tool_call_id
            ),
            None,
        )
        if existing is not None:
            return existing
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="tool_result",
            text=result.content,
            sequence=sequence,
            turn_id=turn_id,
        ).model_copy(
            update={
                "resource_refs": result.resource_refs,
                "tool_result": ToolResultReference(
                    tool_call_id=result.tool_call_id,
                    tool_name=result.tool_name,
                    is_error=result.is_error,
                ),
            }
        )
        self._append_message(session=session, run=run, message=message)
        return message

    def record_usage(self, run: RunRecord, usage: Usage) -> RunRecord:
        latest = self.store.load_run(run.session_id, run.run_id)
        if latest.status == "aborted" or latest.abort_requested:
            return latest
        updated = latest.model_copy(update={"usage": merge_usage(latest.usage, usage)})
        self.store.save_run(updated)
        return updated

    def save_waiting_state(self, *, session: SessionRecord, run: RunRecord) -> SessionRecord:
        latest_session = self.store.load_session_record(session.session_id)
        updated_session = latest_session.model_copy(
            update={
                "status": session.status,
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
                "last_run_id": run.run_id,
                "active_run_id": run.run_id,
                "last_message_id": latest_session.last_message_id,
                "message_count": latest_session.message_count,
            }
        )
        self.store.save_run(run)
        self.store.save_session(updated_session)
        self.store.try_update_index(updated_session.session_id)
        return updated_session

    def save_batch_state(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord | None,
        status: RunStatus,
        phase: RunPhase,
    ) -> tuple[SessionRecord, RunRecord]:
        latest_session = self.store.load_session_record(session.session_id)
        latest_run = self.store.load_run(session.session_id, run.run_id)
        if latest_run.status == "aborted" or latest_run.abort_requested:
            if batch is not None:
                latest_run = latest_run.model_copy(update={"active_tool_batch": batch})
                self.store.save_run(latest_run)
            return latest_session, latest_run
        pending = batch.pending_approvals if batch is not None else []
        updated_run = latest_run.model_copy(
            update={
                "status": status,
                "phase": phase,
                "active_tool_batch": batch,
                "pending_approval_id": pending[0].approval_id if pending else None,
            }
        )
        session_status = status if status in {"waiting_approval", "waiting_user"} else "running"
        updated_session = latest_session.model_copy(
            update={
                "status": session_status,
                "active_run_id": run.run_id,
                "last_run_id": run.run_id,
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
            }
        )
        self.store.save_run(updated_run)
        self.store.save_session(updated_session)
        self.store.try_update_index(updated_session.session_id)
        return updated_session, updated_run

    async def complete(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        final_text: str,
        reasoning_content: str = "",
        tool_results: list[ToolResult],
    ) -> RunResult:
        if final_text or reasoning_content:
            assistant_message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=final_text,
                sequence=len(messages) + 1,
            )
            if reasoning_content:
                assistant_message = assistant_message.model_copy(
                    update={
                        "metadata": {
                            **assistant_message.metadata,
                            "reasoning_content": reasoning_content,
                        }
                    }
                )
            self._append_message(session=session, run=run, message=assistant_message)
            messages.append(assistant_message)

        latest_session = self.store.load_session_record(session.session_id)
        last_message_id = messages[-1].message_id if messages else session.last_message_id
        completed_run = run.model_copy(
            update={
                "status": "completed",
                "ended_at": utc_now(),
                "pending_approval_id": None,
                "active_tool_batch": None,
                "metadata": clear_pending_metadata(run.metadata),
            }
        )
        completed_session = latest_session.model_copy(
            update={
                "status": "idle",
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
                "active_run_id": None,
                "last_message_id": last_message_id,
                "message_count": len(messages),
                "last_run_id": completed_run.run_id,
            }
        )
        self.store.save_run(completed_run)
        self.store.save_session(completed_session)
        self.store.try_update_index(completed_session.session_id)
        await self.events.emit(
            emitted,
            sequence,
            session_id=completed_session.session_id,
            run_id=completed_run.run_id,
            event_type="run_completed",
            payload={"final_text_length": len(final_text)},
        )
        return RunResult(
            session=completed_session,
            run=completed_run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    async def fail(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        error: str,
        final_text: str,
        reasoning_content: str = "",
        tool_results: list[ToolResult],
    ) -> RunResult:
        latest_session = self.store.load_session_record(session.session_id)
        if final_text or reasoning_content:
            metadata: dict[str, Any] = {
                "incomplete": True,
                "failure_reason": error,
            }
            if reasoning_content:
                metadata["reasoning_content"] = reasoning_content
            assistant_message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=final_text,
                sequence=latest_session.message_count + 1,
            ).model_copy(update={"metadata": metadata})
            self._append_message(session=session, run=run, message=assistant_message)

            latest_session = self.store.load_session_record(session.session_id)
        failed_run = run.model_copy(
            update={
                "status": "failed",
                "ended_at": utc_now(),
                "error": error,
                "active_tool_batch": None,
                "metadata": clear_pending_metadata(run.metadata),
            }
        )
        failed_session = latest_session.model_copy(
            update={
                "status": "failed",
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
                "active_run_id": None,
                "last_message_id": latest_session.last_message_id,
                "message_count": latest_session.message_count,
                "last_run_id": failed_run.run_id,
            }
        )
        self.store.save_run(failed_run)
        self.store.save_session(failed_session)
        self.store.try_update_index(failed_session.session_id)
        await self.events.emit(
            emitted,
            sequence,
            session_id=failed_session.session_id,
            run_id=failed_run.run_id,
            event_type="run_failed",
            payload={"error": error},
        )
        return RunResult(
            session=failed_session,
            run=failed_run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _append_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        message: MessageRecord,
    ) -> None:
        self.store.append_message(message)
        self._persist_appended_message(session=session, run=run, message=message)

    def _persist_appended_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        message: MessageRecord,
    ) -> None:
        latest_session = self.store.load_session_record(session.session_id)
        latest_run = self.store.load_run(session.session_id, run.run_id)
        if latest_run.status == "aborted" or latest_run.abort_requested:
            updated_session = latest_session.model_copy(
                update={
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                    "last_run_id": run.run_id,
                    "last_message_id": message.message_id,
                    "message_count": max(
                        latest_session.message_count,
                        session.message_count,
                        message.sequence,
                    ),
                }
            )
            self.store.save_session(updated_session)
            self.store.try_update_index(updated_session.session_id)
            return
        updated_session = latest_session.model_copy(
            update={
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
                "last_run_id": run.run_id,
                "active_run_id": run.run_id,
                "last_message_id": message.message_id,
                "message_count": max(
                    latest_session.message_count,
                    session.message_count,
                    message.sequence,
                ),
            }
        )
        self.store.save_session(updated_session)


def find_run(runs: list[RunRecord], run_id: str) -> RunRecord:
    for run in runs:
        if run.run_id == run_id:
            return run
    raise KeyError(f"unknown run: {run_id}")


def find_active_run(runs: list[RunRecord]) -> RunRecord:
    for run in reversed(runs):
        if run.status in ACTIVE_RUN_STATUSES:
            return run
    raise ValueError("no active run")


def maybe_active_run(runs: list[RunRecord]) -> RunRecord | None:
    try:
        return find_active_run(runs)
    except ValueError:
        return None


def clear_pending_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(metadata)
    for key in ("pending_invocation", "pending_approval", "pending_question", "checkpoint"):
        cleaned.pop(key, None)
    return cleaned


def merge_usage(current: dict[str, int | float], usage: Usage) -> dict[str, int | float]:
    merged = dict(current)
    for key, value in usage.model_dump().items():
        if value is None:
            continue
        existing = merged.get(key, 0)
        merged[key] = existing + value if isinstance(existing, int | float) else value
    return merged
