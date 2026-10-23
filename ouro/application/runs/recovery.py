from __future__ import annotations

from typing import cast

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.content import ContentPart
from ouro.core.events import EventRecord
from ouro.core.state import RESUMABLE_RUN_STATUSES, RunRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord, ToolCallStatus
from ouro.core.tools import ToolCallProducer, ToolResult
from ouro.sessions import RuntimeSessionSnapshot, SessionRepository


class RunRecoveryService:
    """Close interrupted tool protocol exchanges before a new run starts."""

    def __init__(
        self,
        *,
        store: SessionRepository,
        runs: RunLifecycle,
        events: RuntimeEventRecorder,
    ) -> None:
        self.store = store
        self.runs = runs
        self.events = events

    async def reconcile(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        run_ids: frozenset[str] | None = None,
    ) -> RuntimeSessionSnapshot:
        sequence = await run_blocking(self.store.latest_event_sequence, snapshot.session.session_id)
        emitted: list[EventRecord] = []
        changed = False
        messages = list(snapshot.messages)
        existing_results = {
            (message.run_id, message.tool_result.tool_call_id): message
            for message in messages
            if message.tool_result is not None
        }

        for run in snapshot.runs:
            if run.history_epoch != snapshot.session.history_epoch:
                continue
            if run_ids is not None and run.run_id not in run_ids:
                continue
            if run.status not in RESUMABLE_RUN_STATUSES:
                continue
            batch = run.active_tool_batch
            calls_by_id = (
                {call.request.id: call for call in batch.calls} if batch is not None else {}
            )
            updated_calls = dict(calls_by_id)
            run_changed = False
            requests = [
                (message.turn_id, request, ToolCallProducer())
                for message in messages
                if message.run_id == run.run_id
                for request in message.tool_calls
            ]
            if batch is not None and batch.producer.kind == "driver":
                requests.extend(
                    (batch.turn_id, call.request, batch.producer) for call in batch.calls
                )
            for turn_id, request, producer in requests:
                result_key = (run.run_id, request.id)
                existing = existing_results.get(result_key)
                call = calls_by_id.get(request.id)
                if existing is not None:
                    if call is not None:
                        assert existing.tool_result is not None
                        result = call.result or ToolResult(
                            tool_call_id=request.id,
                            tool_name=request.name,
                            content=cast(list[ContentPart], existing.content),
                            is_error=existing.tool_result.is_error,
                            resource_refs=existing.resource_refs,
                            started_at=(
                                existing.tool_execution.started_at
                                if existing.tool_execution is not None
                                else existing.created_at
                            ),
                            ended_at=(
                                existing.tool_execution.ended_at
                                if existing.tool_execution is not None
                                else None
                            ),
                        )
                        updated_calls[request.id] = call.model_copy(
                            update={
                                "result": result,
                                "result_message_id": existing.message_id,
                                "status": self._recovered_call_status(result),
                            }
                        )
                        run_changed |= updated_calls[request.id] != call
                    continue
                result = self._recovery_result(request.id, request.name, call)
                result_message = await run_blocking(
                    self.runs.append_tool_result_message,
                    session=snapshot.session,
                    run=run,
                    result=result,
                    sequence=len(messages) + 1,
                    turn_id=turn_id,
                    producer=producer,
                )
                messages.append(result_message)
                existing_results[result_key] = result_message
                changed = True
                run_changed = True
                if call is not None:
                    updated_calls[request.id] = call.model_copy(
                        update={
                            "status": self._recovered_call_status(result),
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
                        "turn_id": turn_id,
                        "tool_call_id": request.id,
                        "tool_name": request.name,
                        "outcome_unknown": bool(result.structured.get("outcome_unknown")),
                    },
                )
            if (
                batch is not None
                and run.driver_checkpoint is not None
                and run.driver_checkpoint.state == "pending"
                and all(call.result is not None for call in updated_calls.values())
            ):
                run_changed = True
            if run_changed:
                changed = True
                await run_blocking(self._save_reconciled_run, run, batch, updated_calls)
            await run_blocking(self.runs.finish_run, run)

        if not changed:
            return snapshot
        return await run_blocking(self.store.load_runtime_session, snapshot.session.session_id)

    @staticmethod
    def _recovery_result(
        tool_call_id: str,
        tool_name: str,
        call: ToolBatchCall | None,
    ) -> ToolResult:
        if (
            call is not None
            and call.result is not None
            and call.result.tool_call_id == tool_call_id
            and call.result.tool_name == tool_name
        ):
            return call.result
        invalid_result_identity = call is not None and call.result is not None
        outcome_unknown = call is not None and call.status in {
            "running",
            "resolving_input",
            "unknown_outcome",
        }
        if invalid_result_identity:
            content = (
                "Tool execution produced a durable result for a different tool call. "
                "The invalid result was discarded to preserve the tool protocol."
            )
        elif outcome_unknown:
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
                "invalid_result_identity": invalid_result_identity,
            },
            ended_at=utc_now(),
        )

    @staticmethod
    def _recovered_call_status(result: ToolResult) -> ToolCallStatus:
        if result.structured.get("outcome_unknown"):
            return "unknown_outcome"
        if result.structured.get("recovered_interruption"):
            return "aborted"
        return "failed" if result.is_error else "completed"

    def _save_reconciled_run(
        self,
        run: RunRecord,
        batch: ToolCallBatchRecord | None,
        calls_by_id: dict[str, ToolBatchCall],
    ) -> None:
        metadata = dict(run.metadata)
        metadata["transcript_reconciled"] = True
        checkpoint = run.driver_checkpoint
        ready = (
            batch is not None
            and checkpoint is not None
            and checkpoint.state == "pending"
            and checkpoint.batch_id == batch.batch_id
            and all(call.result is not None for call in calls_by_id.values())
        )
        if ready:
            assert checkpoint is not None
            checkpoint = checkpoint.model_copy(update={"state": "ready"})
        if batch is not None:
            batch = batch.model_copy(
                update={
                    "phase": "completed" if ready else "recoverable",
                    "calls": [calls_by_id[call.request.id] for call in batch.calls],
                    "updated_at": utc_now(),
                }
            )
        self.store.save_run(
            run.model_copy(
                update={
                    "active_tool_batch": batch,
                    "driver_checkpoint": checkpoint,
                    "metadata": metadata,
                }
            )
        )
