from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Protocol, cast

from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
from ouro.application.prompt_context import PromptContext, PromptContextResolver
from ouro.application.usage import UsageService
from ouro.config.compaction import CompactionConfig, CompactionModelConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id, utc_now
from ouro.core.billing import UsagePurpose
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal, settle_owned_tasks
from ouro.core.compaction import (
    CompactionAbortedError,
    CompactionArtifact,
    CompactionModelOutput,
    CompactionRecord,
    CompactionRequest,
    CompactionStrategy,
    CompactionStrategyError,
    CompactionStrategyRegistry,
    CompactionTrigger,
    compaction_frames,
    compaction_source_digest,
    plan_compaction,
    project_compacted_messages,
)
from ouro.core.context import (
    ContextBuilder,
    ContextView,
    estimate_text_tokens,
    estimate_text_tokens_from_character_counts,
    project_recovery_messages,
    text_character_counts,
)
from ouro.core.context_providers import (
    AttributedContextFragment,
    ContextFragment,
    ContextProviderCall,
    ContextProviderRegistry,
)
from ouro.core.context_snapshots import ContextSnapshotRecord, context_view_digest
from ouro.core.errors import (
    CompactionError,
    ConfigError,
    ContextBlockedError,
    ContextOverflowError,
    PersistenceError,
)
from ouro.core.events import EventRecord
from ouro.core.message_frames import project_tool_exchanges
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import MessageRecord, ModelMessage, model_visible_message
from ouro.core.observers import ContextPreparedObservation, FrozenJson, freeze_json
from ouro.core.provider import (
    ModelMetadata,
    ProviderCompletion,
    ProviderStreamEvent,
    Usage,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolDefinition
from ouro.prompts import AFK_CONTEXT_PROMPT, SUMMARIZATION_PROMPT, render_summarization_prompt
from ouro.sessions import SessionStore

_SUMMARY_PATTERN = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


class ModelMessageProjector(Protocol):
    def __call__(
        self,
        messages: list[MessageRecord],
        *,
        supports_vision: bool,
    ) -> list[MessageRecord]: ...


class CompactionUsageRecorder(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        run_id: str,
        usage: Usage,
        model: ModelMetadata,
        request_id: str,
        purpose: UsagePurpose,
    ) -> RunRecord: ...


class ContextMessageAppender(Protocol):
    def __call__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        prompt: str,
        role: MessageRole,
        scaffold: bool,
        sequence: int,
        metadata: dict[str, object],
        semantic_tag: SemanticTag | None = None,
    ) -> MessageRecord: ...


@dataclass(frozen=True)
class CompactionExecution:
    record: CompactionRecord | None
    sequence: int
    skipped: bool = False


@dataclass(frozen=True)
class _CompactionAdapterRoute:
    adapter: RuntimeAdapter
    route: str


@dataclass(frozen=True)
class _SummaryChunkResult:
    summary: str
    usage: dict[str, int | float]
    model_output: CompactionModelOutput


