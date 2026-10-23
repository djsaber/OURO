from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Protocol, cast

from ouro.application.context.media import MediaContextPreparation, ModelMessagePreparer
from ouro.application.context.prompts import PromptContext, PromptContextResolver
from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
from ouro.application.inputs.skills import compacted_skill_context
from ouro.application.usage import UsageService
from ouro.config.compaction import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id, utc_now
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
    compaction_source_digest,
    plan_compaction,
    project_compacted_messages,
)
from ouro.core.context import (
    ContextBuilder,
    ContextView,
    estimate_message_tokens,
    estimate_text_tokens,
    project_recovery_messages,
)
from ouro.core.context_providers import (
    AttributedContextFragment,
    ContextFragment,
    ContextProviderCall,
    ContextProviderRegistry,
)
from ouro.core.context_snapshots import (
    ContextSnapshotRecord,
    context_view_digest,
)
from ouro.core.errors import (
    CompactionError,
    ConfigError,
    ContextBlockedError,
    ContextOverflowError,
    PersistenceError,
)
from ouro.core.events import EventRecord
from ouro.core.media_context import MEDIA_CONTEXT_KEY, MediaElisionState
from ouro.core.message_frames import project_tool_exchanges
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import (
    SemanticTag,
)
from ouro.core.messages import MessageRecord
from ouro.core.observers import ContextPreparedObservation, FrozenJson, freeze_json
from ouro.core.provider import (
    Usage,
)
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolDefinition
from ouro.prompts import AFK_CONTEXT_PROMPT
from ouro.sessions import SessionStore


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
        prepare_model_messages: ModelMessagePreparer | None = None,
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
        self.media_context = MediaContextPreparation(store, prepare_model_messages)
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
        prompt: str = "",
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
        if prompt:
            added_context.append(ContextFragment(text=prompt, semantic_tag="driver-instruction"))
        if session.interaction_mode == "unattended":
            added_context.append(
                ContextFragment(
                    text=AFK_CONTEXT_PROMPT,
                    semantic_tag="unattended-mode-reminder",
                )
            )
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
            previous = next(
                (
                    message
                    for message in reversed(messages)
                    if message.metadata.get("context_provider_delivery_key") == delivery_key
                ),
                None,
            )
            # Preparation can fail after persistence without advancing the model round.
            # Deduplicate unchanged retries; append changed state after the previous value.
            if (
                previous is not None
                and previous.text_content == fragment.text
                and previous.role == fragment.role
                and previous.scaffold == fragment.scaffold
                and previous.semantic_tag == fragment.semantic_tag
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
    ) -> None:
        """Persist one fully materialized context snapshot on the I/O worker."""

        if context.context_id is None:
            raise ValueError("prepared model context is missing its identity")
        injected_message_count = len(context.model_messages) - len(source_messages)
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
                    supports_image_input=adapter.model.supports_image_input,
                    supports_audio_input=adapter.model.supports_audio_input,
                    supports_video_input=adapter.model.supports_video_input,
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
        # Lineage is validated against canonical history by _project_context.
        # Only the uncovered suffix needs model projection and frame selection.
        covered_count = len(active.covered_message_ids) if active else 0
        materialized_messages, _ = await run_blocking(
            self._materialize_messages,
            messages[covered_count:],
            active_adapter,
            session=session,
            history=messages,
        )
        before_tokens = (
            estimated_tokens_before
            if estimated_tokens_before is not None
            else initial_view.metrics.estimated_input_tokens
        )
        target_tokens = int(budget * self.config.policy.target_ratio)
        fixed_tokens = initial_view.metrics.estimated_fixed_tokens
        summary_tokens = sum(
            estimate_message_tokens(message, self.context_builder.token_estimator)
            for message in initial_view.model_messages
            if message.semantic_tag == "conversation-summary"
        )
        tail_budget = max(0, target_tokens - fixed_tokens - summary_tokens)
        plan = await run_blocking(
            plan_compaction,
            materialized_messages,
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
            agent_system_prompt=initial_view.system_prompt,
            previous_summary=active.summary if active else None,
            messages=plan.messages_to_compact,
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
            source_prefix = messages[: covered_count + len(plan.messages_to_compact)]
            record = CompactionRecord(
                compaction_id=compaction_id,
                session_id=session.session_id,
                previous_compaction_id=active.compaction_id if active else None,
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                trigger=trigger,
                summary=summary,
                covered_message_ids=[message.message_id for message in source_prefix],
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
                model_outputs.extend(
                    exc.model_outputs if isinstance(exc, CompactionStrategyError) else ()
                )
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
        # The durable compaction boundary fixes this prefix, including after recovery.
        added_context = [*added_context, *compacted_skill_context(messages, active)]
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
        effective_messages, media_state = self._materialize_messages(
            source_messages,
            adapter,
            session=session,
            history=messages,
        )
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
                MEDIA_CONTEXT_KEY: media_state.model_dump(mode="json"),
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
        return effective_messages, view.model_copy(update={"context_metadata": metadata})

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
            budget=adapter.model.context_budget,
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
        *,
        session: SessionRecord,
        history: list[MessageRecord],
    ) -> tuple[list[MessageRecord], MediaElisionState]:
        return self.media_context.project(
            messages,
            session=session,
            history=history,
            input_modalities=adapter.model.input_modalities,
            input_budget_tokens=self._input_budget(adapter),
        )

    def _input_budget(self, adapter: RuntimeAdapter) -> int:
        return adapter.model.context_budget.input_tokens
