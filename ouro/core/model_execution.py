from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic

from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.messages import ModelResponseRecord, ProviderContinuationArtifact
from ouro.core.model_completion import model_completion_failure
from ouro.core.provider import ProviderCompletion, ProviderStreamEvent, ToolCallDelta, Usage
from ouro.core.request_context import ProviderRequestContext
from ouro.core.tools import ToolCallRequest


@dataclass(frozen=True)
class ModelExecutionEvent:
    event: ProviderStreamEvent
    tool_call_count: int
    has_reasoning: bool


@dataclass(frozen=True)
class ModelExecutionResult:
    """One request's output, including partial output on failure or cancellation."""

    text: str
    reasoning: str
    tool_calls: tuple[ToolCallRequest, ...]
    continuation: ProviderContinuationArtifact | None
    model_response: ModelResponseRecord
    completion: ProviderCompletion | None
    usage: Usage | None
    error: str | None
    error_code: str | None
    aborted: bool


@dataclass
class _ToolCallParts:
    ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)

    def append(self, delta: ToolCallDelta) -> None:
        if delta.id_delta:
            self.ids.append(delta.id_delta)
        if delta.name_delta:
            self.names.append(delta.name_delta)
        if delta.arguments_delta:
            self.arguments.append(delta.arguments_delta)


@dataclass
class _Response:
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    tool_parts: dict[int, _ToolCallParts] = field(default_factory=dict)
    tool_calls: tuple[ToolCallRequest, ...] = ()
    continuation: ProviderContinuationArtifact | None = None
    completion: ProviderCompletion | None = None
    usage: Usage | None = None
    reasoning_ended_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None
    error_code: str | None = None

    def consume(self, event: ProviderStreamEvent, clock: Callable[[], datetime]) -> None:
        if event.type == "reasoning_delta" and event.reasoning_content:
            self.reasoning.append(event.reasoning_content)
        elif event.type == "text_delta" and event.text:
            self._end_reasoning(clock)
            self.text.append(event.text)
        elif event.type == "tool_call_delta":
            if event.tool_call_delta is None:
                raise ValueError("provider tool call delta is missing its payload")
            self._end_reasoning(clock)
            delta = event.tool_call_delta
            parts = self.tool_parts.get(delta.index)
            if parts is None:
                parts = self.tool_parts[delta.index] = _ToolCallParts()
            parts.append(delta)
        elif event.type == "continuation":
            if event.continuation is None:
                raise ValueError("provider continuation event is missing its payload")
            if self.continuation is not None:
                raise ValueError("provider emitted multiple continuation artifacts")
            self.continuation = event.continuation
        elif event.type == "retry_scheduled" and event.retry is None:
            raise ValueError("provider retry event is missing its payload")
        elif event.type == "done":
            self.completion = event.completion or ProviderCompletion()
            self.usage = event.usage
            self.ended_at = clock()
        elif event.type == "error":
            self.error = event.error or "provider error"
            self.error_code = event.error_code
            self.ended_at = clock()

    def _end_reasoning(self, clock: Callable[[], datetime]) -> None:
        if self.reasoning and self.reasoning_ended_at is None:
            self.reasoning_ended_at = clock()

    def complete(self) -> None:
        if self.completion is None:
            self.error = "provider stream ended without a completion event"
            return
        failure = model_completion_failure(
            self.completion,
            has_text=any(part.strip() for part in self.text),
            has_tool_calls=bool(self.tool_parts),
            has_reasoning=bool(self.reasoning),
        )
        if failure is not None:
            self.error = failure.message
            self.error_code = failure.code
            # Rejected output must not become replayable provider continuation.
            self.continuation = None
            return
        self.tool_calls = _complete_tool_calls(self.tool_parts)


