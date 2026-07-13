from __future__ import annotations

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs import RunLifecycle
from ouro.core.base import utc_now
from ouro.core.events import EventRecord
from ouro.core.state import RunRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord, ToolCallStatus
from ouro.core.tools import ToolResult
from ouro.sessions import SessionSnapshot, SessionStore


class RunRecoveryService:
    """Close interrupted tool protocol exchanges before a new run starts."""

    def __init__(
        self,
        *,
        store: SessionStore,
        runs: RunLifecycle,
        events: RuntimeEventRecorder,
    ) -> None:
        self.store = store
        self.runs = runs
        self.events = events

    async def reconcile(self, snapshot: SessionSnapshot) -> SessionSnapshot:
        sequence = len(snapshot.events)
        emitted: list[EventRecord] = []
        changed = False
        messages = list(snapshot.messages)
        existing_results = {
            (message.run_id, message.tool_result.tool_call_id)
            for message in messages
            if message.tool_result is not None
        }

        for run in snapshot.runs:
            if run.status != "recoverable":
                continue
            batch = run.active_tool_batch
            calls_by_id = (
                {call.request.id: call for call in batch.calls} if batch is not None else {}
            )
            updated_calls = dict(calls_by_id)
            run_changed = False
            for assistant in (
                message
                for message in messages
                if message.run_id == run.run_id and message.role == "assistant"
            ):
                for request in assistant.tool_calls:
                    result_key = (run.run_id, request.id)
                    if result_key in existing_results:
                        continue
                    call = calls_by_id.get(request.id)
                    result = self._recovery_result(request.id, request.name, call)
                    result_message = self.runs.append_tool_result_message(
                        session=snapshot.session,
                        run=run,
                        result=result,
                        sequence=len(messages) + 1,
                        turn_id=assistant.turn_id,
                    )
                    messages.append(result_message)
                    existing_results.add(result_key)
                    changed = True
                    run_changed = True
                    if call is not None:
                        updated_calls[request.id] = call.model_copy(
                            update={
                                "status": self._recovered_call_status(call, result),
                                "result": result,
                                "result_message_id": result_message.message_id,
                            }
                        )
                    sequence = await self.events.emit(
                        emitted,
                        sequence,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        event_type="recovery_tool_result_committed",
                        payload={
                            "turn_id": assistant.turn_id,
                            "tool_call_id": request.id,
                            "tool_name": request.name,
                            "outcome_unknown": bool(result.structured.get("outcome_unknown")),
                        },
                    )
            if run_changed:
                self._save_reconciled_run(run, batch, updated_calls)
            self.runs.finish_run(run)

        if not changed:
            return snapshot
        return self.store.load_session(snapshot.session.session_id, recover=False)

    @staticmethod
    def _recovery_result(
        tool_call_id: str,
        tool_name: str,
        call: ToolBatchCall | None,
    ) -> ToolResult:
        if call is not None and call.result is not None:
            return call.result
        outcome_unknown = call is not None and call.status in {
            "running",
            "unknown_outcome",
        }
        if outcome_unknown:
            content = (
                "Tool execution was interrupted and its outcome is unknown. "
                "Do not repeat side-effecting work without first verifying external state."
            )
        else:
            content = "Tool execution was interrupted before a durable result was produced."
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            is_error=True,
            content=content,
            structured={
                "recovered_interruption": True,
                "outcome_unknown": outcome_unknown,
            },
            ended_at=utc_now(),
        )

    @staticmethod
    def _recovered_call_status(call: ToolBatchCall, result: ToolResult) -> ToolCallStatus:
        if result.structured.get("outcome_unknown"):
            return "unknown_outcome"
        if call.result is not None:
            return "failed" if result.is_error else "completed"
        return "aborted"

    def _save_reconciled_run(
        self,
        run: RunRecord,
        batch: ToolCallBatchRecord | None,
        calls_by_id: dict[str, ToolBatchCall],
    ) -> None:
        metadata = dict(run.metadata)
        metadata["transcript_reconciled"] = True
        if batch is not None:
            batch = batch.model_copy(
                update={
                    "phase": "recoverable",
                    "calls": [calls_by_id[call.request.id] for call in batch.calls],
                    "updated_at": utc_now(),
                }
            )
        self.store.save_run(
            run.model_copy(update={"active_tool_batch": batch, "metadata": metadata})
        )
