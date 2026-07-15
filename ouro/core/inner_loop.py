from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.errors import ContextBlockedError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ProviderContinuationArtifact
from ouro.core.provider import ToolCallDelta, Usage
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy, RunBudgetReason, RunPauseReason
from ouro.core.state import RunRecord, SessionRecord, session_status_for_run
from ouro.core.tool_batches import ToolBatchHandlingResult
from ouro.core.tools import ToolCallRequest, ToolDefinition, ToolResult


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


class ToolBatchHandler(Protocol):
    async def __call__(
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
        assistant_continuation: ProviderContinuationArtifact | None,
        abort_signal: AbortSignal,
    ) -> ToolBatchHandlingResult: ...


class UsageRecorder(Protocol):
    def __call__(self, run: RunRecord, usage: Usage) -> RunRecord: ...


class ToolDefinitionProvider(Protocol):
    def definitions(self) -> list[ToolDefinition]: ...


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
        reasoning_content: str,
        provider_continuation: ProviderContinuationArtifact | None,
        turn_id: str | None = None,
        tool_results: list[ToolResult],
    ) -> RunResult: ...


class WaitingStatePersister(Protocol):
    def __call__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
    ) -> SessionRecord: ...


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
        reasoning_content: str,
        provider_continuation: ProviderContinuationArtifact | None,
        turn_id: str | None,
        tool_results: list[ToolResult],
    ) -> RunResult: ...


