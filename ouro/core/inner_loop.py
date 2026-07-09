from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.messages import MessageRecord
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import Usage
from ouro.tools import ToolExecutionOutcome, ToolRegistry, ToolResult


class EventEmitter(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int: ...


class HookFailureRecorder(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        failures,
    ) -> int: ...


class ToolCallHandler(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        tool_call: dict[str, Any],
        *,
        message_sequence: int,
        assistant_text: str,
        abort_signal: AbortSignal,
    ) -> tuple[ToolExecutionOutcome, int, list[MessageRecord]]: ...


class UsageRecorder(Protocol):
    def __call__(self, run: RunRecord, usage: Usage) -> RunRecord: ...


class RunCheckpointer(Protocol):
    def __call__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        partial_assistant_text: str,
        event_sequence: int,
    ) -> tuple[SessionRecord, RunRecord]: ...


class AbortResultFactory(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str,
        emitted: list[EventRecord],
        tool_results: list[ToolResult],
    ) -> RunResult | None: ...


class RunCompleter(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult: ...


class WaitingStatePersister(Protocol):
    def __call__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
    ) -> None: ...


class RunFailer(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        error: str,
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult: ...


@dataclass(frozen=True)
class InnerLoopPorts:
    emit: EventEmitter
    record_hook_failures: HookFailureRecorder
    handle_tool_call: ToolCallHandler
    record_usage: UsageRecorder
    checkpoint: RunCheckpointer
    aborted_result: AbortResultFactory
    save_waiting_state: WaitingStatePersister
    complete_run: RunCompleter
    fail_run: RunFailer


class InnerAgentLoop:
    """Drive model rounds and tool continuations for one active run.

    The inner loop intentionally does not create sessions, acquire locks, drain
    follow-up input, or decide resume entry points. Those lifecycle concerns
    belong to the outer runtime service.
    """

    def __init__(
        self,
        *,
        adapter: RuntimeAdapter,
        tools: ToolRegistry,
        hooks: HookRegistry,
        context_builder: ContextBuilder,
        max_model_rounds: int,
        ports: InnerLoopPorts,
    ) -> None:
        self.adapter = adapter
        self.tools = tools
        self.hooks = hooks
        self.context_builder = context_builder
        self.max_model_rounds = max_model_rounds
        self.ports = ports

    async def run(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        sequence: int,
        emitted: list[EventRecord],
        final_parts: list[str] | None = None,
        tool_results: list[ToolResult] | None = None,
        abort_signal: AbortSignal,
    ) -> RunResult:
        final_parts = final_parts or []
        tool_results = tool_results or []

        for model_round in range(1, self.max_model_rounds + 1):
            round_parts: list[str] = []
            saw_model_done = False
            abort_result = self.ports.aborted_result(
                session_id=session.session_id,
                run_id=run.run_id,
                final_text="".join(final_parts),
                emitted=emitted,
                tool_results=tool_results,
            )
            if abort_result is not None:
                return abort_result
            hook_context = HookContext(
                hook_name="after_context_build",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"message_count": len(messages), "model_round": model_round},
            )
            hook_result = await self.hooks.dispatch(hook_context)
            sequence = await self.ports.record_hook_failures(
                emitted,
                sequence,
                hook_result.failures,
            )
            added_context = [
                text for result in hook_result.results for text in result.added_context
            ]
            context = self.context_builder.build(
                messages=messages,
                tools=self.tools.definitions(),
                added_context=added_context,
            )

            sequence = await self.ports.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="model_request_started",
                payload={"model": self.adapter.model.model_id, "round": model_round},
            )
            session, run = self.ports.checkpoint(
                session=session,
                run=run,
                messages=messages,
                partial_assistant_text="".join(final_parts),
                event_sequence=sequence,
            )

            completed_tool_in_round = False
            try:
                async for provider_event in self.adapter.stream(context, signal=abort_signal):
                    if provider_event.usage is not None:
                        run = self.ports.record_usage(run, provider_event.usage)
                    abort_result = self.ports.aborted_result(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        final_text="".join([*final_parts, *round_parts]),
                        emitted=emitted,
                        tool_results=tool_results,
                    )
                    if abort_result is not None:
                        return abort_result
                    if provider_event.type == "text_delta" and provider_event.text:
                        round_parts.append(provider_event.text)
                        sequence = await self.ports.emit(
                            emitted,
                            sequence,
                            session_id=session.session_id,
                            run_id=run.run_id,
                            event_type="model_text_delta",
                            payload={"text": provider_event.text, "round": model_round},
                        )
                        session, run = self.ports.checkpoint(
                            session=session,
                            run=run,
                            messages=messages,
                            partial_assistant_text="".join([*final_parts, *round_parts]),
                            event_sequence=sequence,
                        )
                    elif provider_event.type == "tool_call" and provider_event.tool_call:
                        assistant_text = "".join(round_parts)
                        round_parts = []
                        outcome, sequence, new_messages = await self.ports.handle_tool_call(
                            emitted,
                            sequence,
                            session,
                            run,
                            provider_event.tool_call,
                            message_sequence=len(messages) + 1,
                            assistant_text=assistant_text,
                            abort_signal=abort_signal,
                        )
                        abort_result = self.ports.aborted_result(
                            session_id=session.session_id,
                            run_id=run.run_id,
                            final_text="".join([*final_parts, *round_parts]),
                            emitted=emitted,
                            tool_results=tool_results,
                        )
                        if abort_result is not None:
                            return abort_result
                        if outcome.result is not None:
                            tool_results.append(outcome.result)
                        if new_messages:
                            messages.extend(new_messages)
                            completed_tool_in_round = True
                            session, run = self.ports.checkpoint(
                                session=session,
                                run=run,
                                messages=messages,
                                partial_assistant_text="".join([*final_parts, *round_parts]),
                                event_sequence=sequence,
                            )
                        if outcome.status in {"approval_required", "waiting_user"}:
                            run = _run_with_pending_outcome(run, outcome)
                            session = session.model_copy(
                                update={
                                    "status": _session_status_for_run(run.status),
                                    "updated_at": utc_now(),
                                }
                            )
                            session, run = self.ports.checkpoint(
                                session=session,
                                run=run,
                                messages=messages,
                                partial_assistant_text="".join(final_parts),
                                event_sequence=sequence,
                            )
                            self.ports.save_waiting_state(session=session, run=run)
                            return await self._save_waiting_state(
                                emitted=emitted,
                                session=session,
                                run=run,
                                final_text="".join(final_parts),
                                tool_results=tool_results,
                            )
                    elif provider_event.type == "error":
                        return await self.ports.fail_run(
                            emitted,
                            sequence,
                            session=session,
                            run=run,
                            error=provider_event.error or "provider error",
                            final_text="".join([*final_parts, *round_parts]),
                            tool_results=tool_results,
                        )
                    elif provider_event.type == "done":
                        saw_model_done = True
                        sequence = await self.ports.emit(
                            emitted,
                            sequence,
                            session_id=session.session_id,
                            run_id=run.run_id,
                            event_type="model_message_completed",
                            payload={
                                "round": model_round,
                                "usage": provider_event.usage.model_dump(mode="json")
                                if provider_event.usage is not None
                                else {},
                            },
                        )
                        session, run = self.ports.checkpoint(
                            session=session,
                            run=run,
                            messages=messages,
                            partial_assistant_text="".join([*final_parts, *round_parts]),
                            event_sequence=sequence,
                        )
            except Exception as exc:
                return await self.ports.fail_run(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    error=str(exc) or exc.__class__.__name__,
                    final_text="".join([*final_parts, *round_parts]),
                    tool_results=tool_results,
                )

            abort_result = self.ports.aborted_result(
                session_id=session.session_id,
                run_id=run.run_id,
                final_text="".join([*final_parts, *round_parts]),
                emitted=emitted,
                tool_results=tool_results,
            )
            if abort_result is not None:
                return abort_result

            if completed_tool_in_round:
                continue

            if not saw_model_done:
                return await self.ports.fail_run(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    error="provider stream ended without a completion event",
                    final_text="".join([*final_parts, *round_parts]),
                    tool_results=tool_results,
                )

            if not round_parts:
                return await self.ports.fail_run(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    error="provider returned an empty assistant message",
                    final_text="".join(final_parts),
                    tool_results=tool_results,
                )

            final_parts.extend(round_parts)
            return await self.ports.complete_run(
                emitted,
                sequence,
                session=session,
                run=run,
                messages=messages,
                final_text="".join(final_parts),
                tool_results=tool_results,
            )

        return await self.ports.fail_run(
            emitted,
            sequence,
            session=session,
            run=run,
            error=f"model round limit exceeded: {self.max_model_rounds}",
            final_text="".join(final_parts),
            tool_results=tool_results,
        )

    async def _save_waiting_state(
        self,
        *,
        emitted: list[EventRecord],
        session: SessionRecord,
        run: RunRecord,
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult:
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )


def _session_status_for_run(status: str) -> str:
    if status == "waiting_approval":
        return "waiting_approval"
    if status == "waiting_user":
        return "waiting_user"
    if status == "failed":
        return "failed"
    return "running"


def _run_with_pending_outcome(run: RunRecord, outcome: ToolExecutionOutcome) -> RunRecord:
    metadata = dict(run.metadata)
    if outcome.invocation is not None:
        metadata["pending_invocation"] = outcome.invocation.model_dump(mode="json")
    if outcome.approval is not None:
        metadata["pending_approval"] = outcome.approval.model_dump(mode="json")
    if outcome.question is not None:
        metadata["pending_question"] = outcome.question
    return run.model_copy(
        update={
            "status": _run_status_for_outcome(outcome.status),
            "pending_approval_id": outcome.approval.approval_id if outcome.approval else None,
            "metadata": metadata,
        }
    )


def _run_status_for_outcome(status: str) -> str:
    if status == "approval_required":
        return "waiting_approval"
    if status == "waiting_user":
        return "waiting_user"
    return "running"
