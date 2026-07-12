from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ouro.application.events import RuntimeEventRecorder
from ouro.config.compaction import CompactionConfig, CompactionModelConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionRecord,
    CompactionRequest,
    CompactionStrategy,
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
    project_recovery_messages,
)
from ouro.core.errors import CompactionError, ContextBlockedError, ContextOverflowError
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.messages import MessageRecord
from ouro.core.provider import ProviderStreamEvent, Usage
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolDefinition
from ouro.prompts import SUMMARIZATION_PROMPT
from ouro.sessions import SessionStore

_SUMMARY_PATTERN = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


@dataclass(frozen=True)
class CompactionExecution:
    record: CompactionRecord | None
    sequence: int
    skipped: bool = False


@dataclass(frozen=True)
class _CompactionAdapterRoute:
    adapter: RuntimeAdapter
    route: str


class LLMSummaryCompactionStrategy:
    """Generate an incremental summary without entering the normal agent loop."""

    strategy_id = "builtin.llm_summary"
    version = "1"
    prompt_version = "2026-07-11"

    def __init__(
        self,
        adapter: RuntimeAdapter,
        prompt: str = SUMMARIZATION_PROMPT,
        *,
        configured_adapter: RuntimeAdapter | None = None,
        configured_adapter_error: str | None = None,
        model_config: CompactionModelConfig | None = None,
        runtime_adapter_resolver: Callable[[str], RuntimeAdapter] | None = None,
    ) -> None:
        self.runtime_adapter = adapter
        self.runtime_adapter_resolver = runtime_adapter_resolver
        self.configured_adapter = configured_adapter
        self.configured_adapter_error = configured_adapter_error
        self.model_config = model_config or CompactionModelConfig()
        self.prompt = prompt
        self.prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> CompactionArtifact:
        failures: list[str] = []
        if self.configured_adapter_error:
            failures.append(self.configured_adapter_error)

        routes = self._adapter_routes(request.session_id)
        for route in routes:
            try:
                adapter = _apply_compaction_model_config(route.adapter, self.model_config)
                artifact = await self._compact_with_adapter(
                    request,
                    adapter=adapter,
                    signal=signal,
                )
            except Exception as exc:  # noqa: BLE001 - model-route fallback boundary.
                if signal is not None and signal.is_aborted():
                    raise CompactionError("summary compaction was aborted") from exc
                failures.append(f"{route.route}: {type(exc).__name__}: {exc}")
                continue
            metadata = dict(artifact.metadata)
            metadata["model_route"] = route.route
            if failures:
                metadata["model_fallback_reason"] = "; ".join(failures)
            return artifact.model_copy(update={"metadata": metadata})

        raise CompactionError("all compaction model routes failed: " + "; ".join(failures))

    def _adapter_routes(self, session_id: str) -> list[_CompactionAdapterRoute]:
        routes: list[_CompactionAdapterRoute] = []
        if self.configured_adapter is not None:
            routes.append(
                _CompactionAdapterRoute(
                    adapter=self.configured_adapter,
                    route="configured",
                )
            )
        runtime_route = (
            "runtime_fallback" if self.model_config.has_dedicated_endpoint else "runtime"
        )
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
        signal: AbortSignal | None,
    ) -> CompactionArtifact:
        rolling_summary = request.previous_summary
        total_usage: dict[str, int | float] = {}
        remaining_frames = compaction_frames(request.messages)
        if not remaining_frames:
            raise CompactionError("summary compaction received no canonical messages")

        chunk_count = 0
        while remaining_frames:
            summary_output_tokens = min(
                request.summary_output_tokens,
                adapter.model.max_output_tokens,
            )
            chunk, consumed_turns = self._next_chunk(
                remaining_frames,
                adapter=adapter,
                previous_summary=rolling_summary,
                summary_output_tokens=summary_output_tokens,
            )
            rolling_summary, usage = await self._summarize_chunk(
                adapter=adapter,
                session_id=request.session_id,
                previous_summary=rolling_summary,
                messages=chunk,
                summary_output_tokens=summary_output_tokens,
                signal=signal,
            )
            total_usage = _merge_usage(total_usage, usage)
            chunk_count += 1
            remaining_frames = remaining_frames[consumed_turns:]

        return CompactionArtifact(
            summary=rolling_summary or "",
            usage=total_usage,
            metadata={
                "prompt_version": self.prompt_version,
                "prompt_hash": self.prompt_hash,
                "chunk_count": chunk_count,
                "model_provider": adapter.model.provider_id,
                "model_endpoint_id": adapter.model.endpoint_id,
                "model_id": adapter.model.model_id,
                "model_temperature": adapter.model.temperature,
                "model_max_output_tokens": adapter.model.max_output_tokens,
                "model_thinking_level": adapter.model.default_thinking_level,
            },
        )

    def _next_chunk(
        self,
        turns: Sequence[Sequence[MessageRecord]],
        *,
        adapter: RuntimeAdapter,
        previous_summary: str | None,
        summary_output_tokens: int,
    ) -> tuple[list[MessageRecord], int]:
        """Choose one provider-safe chunk using the latest rolling summary size."""

        capacity = adapter.model.context_window - summary_output_tokens
        if self._summary_input_tokens(previous_summary, []) >= capacity:
            raise ContextOverflowError(
                "summary prompt and previous summary exceed model input budget"
            )

        current: list[MessageRecord] = []
        consumed_turns = 0
        for turn in turns:
            candidate = [*current, *turn]
            if self._summary_input_tokens(previous_summary, candidate) > capacity:
                if current:
                    break
                raise ContextOverflowError(
                    "a single context frame exceeds the summary input budget"
                )
            current = candidate
            consumed_turns += 1
        return current, consumed_turns

    def _summary_input_tokens(
        self,
        previous_summary: str | None,
        messages: Sequence[MessageRecord],
    ) -> int:
        payload = _summary_payload(previous_summary, messages)
        return estimate_text_tokens(self.prompt) + estimate_text_tokens(payload) + 8

    async def _summarize_chunk(
        self,
        *,
        adapter: RuntimeAdapter,
        session_id: str,
        previous_summary: str | None,
        messages: Sequence[MessageRecord],
        summary_output_tokens: int,
        signal: AbortSignal | None,
    ) -> tuple[str, dict[str, int | float]]:
        summary_input = MessageRecord.text(
            session_id=session_id,
            role="user",
            text=_summary_payload(previous_summary, messages),
        ).model_copy(update={"synthetic": True, "metadata": {"kind": "compaction_input"}})
        context = ContextView(
            system_prompt=self.prompt,
            model_messages=[summary_input],
            tool_definitions=[],
        )
        text_parts: list[str] = []
        usage: dict[str, int | float] = {}
        saw_done = False
        async for event in adapter.stream(
            context,
            signal=signal,
            max_output_tokens=summary_output_tokens,
        ):
            text_parts, usage, saw_done = _consume_summary_event(
                event,
                text_parts=text_parts,
                usage=usage,
                saw_done=saw_done,
            )
        if signal is not None and signal.is_aborted():
            raise CompactionError("summary compaction was aborted")
        if not saw_done:
            raise CompactionError("summary provider stream ended without completion")
        raw_summary = "".join(text_parts)
        match = _SUMMARY_PATTERN.search(raw_summary)
        if match is None or not match.group(1).strip():
            raise CompactionError("summary response did not contain a non-empty <summary> block")
        return match.group(1).strip(), usage