async def _compact_until_aborted(
    strategy: CompactionStrategy,
    request: CompactionRequest,
    *,
    signal: AbortSignal,
) -> CompactionArtifact:
    """Own one strategy task until it completes or cancellation fully settles it."""

    if signal.is_aborted():
        raise CompactionAbortedError(signal.reason)

    execution = asyncio.create_task(strategy.compact(request, signal=signal))
    abort_waiter = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            (execution, abort_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        execution.cancel()
        abort_waiter.cancel()
        await settle_owned_tasks(execution, abort_waiter)
        raise

    if abort_waiter in done or signal.is_aborted():
        execution.cancel()
        abort_waiter.cancel()
        if await settle_owned_tasks(execution, abort_waiter):
            raise asyncio.CancelledError
        model_outputs: Sequence[CompactionModelOutput] = ()
        if not execution.cancelled():
            exception = execution.exception()
            if isinstance(exception, CompactionStrategyError):
                model_outputs = exception.model_outputs
            elif exception is None:
                model_outputs = execution.result().model_outputs
        raise CompactionAbortedError(signal.reason, model_outputs=model_outputs)

    abort_waiter.cancel()
    if await settle_owned_tasks(abort_waiter):
        raise asyncio.CancelledError
    try:
        return execution.result()
    except CompactionAbortedError as exc:
        # Only the Host signal may classify a Run as aborted. A strategy cannot
        # manufacture cancellation while the owning Run remains active.
        raise CompactionStrategyError(
            str(exc),
            model_outputs=exc.model_outputs,
            terminal=True,
        ) from exc


class LLMSummaryCompactionStrategy:
    """Generate an incremental summary without entering the normal agent loop."""

    strategy_id = "builtin.llm_summary"
    version = "1"
    prompt_version = "2"

    def __init__(
        self,
        adapter: RuntimeAdapter,
        prompt: str = SUMMARIZATION_PROMPT,
        *,
        configured_adapter: RuntimeAdapter | None = None,
        configured_adapter_error: str | None = None,
        configured_adapter_resolver: Callable[[], RuntimeAdapter] | None = None,
        model_config: CompactionModelConfig | None = None,
        runtime_adapter_resolver: Callable[[str], RuntimeAdapter] | None = None,
        usage_recorder: CompactionUsageRecorder | None = None,
    ) -> None:
        self.runtime_adapter = adapter
        self.runtime_adapter_resolver = runtime_adapter_resolver
        self.configured_adapter = configured_adapter
        self.configured_adapter_error = configured_adapter_error
        self.configured_adapter_resolver = configured_adapter_resolver
        self.model_config = model_config or CompactionModelConfig()
        self.usage_recorder = usage_recorder
        self.prompt = prompt

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> CompactionArtifact:
        failures: list[str] = []
        model_outputs: list[CompactionModelOutput] = []
        if self.configured_adapter_error:
            failures.append(self.configured_adapter_error)

        routes = await run_blocking(self._adapter_routes, request.session_id)
        for route in routes:
            try:
                adapter = (
                    _apply_dedicated_compaction_model_config(route.adapter, self.model_config)
                    if route.route == "configured"
                    else route.adapter
                )
                artifact = await self._compact_with_adapter(
                    request,
                    adapter=adapter,
                    route=route.route,
                    signal=signal,
                )
            except Exception as exc:  # noqa: BLE001 - model-route fallback boundary.
                model_outputs.extend(_outputs_from_error(exc))
                if signal is not None and signal.is_aborted():
                    raise CompactionAbortedError(
                        "summary compaction was aborted",
                        model_outputs=model_outputs,
                    ) from exc
                if isinstance(exc, CompactionStrategyError) and exc.terminal:
                    raise CompactionStrategyError(
                        str(exc),
                        model_outputs=model_outputs,
                        terminal=True,
                    ) from exc
                failures.append(f"{route.route}: {type(exc).__name__}: {exc}")
                continue
            prior_outputs = list(model_outputs)
            model_outputs.extend(artifact.model_outputs)
            metadata = dict(artifact.metadata)
            metadata["model_route"] = route.route
            metadata["model_request_count"] = len(model_outputs)
            if failures:
                metadata["model_fallback_reason"] = "; ".join(failures)
            total_usage = dict(artifact.usage)
            for output in prior_outputs:
                total_usage = _merge_usage(total_usage, output.usage)
            return artifact.model_copy(
                update={
                    "usage": total_usage,
                    "metadata": metadata,
                    "model_outputs": model_outputs,
                }
            )

        message = "all compaction model routes failed: " + "; ".join(failures)
        if model_outputs:
            raise CompactionStrategyError(message, model_outputs=model_outputs)
        raise CompactionError(message)

    def _adapter_routes(self, session_id: str) -> list[_CompactionAdapterRoute]:
        routes: list[_CompactionAdapterRoute] = []
        try:
            configured = (
                self.configured_adapter_resolver()
                if self.configured_adapter_resolver is not None
                else self.configured_adapter
            )
        except ConfigError as exc:
            raise CompactionStrategyError(str(exc), terminal=True) from exc
        if configured is not None:
            routes.append(
                _CompactionAdapterRoute(
                    adapter=configured,
                    route="configured",
                )
            )
        runtime_route = "runtime_fallback" if self.model_config.has_configured_route else "runtime"
        runtime_adapter = (
            self.runtime_adapter_resolver(session_id)
            if self.runtime_adapter_resolver is not None
            else self.runtime_adapter
        )
        routes.append(
            _CompactionAdapterRoute(
                adapter=runtime_adapter,
                route=runtime_route,
            )
        )
        return routes

    async def _compact_with_adapter(
        self,
        request: CompactionRequest,
        *,
        adapter: RuntimeAdapter,
        route: str,
        signal: AbortSignal | None,
    ) -> CompactionArtifact:
        rolling_summary = request.previous_summary
        total_usage: dict[str, int | float] = {}
        model_outputs: list[CompactionModelOutput] = []
        remaining_frames = compaction_frames(request.messages)
        if not remaining_frames:
            raise CompactionError("summary compaction received no canonical messages")

        summary_output_tokens = adapter.model.max_output_tokens
        rendered_prompt = render_summarization_prompt(self.prompt, summary_output_tokens)
        chunk_count = 0
        try:
            while remaining_frames:
                chunk, consumed_frames = self._next_chunk(
                    remaining_frames,
                    adapter=adapter,
                    previous_summary=rolling_summary,
                    summary_output_tokens=summary_output_tokens,
                )
                try:
                    result = await self._summarize_chunk(
                        adapter=adapter,
                        route=route,
                        chunk_index=chunk_count + 1,
                        attempt_index=1,
                        input_frame_count=consumed_frames,
                        session_id=request.session_id,
                        run_id=request.run_id,
                        previous_summary=rolling_summary,
                        messages=chunk,
                        rendered_prompt=rendered_prompt,
                        summary_output_tokens=summary_output_tokens,
                        signal=signal,
                    )
                except CompactionStrategyError as first_error:
                    if not _should_retry_smaller_chunk(first_error, consumed_frames):
                        raise
                    model_outputs.extend(first_error.model_outputs)
                    for output in first_error.model_outputs:
                        total_usage = _merge_usage(total_usage, output.usage)
                    consumed_frames = max(1, consumed_frames // 2)
                    chunk = [
                        message for frame in remaining_frames[:consumed_frames] for message in frame
                    ]
                    try:
                        result = await self._summarize_chunk(
                            adapter=adapter,
                            route=route,
                            chunk_index=chunk_count + 1,
                            attempt_index=2,
                            input_frame_count=consumed_frames,
                            session_id=request.session_id,
                            run_id=request.run_id,
                            previous_summary=rolling_summary,
                            messages=chunk,
                            rendered_prompt=rendered_prompt,
                            summary_output_tokens=summary_output_tokens,
                            signal=signal,
                        )
                    except Exception as retry_error:
                        raise CompactionStrategyError(
                            str(retry_error),
                            model_outputs=_outputs_from_error(retry_error),
                            terminal=True,
                        ) from retry_error
                rolling_summary = result.summary
                total_usage = _merge_usage(total_usage, result.usage)
                model_outputs.append(result.model_output)
                chunk_count += 1
                remaining_frames = remaining_frames[consumed_frames:]
        except Exception as exc:
            outputs = [*model_outputs, *_outputs_from_error(exc)]
            if outputs:
                raise CompactionStrategyError(
                    str(exc),
                    model_outputs=outputs,
                    terminal=isinstance(exc, CompactionStrategyError) and exc.terminal,
                ) from exc
            raise

        return CompactionArtifact(
            summary=rolling_summary or "",
            usage=total_usage,
            model_outputs=model_outputs,
            metadata={
                "prompt_version": self.prompt_version,
                "prompt_hash": hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest(),
                "chunk_count": chunk_count,
                "model_request_count": len(model_outputs),
                "model_provider": adapter.model.provider_id,
                "model_endpoint_id": adapter.model.endpoint_id,
                "model_id": adapter.model.model_id,
                "model_temperature": adapter.model.temperature,
                "model_max_output_tokens": adapter.model.max_output_tokens,
                "summary_output_tokens": summary_output_tokens,
                "model_reasoning": adapter.model.reasoning_resolution.effective.model_dump(
                    mode="json"
                ),
            },
        )

    def _next_chunk(
        self,
        frames: Sequence[Sequence[MessageRecord]],
        *,
        adapter: RuntimeAdapter,
        previous_summary: str | None,
        summary_output_tokens: int,
    ) -> tuple[list[MessageRecord], int]:
        """Choose one provider-safe chunk using the latest rolling summary size."""

        capacity = adapter.model.context_window - summary_output_tokens
        rendered_prompt = render_summarization_prompt(self.prompt, summary_output_tokens)
        fixed_tokens = estimate_text_tokens(rendered_prompt) + 8
        empty_payload = _summary_payload(previous_summary, [])
        ascii_characters, non_ascii_characters = text_character_counts(empty_payload)
        if (
            fixed_tokens
            + estimate_text_tokens_from_character_counts(
                ascii_characters=ascii_characters,
                non_ascii_characters=non_ascii_characters,
            )
            >= capacity
        ):
            raise ContextOverflowError(
                "summary prompt and previous summary exceed model input budget"
            )

        current: list[MessageRecord] = []
        consumed_frames = 0
        has_serialized_messages = False
        for frame in frames:
            serialized_frame = _serialize_messages(frame)
            separator = "\n" if serialized_frame and has_serialized_messages else ""
            added_ascii, added_non_ascii = text_character_counts(f"{separator}{serialized_frame}")
            if (
                fixed_tokens
                + estimate_text_tokens_from_character_counts(
                    ascii_characters=ascii_characters + added_ascii,
                    non_ascii_characters=non_ascii_characters + added_non_ascii,
                )
                > capacity
            ):
                if current:
                    break
                raise ContextOverflowError(
                    "a single context frame exceeds the summary input budget"
                )
            current.extend(frame)
            ascii_characters += added_ascii
            non_ascii_characters += added_non_ascii
            has_serialized_messages = has_serialized_messages or bool(serialized_frame)
            consumed_frames += 1
        return current, consumed_frames

    async def _summarize_chunk(
        self,
        *,
        adapter: RuntimeAdapter,
        route: str,
        chunk_index: int,
        attempt_index: int,
        input_frame_count: int,
        session_id: str,
        run_id: str | None,
        previous_summary: str | None,
        messages: Sequence[MessageRecord],
        rendered_prompt: str,
        summary_output_tokens: int,
        signal: AbortSignal | None,
    ) -> _SummaryChunkResult:
        summary_input = MessageRecord.text(
            session_id=session_id,
            role="user",
            text=_summary_payload(previous_summary, messages),
            scaffold=True,
        ).model_copy(update={"metadata": {"kind": "compaction_input"}})
        context = ContextView(
            system_prompt=rendered_prompt,
            model_messages=[ModelMessage.from_record(summary_input)],
            tool_definitions=[],
        )
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, int | float] = {}
        saw_done = False
        completion = ProviderCompletion()
        request_id = new_id("req")
        estimated_input_tokens = (
            estimate_text_tokens(rendered_prompt)
            + estimate_text_tokens(summary_input.text_content)
            + 8
        )
        try:
            async for event in adapter.stream(
                context,
                signal=signal,
                max_output_tokens=summary_output_tokens,
                request_context=ProviderRequestContext(
                    {
                        "session.id": session_id,
                        "request.purpose": "compaction",
                        **({"run.id": run_id} if run_id is not None else {}),
                    }
                ),
            ):
                if event.type == "reasoning_delta" and event.reasoning_content:
                    reasoning_parts.append(event.reasoning_content)
                text_parts, usage, saw_done = _consume_summary_event(
                    event,
                    text_parts=text_parts,
                    usage=usage,
                    saw_done=saw_done,
                )
                if event.type == "done" and event.completion is not None:
                    completion = event.completion
                if (
                    event.type == "done"
                    and event.usage is not None
                    and self.usage_recorder is not None
                    and run_id is not None
                ):
                    self.usage_recorder(
                        session_id=session_id,
                        run_id=run_id,
                        usage=event.usage,
                        model=adapter.model,
                        request_id=request_id,
                        purpose="compaction",
                    )
        except Exception as exc:
            raise _summary_output_error(
                exc,
                adapter=adapter,
                request_id=request_id,
                route=route,
                chunk_index=chunk_index,
                attempt_index=attempt_index,
                input_frame_count=input_frame_count,
                requested_output_tokens=summary_output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                input_message_count=len(messages),
                completion=completion,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                usage=usage,
            ) from exc
        if signal is not None and signal.is_aborted():
            error: CompactionError = CompactionAbortedError("summary compaction was aborted")
            raise _summary_output_error(
                error,
                adapter=adapter,
                request_id=request_id,
                route=route,
                chunk_index=chunk_index,
                attempt_index=attempt_index,
                input_frame_count=input_frame_count,
                requested_output_tokens=summary_output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                input_message_count=len(messages),
                completion=completion,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                usage=usage,
            ) from error
        if not saw_done:
            error = CompactionError("summary provider stream ended without completion")
            raise _summary_output_error(
                error,
                adapter=adapter,
                request_id=request_id,
                route=route,
                chunk_index=chunk_index,
                attempt_index=attempt_index,
                input_frame_count=input_frame_count,
                requested_output_tokens=summary_output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                input_message_count=len(messages),
                completion=completion,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                usage=usage,
            ) from error
        raw_summary = "".join(text_parts)
        match = _SUMMARY_PATTERN.search(raw_summary)
        if match is None or not match.group(1).strip():
            error = CompactionError("summary response did not contain a non-empty <summary> block")
            raise _summary_output_error(
                error,
                adapter=adapter,
                request_id=request_id,
                route=route,
                chunk_index=chunk_index,
                attempt_index=attempt_index,
                input_frame_count=input_frame_count,
                requested_output_tokens=summary_output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                input_message_count=len(messages),
                completion=completion,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                usage=usage,
            ) from error
        return _SummaryChunkResult(
            summary=match.group(1).strip(),
            usage=usage,
            model_output=_compaction_model_output(
                adapter=adapter,
                request_id=request_id,
                route=route,
                chunk_index=chunk_index,
                attempt_index=attempt_index,
                input_frame_count=input_frame_count,
                requested_output_tokens=summary_output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                input_message_count=len(messages),
                completion=completion,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                usage=usage,
            ),
        )


class ContextPreparationService:
    """Prepare provider context and coordinate durable summary compaction."""

    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        context_providers: ContextProviderRegistry,
        events: RuntimeEventRecorder,
        context_builder: ContextBuilder,
        strategies: CompactionStrategyRegistry,
        config: CompactionConfig,
        system_prompt: str,
        append_context_message: ContextMessageAppender,
        prompt_context_resolver: PromptContextResolver | None = None,
        project_model_messages: ModelMessageProjector | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.context_providers = context_providers
        self.events = events
        self.context_builder = context_builder
        self.strategies = strategies
        self.config = config
        self.system_prompt = system_prompt
        self.prompt_context_resolver = prompt_context_resolver
        self.append_context_message = append_context_message
        self.project_model_messages = project_model_messages
        self.usage_service = usage_service

    async def prepare(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        model_round: int,
        turn_id: str,
        abort_signal: AbortSignal,
        adapter: RuntimeAdapter | None = None,
        context_providers: ContextProviderRegistry | None = None,
        strategies: CompactionStrategyRegistry | None = None,
        skill_instructions: str = "",
    ) -> tuple[ContextView, int]:
        prompt_context = await self._resolve_prompt_context(emitted, sequence, session, run)
        active_adapter = adapter or self.adapter
        active_context_providers = context_providers or self.context_providers
        provider_result = await active_context_providers.evaluate(
            ContextProviderCall(
                harness=await run_blocking(
                    capture_harness,
                    self.store,
                    session.session_id,
                    run=run,
                    usage_service=self.usage_service,
                )
                if active_context_providers.registered()
                else None,
                session_id=session.session_id,
                run_id=run.run_id,
                message_count=len(messages),
                model_round=model_round,
                workspace=session.workspace,
            )
        )
        sequence = await self.events.record_context_provider_failures(
            emitted,
            sequence,
            provider_result.failures,
        )
        if provider_result.blocked:
            failure = provider_result.failures[-1]
            raise ContextBlockedError(
                "required context provider failed: "
                f"{failure.plugin_id}:{failure.provider_id}: {failure.error_type}"
            )
        added_context = await run_blocking(
            self._materialize_context_fragments,
            provider_result.fragments,
            session=session,
            run=run,
            messages=messages,
            model_round=model_round,
        )
        if session.interaction_mode == "unattended":
            added_context.append(
                ContextFragment(
                    text=AFK_CONTEXT_PROMPT,
                    semantic_tag="unattended-mode-reminder",
                )
            )
        injected_message_count = sum(fragment.target == "message" for fragment in added_context)
        budget = self._input_budget(active_adapter)
        active = await run_blocking(self.store.load_active_compaction, session.session_id)
        source_messages, view = await run_blocking(
            self._project_context,
            session=session,
            messages=messages,
            active=active,
            tools=tools,
            added_context=added_context,
            adapter=active_adapter,
            skill_instructions=skill_instructions,
            prompt_context=prompt_context,
        )

        trigger_tokens = int(budget * self.config.policy.trigger_ratio)
        estimated = view.metrics.estimated_input_tokens
        if self.config.policy.auto_enabled and estimated >= trigger_tokens:
            try:
                execution = await self.compact(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    messages=messages,
                    tools=tools,
                    added_context=added_context,
                    trigger="auto",
                    force=False,
                    abort_signal=abort_signal,
                    estimated_tokens_before=estimated,
                    adapter=active_adapter,
                    strategies=strategies,
                    skill_instructions=skill_instructions,
                    prompt_context=prompt_context,
                )
            except CompactionError as exc:
                if isinstance(exc, CompactionAbortedError):
                    raise
                sequence = await run_blocking(
                    self.store.latest_event_sequence,
                    session.session_id,
                )
                if view.metrics.over_budget:
                    raise ContextBlockedError(
                        f"context compaction is required before the run can continue: {exc}"
                    ) from exc
                metadata = dict(view.context_metadata)
                metadata.update(
                    {
                        "compaction_deferred": True,
                        "compaction_error": str(exc),
                        "compaction_id": active.compaction_id if active else None,
                        "compaction_strategy": active.strategy_id if active else None,
                        "covered_message_count": len(active.covered_message_ids) if active else 0,
                    }
                )
                final_view = view.model_copy(update={"context_metadata": metadata})
                return await self._finalize_prepared_context(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    turn_id=turn_id,
                    model_round=model_round,
                    adapter=active_adapter,
                    view=final_view,
                    source_messages=source_messages,
                    injected_message_count=injected_message_count,
                )
            sequence = execution.sequence
            if execution.record is None and view.metrics.over_budget:
                raise ContextBlockedError(
                    "context is over budget and no complete older context frame is eligible "
                    "for compaction"
                )
            active = execution.record or active
            source_messages, view = await run_blocking(
                self._project_context,
                session=session,
                messages=messages,
                active=active,
                tools=tools,
                added_context=added_context,
                adapter=active_adapter,
                skill_instructions=skill_instructions,
                prompt_context=prompt_context,
            )

        if view.metrics.over_budget:
            raise ContextBlockedError(
                "context remains over the model input budget after summary compaction"
            )
        return await self._finalize_prepared_context(
            emitted,
            sequence,
            session=session,
            run=run,
            turn_id=turn_id,
            model_round=model_round,
            adapter=active_adapter,
            view=view,
            source_messages=source_messages,
            injected_message_count=injected_message_count,
        )

    def record_provider_completion(
        self,
        *,
        session_id: str,
        context_id: str,
        usage: Usage | None,
    ) -> None:
        record = self.store.load_latest_context(session_id)
        if record is None or record.context_id != context_id:
            raise PersistenceError(
                f"latest context for session {session_id} does not match {context_id}"
            )
        self.store.save_latest_context(
            record.model_copy(
                update={
                    "completed_at": utc_now(),
                    "provider_reported_input_tokens": (
                        usage.input_tokens if usage is not None else None
                    ),
                }
            )
        )

    def _materialize_context_fragments(
        self,
        fragments: list[AttributedContextFragment],
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        model_round: int,
    ) -> list[ContextFragment]:
        ephemeral: list[ContextFragment] = []
        for fragment in fragments:
            if fragment.persistence == "ephemeral":
                ephemeral.append(
                    ContextFragment(
                        text=fragment.text,
                        target=fragment.target,
                        role=fragment.role,
                        scaffold=fragment.scaffold,
                        semantic_tag=fragment.semantic_tag,
                    )
                )
                continue
            delivery_key = (
                f"{run.run_id}:{model_round}:{fragment.plugin_id}:"
                f"{fragment.provider_id}:{fragment.fragment_index}"
            )
            if any(
                message.metadata.get("context_provider_delivery_key") == delivery_key
                for message in messages
            ):
                continue
            metadata: dict[str, object] = {
                "kind": "context_provider",
                "context_provider_delivery_key": delivery_key,
                "plugin_id": fragment.plugin_id,
                "contribution_id": fragment.provider_id,
                "model_round": model_round,
                "fragment_index": fragment.fragment_index,
            }
            if fragment.generation_id is not None:
                metadata["generation_id"] = fragment.generation_id
            try:
                message = self.append_context_message(
                    session=session,
                    run=run,
                    prompt=fragment.text,
                    role=fragment.role,
                    scaffold=fragment.scaffold,
                    sequence=len(messages) + 1,
                    metadata=metadata,
                    semantic_tag=fragment.semantic_tag,
                )
            except PersistenceError as exc:
                raise ContextBlockedError(
                    "context provider message could not be persisted before model execution"
                ) from exc
            messages.append(message)
        return ephemeral

    async def _observe_built_context(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        view: ContextView,
        model_round: int,
    ) -> None:
        metadata = cast(
            "Mapping[str, FrozenJson]",
            freeze_json(view.context_metadata),
        )
        await self.events.observe(
            ContextPreparedObservation(
                session_id=session.session_id,
                run_id=run.run_id,
                message_count=len(view.model_messages),
                tool_count=len(view.tool_definitions),
                model_round=model_round,
                context_metadata=metadata,
            )
        )

    async def _finalize_prepared_context(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        model_round: int,
        adapter: RuntimeAdapter,
        view: ContextView,
        source_messages: list[MessageRecord],
        injected_message_count: int,
    ) -> tuple[ContextView, int]:
        context_id = new_id("ctx")
        prepared = view.model_copy(update={"context_id": context_id})
        await run_blocking(
            self._save_prepared_context,
            context=prepared,
            session=session,
            run=run,
            turn_id=turn_id,
            model_round=model_round,
            adapter=adapter,
            source_messages=source_messages,
            injected_message_count=injected_message_count,
        )
        await self._observe_built_context(
            session=session,
            run=run,
            view=prepared,
            model_round=model_round,
        )
        return prepared, sequence

    def _save_prepared_context(
        self,
        *,
        context: ContextView,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        model_round: int,
        adapter: RuntimeAdapter,
        source_messages: list[MessageRecord],
        injected_message_count: int,
    ) -> None:
        """Persist one fully materialized context snapshot on the I/O worker."""

        if context.context_id is None:
            raise ValueError("prepared model context is missing its identity")
        with self.store.session_mutation(session.session_id):
            if (
                run.history_epoch
                != self.store.load_session_record(session.session_id).history_epoch
            ):
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            self.store.save_latest_context(
                ContextSnapshotRecord(
                    context_id=context.context_id,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    turn_id=turn_id,
                    model_round=model_round,
                    provider_id=adapter.model.provider_id,
                    endpoint_id=adapter.model.endpoint_id,
                    model_id=adapter.model.model_id,
                    supports_vision=adapter.model.supports_vision,
                    system_prompt=context.system_prompt,
                    injected_messages=context.model_messages[:injected_message_count],
                    source_messages=source_messages,
                    tool_definitions=context.tool_definitions,
                    metrics=context.metrics,
                    context_metadata=context.context_metadata,
                    context_digest=context_view_digest(context),
                )
            )

    async def compact(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        added_context: Sequence[str | ContextFragment] | None,
        trigger: CompactionTrigger,
        force: bool,
        abort_signal: AbortSignal,
        estimated_tokens_before: int | None = None,
        adapter: RuntimeAdapter | None = None,
        strategies: CompactionStrategyRegistry | None = None,
        skill_instructions: str = "",
        prompt_context: PromptContext | None = None,
    ) -> CompactionExecution:
        # Auto compaction shares the request snapshot; a manual command resolves its own.
        if prompt_context is None:
            prompt_context = await self._resolve_prompt_context(emitted, sequence, session, run)
        active_adapter = adapter or self.adapter
        budget = self._input_budget(active_adapter)
        active = await run_blocking(self.store.load_active_compaction, session.session_id)
        materialized_messages = await run_blocking(
            self._materialize_messages, messages, active_adapter
        )
        _, initial_view = await run_blocking(
            self._project_context,
            session=session,
            messages=messages,
            active=active,
            tools=tools,
            added_context=added_context or [],
            adapter=active_adapter,
            skill_instructions=skill_instructions,
            prompt_context=prompt_context,
        )
        before_tokens = (
            estimated_tokens_before
            if estimated_tokens_before is not None
            else initial_view.metrics.estimated_input_tokens
        )
        target_tokens = int(budget * self.config.policy.target_ratio)
        fixed_tokens = initial_view.metrics.estimated_fixed_tokens
        summary_output_tokens = active_adapter.model.max_output_tokens
        tail_budget = max(
            1,
            target_tokens - fixed_tokens - summary_output_tokens,
        )
        plan = await run_blocking(
            plan_compaction,
            materialized_messages,
            active,
            tail_token_budget=tail_budget,
            force=force,
            token_estimator=self.context_builder.token_estimator,
            protected_run_id=run.run_id if run.kind == "agent" else None,
        )
        if not plan.messages_to_compact:
            if trigger == "auto" and before_tokens > budget:
                raise ContextOverflowError(
                    "context is over budget and no complete older turn is eligible for compaction"
                )
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="compaction_skipped",
                payload={"trigger": trigger, "reason": "no_eligible_closed_turn"},
            )
            return CompactionExecution(record=None, sequence=sequence, skipped=True)

        compaction_id = new_id("cmp")
        compaction_started_clock = monotonic()
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="compaction_started",
            payload={
                "compaction_id": compaction_id,
                "trigger": trigger,
                "strategy_id": self.config.strategy_id,
                "tokens_before": before_tokens,
                "target_tokens": target_tokens,
                "configured_model_provider": self.config.model.provider_id,
                "configured_model_endpoint": self.config.model.endpoint_id,
            },
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="compaction_progress",
            payload={"compaction_id": compaction_id, "trigger": trigger, "phase": "generating"},
        )

        request = CompactionRequest(
            harness=await run_blocking(
                capture_harness,
                self.store,
                session.session_id,
                run=run,
                usage_service=self.usage_service,
            ),
            session_id=session.session_id,
            run_id=run.run_id,
            strategy_id=self.config.strategy_id,
            trigger=trigger,
            previous_summary=active.summary if active else None,
            messages=plan.messages_to_compact,
            input_token_budget=budget,
            summary_output_tokens=summary_output_tokens,
        )
        try:
            strategy, artifact = await self._execute_strategy(
                request,
                signal=abort_signal,
                strategies=strategies,
            )
            sequence = await self._record_model_outputs(
                emitted,
                sequence,
                session=session,
                run=run,
                compaction_id=compaction_id,
                outputs=artifact.model_outputs,
            )
            summary = artifact.summary.strip()
            if not summary:
                raise CompactionError("compaction strategy returned an empty summary")
            source_prefix = messages[: len(plan.covered_message_ids)]
            record = CompactionRecord(
                compaction_id=compaction_id,
                session_id=session.session_id,
                previous_compaction_id=active.compaction_id if active else None,
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                trigger=trigger,
                summary=summary,
                covered_message_ids=plan.covered_message_ids,
                history_boundary_message_id=messages[-1].message_id,
                source_digest=await run_blocking(compaction_source_digest, source_prefix),
                model_provider=str(artifact.metadata.get("model_provider") or "") or None,
                model_endpoint_id=(str(artifact.metadata.get("model_endpoint_id") or "") or None),
                model_id=str(artifact.metadata.get("model_id") or "") or None,
                prompt_version=str(artifact.metadata.get("prompt_version") or "") or None,
                prompt_hash=str(artifact.metadata.get("prompt_hash") or "") or None,
                estimated_tokens_before=before_tokens,
                estimated_summary_tokens=estimate_text_tokens(summary),
                usage=artifact.usage,
                metadata=dict(artifact.metadata),
            )
            _, final_view = await run_blocking(
                self._project_context,
                session=session,
                messages=messages,
                active=record,
                tools=tools,
                added_context=added_context or [],
                adapter=active_adapter,
                skill_instructions=skill_instructions,
                prompt_context=prompt_context,
            )
            after_tokens = final_view.metrics.estimated_input_tokens
            if final_view.metrics.over_budget:
                raise ContextOverflowError(
                    "generated summary and protected recent turn exceed the model input budget"
                )
            record = record.model_copy(
                update={
                    "estimated_tokens_after": after_tokens,
                    "context_metrics_after": final_view.metrics,
                    "duration_seconds": max(
                        0.0,
                        monotonic() - compaction_started_clock,
                    ),
                }
            )
            await run_blocking(self._commit_compaction, record, run)
        except Exception as exc:
            if isinstance(exc, CompactionStrategyError):
                sequence = await self._record_model_outputs(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    compaction_id=compaction_id,
                    outputs=exc.model_outputs,
                )
            if isinstance(exc, CompactionAbortedError):
                raise
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="compaction_failed",
                payload={
                    "compaction_id": compaction_id,
                    "trigger": trigger,
                    "strategy_id": self.config.strategy_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            if isinstance(exc, CompactionError):
                raise
            raise CompactionError(f"summary compaction failed: {exc}") from exc

        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="compaction_completed",
            payload={
                "compaction_id": record.compaction_id,
                "trigger": trigger,
                "strategy_id": record.strategy_id,
                "covered_message_count": len(record.covered_message_ids),
                "tokens_before": before_tokens,
                "tokens_after": record.estimated_tokens_after,
                "duration_seconds": record.duration_seconds,
                "context_metrics_after": final_view.metrics.model_dump(mode="json"),
                "model_provider": record.model_provider,
                "model_endpoint_id": record.model_endpoint_id,
                "model_id": record.model_id,
                "model_route": record.metadata.get("model_route"),
                "model_fallback_reason": record.metadata.get("model_fallback_reason"),
            },
        )
        return CompactionExecution(record=record, sequence=sequence)

    async def _execute_strategy(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal,
        strategies: CompactionStrategyRegistry | None = None,
    ) -> tuple[CompactionStrategy, CompactionArtifact]:
        active_strategies = strategies or self.strategies
        strategy_ids = [self.config.strategy_id]
        if self.config.fallback_strategy_id:
            strategy_ids.append(self.config.fallback_strategy_id)
        failures: list[Exception] = []
        model_outputs: list[CompactionModelOutput] = []
        for strategy_id in strategy_ids:
            try:
                strategy = active_strategies.get(strategy_id)
                active_request = request.model_copy(update={"strategy_id": strategy_id})
                artifact = await _compact_until_aborted(
                    strategy,
                    active_request,
                    signal=signal,
                )
                if model_outputs:
                    artifact = artifact.model_copy(
                        update={
                            "model_outputs": [*model_outputs, *artifact.model_outputs],
                        }
                    )
                return strategy, artifact
            except Exception as exc:  # noqa: BLE001 - strategy isolation boundary.
                if isinstance(exc, CompactionAbortedError):
                    raise CompactionAbortedError(
                        str(exc),
                        model_outputs=[*model_outputs, *exc.model_outputs],
                    ) from exc
                if isinstance(exc, CompactionStrategyError) and exc.terminal:
                    raise
                failures.append(exc)
                model_outputs.extend(_outputs_from_error(exc))
        if model_outputs:
            message = (
                str(failures[0])
                if len(failures) == 1
                else "all configured compaction strategies failed: "
                + "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
            )
            raise CompactionStrategyError(message, model_outputs=model_outputs)
        if len(failures) == 1:
            raise CompactionError(str(failures[0])) from failures[0]
        raise CompactionError(
            "all configured compaction strategies failed: "
            + "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
        )

    async def _record_model_outputs(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        compaction_id: str,
        outputs: Sequence[CompactionModelOutput],
    ) -> int:
        for output in outputs:
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="compaction_model_output",
                payload={
                    "compaction_id": compaction_id,
                    **output.model_dump(mode="json"),
                },
            )
        return sequence

    async def _resolve_prompt_context(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
    ) -> PromptContext:
        if self.prompt_context_resolver is None:
            return PromptContext(system_prompt=self.system_prompt)
        try:
            return await run_blocking(self.prompt_context_resolver)
        except ConfigError as exc:
            message = f"Cannot prepare system instructions: {exc}"
            await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="diagnostic",
                payload={
                    "code": "prompt_configuration_invalid",
                    "severity": "error",
                    "component": "application.context_preparation",
                    "message": message,
                    "recoverable": True,
                    "suggested_action": "Correct the personal prompt configuration, then continue.",
                },
            )
            raise ContextBlockedError(message) from exc

    def _project_context(
        self,
        *,
        session: SessionRecord,
        messages: list[MessageRecord],
        active: CompactionRecord | None,
        tools: list[ToolDefinition],
        added_context: Sequence[str | ContextFragment],
        adapter: RuntimeAdapter,
        skill_instructions: str,
        prompt_context: PromptContext,
    ) -> tuple[list[MessageRecord], ContextView]:
        complete_messages, excluded = project_tool_exchanges(
            project_compacted_messages(messages, active)
        )
        # Budget only native state that can belong to this request. Protocol and
        # account compatibility stay inside the codec; execution epochs stay here.
        candidate_runs = {
            message.message_id: message.run_id
            for message in complete_messages
            if (artifact := message.provider_continuation) is not None
            and artifact.provider_id == adapter.model.provider_id
            and artifact.model_id == adapter.model.model_id
            and message.run_id is not None
        }
        replayable = {
            run_id
            for run_id in set(candidate_runs.values())
            if self.store.load_run(session.session_id, run_id).history_epoch
            == session.history_epoch
        }
        complete_messages = [
            message.model_copy(update={"provider_continuation": None})
            if message.provider_continuation is not None
            and candidate_runs.get(message.message_id) not in replayable
            else message
            for message in complete_messages
        ]
        source_messages = project_recovery_messages(complete_messages)
        effective_messages = self._materialize_messages(source_messages, adapter)
        view = self._build_view(
            messages=effective_messages,
            tools=tools,
            added_context=added_context,
            adapter=adapter,
            skill_instructions=skill_instructions,
            prompt_context=prompt_context,
        )
        metadata = dict(view.context_metadata)
        metadata.update(
            {
                "personal_prompt": prompt_context.metadata(),
                "compaction_id": active.compaction_id if active else None,
                "compaction_strategy": active.strategy_id if active else None,
                "covered_message_count": len(active.covered_message_ids) if active else 0,
                "history_diagnostics": (
                    [{"code": "incomplete_tool_exchange", "message_ids": list(excluded)}]
                    if excluded
                    else []
                ),
            }
        )
        return source_messages, view.model_copy(update={"context_metadata": metadata})

    def _build_view(
        self,
        *,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        added_context: Sequence[str | ContextFragment],
        adapter: RuntimeAdapter,
        prompt_context: PromptContext,
        skill_instructions: str = "",
    ) -> ContextView:
        return self.context_builder.build(
            messages=messages,
            tools=tools,
            system_prompt=prompt_context.system_prompt,
            added_context=added_context,
            additional_skill_instructions=skill_instructions,
            context_window_tokens=adapter.model.context_window,
            reserved_output_tokens=adapter.model.max_output_tokens,
        )

    def _commit_compaction(self, record: CompactionRecord, run: RunRecord) -> None:
        with self.store.session_mutation(record.session_id):
            if run.history_epoch != self.store.load_session_record(record.session_id).history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            self.store.append_compaction(record)
            self.store.activate_compaction(record.session_id, record.compaction_id)

    def _materialize_messages(
        self,
        messages: list[MessageRecord],
        adapter: RuntimeAdapter,
    ) -> list[MessageRecord]:
        if self.project_model_messages is None:
            return messages
        return self.project_model_messages(
            messages,
            supports_vision=adapter.model.supports_vision,
        )

    def _input_budget(self, adapter: RuntimeAdapter) -> int:
        return max(1, adapter.model.context_window - adapter.model.max_output_tokens)


