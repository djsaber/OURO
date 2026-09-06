from __future__ import annotations

import itertools
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.errors import ContextBlockedError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ModelResponseRecord, ProviderContinuationArtifact
from ouro.core.provider import ProviderCompletion, ToolCallDelta
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy, RunBudgetReason
from ouro.core.run_transitions import plan_run_transition
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchHandlingResult
from ouro.core.tools import ToolCallRequest, ToolDefinition, ToolResult


@dataclass(frozen=True)
class TurnBoundaryInputResult:
    sequence: int
    messages: list[MessageRecord] = field(default_factory=list)


@dataclass(frozen=True)
class TurnCompletionPolicyResult:
    sequence: int
    messages: list[MessageRecord] = field(default_factory=list)
    blocked_reason: str | None = None

    @property
    def should_continue(self) -> bool:
        return bool(self.messages) and self.blocked_reason is None


EventEmitter = Callable[..., Awaitable[int]]
ToolBatchHandler = Callable[..., Awaitable[ToolBatchHandlingResult]]
ContextPreparer = Callable[..., Awaitable[tuple[ContextView, int]]]
ContextCompletionRecorder = Callable[..., None]
ModelRequestResolver = Callable[..., tuple[SessionRecord, RuntimeAdapter]]
SessionRefresher = Callable[[str], SessionRecord]
UsageRecorder = Callable[..., RunRecord]
AbortResultFactory = Callable[..., RunResult | None]
WaitingStatePersister = Callable[..., SessionRecord]
RunCompleter = Callable[..., Awaitable[RunResult]]
RunFailer = Callable[..., Awaitable[RunResult]]
RunPauser = Callable[..., Awaitable[RunResult]]
TurnBoundaryInputConsumer = Callable[..., Awaitable[TurnBoundaryInputResult]]
TurnCompletionPolicyEvaluator = Callable[..., Awaitable[TurnCompletionPolicyResult]]
ModelRoundUpdater = Callable[..., RunRecord]
SharedBudgetReasonProvider = Callable[[RunRecord, int], Awaitable[RunBudgetReason | None]]
ProviderStreamStartNotifier = Callable[[], None]


@dataclass(frozen=True)
class InnerLoopContext:
    prepare: ContextPreparer
    record_completion: ContextCompletionRecorder | None = None
    resolve_model_request: ModelRequestResolver | None = None


@dataclass(frozen=True)
class InnerLoopRunBoundaries:
    record_usage: UsageRecorder
    aborted_result: AbortResultFactory
    save_waiting_state: WaitingStatePersister
    complete: RunCompleter
    fail: RunFailer
    pause: RunPauser


@dataclass(frozen=True)
class InnerLoopTurnBoundaries:
    consume_inputs: TurnBoundaryInputConsumer | None = None
    evaluate_completion: TurnCompletionPolicyEvaluator | None = None
    begin_model_round: ModelRoundUpdater | None = None
    commit_model_request: ModelRoundUpdater | None = None
    shared_budget_reason: SharedBudgetReasonProvider | None = None
    notify_provider_stream_start: ProviderStreamStartNotifier | None = None