class ContextPreparationService:
    """Prepare provider context and coordinate durable summary compaction."""

    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        hooks: HookRegistry,
        events: RuntimeEventRecorder,
        context_builder: ContextBuilder,
        strategies: CompactionStrategyRegistry,
        config: CompactionConfig,
        system_prompt: str,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.hooks = hooks
        self.events = events
        self.context_builder = context_builder
        self.strategies = strategies
        self.config = config
        self.system_prompt = system_prompt

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
        abort_signal: AbortSignal,
        adapter: RuntimeAdapter | None = None,
    ) -> tuple[ContextView, int]:
        active_adapter = adapter or self.adapter
        hook_result = await self.hooks.dispatch(
            HookContext(
                hook_name="after_context_build",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"message_count": len(messages), "model_round": model_round},
            )
        )
        sequence = await self.events.record_hook_failures(
            emitted,
            sequence,
            hook_result.failures,
        )
        added_context = [text for result in hook_result.results for text in result.added_context]
        budget = self._input_budget(active_adapter)
        active = self.store.load_session(session.session_id, recover=False).active_compaction
        effective_messages = project_recovery_messages(project_compacted_messages(messages, active))
        view = self._build_view(
            messages=effective_messages,
            tools=tools,
            added_context=added_context,
            budget=budget,
        )

        trigger_tokens = int(budget * self.config.policy.trigger_ratio)
        estimated = int(view.context_metadata["estimated_input_tokens"])
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
                )
            except CompactionError as exc:
                sequence = len(self.store.load_session(session.session_id, recover=False).events)
                if view.context_metadata["over_budget"]:
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
                return view.model_copy(update={"context_metadata": metadata}), sequence
            sequence = execution.sequence
            if execution.record is None and view.context_metadata["over_budget"]:
                raise ContextBlockedError(
                    "context is over budget and no complete older context frame is eligible "
                    "for compaction"
                )
            active = execution.record or active
            effective_messages = project_recovery_messages(
                project_compacted_messages(messages, active)
            )
            view = self._build_view(
                messages=effective_messages,
                tools=tools,
                added_context=added_context,
                budget=budget,
            )

        if view.context_metadata["over_budget"]:
            raise ContextBlockedError(
                "context remains over the model input budget after summary compaction"
            )
        metadata = dict(view.context_metadata)
        metadata.update(
            {
                "compaction_id": active.compaction_id if active else None,
                "compaction_strategy": active.strategy_id if active else None,
                "covered_message_count": len(active.covered_message_ids) if active else 0,
            }
        )
        return view.model_copy(update={"context_metadata": metadata}), sequence

    async def compact(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        added_context: list[str] | None,
        trigger: CompactionTrigger,
        force: bool,
        abort_signal: AbortSignal,
        estimated_tokens_before: int | None = None,
        adapter: RuntimeAdapter | None = None,
    ) -> CompactionExecution:
        active_adapter = adapter or self.adapter
        budget = self._input_budget(active_adapter)
        snapshot = self.store.load_session(session.session_id, recover=False)
        active = snapshot.active_compaction
        projected = project_compacted_messages(messages, active)
        initial_view = self._build_view(
            messages=projected,
            tools=tools,
            added_context=added_context or [],
            budget=budget,
        )
        before_tokens = (
            estimated_tokens_before
            if estimated_tokens_before is not None
            else int(initial_view.context_metadata["estimated_input_tokens"])
        )
        target_tokens = int(budget * self.config.policy.target_ratio)
        fixed_tokens = int(initial_view.context_metadata["estimated_fixed_tokens"])
        summary_output_tokens = self.config.policy.summary_output_tokens
        tail_budget = max(
            1,
            target_tokens - fixed_tokens - summary_output_tokens,
        )
        plan = plan_compaction(
            messages,
            active,
            tail_token_budget=tail_budget,
            force=force,
            token_estimator=self.context_builder.token_estimator,
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
            session_id=session.session_id,
            strategy_id=self.config.strategy_id,
            trigger=trigger,
            previous_summary=active.summary if active else None,
            messages=plan.messages_to_compact,
            input_token_budget=budget,
            summary_output_tokens=summary_output_tokens,
        )
        try:
            strategy, artifact = await self._execute_strategy(request, signal=abort_signal)
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
                source_digest=compaction_source_digest(source_prefix),
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
            projected_after = project_compacted_messages(messages, record)
            final_view = self._build_view(
                messages=projected_after,
                tools=tools,
                added_context=added_context or [],
                budget=budget,
            )
            after_tokens = int(final_view.context_metadata["estimated_input_tokens"])
            if final_view.context_metadata["over_budget"]:
                raise ContextOverflowError(
                    "generated summary and protected recent turn exceed the model input budget"
                )
            record = record.model_copy(update={"estimated_tokens_after": after_tokens})
            self.store.append_compaction(record)
            self.store.activate_compaction(session.session_id, record.compaction_id)
        except Exception as exc:
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
    ) -> tuple[CompactionStrategy, CompactionArtifact]:
        strategy_ids = [self.config.strategy_id]
        if self.config.fallback_strategy_id:
            strategy_ids.append(self.config.fallback_strategy_id)
        failures: list[Exception] = []
        for strategy_id in strategy_ids:
            try:
                strategy = self.strategies.get(strategy_id)
                active_request = request.model_copy(update={"strategy_id": strategy_id})
                artifact = await strategy.compact(active_request, signal=signal)
                return strategy, artifact
            except Exception as exc:  # noqa: BLE001 - strategy isolation boundary.
                failures.append(exc)
        if len(failures) == 1:
            raise CompactionError(str(failures[0])) from failures[0]
        raise CompactionError(
            "all configured compaction strategies failed: "
            + "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
        )

    def _build_view(
        self,
        *,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        added_context: list[str],
        budget: int,
    ) -> ContextView:
        return self.context_builder.build(
            messages=messages,
            tools=tools,
            system_prompt=self.system_prompt,
            added_context=added_context,
            input_token_budget=budget,
        )

    def _input_budget(self, adapter: RuntimeAdapter) -> int:
        return max(1, adapter.model.context_window - adapter.model.max_output_tokens)


