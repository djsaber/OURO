from __future__ import annotations

import asyncio
from collections.abc import Callable

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs import RunLifecycle
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.errors import SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import (
    ToolBatchCall,
    ToolBatchHandlingResult,
    ToolBatchOutcomeStatus,
    ToolCallBatchRecord,
    ToolExecutionMode,
)
from ouro.core.tools import ToolCallRequest, ToolExecutionOutcome, ToolInvocation, ToolResult
from ouro.tools import ToolBatchExecutor, ToolExecutor, ToolRegistry


class ToolCallCoordinator:
    """Coordinate complete assistant tool-call batches across application boundaries."""

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        registry: ToolRegistry,
        hooks: HookRegistry,
        events: RuntimeEventRecorder,
        runs: RunLifecycle,
        execution_config: ToolExecutionConfig,
        is_aborted: Callable[[str, str], bool],
    ) -> None:
        self.executor = executor
        self.batch_executor = ToolBatchExecutor(executor)
        self.registry = registry
        self.hooks = hooks
        self.events = events
        self.runs = runs
        self.execution_config = execution_config
        self.is_aborted = is_aborted

    async def handle_batch(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        tool_calls: list[ToolCallRequest],
        *,
        turn_id: str,
        message_sequence: int,
        assistant_text: str,
        assistant_reasoning: str,
        abort_signal: AbortSignal,
    ) -> ToolBatchHandlingResult:
        assistant_message = self.runs.append_assistant_tool_batch_message(
            session=session,
            run=run,
            tool_calls=tool_calls,
            text=assistant_text,
            sequence=message_sequence,
            turn_id=turn_id,
            reasoning_content=assistant_reasoning,
        )
        batch = ToolCallBatchRecord(
            turn_id=turn_id,
            assistant_message_id=assistant_message.message_id,
            execution_mode=self._effective_mode(tool_calls),
            max_concurrency=self.execution_config.max_concurrency,
            calls=[ToolBatchCall(request=request) for request in tool_calls],
        )
        self.runs.save_batch_state(
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_preflight",
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_batch_planned",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": turn_id,
                "tool_call_count": len(tool_calls),
                "execution_mode": batch.execution_mode,
            },
        )

        calls: list[ToolBatchCall] = []
        for request in tool_calls:
            call, sequence = await self._preflight_call(
                emitted,
                sequence,
                session=session,
                run=run,
                batch_id=batch.batch_id,
                turn_id=turn_id,
                request=request,
                abort_signal=abort_signal,
            )
            calls.append(call)
        batch = batch.model_copy(update={"calls": calls, "updated_at": utc_now()})

        pending_approvals = batch.pending_approvals
        waiting_user = next((call for call in calls if call.status == "waiting_user"), None)
        if pending_approvals or waiting_user is not None:
            phase = "awaiting_interaction"
            status: ToolBatchOutcomeStatus = (
                "waiting_approval" if pending_approvals else "waiting_user"
            )
            batch = batch.model_copy(update={"phase": phase, "updated_at": utc_now()})
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status=status,
                phase="human_interaction",
            )
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_status_changed",
                payload={
                    "to_status": status,
                    "status": status,
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                },
            )
            return ToolBatchHandlingResult(
                status=status,
                batch=batch,
                messages=[assistant_message],
                question=waiting_user.result.content
                if waiting_user is not None and waiting_user.result is not None
                else None,
                sequence=sequence,
            )

        batch = batch.model_copy(update={"phase": "ready", "updated_at": utc_now()})
        self.runs.save_batch_state(
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_execution",
        )
        executed = await self.execute_batch(
            emitted,
            sequence,
            session=session,
            run=run,
            batch=batch,
            message_sequence=message_sequence + 1,
            abort_signal=abort_signal,
        )
        return executed.model_copy(update={"messages": [assistant_message, *executed.messages]})

    async def execute_batch(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        message_sequence: int,
        abort_signal: AbortSignal,
    ) -> ToolBatchHandlingResult:
        ready_calls = [call for call in batch.calls if call.status == "ready" and call.invocation]
        batch = batch.model_copy(update={"phase": "executing", "updated_at": utc_now()})
        self.runs.save_batch_state(
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_execution",
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_batch_started",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "execution_mode": batch.execution_mode,
                "tool_calls": [
                    {
                        "tool_call_id": call.request.id,
                        "tool_name": call.request.name,
                        "status": call.status,
                        "source_index": call.request.source_index,
                    }
                    for call in batch.calls
                ],
            },
        )
        checkpoint_batch = batch
        checkpoint_lock = asyncio.Lock()

        async def checkpoint_started(invocation: ToolInvocation) -> None:
            nonlocal checkpoint_batch, sequence
            async with checkpoint_lock:
                started_call: ToolBatchCall | None = None
                calls: list[ToolBatchCall] = []
                for item in checkpoint_batch.calls:
                    if item.request.id != invocation.tool_call_id:
                        calls.append(item)
                        continue
                    started_call = item.model_copy(update={"status": "running"})
                    calls.append(started_call)
                if started_call is None:
                    raise ValueError(f"unknown tool call in batch: {invocation.tool_call_id}")
                checkpoint_batch = checkpoint_batch.model_copy(
                    update={"calls": calls, "updated_at": utc_now()}
                )
                self.runs.save_batch_state(
                    session=session,
                    run=run,
                    batch=checkpoint_batch,
                    status="running",
                    phase="tool_execution",
                )
                sequence = await self.events.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="tool_call_started",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": batch.turn_id,
                        "tool_call_id": invocation.tool_call_id,
                        "tool_name": invocation.tool_name,
                        "source_index": started_call.request.source_index,
                    },
                )

        async def checkpoint_outcome(outcome: ToolExecutionOutcome) -> None:
            nonlocal checkpoint_batch, sequence
            async with checkpoint_lock:
                checkpoint_calls: list[ToolBatchCall] = []
                completed_call: ToolBatchCall | None = None
                for item in checkpoint_batch.calls:
                    if item.request.id != outcome.invocation.tool_call_id:
                        checkpoint_calls.append(item)
                        continue
                    result = outcome.result
                    status = (
                        "aborted"
                        if outcome.status == "aborted"
                        else "failed"
                        if result is not None and result.is_error
                        else "completed"
                    )
                    completed_call = item.model_copy(update={"status": status, "result": result})
                    checkpoint_calls.append(completed_call)
                checkpoint_batch = checkpoint_batch.model_copy(
                    update={"calls": checkpoint_calls, "updated_at": utc_now()}
                )
                try:
                    self.runs.save_batch_state(
                        session=session,
                        run=run,
                        batch=checkpoint_batch,
                        status="running",
                        phase="tool_execution",
                    )
                except SessionBusyError:
                    if not abort_signal.is_aborted():
                        raise
                if completed_call is not None and completed_call.result is not None:
                    result = completed_call.result
                    sequence = await self.events.emit(
                        emitted,
                        sequence,
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="tool_call_completed",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": result.tool_call_id,
                            "tool_name": result.tool_name,
                            "status": completed_call.status,
                            "is_error": result.is_error,
                            "source_index": completed_call.request.source_index,
                        },
                    )

        await self.batch_executor.execute(
            [call.invocation for call in ready_calls if call.invocation is not None],
            mode=batch.execution_mode,
            max_concurrency=batch.max_concurrency,
            signal=abort_signal,
            on_start=checkpoint_started,
            on_outcome=checkpoint_outcome,
        )
        batch = checkpoint_batch.model_copy(
            update={
                "phase": "committing_results",
                "updated_at": utc_now(),
            }
        )
        try:
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status="running",
                phase="result_commit",
            )
        except SessionBusyError:
            if not abort_signal.is_aborted():
                raise
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_batch_results_committing",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "tool_result_count": sum(call.result is not None for call in batch.calls),
            },
        )

        messages: list[MessageRecord] = []
        results: list[ToolResult] = []
        committed_calls: list[ToolBatchCall] = []
        for call in sorted(batch.calls, key=lambda item: item.request.source_index):
            if call.result is None:
                committed_calls.append(call)
                continue
            sequence = await self.after(emitted, sequence, session, run, call.result)
            message = self.runs.append_tool_result_message(
                session=session,
                run=run,
                result=call.result,
                sequence=message_sequence + len(messages),
                turn_id=batch.turn_id,
            )
            messages.append(message)
            results.append(call.result)
            committed_calls.append(
                call.model_copy(update={"result_message_id": message.message_id})
            )

        terminal_phase = "aborted" if abort_signal.is_aborted() else "completed"
        completed_batch = batch.model_copy(
            update={
                "phase": terminal_phase,
                "calls": committed_calls,
                "updated_at": utc_now(),
            }
        )
        if terminal_phase == "aborted":
            self.runs.save_aborted_batch(
                session_id=session.session_id,
                run_id=run.run_id,
                batch=completed_batch,
            )
        else:
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=None,
                status="running",
                phase="model_streaming",
            )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type=(
                "tool_batch_aborted" if terminal_phase == "aborted" else "tool_batch_completed"
            ),
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "tool_result_count": len(results),
            },
        )
        return ToolBatchHandlingResult(
            status="completed",
            batch=completed_batch,
            messages=messages,
            results=results,
            sequence=sequence,
        )

    def resolve_approval(
        self,
        batch: ToolCallBatchRecord,
        *,
        approval_id: str,
        approved: bool,
        reason: str,
        arguments_override: dict[str, object] | None,
    ) -> ToolCallBatchRecord:
        resolved_calls: list[ToolBatchCall] = []
        found = False
        for call in batch.calls:
            approval = call.approval
            if approval is None or approval.approval_id != approval_id:
                resolved_calls.append(call)
                continue
            found = True
            if call.status != "awaiting_approval" or call.invocation is None:
                raise ValueError(f"approval is not pending: {approval_id}")
            if approved:
                prepared = self.executor.preflight(
                    call.invocation,
                    approved=True,
                    arguments_override=arguments_override,
                )
                resolved_approval = approval.model_copy(
                    update={
                        "status": "modified" if arguments_override is not None else "approved",
                        "resolved_at": utc_now(),
                        "modified_arguments": arguments_override,
                    }
                )
                if prepared.status == "ready":
                    resolved_calls.append(
                        call.model_copy(
                            update={
                                "status": "ready",
                                "invocation": prepared.invocation,
                                "approval": resolved_approval,
                            }
                        )
                    )
                else:
                    resolved_calls.append(
                        call.model_copy(
                            update={
                                "status": "failed",
                                "invocation": prepared.invocation,
                                "approval": resolved_approval,
                                "result": prepared.result,
                            }
                        )
                    )
            else:
                result = self.executor.rejected_result(call.invocation, reason)
                resolved_calls.append(
                    call.model_copy(
                        update={
                            "status": "rejected",
                            "approval": approval.model_copy(
                                update={
                                    "status": "rejected",
                                    "resolved_at": utc_now(),
                                    "reason": reason,
                                }
                            ),
                            "result": result,
                        }
                    )
                )
        if not found:
            raise ValueError(f"unknown approval: {approval_id}")
        phase = (
            "ready"
            if all(
                call.status not in {"awaiting_approval", "waiting_user"} for call in resolved_calls
            )
            else "awaiting_interaction"
        )
        return batch.model_copy(
            update={"calls": resolved_calls, "phase": phase, "updated_at": utc_now()}
        )

    def resolve_user_answer(
        self,
        batch: ToolCallBatchRecord,
        *,
        tool_call_id: str,
        answer: str,
    ) -> ToolCallBatchRecord:
        resolved_calls: list[ToolBatchCall] = []
        found = False
        for call in batch.calls:
            if call.request.id != tool_call_id:
                resolved_calls.append(call)
                continue
            found = True
            if call.status != "waiting_user" or call.invocation is None:
                raise ValueError(f"tool call is not waiting for user input: {tool_call_id}")
            resolved_calls.append(
                call.model_copy(
                    update={
                        "status": "completed",
                        "result": ToolResult(
                            tool_call_id=call.request.id,
                            tool_name=call.request.name,
                            content=answer,
                            structured={"answer": answer},
                            ended_at=utc_now(),
                        ),
                    }
                )
            )
        if not found:
            raise ValueError(f"unknown tool call: {tool_call_id}")
        phase = (
            "ready"
            if all(
                call.status not in {"awaiting_approval", "waiting_user"} for call in resolved_calls
            )
            else "awaiting_interaction"
        )
        return batch.model_copy(
            update={"calls": resolved_calls, "phase": phase, "updated_at": utc_now()}
        )

    async def after(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
    ) -> int:
        hook_result = await self.hooks.dispatch(
            HookContext(
                hook_name="after_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"result": result.model_dump(mode="json")},
            )
        )
        return await self.events.record_hook_failures(
            emitted,
            sequence,
            hook_result.failures,
        )

    async def _preflight_call(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch_id: str,
        turn_id: str,
        request: ToolCallRequest,
        abort_signal: AbortSignal,
    ) -> tuple[ToolBatchCall, int]:
        invocation = ToolInvocation(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=turn_id,
            tool_call_id=request.id,
            tool_name=request.name,
            arguments=request.arguments,
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_call_requested",
            payload={
                "batch_id": batch_id,
                "turn_id": turn_id,
                "tool_call_id": request.id,
                "tool_name": request.name,
                "arguments": request.arguments,
                "source_index": request.source_index,
            },
        )
        before_result = await self.hooks.dispatch(
            HookContext(
                hook_name="before_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"tool_name": request.name, "arguments": request.arguments},
            )
        )
        sequence = await self.events.record_hook_failures(
            emitted,
            sequence,
            before_result.failures,
        )
        if abort_signal.is_aborted() or self.is_aborted(session.session_id, run.run_id):
            result = ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                is_error=True,
                content=abort_signal.reason,
                structured={"aborted": True},
                ended_at=utc_now(),
            )
            return ToolBatchCall(
                request=request, status="aborted", invocation=invocation, result=result
            ), sequence

        outcome = self.executor.preflight(invocation, approval_mode=session.approval_mode)
        if outcome.status == "approval_required":
            assert outcome.approval is not None
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_requested",
                payload={
                    "batch_id": batch_id,
                    "turn_id": turn_id,
                    "approval_id": outcome.approval.approval_id,
                    "tool_call_id": request.id,
                    "tool_name": request.name,
                },
            )
            return ToolBatchCall(
                request=request,
                status="awaiting_approval",
                invocation=outcome.invocation,
                approval=outcome.approval,
            ), sequence
        if outcome.status == "waiting_user":
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_requested",
                payload={
                    "batch_id": batch_id,
                    "turn_id": turn_id,
                    "tool_call_id": request.id,
                    "question": outcome.question,
                },
            )
            return ToolBatchCall(
                request=request,
                status="waiting_user",
                invocation=outcome.invocation,
                result=outcome.result,
            ), sequence
        if outcome.status == "ready":
            return ToolBatchCall(
                request=request, status="ready", invocation=outcome.invocation
            ), sequence
        return ToolBatchCall(
            request=request,
            status="failed" if outcome.result and outcome.result.is_error else "completed",
            invocation=outcome.invocation,
            result=outcome.result,
        ), sequence

    def _effective_mode(self, calls: list[ToolCallRequest]) -> ToolExecutionMode:
        if self.execution_config.mode == "sequential":
            return "sequential"
        for call in calls:
            try:
                definition = self.registry.get(call.name).definition
            except KeyError:
                continue
            if definition.execution_mode == "sequential":
                return "sequential"
        return "parallel"