class RunPauser(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        reason: RunPauseReason,
        next_model_round: int,
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult: ...


class ContextPreparer(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        model_round: int,
        abort_signal: AbortSignal,
    ) -> tuple[ContextView, int]: ...


class TurnBoundaryInputConsumer(Protocol):
    async def __call__(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        assistant_text: str,
        assistant_reasoning: str,
        assistant_continuation: ProviderContinuationArtifact | None,
        turn_id: str,
    ) -> TurnBoundaryInputResult: ...


@dataclass(frozen=True)
class TurnBoundaryInputResult:
    sequence: int
    messages: list[MessageRecord] = field(default_factory=list)


@dataclass(frozen=True)
class InnerLoopPorts:
    prepare_context: ContextPreparer
    emit: EventEmitter
    record_hook_failures: HookFailureRecorder
    handle_tool_batch: ToolBatchHandler
    record_usage: UsageRecorder
    aborted_result: AbortResultFactory
    save_waiting_state: WaitingStatePersister
    complete_run: RunCompleter
    fail_run: RunFailer
    pause_run: RunPauser
    consume_turn_boundary_inputs: TurnBoundaryInputConsumer | None = None


@dataclass
class _LoopState:
    session: SessionRecord
    run: RunRecord
    messages: list[MessageRecord]
    sequence: int
    emitted: list[EventRecord]
    final_parts: list[str]
    tool_results: list[ToolResult]

    def final_text(self, round_parts: list[str] | None = None) -> str:
        return "".join([*self.final_parts, *(round_parts or [])])


@dataclass
class _RoundState:
    turn_id: str = field(default_factory=lambda: new_id("turn"))
    parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    continuation: ProviderContinuationArtifact | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_parts: dict[int, _ToolCallParts] = field(default_factory=dict)
    saw_model_done: bool = False
    completed_tool: bool = False


@dataclass
class _ToolCallParts:
    id: str = ""
    name: str = ""
    arguments: str = ""

    def append(self, delta: ToolCallDelta) -> None:
        self.id += delta.id_delta
        self.name += delta.name_delta
        self.arguments += delta.arguments_delta


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
        tools: ToolDefinitionProvider,
        run_budget: RunBudgetPolicy | None = None,
        ports: InnerLoopPorts,
    ) -> None:
        self.adapter = adapter
        self.tools = tools
        self.run_budget = run_budget or RunBudgetPolicy()
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
        state = _LoopState(
            session=session,
            run=run,
            messages=messages,
            sequence=sequence,
            emitted=emitted,
            final_parts=list(final_parts or []),
            tool_results=list(tool_results or []),
        )

        for model_round in itertools.count(1):
            if aborted := self._aborted_result(state):
                return aborted
            if reason := self._budget_reason(state.run, model_round):
                return await self.ports.pause_run(
                    state.emitted,
                    state.sequence,
                    session=state.session,
                    run=state.run,
                    reason=reason,
                    next_model_round=model_round,
                    final_text=state.final_text(),
                    tool_results=state.tool_results,
                )

            try:
                context = await self._build_context(state, model_round, abort_signal)
            except ContextBlockedError:
                return await self.ports.pause_run(
                    state.emitted,
                    state.sequence,
                    session=state.session,
                    run=state.run,
                    reason="context_blocked",
                    next_model_round=model_round,
                    final_text=state.final_text(),
                    tool_results=state.tool_results,
                )
            except Exception as exc:  # noqa: BLE001 - context preparation is a runtime boundary.
                return await self._fail(state, f"context preparation failed: {exc}")
            round_state = _RoundState()
            await self._start_round(state, model_round, round_state.turn_id)
            terminal_result = await self._consume_provider_stream(
                state=state,
                round_state=round_state,
                model_round=model_round,
                context=context,
                abort_signal=abort_signal,
            )
            if terminal_result is not None:
                return terminal_result
            if aborted := self._aborted_result(state, round_state.parts):
                return aborted
            if not round_state.saw_model_done:
                return await self._fail(
                    state,
                    "provider stream ended without a completion event",
                    round_state.parts,
                    round_state.reasoning_parts,
                    round_state.turn_id,
                    provider_continuation=round_state.continuation,
                )
            if round_state.tool_calls:
                try:
                    batch_result = await self._handle_round_tool_batch(
                        state=state,
                        round_state=round_state,
                        abort_signal=abort_signal,
                    )
                except Exception as exc:  # noqa: BLE001 - application port is a runtime boundary.
                    return await self._fail(
                        state,
                        f"tool batch handling failed: {exc}",
                    )
                if batch_result is not None:
                    return batch_result
                if round_state.completed_tool:
                    if await self._consume_turn_boundary_inputs(
                        state,
                        round_state,
                        assistant_already_committed=True,
                    ):
                        continue
                    continue
            if not round_state.parts:
                return await self._fail(
                    state,
                    "provider returned an empty assistant message",
                    round_parts=None,
                    round_reasoning_parts=round_state.reasoning_parts,
                    turn_id=round_state.turn_id,
                    provider_continuation=round_state.continuation,
                )

            state.final_parts.extend(round_state.parts)
            if await self._consume_turn_boundary_inputs(
                state,
                round_state,
                assistant_already_committed=False,
            ):
                state.final_parts.clear()
                continue
            return await self.ports.complete_run(
                state.emitted,
                state.sequence,
                session=state.session,
                run=state.run,
                messages=state.messages,
                final_text=state.final_text(),
                reasoning_content="".join(round_state.reasoning_parts),
                provider_continuation=round_state.continuation,
                turn_id=round_state.turn_id,
                tool_results=state.tool_results,
            )

        raise AssertionError("unreachable model loop termination")

    def _budget_reason(
        self,
        run: RunRecord,
        next_model_round: int,
    ) -> RunBudgetReason | None:
        elapsed_seconds = max(0.0, (utc_now() - run.started_at).total_seconds())
        return self.run_budget.exceeded_reason(
            next_model_round=next_model_round,
            input_tokens=int(run.usage.get("input_tokens", 0)),
            output_tokens=int(run.usage.get("output_tokens", 0)),
            elapsed_seconds=elapsed_seconds,
        )

    async def _build_context(
        self,
        state: _LoopState,
        model_round: int,
        abort_signal: AbortSignal,
    ) -> ContextView:
        context, state.sequence = await self.ports.prepare_context(
            state.emitted,
            state.sequence,
            session=state.session,
            run=state.run,
            messages=state.messages,
            tools=self.tools.definitions(),
            model_round=model_round,
            abort_signal=abort_signal,
        )
        return context

    async def _consume_turn_boundary_inputs(
        self,
        state: _LoopState,
        round_state: _RoundState,
        *,
        assistant_already_committed: bool,
    ) -> bool:
        consumer = self.ports.consume_turn_boundary_inputs
        if consumer is None:
            return False
        result = await consumer(
            state.emitted,
            state.sequence,
            session=state.session,
            run=state.run,
            messages=state.messages,
            assistant_text="" if assistant_already_committed else "".join(round_state.parts),
            assistant_reasoning=""
            if assistant_already_committed
            else "".join(round_state.reasoning_parts),
            assistant_continuation=None
            if assistant_already_committed
            else round_state.continuation,
            turn_id=round_state.turn_id,
        )
        state.sequence = result.sequence
        if not result.messages:
            return False
        state.messages.extend(result.messages)
        return True

    async def _start_round(
        self,
        state: _LoopState,
        model_round: int,
        turn_id: str,
    ) -> None:
        reasoning = self.adapter.model.reasoning_resolution
        state.sequence = await self.ports.emit(
            state.emitted,
            state.sequence,
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            event_type="model_request_started",
            payload={
                "model": self.adapter.model.model_id,
                "round": model_round,
                "turn_id": turn_id,
                "reasoning": {
                    "requested": reasoning.requested.model_dump(mode="json"),
                    "effective": reasoning.effective.model_dump(mode="json"),
                },
            },
        )

    async def _consume_provider_stream(
        self,
        *,
        state: _LoopState,
        round_state: _RoundState,
        model_round: int,
        context: ContextView,
        abort_signal: AbortSignal,
    ) -> RunResult | None:
        try:
            async for provider_event in self.adapter.stream(context, signal=abort_signal):
                if provider_event.usage is not None:
                    state.run = self.ports.record_usage(state.run, provider_event.usage)
                if aborted := self._aborted_result(state, round_state.parts):
                    return aborted

                if provider_event.type == "text_delta" and provider_event.text:
                    await self._record_text_delta(
                        state,
                        round_state,
                        model_round,
                        provider_event.text,
                    )
                elif provider_event.type == "reasoning_delta" and provider_event.reasoning_content:
                    await self._record_reasoning_delta(
                        state,
                        round_state,
                        model_round,
                        provider_event.reasoning_content,
                    )
                elif provider_event.type == "continuation":
                    if provider_event.continuation is None:
                        raise ValueError("provider continuation event is missing its payload")
                    if round_state.continuation is not None:
                        raise ValueError("provider emitted multiple continuation artifacts")
                    round_state.continuation = provider_event.continuation
                elif provider_event.type == "tool_call_delta":
                    if provider_event.tool_call_delta is None:
                        raise ValueError("provider tool call delta is missing its payload")
                    await self._record_tool_call_delta(
                        state,
                        round_state,
                        model_round,
                        provider_event.tool_call_delta,
                    )
                elif provider_event.type == "tool_call":
                    if provider_event.tool_call is None:
                        raise ValueError("provider tool call event is missing its payload")
                    await self._record_complete_tool_call(
                        state,
                        round_state,
                        model_round,
                        provider_event.tool_call,
                    )
                elif provider_event.type == "retry_scheduled":
                    if provider_event.retry is None:
                        raise ValueError("provider retry event is missing its payload")
                    state.sequence = await self.ports.emit(
                        state.emitted,
                        state.sequence,
                        session_id=state.session.session_id,
                        run_id=state.run.run_id,
                        event_type="run_retry_scheduled",
                        payload={
                            **provider_event.retry.model_dump(mode="json"),
                            "round": model_round,
                            "turn_id": round_state.turn_id,
                        },
                    )
                elif provider_event.type == "error":
                    return await self._fail(
                        state,
                        provider_event.error or "provider error",
                        round_state.parts,
                        round_state.reasoning_parts,
                        round_state.turn_id,
                        provider_continuation=round_state.continuation,
                    )
                elif provider_event.type == "done":
                    if round_state.tool_call_parts:
                        round_state.tool_calls = _complete_streamed_tool_calls(
                            round_state.tool_call_parts
                        )
                    _validate_tool_calls(round_state.tool_calls)
                    round_state.saw_model_done = True
                    state.sequence = await self.ports.emit(
                        state.emitted,
                        state.sequence,
                        session_id=state.session.session_id,
                        run_id=state.run.run_id,
                        event_type="model_message_completed",
                        payload={
                            "round": model_round,
                            "turn_id": round_state.turn_id,
                            "tool_call_count": len(round_state.tool_calls),
                            "usage": provider_event.usage.model_dump(mode="json")
                            if provider_event.usage is not None
                            else {},
                        },
                    )
        except Exception as exc:
            return await self._fail(
                state,
                str(exc) or exc.__class__.__name__,
                round_state.parts,
                round_state.reasoning_parts,
                round_state.turn_id,
                provider_continuation=round_state.continuation,
            )
        return None

    async def _record_text_delta(
        self,
        state: _LoopState,
        round_state: _RoundState,
        model_round: int,
        text: str,
    ) -> None:
        round_state.parts.append(text)
        state.sequence = await self.ports.emit(
            state.emitted,
            state.sequence,
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            event_type="model_text_delta",
            payload={"text": text, "round": model_round, "turn_id": round_state.turn_id},
        )

    async def _record_reasoning_delta(
        self,
        state: _LoopState,
        round_state: _RoundState,
        model_round: int,
        reasoning: str,
    ) -> None:
        round_state.reasoning_parts.append(reasoning)
        state.sequence = await self.ports.emit(
            state.emitted,
            state.sequence,
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            event_type="model_reasoning_delta",
            payload={
                "reasoning_content": reasoning,
                "round": model_round,
                "turn_id": round_state.turn_id,
            },
        )

    async def _record_tool_call_delta(
        self,
        state: _LoopState,
        round_state: _RoundState,
        model_round: int,
        delta: ToolCallDelta,
    ) -> None:
        if round_state.tool_calls:
            raise ValueError("provider mixed streamed and complete tool calls in one response")
        parts = round_state.tool_call_parts.setdefault(delta.index, _ToolCallParts())
        parts.append(delta)
        await self._emit_tool_call_delta(state, round_state, model_round, delta)

    async def _record_complete_tool_call(
        self,
        state: _LoopState,
        round_state: _RoundState,
        model_round: int,
        tool_call: ToolCallRequest,
    ) -> None:
        if round_state.tool_call_parts:
            raise ValueError("provider mixed streamed and complete tool calls in one response")
        source_index = len(round_state.tool_calls)
        complete_call = tool_call.model_copy(update={"source_index": source_index})
        round_state.tool_calls.append(complete_call)
        await self._emit_tool_call_delta(
            state,
            round_state,
            model_round,
            ToolCallDelta(
                index=source_index,
                id_delta=complete_call.id,
                name_delta=complete_call.name,
                arguments_delta=json.dumps(
                    complete_call.arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )

    async def _emit_tool_call_delta(
        self,
        state: _LoopState,
        round_state: _RoundState,
        model_round: int,
        delta: ToolCallDelta,
    ) -> None:
        state.sequence = await self.ports.emit(
            state.emitted,
            state.sequence,
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            event_type="model_tool_call_delta",
            payload={
                **delta.model_dump(mode="json"),
                "round": model_round,
                "turn_id": round_state.turn_id,
            },
        )

    async def _handle_round_tool_batch(
        self,
        *,
        state: _LoopState,
        round_state: _RoundState,
        abort_signal: AbortSignal,
    ) -> RunResult | None:
        assistant_text = "".join(round_state.parts)
        assistant_reasoning = "".join(round_state.reasoning_parts)
        handling = await self.ports.handle_tool_batch(
            state.emitted,
            state.sequence,
            state.session,
            state.run,
            list(round_state.tool_calls),
            turn_id=round_state.turn_id,
            message_sequence=len(state.messages) + 1,
            assistant_text=assistant_text,
            assistant_reasoning=assistant_reasoning,
            assistant_continuation=round_state.continuation,
            abort_signal=abort_signal,
        )
        state.sequence = handling.sequence
        if aborted := self._aborted_result(state):
            return aborted
        state.tool_results.extend(handling.results)
        if handling.messages:
            state.messages.extend(handling.messages)
            round_state.completed_tool = True
        if handling.status == "completed":
            return None

        metadata = dict(state.run.metadata)
        if handling.question:
            metadata["pending_question"] = handling.question
        pending_approvals = handling.batch.pending_approvals
        state.run = state.run.model_copy(
            update={
                "status": handling.status,
                "phase": "human_interaction",
                "active_tool_batch": handling.batch,
                "pending_approval_id": pending_approvals[0].approval_id
                if pending_approvals
                else None,
                "metadata": metadata,
            }
        )
        state.session = state.session.model_copy(
            update={
                "status": session_status_for_run(state.run.status),
                "updated_at": utc_now(),
            }
        )
        state.session = self.ports.save_waiting_state(session=state.session, run=state.run)
        return await self._save_waiting_state(
            emitted=state.emitted,
            session=state.session,
            run=state.run,
            final_text=state.final_text(),
            tool_results=state.tool_results,
        )

    def _aborted_result(
        self,
        state: _LoopState,
        round_parts: list[str] | None = None,
    ) -> RunResult | None:
        return self.ports.aborted_result(
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            final_text=state.final_text(round_parts),
            emitted=state.emitted,
            tool_results=state.tool_results,
        )

    async def _fail(
        self,
        state: _LoopState,
        error: str,
        round_parts: list[str] | None = None,
        round_reasoning_parts: list[str] | None = None,
        turn_id: str | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
    ) -> RunResult:
        return await self.ports.fail_run(
            state.emitted,
            state.sequence,
            session=state.session,
            run=state.run,
            error=error,
            final_text=state.final_text(round_parts),
            reasoning_content="".join(round_reasoning_parts or []),
            provider_continuation=provider_continuation,
            turn_id=turn_id,
            tool_results=state.tool_results,
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


def _validate_tool_calls(tool_calls: list[ToolCallRequest]) -> None:
    ids: set[str] = set()
    for call in tool_calls:
        if not call.id:
            raise ValueError("provider tool call id must not be empty")
        if not call.name:
            raise ValueError(f"provider tool call name must not be empty: {call.id}")
        if call.id in ids:
            raise ValueError(f"duplicate provider tool call id: {call.id}")
        ids.add(call.id)


def _complete_streamed_tool_calls(
    tool_call_parts: dict[int, _ToolCallParts],
) -> list[ToolCallRequest]:
    tool_calls: list[ToolCallRequest] = []
    for source_index, index in enumerate(sorted(tool_call_parts)):
        parts = tool_call_parts[index]
        if not parts.id:
            raise ValueError(f"provider tool call id must not be empty at stream index {index}")
        if not parts.name:
            raise ValueError(f"provider tool call name must not be empty at stream index {index}")
        try:
            arguments = json.loads(parts.arguments) if parts.arguments.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"provider tool call arguments are not valid JSON at stream index {index}: "
                f"{exc.msg}"
            ) from exc
        if not isinstance(arguments, dict):
            raise ValueError(
                f"provider tool call arguments must be a JSON object at stream index {index}"
            )
        tool_calls.append(
            ToolCallRequest(
                id=parts.id,
                name=parts.name,
                arguments=arguments,
                source_index=source_index,
            )
        )
    return tool_calls