def _apply_dedicated_compaction_model_config(
    adapter: RuntimeAdapter,
    config: CompactionModelConfig,
) -> RuntimeAdapter:
    """Apply explicit dedicated-route overrides without replacing endpoint defaults."""

    updates: dict[str, object] = {}
    if config.temperature is not None:
        updates["temperature"] = config.temperature
    if config.max_output_tokens is not None:
        if config.max_output_tokens > adapter.model.max_output_tokens:
            raise CompactionError(
                "compaction max_output_tokens exceed the selected endpoint output limit"
            )
        updates["max_output_tokens"] = config.max_output_tokens

    if config.reasoning is not None:
        updates["reasoning_request"] = config.reasoning

    if not updates:
        return adapter

    try:
        model = type(adapter.model).model_validate(
            adapter.model.model_copy(update=updates).model_dump()
        )
    except ValueError as exc:
        raise CompactionError(f"invalid compaction model override: {exc}") from exc
    return RuntimeAdapter(provider=adapter.provider, model=model)


def _consume_summary_event(
    event: ProviderStreamEvent,
    *,
    text_parts: list[str],
    usage: dict[str, int | float],
    saw_done: bool,
) -> tuple[list[str], dict[str, int | float], bool]:
    if event.usage is not None:
        usage = _merge_usage(usage, event.usage)
    if event.type == "text_delta" and event.text:
        text_parts.append(event.text)
    elif event.type == "tool_call_delta":
        raise CompactionError("summary provider attempted to call a tool")
    elif event.type == "error":
        raise CompactionStrategyError(
            event.error or "summary provider returned an error",
            # A classified authentication/access/quota failure must not silently
            # switch accounts or billing sources through the compaction fallback.
            terminal=event.error_code is not None,
        )
    elif event.type == "done":
        saw_done = True
    return text_parts, usage, saw_done


