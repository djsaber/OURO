from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs import RunLifecycle
from ouro.core.cancellation import AbortSignal
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.tools import ToolExecutionOutcome, ToolExecutor, ToolInvocation, ToolResult


class ToolCallCoordinator:
    """Coordinate one model-requested tool call across hooks, events, and storage."""

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        hooks: HookRegistry,
        events: RuntimeEventRecorder,
        runs: RunLifecycle,
        is_aborted: Callable[[str, str], bool],
    ) -> None:
        self.executor = executor
        self.hooks = hooks
        self.events = events
        self.runs = runs
        self.is_aborted = is_aborted

    async def handle(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        tool_call: dict[str, Any],
        *,
        message_sequence: int,
        assistant_text: str,
        assistant_reasoning: str,
        abort_signal: AbortSignal,
    ) -> tuple[ToolExecutionOutcome, int, list[MessageRecord]]:
        assistant_message = self.runs.append_assistant_tool_call_message(
            session=session,
            run=run,
            tool_call=tool_call,
            text=assistant_text,
            sequence=message_sequence,
            reasoning_content=assistant_reasoning,
        )
        invocation = ToolInvocation(
            session_id=session.session_id,
            run_id=run.run_id,
            tool_call_id=str(tool_call.get("id") or ""),
            tool_name=str(tool_call["name"]),
            arguments=dict(tool_call.get("arguments") or {}),
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_call_requested",
            payload={"tool_name": invocation.tool_name, "arguments": invocation.arguments},
        )
        before_result = await self.hooks.dispatch(
            HookContext(
                hook_name="before_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"tool_name": invocation.tool_name, "arguments": invocation.arguments},
            )
        )
        sequence = await self.events.record_hook_failures(
            emitted,
            sequence,
            before_result.failures,
        )
        outcome = await self.executor.execute(invocation, signal=abort_signal)
        if self.is_aborted(session.session_id, run.run_id):
            return outcome, sequence, [assistant_message]
        if outcome.status == "approval_required":
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_requested",
                payload={
                    "approval_id": outcome.approval.approval_id if outcome.approval else None,
                    "tool_name": invocation.tool_name,
                },
            )
            return outcome, sequence, [assistant_message]
        if outcome.status == "waiting_user":
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_requested",
                payload={"question": outcome.question},
            )
            return outcome, sequence, [assistant_message]

        messages = [assistant_message]
        if outcome.result is not None:
            sequence = await self.after(emitted, sequence, session, run, outcome.result)
            message = self.runs.append_tool_result_message(
                session=session,
                run=run,
                result=outcome.result,
                sequence=message_sequence + 1,
            )
            messages.append(message)
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="tool_call_completed",
                payload={
                    "tool_name": outcome.result.tool_name,
                    "is_error": outcome.result.is_error,
                },
            )
        return outcome, sequence, messages

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
