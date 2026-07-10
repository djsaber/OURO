from __future__ import annotations

from typing import Any

from ouro.application.events import RuntimeEventRecorder
from ouro.core.base import new_id, utc_now
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import Usage
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolResult
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

    def append_assistant_tool_call_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        tool_call: dict[str, Any],
        text: str,
        sequence: int,
    ) -> MessageRecord:
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=sequence,
        ).model_copy(
            update={
                "metadata": {
                    "tool_calls": [
                        {
                            "id": str(tool_call.get("id") or ""),
                            "name": str(tool_call.get("name") or ""),
                            "arguments": dict(tool_call.get("arguments") or {}),
                        }
                    ]
                }
            }
        )
        self._append_message(session=session, run=run, message=message)
        return message

    def append_tool_result_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
        sequence: int,
    ) -> MessageRecord:
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="tool_result",
            text=result.content,
            sequence=sequence,
        ).model_copy(
            update={
                "resource_refs": result.resource_refs,
                "metadata": {
                    "tool_call_id": result.tool_call_id,
                    "tool_name": result.tool_name,
                    "is_error": result.is_error,
                },
            }
        )
        self._append_message(session=session, run=run, message=message)
        return message

    def record_usage(self, run: RunRecord, usage: Usage) -> RunRecord:
        latest = find_run(
            self.store.load_session(run.session_id, recover=False).runs,
            run.run_id,
        )
        if latest.status == "aborted" or latest.abort_requested:
            return latest
        updated = latest.model_copy(update={"usage": merge_usage(latest.usage, usage)})
        self.store.save_run(updated)
        return updated

    def checkpoint(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        partial_assistant_text: str,
        event_sequence: int,
    ) -> tuple[SessionRecord, RunRecord]:
        snapshot = self.store.load_session(session.session_id, recover=False)
        latest_run = find_run(snapshot.runs, run.run_id)
        if latest_run.status == "aborted" or latest_run.abort_requested:
            return snapshot.session, latest_run

        message_count = max(len(messages), len(snapshot.messages), session.message_count)
        last_message_id: str | None
        if len(snapshot.messages) >= len(messages) and snapshot.messages:
            last_message_id = snapshot.messages[-1].message_id
        elif messages:
            last_message_id = messages[-1].message_id
        else:
            last_message_id = session.last_message_id

        metadata = dict(latest_run.metadata)
        metadata.update(run.metadata)
        raw_checkpoint = metadata.get("checkpoint")
        checkpoint = dict(raw_checkpoint) if isinstance(raw_checkpoint, dict) else {}
        checkpoint.update(
            {
                "run_id": run.run_id,
                "message_count": message_count,
                "last_message_id": last_message_id,
                "event_sequence": event_sequence,
                "updated_at": utc_now(),
            }
        )
        if partial_assistant_text:
            checkpoint["partial_assistant_text"] = partial_assistant_text
            checkpoint["partial_assistant_message_id"] = checkpoint.get(
                "partial_assistant_message_id",
                new_id("msg"),
            )
            checkpoint["partial_assistant_sequence"] = message_count + 1
        else:
            checkpoint.pop("partial_assistant_text", None)
            checkpoint.pop("partial_assistant_message_id", None)
            checkpoint.pop("partial_assistant_sequence", None)
        metadata["checkpoint"] = checkpoint

        updated_run = run.model_copy(update={"metadata": metadata})
        updated_session = snapshot.session.model_copy(
            update={
                "status": session.status,
                "updated_at": utc_now(),
                "last_run_id": run.run_id,
                "last_message_id": last_message_id,
                "message_count": message_count,
            }
        )
        self.store.save_run(updated_run)
        self.store.save_session(updated_session)
        return updated_session, updated_run

    def save_waiting_state(self, *, session: SessionRecord, run: RunRecord) -> None:
        snapshot = self.store.load_session(session.session_id, recover=False)
        last_message_id = (
            snapshot.messages[-1].message_id if snapshot.messages else session.last_message_id
        )
        updated_session = snapshot.session.model_copy(
            update={
                "status": session.status,
                "updated_at": utc_now(),
                "last_run_id": run.run_id,
                "last_message_id": last_message_id,
                "message_count": len(snapshot.messages),
            }
        )
        self.store.save_run(run)
        self.store.save_session(updated_session)
        self.store.rebuild_index()

    async def complete(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult:
        if final_text:
            assistant_message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=final_text,
                sequence=len(messages) + 1,
            )
            self._append_message(session=session, run=run, message=assistant_message)
            messages.append(assistant_message)

        latest_session = self.store.load_session(session.session_id, recover=False).session
        last_message_id = messages[-1].message_id if messages else session.last_message_id
        completed_run = run.model_copy(
            update={
                "status": "completed",
                "ended_at": utc_now(),
                "pending_approval_id": None,
                "metadata": clear_pending_metadata(run.metadata),
            }
        )
        completed_session = latest_session.model_copy(
            update={
                "status": "idle",
                "updated_at": utc_now(),
                "last_message_id": last_message_id,
                "message_count": len(messages),
                "last_run_id": completed_run.run_id,
            }
        )
        self.store.save_run(completed_run)
        self.store.save_session(completed_session)
        self.store.rebuild_index()
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
        tool_results: list[ToolResult],
    ) -> RunResult:
        if final_text:
            snapshot = self.store.load_session(session.session_id, recover=False)
            assistant_message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=final_text,
                sequence=len(snapshot.messages) + 1,
            ).model_copy(
                update={
                    "metadata": {
                        "incomplete": True,
                        "failure_reason": error,
                    }
                }
            )
            self._append_message(session=session, run=run, message=assistant_message)

        snapshot = self.store.load_session(session.session_id, recover=False)
        last_message_id = (
            snapshot.messages[-1].message_id if snapshot.messages else session.last_message_id
        )
        failed_run = run.model_copy(
            update={
                "status": "failed",
                "ended_at": utc_now(),
                "error": error,
            }
        )
        failed_session = snapshot.session.model_copy(
            update={
                "status": "failed",
                "updated_at": utc_now(),
                "last_message_id": last_message_id,
                "message_count": len(snapshot.messages),
                "last_run_id": failed_run.run_id,
            }
        )
        self.store.save_run(failed_run)
        self.store.save_session(failed_session)
        self.store.rebuild_index()
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
        self._checkpoint_appended_message(session=session, run=run, message=message)

    def _checkpoint_appended_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        message: MessageRecord,
    ) -> None:
        snapshot = self.store.load_session(session.session_id, recover=False)
        latest_run = find_run(snapshot.runs, run.run_id)
        if latest_run.status == "aborted" or latest_run.abort_requested:
            return
        updated_session = snapshot.session.model_copy(
            update={
                "updated_at": utc_now(),
                "last_run_id": run.run_id,
                "last_message_id": message.message_id,
                "message_count": max(snapshot.session.message_count, len(snapshot.messages)),
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
    for key in ("pending_invocation", "pending_approval", "pending_question"):
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