def _compaction_model_output(
    *,
    adapter: RuntimeAdapter,
    request_id: str,
    route: str,
    chunk_index: int,
    attempt_index: int,
    input_frame_count: int,
    requested_output_tokens: int,
    estimated_input_tokens: int,
    input_message_count: int,
    completion: ProviderCompletion,
    text_parts: Sequence[str],
    reasoning_parts: Sequence[str],
    usage: dict[str, int | float],
    error: Exception | None = None,
) -> CompactionModelOutput:
    raw_text = "".join(text_parts)
    summary_opened, summary_closed = _summary_markup_state(raw_text)
    return CompactionModelOutput(
        request_id=request_id,
        route=route,
        chunk_index=chunk_index,
        attempt_index=attempt_index,
        status="failed" if error is not None else "completed",
        requested_output_tokens=requested_output_tokens,
        estimated_input_tokens=estimated_input_tokens,
        input_message_count=input_message_count,
        input_frame_count=input_frame_count,
        finish_reason=completion.finish_reason,
        provider_finish_reason=completion.provider_finish_reason,
        summary_opened=summary_opened,
        summary_closed=summary_closed,
        raw_text=raw_text,
        raw_reasoning="".join(reasoning_parts),
        model_provider=adapter.model.provider_id,
        model_endpoint_id=adapter.model.endpoint_id,
        model_id=adapter.model.model_id,
        usage=usage,
        error_type=type(error).__name__ if error is not None else None,
        error=str(error) if error is not None else None,
    )