def _apply_compaction_model_config(
    adapter: RuntimeAdapter,
    config: CompactionModelConfig,
) -> RuntimeAdapter:
    """Create a request-local model view without mutating the session runtime adapter."""

    updates: dict[str, object] = {}
    if config.temperature is not None:
        updates["temperature"] = config.temperature
    if config.max_output_tokens is not None:
        if config.max_output_tokens > adapter.model.context_window:
            raise ContextOverflowError(
                "compaction max_output_tokens exceed the selected endpoint context window"
            )
        updates["max_output_tokens"] = config.max_output_tokens

    thinking_level = config.thinking_level
    thinking_requested = config.thinking_enabled is True or thinking_level not in {None, "off"}
    if thinking_requested:
        if not adapter.model.supports_reasoning:
            raise CompactionError(
                "compaction thinking is enabled but the selected endpoint does not support it"
            )
        if (
            thinking_level is not None
            and adapter.model.supported_thinking_levels
            and thinking_level not in adapter.model.supported_thinking_levels
        ):
            raise CompactionError(
                f"compaction thinking level {thinking_level!r} is not supported by "
                "the selected endpoint"
            )
        if thinking_level is not None:
            updates["default_thinking_level"] = thinking_level
        elif adapter.model.default_thinking_level == "off":
            updates["default_thinking_level"] = None
    elif config.thinking_enabled is False or thinking_level == "off":
        updates["default_thinking_level"] = "off"

    return RuntimeAdapter(
        provider=adapter.provider,
        model=adapter.model.model_copy(update=updates),
    )


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
    elif event.type == "tool_call":
        raise CompactionError("summary provider attempted to call a tool")
    elif event.type == "error":
        raise CompactionError(event.error or "summary provider returned an error")
    elif event.type == "done":
        if event.tool_calls:
            raise CompactionError("summary provider attempted to call a tool")
        saw_done = True
    return text_parts, usage, saw_done


def _serialize_messages(messages: Sequence[MessageRecord]) -> str:
    parts: list[str] = []
    for message in messages:
        label = {
            "user": "User",
            "assistant": "Assistant",
            "tool_result": "Tool result",
            "system_synthetic": "Synthetic context",
        }[message.role]
        if message.metadata.get("incomplete"):
            label += " (interrupted/incomplete)"
        parts.append(f"[{label}] {message.text_content}")
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
