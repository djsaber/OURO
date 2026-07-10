from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder, ContextView
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.messages import MessageRecord
from ouro.core.provider import Usage
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolDefinition, ToolExecutionOutcome, ToolResult


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
        assistant_reasoning: str,
        abort_signal: AbortSignal,
    ) -> tuple[ToolExecutionOutcome, int, list[MessageRecord]]: ...


class UsageRecorder(Protocol):
    def __call__(self, run: RunRecord, usage: Usage) -> RunRecord: ...


class ToolDefinitionProvider(Protocol):
    def definitions(self) -> list[ToolDefinition]: ...


class RunCheckpointer(Protocol):
    def __call__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        partial_assistant_text: str,
        partial_assistant_reasoning: str,
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
        reasoning_content: str,
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
        reasoning_content: str,
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
    parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    saw_model_done: bool = False
    completed_tool: bool = False


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
        hooks: HookRegistry,
        context_builder: ContextBuilder,
        max_model_rounds: int,
        ports: InnerLoopPorts,
        system_prompt: str = "",
    ) -> None:
        self.adapter = adapter
        self.tools = tools
        self.hooks = hooks
        self.context_builder = context_builder
        self.max_model_rounds = max_model_rounds
        self.system_prompt = system_prompt
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

        for model_round in range(1, self.max_model_rounds + 1):
            if aborted := self._aborted_result(state):
                return aborted

            context = await self._build_context(state, model_round)
            await self._start_round(state, model_round)
            round_state = _RoundState()
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
            if round_state.completed_tool:
                continue
            if not round_state.saw_model_done:
                return await self._fail(
                    state,
                    "provider stream ended without a completion event",
                    round_state.parts,
                    round_state.reasoning_parts,
                )
            if not round_state.parts:
                return await self._fail(
                    state,
                    "provider returned an empty assistant message",
                    round_parts=None,
                    round_reasoning_parts=round_state.reasoning_parts,
                )

            state.final_parts.extend(round_state.parts)
            return await self.ports.complete_run(
                state.emitted,
                state.sequence,
                session=state.session,
                run=state.run,
                messages=state.messages,
                final_text=state.final_text(),
                reasoning_content="".join(round_state.reasoning_parts),
                tool_results=state.tool_results,
            )

        return await self._fail(
            state,
            f"model round limit exceeded: {self.max_model_rounds}",
        )

    async def _build_context(self, state: _LoopState, model_round: int) -> ContextView:
        hook_result = await self.hooks.dispatch(
            HookContext(
                hook_name="after_context_build",
                session_id=state.session.session_id,
                run_id=state.run.run_id,
                data={"message_count": len(state.messages), "model_round": model_round},
            )
        )
        state.sequence = await self.ports.record_hook_failures(
            state.emitted,
            state.sequence,
            hook_result.failures,
        )
        added_context = [text for result in hook_result.results for text in result.added_context]
        return self.context_builder.build(
            messages=state.messages,
            tools=self.tools.definitions(),
            system_prompt=self.system_prompt,
            added_context=added_context,
            input_token_budget=max(
                1,
                self.adapter.model.context_window - self.adapter.model.max_output_tokens,
            ),
        )

    async def _start_round(self, state: _LoopState, model_round: int) -> None:
        state.sequence = await self.ports.emit(
            state.emitted,
            state.sequence,
            session_id=state.session.session_id,
            run_id=state.run.run_id,
            event_type="model_request_started",
            payload={"model": self.adapter.model.model_id, "round": model_round},
        )
        self._checkpoint(state)

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
                elif provider_event.type == "tool_call" and provider_event.tool_call:
                    terminal_result = await self._handle_round_tool_call(
                        state=state,
                        round_state=round_state,
                        tool_call=provider_event.tool_call,
                        abort_signal=abort_signal,
                    )
                    if terminal_result is not None:
                        return terminal_result
                elif provider_event.type == "error":
                    return await self._fail(
                        state,
                        provider_event.error or "provider error",
                        round_state.parts,
                        round_state.reasoning_parts,
                    )
                elif provider_event.type == "done":
                    round_state.saw_model_done = True
                    state.sequence = await self.ports.emit(
                        state.emitted,
                        state.sequence,
                        session_id=state.session.session_id,
                        run_id=state.run.run_id,
                        event_type="model_message_completed",
                        payload={
                            "round": model_round,
                            "usage": provider_event.usage.model_dump(mode="json")
                            if provider_event.usage is not None
                            else {},
                        },
                    )
                    self._checkpoint(state, round_state.parts, round_state.reasoning_parts)
        except Exception as exc:
            return await self._fail(
                state,
                str(exc) or exc.__class__.__name__,
                round_state.parts,
                round_state.reasoning_parts,
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
            payload={"text": text, "round": model_round},
        )
        self._checkpoint(state, round_state.parts, round_state.reasoning_parts)

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
            payload={"reasoning_content": reasoning, "round": model_round},
        )
        self._checkpoint(state, round_state.parts, round_state.reasoning_parts)

    async def _handle_round_tool_call(
        self,
        *,
        state: _LoopState,
        round_state: _RoundState,
        tool_call: dict[str, Any],
        abort_signal: AbortSignal,
    ) -> RunResult | None:
        assistant_text = "".join(round_state.parts)
        assistant_reasoning = "".join(round_state.reasoning_parts)
        round_state.parts.clear()
        round_state.reasoning_parts.clear()
        outcome, state.sequence, new_messages = await self.ports.handle_tool_call(
            state.emitted,
            state.sequence,
            state.session,
            state.run,
            tool_call,
            message_sequence=len(state.messages) + 1,
            assistant_text=assistant_text,
            assistant_reasoning=assistant_reasoning,
            abort_signal=abort_signal,
        )
        if aborted := self._aborted_result(state):
            return aborted
        if outcome.result is not None:
            state.tool_results.append(outcome.result)
        if new_messages:
            state.messages.extend(new_messages)
            round_state.completed_tool = True
            self._checkpoint(state)
        if outcome.status not in {"approval_required", "waiting_user"}:
            return None

        state.run = _run_with_pending_outcome(state.run, outcome)
        state.session = state.session.model_copy(
            update={
                "status": _session_status_for_run(state.run.status),
                "updated_at": utc_now(),
            }
        )
        self._checkpoint(state)
        self.ports.save_waiting_state(session=state.session, run=state.run)
        return await self._save_waiting_state(
            emitted=state.emitted,
            session=state.session,
            run=state.run,
            final_text=state.final_text(),
            tool_results=state.tool_results,
        )

    def _checkpoint(
        self,
        state: _LoopState,
        round_parts: list[str] | None = None,
        round_reasoning_parts: list[str] | None = None,
    ) -> None:
        state.session, state.run = self.ports.checkpoint(
            session=state.session,
            run=state.run,
            messages=state.messages,
            partial_assistant_text=state.final_text(round_parts),
            partial_assistant_reasoning="".join(round_reasoning_parts or []),
            event_sequence=state.sequence,
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
    ) -> RunResult:
        return await self.ports.fail_run(
            state.emitted,
            state.sequence,
            session=state.session,
            run=state.run,
            error=error,
            final_text=state.final_text(round_parts),
            reasoning_content="".join(round_reasoning_parts or []),
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