def _summary_output_error(
    error: Exception,
    *,
    adapter: RuntimeAdapter,
    request_id: str,
    route: str,
    chunk_index: int,
    attempt_index: int,
    input_frame_count: int,
    requested_output_tokens: int,
    estimated_input_tokens: int,
    input_message_count: int,
    completion: ProviderCompletion,
    text_parts: Sequence[str],
    reasoning_parts: Sequence[str],
    usage: dict[str, int | float],
) -> CompactionStrategyError:
    return CompactionStrategyError(
        str(error),
        terminal=isinstance(error, CompactionStrategyError) and error.terminal,
        model_outputs=(
            _compaction_model_output(
                adapter=adapter,
                request_id=request_id,
                route=route,
                chunk_index=chunk_index,
                attempt_index=attempt_index,
                input_frame_count=input_frame_count,
                requested_output_tokens=requested_output_tokens,
                estimated_input_tokens=estimated_input_tokens,
                input_message_count=input_message_count,
                completion=completion,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                usage=usage,
                error=error,
            ),
        ),
    )


def _summary_markup_state(raw_text: str) -> tuple[bool, bool]:
    opening = raw_text.find("<summary>")
    if opening < 0:
        return False, False
    return True, raw_text.find("</summary>", opening + len("<summary>")) >= 0


