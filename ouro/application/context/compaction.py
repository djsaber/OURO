from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic

from ouro.application.context.preparation import ContextPreparationService, PreparedContext
from ouro.application.context.prompts import PromptContext
from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
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
    plan_compaction,
)
from ouro.core.context import (
    ContextView,
    estimate_text_tokens,
)
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderRegistry,
)
from ouro.core.context_selection import ContextSelection
from ouro.core.errors import (
    CompactionError,
    ContextBlockedError,
    ContextOverflowError,
)
from ouro.core.events import EventRecord
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolDefinition
from ouro.sessions import SessionStore


@dataclass(frozen=True)
class CompactionExecution:
    record: CompactionRecord | None
    sequence: int
    view: ContextView
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


class ContextCompactionService:
    """The default request preparation policy: compact selected history, then continue."""

    def __init__(
        self,
        *,
        preparation: ContextPreparationService,
        store: SessionStore,
        events: RuntimeEventRecorder,
        strategies: CompactionStrategyRegistry,
        config: CompactionConfig,
        usage_service: UsageService | None = None,
    ) -> None:
        self.preparation = preparation
        self.store = store
        self.events = events
        self.strategies = strategies
        self.config = config
        self.usage_service = usage_service

    async def prepare(
        self,
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
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
        prepared = await self.preparation.build(
            sequence,
            session=session,
            run=run,
            tools=tools,
            model_round=model_round,
            adapter=adapter,
            context_providers=context_providers,
            skill_instructions=skill_instructions,
            prompt=prompt,
        )
        session, active, messages = prepared.session, prepared.active, prepared.messages
        view, sequence = prepared.view, prepared.sequence
        added_context, prompt_context = prepared.added_context, prepared.prompt_context
        active_adapter = adapter or self.preparation.adapter
        budget = active_adapter.model.context_budget.input_tokens
        trigger_tokens = int(budget * self.config.policy.trigger_ratio)
        estimated = view.metrics.estimated_input_tokens
        if self.config.policy.auto_enabled and estimated >= trigger_tokens:
            try:
                execution = await self.compact(
                    sequence,
                    session=session,
                    run=run,
                    tools=tools,
                    added_context=added_context,
                    trigger="auto",
                    force=False,
                    abort_signal=abort_signal,
                    adapter=active_adapter,
                    strategies=strategies,
                    skill_instructions=skill_instructions,
                    prompt_context=prompt_context,
                    prepared=prepared,
                )
            except CompactionError as exc:
                if isinstance(exc, CompactionAbortedError):
                    raise
                sequence = 0
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
                        "covered_message_count": active.covered_message_count if active else 0,
                    }
                )
                final_view = view.model_copy(update={"context_metadata": metadata})
                return await self.preparation.finalize(
                    sequence,
                    session=session,
                    run=run,
                    turn_id=turn_id,
                    model_round=model_round,
                    adapter=active_adapter,
                    view=final_view,
                )
            sequence = execution.sequence
            if execution.record is None and view.metrics.over_budget:
                raise ContextBlockedError(
                    "context is over budget and no complete older context frame is eligible "
                    "for compaction"
                )
            current = await run_blocking(self.store.load_session_record, session.session_id)
            expected_selection = (
                execution.view.selection if execution.record else session.context_selection
            )
            if (
                current.history_epoch == session.history_epoch
                and current.message_count
                == (active.covered_message_count if active else 0) + len(messages)
                and current.context_selection == expected_selection
            ):
                # The accepted summary already has its exact next request view.
                session, view = current, execution.view
            else:
                # A strategy may span a concurrent append or explicit selection change.
                session, active, messages = await run_blocking(
                    self.preparation.load_history, session.session_id
                )
                view = await run_blocking(
                    self.preparation.project,
                    session=session,
                    messages=messages,
                    active=active,
                    tools=tools,
                    added_context=added_context,
                    adapter=active_adapter,
                    skill_instructions=skill_instructions,
                    prompt_context=prompt_context,
                )

        return await self.preparation.finalize(
            sequence,
            session=session,
            run=run,
            turn_id=turn_id,
            model_round=model_round,
            adapter=active_adapter,
            view=view,
        )

    def _mark_compaction(self, session_id: str, run_id: str, compaction_id: str | None) -> None:
        with self.store.session_mutation(session_id):
            run = self.store.load_run(session_id, run_id)
            self.store.save_run(run.model_copy(update={"current_compaction_id": compaction_id}))

    async def compact(
        self,
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        tools: list[ToolDefinition],
        added_context: Sequence[str | ContextFragment] | None,
        trigger: CompactionTrigger,
        force: bool,
        abort_signal: AbortSignal,
        adapter: RuntimeAdapter | None = None,
        strategies: CompactionStrategyRegistry | None = None,
        skill_instructions: str = "",
        prompt_context: PromptContext | None = None,
        prepared: PreparedContext | None = None,
    ) -> CompactionExecution:
        if prepared is None:
            session, active, messages = await run_blocking(
                self.preparation.load_history, session.session_id
            )
        else:
            active, messages = prepared.active, prepared.messages
        # Auto compaction shares the request snapshot; a manual command resolves its own.
        if prompt_context is None:
            prompt_context = await self.preparation.resolve_prompt_context(sequence, session, run)
        active_adapter = adapter or self.preparation.adapter
        budget = active_adapter.model.context_budget.input_tokens
        initial_view = (
            prepared.view
            if prepared is not None
            else await run_blocking(
                self.preparation.project,
                session=session,
                messages=messages,
                active=active,
                tools=tools,
                added_context=added_context or [],
                adapter=active_adapter,
                skill_instructions=skill_instructions,
                prompt_context=prompt_context,
            )
        )
        # Activation/withdrawal establish validity. Project only the uncovered suffix.
        covered_count = active.covered_message_count if active else 0
        materialized_messages, _ = await run_blocking(
            self.preparation.materialize_messages,
            messages,
            active_adapter,
            session=session,
            history=messages,
        )
        before_tokens = initial_view.metrics.estimated_input_tokens
        target_tokens = int(budget * self.config.policy.target_ratio)
        fixed_tokens = initial_view.metrics.estimated_fixed_tokens
        summary_tokens = initial_view.estimated_summary_tokens
        tail_budget = max(0, target_tokens - fixed_tokens - summary_tokens)
        plan = await run_blocking(
            plan_compaction,
            materialized_messages,
            tail_token_budget=tail_budget,
            force=force,
            token_estimator=self.preparation.context_builder.token_estimator,
        )
        if not plan.messages_to_compact:
            if trigger == "auto" and before_tokens > budget:
                raise ContextOverflowError(
                    "context is over budget and no complete older turn is eligible for compaction"
                )
            sequence = await self.events.emit(
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="compaction_skipped",
                payload={"trigger": trigger, "reason": "no_eligible_closed_turn"},
            )
            return CompactionExecution(
                record=None, sequence=sequence, view=initial_view, skipped=True
            )

        compaction_id = new_id("cmp")
        compaction_started_clock = monotonic()
        await run_blocking(self._mark_compaction, session.session_id, run.run_id, compaction_id)
        sequence = await self.events.emit(
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
            agent_system_prompt=initial_view.provider_context.system_prompt,
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
                sequence,
                session=session,
                run=run,
                compaction_id=compaction_id,
                outputs=artifact.model_outputs,
            )
            summary = artifact.summary.strip()
            if not summary:
                raise CompactionError("compaction strategy returned an empty summary")
            newly_covered = len(plan.messages_to_compact)
            covered_end = covered_count + newly_covered
            skill_sources = []
            if active is not None:
                for identity in active.retained_skill_message_ids:
                    skill_sources.append(
                        await run_blocking(self.store.load_message, session.session_id, identity)
                    )
            skill_sources.extend(messages[:newly_covered])
            record = CompactionRecord(
                compaction_id=compaction_id,
                session_id=session.session_id,
                previous_compaction_id=active.compaction_id if active else None,
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                trigger=trigger,
                summary=summary,
                covered_message_count=covered_end,
                covered_end_message_id=messages[newly_covered - 1].message_id,
                retained_skill_message_ids=tuple(
                    message.message_id
                    for message in skill_sources
                    if message.run_id is not None
                    and message.run_id == messages[-1].run_id
                    and message.skill_activations
                ),
                history_boundary_message_id=messages[-1].message_id,
                model_provider=str(artifact.metadata.get("model_provider") or "") or None,
                model_endpoint_id=(str(artifact.metadata.get("model_endpoint_id") or "") or None),
                model_id=str(artifact.metadata.get("model_id") or "") or None,
                prompt_version=str(artifact.metadata.get("prompt_version") or "") or None,
                estimated_tokens_before=before_tokens,
                estimated_summary_tokens=estimate_text_tokens(summary),
                usage=artifact.usage,
                metadata=dict(artifact.metadata),
            )
            final_view = await run_blocking(
                self.preparation.project,
                session=session,
                messages=messages[newly_covered:],
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
            events = await run_blocking(
                self._commit_compaction,
                record,
                run,
                selection=final_view.selection,
                expected_selection=session.context_selection,
                request_id=artifact.model_outputs[-1].request_id
                if artifact.model_outputs
                else None,
            )
        except Exception as exc:
            if isinstance(exc, CompactionStrategyError):
                sequence = await self._record_model_outputs(
                    sequence,
                    session=session,
                    run=run,
                    compaction_id=compaction_id,
                    outputs=exc.model_outputs,
                )
            await run_blocking(self._mark_compaction, session.session_id, run.run_id, None)
            if isinstance(exc, CompactionAbortedError):
                raise
            sequence = await self.events.emit(
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

        await self.events.publish_committed(events)
        if events:
            sequence = events[-1].sequence
        return CompactionExecution(record=record, sequence=sequence, view=final_view)

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
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        compaction_id: str,
        outputs: Sequence[CompactionModelOutput],
    ) -> int:
        for output in outputs:
            sequence = await self.events.emit(
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

    def _commit_compaction(
        self,
        record: CompactionRecord,
        run: RunRecord,
        *,
        selection: ContextSelection,
        expected_selection: ContextSelection,
        request_id: str | None = None,
    ) -> tuple[EventRecord, ...]:
        with self.store.session_mutation(record.session_id):
            session = self.store.load_session_record(record.session_id)
            if run.history_epoch != session.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            if session.context_selection != expected_selection:
                raise SessionCommitConflict("context selection changed during summary generation")
            request_header = (
                self.store.load_request_header(record.session_id, request_id)
                if request_id is not None
                else None
            )
            if request_header is not None:
                if request_header.run_id != run.run_id or request_header.purpose != "compaction":
                    raise SessionCommitConflict("summary output references a different request")
                request_header = request_header.model_copy(
                    update={
                        "response_compaction_id": record.compaction_id,
                        "response_closed_at": utc_now(),
                    }
                )
            committed = self.store.commit_session(
                SessionCommit(
                    before=session,
                    runs=(
                        self.store.load_run(record.session_id, run.run_id).model_copy(
                            update={"current_compaction_id": None}
                        ),
                    ),
                    compaction=record,
                    request_header=request_header,
                    events=(
                        EventRecord(
                            session_id=record.session_id,
                            run_id=run.run_id,
                            event_type="compaction_completed",
                            payload={
                                "compaction_id": record.compaction_id,
                                "trigger": record.trigger,
                                "strategy_id": record.strategy_id,
                                "covered_message_count": record.covered_message_count,
                                "tokens_before": record.estimated_tokens_before,
                                "tokens_after": record.estimated_tokens_after,
                                "duration_seconds": record.duration_seconds,
                                "context_metrics_after": record.context_metrics_after.model_dump(
                                    mode="json"
                                )
                                if record.context_metrics_after is not None
                                else None,
                                "model_provider": record.model_provider,
                                "model_endpoint_id": record.model_endpoint_id,
                                "model_id": record.model_id,
                                "model_route": record.metadata.get("model_route"),
                                "model_fallback_reason": record.metadata.get(
                                    "model_fallback_reason"
                                ),
                            },
                        ),
                    ),
                    after=session.model_copy(update={"context_selection": selection}),
                )
            )
        return committed.events