@dataclass(frozen=True)
class InnerLoopPorts:
    context: InnerLoopContext
    runs: InnerLoopRunBoundaries
    turns: InnerLoopTurnBoundaries
    emit: EventEmitter
    handle_tool_batch: ToolBatchHandler
    refresh_session: SessionRefresher | None = None


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
    model_started_at: datetime | None = None
    model_response: ModelResponseRecord | None = None
    reasoning_ended_at: datetime | None = None


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
        tool_definitions: Callable[[], list[ToolDefinition]],
        run_budget: RunBudgetPolicy | None = None,
        ports: InnerLoopPorts,
    ) -> None:
        self.adapter = adapter
        self.tool_definitions = tool_definitions
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

        # All resumptions share one Run's durable request sequence, including
        # context/turn-policy identity and budget checks at the next boundary.
        for model_round in itertools.count(run.model_round_count + 1):
            if aborted := self._aborted_result(state):
                return aborted
            if reason := await self._budget_reason(state.run, model_round):
                return await self.ports.runs.pause(
                    state.emitted,
                    state.sequence,
                    session=state.session,
                    run=state.run,
                    reason=reason,
                    next_model_round=model_round,
                    final_text=state.final_text(),
                    tool_results=state.tool_results,
                )
            if self.ports.turns.begin_model_round is not None:
                state.run = await run_blocking(
                    self.ports.turns.begin_model_round,
                    session=state.session,
                    run=state.run,
                    model_round=model_round,
                )
            round_state = _RoundState()
            try:
                if self.ports.context.resolve_model_request is not None:
                    state.session, self.adapter = await run_blocking(
                        self.ports.context.resolve_model_request,
                        session=state.session,
                        run=state.run,
                        model_round=model_round,
                    )
                    state.run = state.run.model_copy(update={"model": self.adapter.model.model_id})
                context = await self._build_context(
                    state,
                    model_round,
                    round_state.turn_id,
                    abort_signal,
                )
            except ContextBlockedError:
                return await self.ports.runs.pause(
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
            round_state.model_started_at = utc_now()
            state.run = state.run.model_copy(
                update={
                    "model": self.adapter.model.model_id,
                    "model_endpoint_id": self.adapter.model.endpoint_id,
                }
            )
            if self.ports.turns.commit_model_request is not None:
                state.run = await run_blocking(
                    self.ports.turns.commit_model_request,
                    session=state.session,
                    run=state.run,
                    model_round=model_round,
                )
            await self._start_round(state, model_round, round_state.turn_id, context)
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
                self._finish_model_response(round_state)
                return aborted
            if not round_state.saw_model_done:
                self._finish_model_response(round_state)
                return await self._fail(
                    state,
                    "provider stream ended without a completion event",
                    round_state.parts,
                    round_state.reasoning_parts,
                    round_state.turn_id,
                    model_response=round_state.model_response,
                    provider_continuation=round_state.continuation,
                )
            if round_state.tool_calls:
                try:
                    if self.ports.refresh_session is not None:
                        state.session = await run_blocking(
                            self.ports.refresh_session,
                            state.session.session_id,
                        )
                    batch_result = await self._handle_round_tool_batch(
                        state=state,
                        round_state=round_state,
                        abort_signal=abort_signal,
                    )
                except Exception as exc:  # noqa: BLE001 - application port is a runtime boundary.
                    return await self._fail(
                        state,
                        f"tool batch handling failed: {exc}",
                        model_response=round_state.model_response,
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
                    model_response=round_state.model_response,
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
            if self.ports.turns.evaluate_completion is not None:
                try:
                    completion = await self.ports.turns.evaluate_completion(
                        state.emitted,
                        state.sequence,
                        session=state.session,
                        run=state.run,
                        messages=state.messages,
                        assistant_text="".join(round_state.parts),
                        assistant_reasoning="".join(round_state.reasoning_parts),
                        assistant_model_response=round_state.model_response,
                        assistant_continuation=round_state.continuation,
                        turn_id=round_state.turn_id,
                    )
                except Exception as exc:  # noqa: BLE001 - application port boundary.
                    return await self._fail(
                        state,
                        f"turn policy evaluation failed: {exc}",
                        model_response=round_state.model_response,
                    )
                state.sequence = completion.sequence
                if completion.blocked_reason is not None:
                    return await self.ports.runs.pause(
                        state.emitted,
                        state.sequence,
                        session=state.session,
                        run=state.run,
                        reason="turn_policy_blocked",
                        next_model_round=model_round + 1,
                        final_text=state.final_text(),
                        tool_results=state.tool_results,
                    )
                if completion.should_continue:
                    state.messages.extend(completion.messages)
                    state.final_parts.clear()
                    continue
            return await self.ports.runs.complete(
                state.emitted,
                state.sequence,
                session=state.session,
                run=state.run,
                messages=state.messages,
                final_text=state.final_text(),
                reasoning_content="".join(round_state.reasoning_parts),
                model_response=round_state.model_response,
                provider_continuation=round_state.continuation,
                turn_id=round_state.turn_id,
                tool_results=state.tool_results,
            )

        raise AssertionError("unreachable model loop termination")

    async def _budget_reason(
        self,
        run: RunRecord,
        next_model_round: int,
    ) -> RunBudgetReason | None:
        elapsed_seconds = max(0.0, (utc_now() - run.started_at).total_seconds())
        local_reason = self.run_budget.exceeded_reason(
            next_model_round=max(next_model_round, run.model_round_count + 1),
            input_tokens=int(run.usage.get("input_tokens", 0)),
            output_tokens=int(run.usage.get("output_tokens", 0)),
            elapsed_seconds=elapsed_seconds,
        )
        if local_reason is not None or self.ports.turns.shared_budget_reason is None:
            return local_reason
        return await self.ports.turns.shared_budget_reason(run, next_model_round)

    async def _build_context(
        self,
        state: _LoopState,
        model_round: int,
        turn_id: str,
        abort_signal: AbortSignal,
    ) -> ContextView:
        context, state.sequence = await self.ports.context.prepare(
            state.emitted,
            state.sequence,
            session=state.session,
            run=state.run,
            messages=state.messages,
            tools=self.tool_definitions(),
            model_round=model_round,
            turn_id=turn_id,
            abort_signal=abort_signal,
            adapter=self.adapter,
        )
        if context.context_id is None:
            raise ValueError("prepared model context is missing its identity")
        return context

    async def _consume_turn_boundary_inputs(
        self,
        state: _LoopState,
        round_state: _RoundState,
        *,
        assistant_already_committed: bool,
    ) -> bool:
        consumer = self.ports.turns.consume_inputs
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
            assistant_model_response=None
            if assistant_already_committed
            else round_state.model_response,
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
        context: ContextView,
    ) -> None:
        reasoning = self.adapter.model.reasoning_resolution
        state.sequence = await self.ports.emit(
            state.emitted,
            state.sequence,
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            event_type="model_request_started",
            payload={
                "provider": self.adapter.model.provider_id,
                "endpoint": self.adapter.model.endpoint_id,
                "model": self.adapter.model.model_id,
                "round": model_round,
                "turn_id": turn_id,
                "context_id": context.context_id,
                "context_metrics": context.metrics.model_dump(mode="json"),
                "active_compaction_id": context.context_metadata.get("compaction_id"),
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
            async for provider_event in self.adapter.stream(
                context,
                signal=abort_signal,
                on_stream_start=self.ports.turns.notify_provider_stream_start,
            ):
                if provider_event.type == "done":
                    if context.context_id is None:
                        raise ValueError("prepared model context is missing its identity")
                    if self.ports.context.record_completion is not None:
                        await run_blocking(
                            self.ports.context.record_completion,
                            session_id=state.session.session_id,
                            context_id=context.context_id,
                            usage=provider_event.usage,
                        )
                if provider_event.type == "done" and provider_event.usage is not None:
                    state.run = await run_blocking(
                        self.ports.runs.record_usage,
                        state.run,
                        provider_event.usage,
                        model=self.adapter.model,
                        request_id=round_state.turn_id,
                        purpose="agent",
                    )
                if aborted := self._aborted_result(state, round_state.parts):
                    self._finish_model_response(round_state)
                    return aborted

                if provider_event.type == "text_delta" and provider_event.text:
                    self._mark_reasoning_ended(round_state)
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
                    self._mark_reasoning_ended(round_state)
                    await self._record_tool_call_delta(
                        state,
                        round_state,
                        model_round,
                        provider_event.tool_call_delta,
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
                    self._finish_model_response(round_state)
                    return await self._fail(
                        state,
                        provider_event.error or "provider error",
                        round_state.parts,
                        round_state.reasoning_parts,
                        round_state.turn_id,
                        model_response=round_state.model_response,
                        provider_continuation=round_state.continuation,
                    )
                elif provider_event.type == "done":
                    completion = provider_event.completion or ProviderCompletion()
                    if round_state.tool_call_parts:
                        round_state.tool_calls = _complete_streamed_tool_calls(
                            round_state.tool_call_parts
                        )
                    _validate_tool_calls(round_state.tool_calls)
                    round_state.saw_model_done = True
                    self._finish_model_response(round_state)
                    state.sequence = await self.ports.emit(
                        state.emitted,
                        state.sequence,
                        session_id=state.session.session_id,
                        run_id=state.run.run_id,
                        event_type="model_message_completed",
                        payload={
                            "round": model_round,
                            "turn_id": round_state.turn_id,
                            "context_id": context.context_id,
                            "tool_call_count": len(round_state.tool_calls),
                            "finish_reason": completion.finish_reason,
                            "provider_finish_reason": completion.provider_finish_reason,
                            "usage": provider_event.usage.model_dump(mode="json")
                            if provider_event.usage is not None
                            else {},
                        },
                    )
        except Exception as exc:
            self._finish_model_response(round_state)
            return await self._fail(
                state,
                str(exc) or exc.__class__.__name__,
                round_state.parts,
                round_state.reasoning_parts,
                round_state.turn_id,
                model_response=round_state.model_response,
                provider_continuation=round_state.continuation,
            )
        return None

    @staticmethod
    def _mark_reasoning_ended(round_state: _RoundState) -> None:
        if round_state.reasoning_parts and round_state.reasoning_ended_at is None:
            round_state.reasoning_ended_at = utc_now()

    def _finish_model_response(self, round_state: _RoundState) -> None:
        started_at = round_state.model_started_at
        if started_at is None or round_state.model_response is not None:
            return
        ended_at = utc_now()
        reasoning_ended_at = round_state.reasoning_ended_at
        if round_state.reasoning_parts and reasoning_ended_at is None:
            reasoning_ended_at = ended_at
        round_state.model_response = ModelResponseRecord(
            provider_id=self.adapter.model.provider_id,
            endpoint_id=self.adapter.model.endpoint_id,
            model_id=self.adapter.model.model_id,
            started_at=started_at,
            ended_at=ended_at,
            reasoning_ended_at=reasoning_ended_at,
        )

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
            assistant_model_response=round_state.model_response,
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

        pending_approvals = handling.batch.pending_approvals
        transition = plan_run_transition(
            state.session,
            current_run=state.run,
            next_run=state.run.model_copy(
                update={
                    "status": handling.status,
                    "phase": (
                        "subagent_join"
                        if handling.status == "waiting_child"
                        else "human_interaction"
                    ),
                    "active_tool_batch": handling.batch,
                    "pending_approval_id": (
                        pending_approvals[0].approval_id if pending_approvals else None
                    ),
                }
            ),
        )
        state.run = transition.run
        state.session = transition.session
        state.session = await run_blocking(
            self.ports.runs.save_waiting_state,
            session=state.session,
            run=state.run,
        )
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
        return self.ports.runs.aborted_result(
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
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
    ) -> RunResult:
        return await self.ports.runs.fail(
            state.emitted,
            state.sequence,
            session=state.session,
            run=state.run,
            error=error,
            final_text=state.final_text(round_parts),
            reasoning_content="".join(round_reasoning_parts or []),
            model_response=model_response,
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