def _should_retry_smaller_chunk(
    error: CompactionStrategyError,
    input_frame_count: int,
) -> bool:
    if input_frame_count <= 1 or len(error.model_outputs) != 1:
        return False
    output = error.model_outputs[0]
    return (
        output.finish_reason == "length"
        and output.summary_opened
        and not output.summary_closed
        and output.error == "summary response did not contain a non-empty <summary> block"
    )


def _outputs_from_error(exc: Exception) -> tuple[CompactionModelOutput, ...]:
    if isinstance(exc, CompactionStrategyError):
        return exc.model_outputs
    return ()


def _serialize_messages(messages: Sequence[MessageRecord]) -> str:
    parts: list[str] = []
    for message in messages:
        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool_result": "Tool result",
        }[message.role]
        if message.semantic_tag is not None:
            label += f" ({message.semantic_tag})"
        if message.metadata.get("incomplete"):
            label += " (interrupted/incomplete)"
        parts.append(f"[{label}] {model_visible_message(message).text_content}")
        if message.tool_calls:
            serialized_calls = [call.model_dump(mode="json") for call in message.tool_calls]
            parts.append(f"[Assistant tool calls] {serialized_calls}")
        if message.role == "tool_result" and message.tool_result is not None:
            parts.append(f"[Tool result metadata] {message.tool_result.model_dump(mode='json')}")
    return "\n".join(parts)


def _summary_payload(
    previous_summary: str | None,
    messages: Sequence[MessageRecord],
) -> str:
    sections: list[str] = []
    if previous_summary:
        sections.extend(["<previous_summary>", previous_summary, "</previous_summary>", ""])
    sections.extend(["<conversation>", _serialize_messages(messages), "</conversation>"])
    return "\n".join(sections)


def _merge_usage(
    current: dict[str, int | float],
    usage: Usage | dict[str, int | float],
) -> dict[str, int | float]:
    values = usage.model_dump() if isinstance(usage, Usage) else usage
    merged = dict(current)
    for key, value in values.items():
        if value is None:
            continue
        existing = merged.get(key, 0)
        merged[key] = existing + value if isinstance(existing, int | float) else value
    return merged