async def execute_model_request(
    adapter: RuntimeAdapter,
    context: ContextView,
    *,
    request_context: ProviderRequestContext,
    signal: AbortSignal,
    on_event: Callable[[ModelExecutionEvent], Awaitable[None]],
    on_provider_done: Callable[[ProviderStreamEvent], Awaitable[None]] | None = None,
    on_stream_start: Callable[[], None] | None = None,
    started: tuple[datetime, float] | None = None,
    clock: Callable[[], datetime] = utc_now,
    monotonic_clock: Callable[[], float] = monotonic,
) -> ModelExecutionResult:
    """Own a provider stream through terminal output and resource release.

    The host settles accounting on provider receipt and observes output for
    presentation. This boundary assembles and validates model output; it never
    advances or finishes a Run. The receipt callback runs before done enters the
    output FIFO and does not publish response content. Task cancellation propagates
    after closing the stream. An AbortSignal returns partial output for the host
    to settle against its durable cancellation fact.

    ``started`` pairs a UTC anchor with a reading from ``monotonic_clock``. The
    host may capture it before request bookkeeping so that latency is included.
    Stage timestamps share this anchor and measure only monotonic elapsed time.
    """
    started_at, started_monotonic = started or (clock(), monotonic_clock())

    def request_time() -> datetime:
        return started_at + timedelta(seconds=monotonic_clock() - started_monotonic)

    response = _Response()
    try:
        async with aclosing(
            adapter.stream(
                context,
                request_context=request_context,
                signal=signal,
                on_stream_start=on_stream_start,
                on_provider_done=on_provider_done,
            )
        ) as events:
            async for event in events:
                response.consume(event, request_time)
                await on_event(
                    ModelExecutionEvent(
                        event=event,
                        tool_call_count=len(response.tool_parts),
                        has_reasoning=bool(response.reasoning),
                    )
                )
                if event.type in {"done", "error"} or signal.is_aborted():
                    break
        if response.error is None and not signal.is_aborted():
            response.complete()
    except Exception as exc:  # noqa: BLE001 - provider and host event boundary.
        response.error = str(exc) or exc.__class__.__name__
        response.error_code = None

    ended_at = response.ended_at or request_time()
    return ModelExecutionResult(
        text="".join(response.text),
        reasoning="".join(response.reasoning),
        tool_calls=response.tool_calls,
        continuation=response.continuation,
        model_response=ModelResponseRecord(
            provider_id=adapter.model.provider_id,
            endpoint_id=adapter.model.endpoint_id,
            model_id=adapter.model.model_id,
            started_at=started_at,
            ended_at=ended_at,
            reasoning_ended_at=(response.reasoning_ended_at or ended_at)
            if response.reasoning
            else None,
        ),
        completion=response.completion,
        usage=response.usage,
        error=response.error,
        error_code=response.error_code,
        aborted=signal.is_aborted(),
    )


def _complete_tool_calls(parts_by_index: dict[int, _ToolCallParts]) -> tuple[ToolCallRequest, ...]:
    calls: list[ToolCallRequest] = []
    ids: set[str] = set()
    for source_index, index in enumerate(sorted(parts_by_index)):
        parts = parts_by_index[index]
        call_id = "".join(parts.ids)
        name = "".join(parts.names)
        raw_arguments = "".join(parts.arguments)
        if not call_id:
            raise ValueError(f"provider tool call id must not be empty at stream index {index}")
        if not name:
            raise ValueError(f"provider tool call name must not be empty at stream index {index}")
        if call_id in ids:
            raise ValueError(f"duplicate provider tool call id: {call_id}")
        ids.add(call_id)
        try:
            arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"provider tool call arguments are not valid JSON at stream index {index}: "
                f"{exc.msg}"
            ) from exc
        if not isinstance(arguments, dict):
            raise ValueError(
                f"provider tool call arguments must be a JSON object at stream index {index}"
            )
        calls.append(
            ToolCallRequest(id=call_id, name=name, arguments=arguments, source_index=source_index)
        )
    return tuple(calls)
