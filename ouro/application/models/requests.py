from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import Metadata, utc_now
from ouro.core.billing import UsagePurpose
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.errors import ContextBlockedError
from ouro.core.events import EventRecord, EventType
from ouro.core.model_execution import ModelExecutionEvent, execute_model_request
from ouro.core.model_requests import (
    ModelRequestBlocked,
    ModelRequestFailed,
    RunModelRequest,
    RunModelResult,
)
from ouro.core.provider import (
    ModelMetadata,
    ProviderCompletion,
    ProviderRetryNotice,
    ProviderStreamEvent,
    Usage,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.core.response_drafts import ResponseDraftChunk, ResponseStreamPosition
from ouro.core.run_budget import RunBudgetPolicy, RunPauseReason
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tools import ToolDefinition
from ouro.core.usage_queries import RunUsageTotals


class ContextPreparer(Protocol):
    async def __call__(
        self,
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        tools: list[ToolDefinition],
        model_round: int,
        turn_id: str,
        abort_signal: AbortSignal,
        adapter: RuntimeAdapter,
        prompt: str = "",
    ) -> tuple[ContextView, int]: ...


class RequestSettlementRecorder(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        context_id: str,
        provider_done: bool,
        usage_entry_id: str | None,
    ) -> None: ...


class ModelRequestResolver(Protocol):
    def __call__(
        self, *, session: SessionRecord, run: RunRecord, model_round: int
    ) -> tuple[SessionRecord, RuntimeAdapter]: ...


class UsageRecorder(Protocol):
    def __call__(
        self,
        run: RunRecord,
        usage: Usage,
        *,
        model: ModelMetadata,
        request_id: str,
        purpose: UsagePurpose,
        workspace: str | None = None,
    ) -> str | None: ...


class ModelRoundStarter(Protocol):
    async def __call__(
        self, *, session: SessionRecord, run: RunRecord, model_round: int
    ) -> RunRecord: ...


class ModelRoundCommitter(Protocol):
    def __call__(
        self, *, session: SessionRecord, run: RunRecord, context_id: str, event: EventRecord
    ) -> tuple[RunRecord, tuple[EventRecord, ...]]: ...


class ModelEventEmitter(Protocol):
    async def __call__(
        self,
        sequence: int,
        *,
        session_id: str,
        run_id: str,
        event_type: EventType,
        payload: Metadata,
        stream: ResponseStreamPosition | None = None,
        history_epoch: int | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class ModelRequestPorts:
    prepare_context: ContextPreparer
    record_usage: UsageRecorder
    emit: ModelEventEmitter
    record_settlement: RequestSettlementRecorder
    commit_model_request: ModelRoundCommitter
    publish_committed: Callable[[Sequence[EventRecord]], Awaitable[None]]
    append_draft: Callable[[str, str, ResponseDraftChunk], int]
    record_retry: Callable[[str, str, ProviderRetryNotice], None]
    resolve_model_request: ModelRequestResolver | None = None
    begin_model_round: ModelRoundStarter | None = None
    shared_budget_reason: Callable[[RunRecord, int], Awaitable[RunPauseReason | None]] | None = None
    notify_provider_stream_start: Callable[[], None] | None = None
    run_usage: Callable[[str, str], RunUsageTotals | None] | None = None


class RunModelRequests:
    """Execute one model request inside the host's accepted Run.

    Context, request counters, usage and events belong to this boundary. The
    caller receives output or a blocked/failed request and decides its next step.
    """

    def __init__(
        self,
        *,
        adapter: RuntimeAdapter,
        tool_definitions: Callable[[], list[ToolDefinition]],
        ports: ModelRequestPorts,
        run_budget: RunBudgetPolicy | None = None,
    ) -> None:
        self.adapter = adapter
        self.tool_definitions = tool_definitions
        self.ports = ports
        self.run_budget = run_budget or RunBudgetPolicy()
        if (
            self.run_budget.max_input_tokens is not None
            or self.run_budget.max_output_tokens is not None
        ) and self.ports.run_usage is None:
            raise ValueError("Token consumption budgets require a usage ledger")

    async def execute(self, request: RunModelRequest) -> RunModelResult:
        session, run, sequence = request.session, request.run, request.sequence
        model_round = request.model_round
        if reason := await self._budget_reason(run, model_round):
            return RunModelResult(session, run, sequence, ModelRequestBlocked(reason))

        adapter = self.adapter
        try:
            if self.ports.begin_model_round is not None:
                run = await self.ports.begin_model_round(
                    session=session, run=run, model_round=model_round
                )
            if self.ports.resolve_model_request is not None:
                session, adapter = await run_blocking(
                    self.ports.resolve_model_request,
                    session=session,
                    run=run,
                    model_round=model_round,
                )
                run = run.model_copy(update={"model": adapter.model.model_id})
            definitions = self.tool_definitions()
            if request.tools is not None:
                selected = set(request.tools)
                unknown = selected - {item.name for item in definitions}
                if unknown:
                    raise ValueError(f"requested tools are unavailable: {sorted(unknown)}")
                definitions = [item for item in definitions if item.name in selected]
            context, sequence = await self.ports.prepare_context(
                sequence,
                session=session,
                run=run,
                tools=definitions,
                prompt=request.prompt,
                model_round=model_round,
                turn_id=request.turn_id,
                abort_signal=request.abort_signal,
                adapter=adapter,
            )
            context_id = context.context_id
            if context_id is None:
                raise ValueError("prepared model context is missing its identity")
        except ContextBlockedError:
            return RunModelResult(session, run, sequence, ModelRequestBlocked("context_blocked"))
        except Exception as exc:  # noqa: BLE001 - context preparation boundary.
            return RunModelResult(
                session, run, sequence, ModelRequestFailed(f"context preparation failed: {exc}")
            )

        started = (utc_now(), monotonic())
        run = run.model_copy(
            update={"model": adapter.model.model_id, "model_endpoint_id": adapter.model.endpoint_id}
        )
        reasoning = adapter.model.reasoning_resolution
        run, events = await run_blocking(
            self.ports.commit_model_request,
            session=session,
            run=run,
            context_id=context_id,
            event=EventRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                turn_id=request.turn_id,
                event_type="model_request_started",
                payload={
                    "provider": adapter.model.provider_id,
                    "endpoint": adapter.model.endpoint_id,
                    "model": adapter.model.model_id,
                    "round": model_round,
                    "turn_id": request.turn_id,
                    "context_id": context_id,
                    "context_metrics": context.metrics.model_dump(mode="json"),
                    "active_compaction_id": context.context_metadata.get("compaction_id"),
                    "reasoning": {
                        "requested": reasoning.requested.model_dump(mode="json"),
                        "effective": reasoning.effective.model_dump(mode="json"),
                    },
                },
            ),
        )
        if run.abort_requested or run.status not in ACTIVE_RUN_STATUSES:
            request.abort_signal.abort("Run no longer owns the model request")
        if events:
            sequence = events[-1].sequence
            await self.ports.publish_committed(events)

        provider_done_seen = False

        async def settle_provider_done(event: ProviderStreamEvent) -> None:
            nonlocal provider_done_seen
            provider_done_seen = True
            usage_entry_id = None
            if event.usage is not None:
                usage_entry_id = await run_blocking(
                    self.ports.record_usage,
                    run,
                    event.usage,
                    model=adapter.model,
                    request_id=request.turn_id,
                    purpose="agent",
                    workspace=session.workspace,
                )
            await run_blocking(
                self.ports.record_settlement,
                session_id=session.session_id,
                context_id=context_id,
                provider_done=True,
                usage_entry_id=usage_entry_id,
            )

        draft_cursor = 0

        async def observe(progress: ModelExecutionEvent) -> None:
            nonlocal sequence, draft_cursor
            if request.abort_signal.is_aborted():
                return
            event = progress.event
            if event.retry is not None:
                await run_blocking(
                    self.ports.record_retry, session.session_id, context_id, event.retry
                )
            chunk = None
            if event.type == "text_delta" and event.text:
                chunk = ResponseDraftChunk(channel="text", content=event.text)
            elif event.type == "reasoning_delta" and event.reasoning_content:
                chunk = ResponseDraftChunk(channel="reasoning", content=event.reasoning_content)
            stream = None
            if event.type in {"text_delta", "reasoning_delta", "tool_call_delta"}:
                before_cursor = draft_cursor
                if chunk is not None:
                    draft_cursor = await run_blocking(
                        self.ports.append_draft, session.session_id, context_id, chunk
                    )
                stream = ResponseStreamPosition(
                    context_id=context_id,
                    after_cursor=before_cursor,
                    cursor=draft_cursor if chunk is not None and draft_cursor else None,
                )
            publication = _model_event(progress, context_id=context_id)
            if publication is not None:
                event_type, payload = publication
                sequence = await self.ports.emit(
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type=event_type,
                    payload={**payload, "round": model_round, "turn_id": request.turn_id},
                    stream=stream,
                    history_epoch=run.history_epoch,
                )

        output = await execute_model_request(
            adapter,
            context,
            request_context=ProviderRequestContext(
                {"session.id": session.session_id, "run.id": run.run_id, "request.purpose": "chat"}
            ),
            signal=request.abort_signal,
            on_event=observe,
            on_provider_done=settle_provider_done,
            on_stream_start=self.ports.notify_provider_stream_start,
            started=started,
        )
        if not provider_done_seen:
            await run_blocking(
                self.ports.record_settlement,
                session_id=session.session_id,
                context_id=context_id,
                provider_done=False,
                usage_entry_id=None,
            )
        if request.tools is not None and any(
            call.name not in selected for call in output.tool_calls
        ):
            return RunModelResult(
                session,
                run,
                sequence,
                ModelRequestFailed("model requested a tool outside this request's selected tools"),
            )
        return RunModelResult(session, run, sequence, output)

    async def _budget_reason(self, run: RunRecord, model_round: int) -> RunPauseReason | None:
        values = (
            await run_blocking(self.ports.run_usage, run.session_id, run.run_id)
            if self.ports.run_usage is not None
            and (
                self.run_budget.max_input_tokens is not None
                or self.run_budget.max_output_tokens is not None
            )
            else None
        )
        if (
            (values.agent_request_count if values is not None else 0) < run.model_round_count
            or (values.compaction_request_count if values is not None else 0)
            < run.summary_request_count
        ) and (
            self.run_budget.max_input_tokens is not None
            or self.run_budget.max_output_tokens is not None
        ):
            return "usage_unavailable"
        local_reason = self.run_budget.exceeded_reason(
            next_model_round=max(model_round, run.model_round_count + 1),
            input_tokens=values.input_tokens if values is not None else 0,
            output_tokens=values.output_tokens if values is not None else 0,
            elapsed_seconds=max(0.0, (utc_now() - run.started_at).total_seconds()),
        )
        if local_reason is not None or self.ports.shared_budget_reason is None:
            return local_reason
        return await self.ports.shared_budget_reason(run, model_round)


def _model_event(
    progress: ModelExecutionEvent, *, context_id: str
) -> tuple[EventType, Metadata] | None:
    event = progress.event
    if event.type == "text_delta" and event.text:
        return "model_text_delta", {"text": event.text}
    if event.type == "reasoning_delta" and event.reasoning_content:
        return "model_reasoning_delta", {"reasoning_content": event.reasoning_content}
    if event.type == "tool_call_delta" and event.tool_call_delta is not None:
        return "model_tool_call_delta", event.tool_call_delta.model_dump(mode="json")
    if event.type == "retry_scheduled" and event.retry is not None:
        return "run_retry_scheduled", event.retry.model_dump(mode="json")
    if event.type == "done":
        completion = event.completion or ProviderCompletion()
        return "model_message_completed", {
            "context_id": context_id,
            "tool_call_count": progress.tool_call_count,
            "finish_reason": completion.finish_reason,
            "provider_finish_reason": completion.provider_finish_reason,
            "has_reasoning": completion.has_reasoning or progress.has_reasoning,
            "usage": event.usage.model_dump(mode="json") if event.usage is not None else {},
        }
    return None
