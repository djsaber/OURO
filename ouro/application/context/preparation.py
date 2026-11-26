from __future__ import annotations

from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Protocol, cast

from ouro.application.context.media import MediaContextPreparation, ModelMessagePreparer
from ouro.application.context.prompts import PromptContext, PromptContextResolver
from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
from ouro.application.inputs.files import PreparedFileInputs
from ouro.application.inputs.skills import compacted_skill_messages
from ouro.application.usage import UsageService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.compaction import (
    CompactionRecord,
    project_compacted_messages,
)
from ouro.core.context import (
    ContextBuilder,
    ContextView,
    project_recovery_messages,
)
from ouro.core.context_providers import (
    ContextObservation,
    ContextProviderCall,
    RequestProviders,
)
from ouro.core.context_selection import ContextSelection
from ouro.core.context_snapshots import (
    ModelRequestHeader,
    RequestManifestRecord,
)
from ouro.core.errors import (
    ConfigError,
    ContextBlockedError,
    PersistenceError,
)
from ouro.core.events import EventRecord
from ouro.core.message_frames import project_tool_exchanges
from ouro.core.messages import MessageRecord, ModelMessage, TextContent
from ouro.core.observers import ContextPreparedObservation, FrozenJson, freeze_json
from ouro.core.request_providers import OrderedProviderRegistry, ProviderEvaluation
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.session_ports import SessionStore
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.system_prompts import SystemPromptRegistry
from ouro.core.tools import ToolDefinition
from ouro.prompts import AFK_CONTEXT_PROMPT


class ContextMessageAppender(Protocol):
    """Commit a message batch and return notifications at its canonical position."""

    def __call__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        events: tuple[EventRecord, ...],
    ) -> tuple[EventRecord, ...]: ...


@dataclass(frozen=True)
class PreparedContext:
    """One loaded selection and its request projection, reused within this preparation."""

    session: SessionRecord
    active: CompactionRecord | None
    messages: list[MessageRecord]
    view: ContextView
    prompt_context: PromptContext


def _active_scope[R: OrderedProviderRegistry](registry: R, controllers: Container[str]) -> R | None:
    if not registry.snapshot():
        return None
    selected = registry.scoped(controllers)
    return selected if selected.snapshot() else None


class ContextPreparationService:
    """Build and record model context without selecting or executing a maintenance policy."""

    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        providers_for_run: Callable[[str], tuple[RequestProviders, Container[str]]],
        events: RuntimeEventRecorder,
        context_builder: ContextBuilder,
        system_prompt: str,
        append_context_messages: ContextMessageAppender,
        prompt_context_resolver: PromptContextResolver | None = None,
        prepare_model_messages: ModelMessagePreparer | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.providers_for_run = providers_for_run
        self.events = events
        self.context_builder = context_builder
        self.system_prompt = system_prompt
        self.prompt_context_resolver = prompt_context_resolver
        self.append_context_messages = append_context_messages
        self.media_context = MediaContextPreparation(prepare_model_messages)
        self.usage_service = usage_service

    def load_history(
        self, session_id: str
    ) -> tuple[SessionRecord, CompactionRecord | None, list[MessageRecord]]:
        """Materialize one selected suffix while its selection and head are stable."""
        with self.store.session_read(session_id):
            session = self.store.load_session_record(session_id)
            active = self.store.load_active_compaction(session_id)
            remaining = session.message_count - (active.covered_message_count if active else 0)
            if remaining < 0:
                raise PersistenceError("selected summary extends beyond canonical history")
            messages = (
                list(
                    reversed(
                        self.store.load_messages_tail(
                            session_id,
                            limit=remaining,
                            include_media=self.media_context.prepare_messages is None,
                        )
                    )
                )
                if remaining
                else []
            )
            if len(messages) != remaining:
                raise PersistenceError(
                    "selected history is shorter than the canonical message count"
                )
            return session, active, messages

    async def build(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        tools: list[ToolDefinition],
        model_round: int,
        adapter: RuntimeAdapter | None = None,
        skill_instructions: str = "",
        prompt: str = "",
    ) -> PreparedContext:
        providers, controllers = self.providers_for_run(run.run_id)
        system_prompts = _active_scope(providers.system_prompts, controllers)
        active_context_providers = _active_scope(providers.observations, controllers)
        session, active, messages = await run_blocking(self.load_history, session.session_id)
        prompt_context = await self._resolve_prompt_context(session, run, system_prompts)
        active_adapter = adapter or self.adapter
        observations: list[ContextObservation] = []
        if active_context_providers is not None:
            evaluation = await active_context_providers.evaluate(
                ContextProviderCall(
                    harness=await run_blocking(
                        capture_harness,
                        self.store,
                        session.session_id,
                        run=run,
                        usage_service=self.usage_service,
                    ),
                    session_id=session.session_id,
                    run_id=run.run_id,
                    message_count=session.message_count,
                    model_round=model_round,
                    workspace=session.workspace,
                )
            )
            await self._check_provider_failures(run, evaluation, "context_provider_failed")
            observations = evaluation.fragments
        appended, events = await run_blocking(
            self._commit_context_observations,
            observations,
            session=session,
            run=run,
            turn_id=turn_id,
            model_round=model_round,
            driver_prompt=prompt,
        )
        await self.events.publish_committed(events)
        if appended and appended[0].sequence != session.message_count + 1:
            session, active, messages = await run_blocking(self.load_history, session.session_id)
        else:
            messages.extend(appended)
        view = await run_blocking(
            self.project,
            session=session,
            messages=messages,
            active=active,
            tools=tools,
            adapter=active_adapter,
            skill_instructions=skill_instructions,
            prompt_context=prompt_context,
        )

        return PreparedContext(
            session=session,
            active=active,
            messages=messages,
            view=view,
            prompt_context=prompt_context,
        )

    def record_request_settlement(
        self,
        *,
        session_id: str,
        context_id: str,
        provider_done: bool,
        usage_entry_id: str | None,
    ) -> None:
        self.store.settle_request_header(
            session_id,
            context_id,
            settled_at=utc_now(),
            provider_done=provider_done,
            usage_entry_id=usage_entry_id,
        )

    def _commit_context_observations(
        self,
        provider_observations: Sequence[ContextObservation],
        *,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        model_round: int,
        driver_prompt: str,
    ) -> tuple[list[MessageRecord], tuple[EventRecord, ...]]:
        observations = list(provider_observations)
        if driver_prompt:
            observations.append(
                ContextObservation(
                    plugin_id="ouro.runtime",
                    provider_id="driver",
                    fragment_index=0,
                    text=driver_prompt,
                    semantic_tag="driver-instruction",
                    revision=f"{run.run_id}:{model_round}",
                )
            )
        unattended = session.interaction_mode == "unattended"
        mode = ContextObservation(
            plugin_id="ouro.runtime",
            provider_id="interaction-mode",
            fragment_index=0,
            text=AFK_CONTEXT_PROMPT
            if unattended
            else "Interactive mode is active. The user is available.",
            semantic_tag="unattended-mode-reminder" if unattended else "interactive-mode",
        )
        observations.append(mode)
        pending = []
        for fragment in observations:
            delivery_key = f"{fragment.plugin_id}:{fragment.provider_id}:{fragment.fragment_index}"
            previous = self.store.find_context_provider_message(session.session_id, delivery_key)
            if previous is None and fragment is mode and not unattended:
                continue
            if (
                previous is not None
                and previous.role == fragment.role
                and previous.semantic_tag == fragment.semantic_tag
                and previous.metadata.get("fragment_revision") == fragment.revision
                and previous.text_content == fragment.text
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
            if fragment.revision is not None:
                metadata["fragment_revision"] = fragment.revision
            if fragment.generation_id is not None:
                metadata["generation_id"] = fragment.generation_id
            pending.append(
                MessageRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    turn_id=turn_id,
                    role=fragment.role,
                    content=[TextContent(text=fragment.text)],
                    semantic_tag=fragment.semantic_tag,
                    metadata=metadata,
                )
            )
        if not pending:
            return pending, ()
        try:
            events = self.append_context_messages(
                session=session,
                run=run,
                messages=pending,
                events=tuple(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        turn_id=turn_id,
                        event_type="context_message_added",
                        payload={"message_id": message.message_id},
                    )
                    for message in pending
                ),
            )
        except PersistenceError as exc:
            raise ContextBlockedError(
                "context provider message could not be persisted before model execution"
            ) from exc
        return pending, events

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
                message_count=len(view.provider_context.messages),
                tool_count=len(view.provider_context.tools),
                model_round=model_round,
                context_metadata=metadata,
            )
        )

    async def finalize(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        model_round: int,
        adapter: RuntimeAdapter,
        view: ContextView,
    ) -> ContextView:
        if view.metrics.over_budget:
            raise ContextBlockedError("context exceeds the model input budget")
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
        )
        await self._observe_built_context(
            session=session,
            run=run,
            view=prepared,
            model_round=model_round,
        )
        return prepared

    def _save_prepared_context(
        self,
        *,
        context: ContextView,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        model_round: int,
        adapter: RuntimeAdapter,
    ) -> None:
        """Publish the skeleton produced alongside the Adapter's actual input."""

        if context.context_id is None:
            raise ValueError("prepared model context is missing its identity")
        if context.manifest is None:
            raise ValueError("prepared model context is missing its manifest")
        with self.store.session_mutation(session.session_id):
            current = self.store.load_session_record(session.session_id)
            if run.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            if current.context_selection != session.context_selection:
                raise SessionCommitConflict("context selection changed during preparation")
            latest = self.store.load_run(session.session_id, run.run_id)
            if current.active_run_id != run.run_id or latest.abort_requested:
                raise SessionCommitConflict("Run no longer owns request preparation")
            self.store.commit_session(
                SessionCommit(
                    before=current,
                    after=current.model_copy(update={"context_selection": context.selection}),
                    runs=(latest.model_copy(update={"current_request_id": context.context_id}),),
                    request_header=ModelRequestHeader(
                        context_id=context.context_id,
                        session_id=session.session_id,
                        run_id=run.run_id,
                        turn_id=turn_id,
                        model_round=model_round,
                        provider_id=adapter.model.provider_id,
                        endpoint_id=adapter.model.endpoint_id,
                        model_id=adapter.model.model_id,
                        metrics=context.metrics,
                        context_metadata=context.context_metadata,
                    ),
                    request_manifest=RequestManifestRecord(
                        context_id=context.context_id,
                        supports_image_input=adapter.model.supports_image_input,
                        supports_audio_input=adapter.model.supports_audio_input,
                        supports_video_input=adapter.model.supports_video_input,
                        manifest=context.manifest,
                    ),
                )
            )

    async def _check_provider_failures[T](
        self, run: RunRecord, evaluation: ProviderEvaluation[T], event_type: str
    ) -> None:
        await self.events.record_provider_failures(run, evaluation.failures, event_type)
        if evaluation.blocked:
            failure = evaluation.failures[-1]
            raise ContextBlockedError(
                f"required provider failed: {failure.plugin_id}:{failure.provider_id}: "
                f"{failure.error_type}"
            )

    async def load_prompt_history(
        self, session: SessionRecord, run: RunRecord
    ) -> tuple[SessionRecord, CompactionRecord | None, list[MessageRecord], PromptContext]:
        """Select manual compaction's system providers before the history read yields."""
        providers, controllers = self.providers_for_run(run.run_id)
        system_prompts = _active_scope(providers.system_prompts, controllers)
        session, active, messages = await run_blocking(self.load_history, session.session_id)
        prompt_context = await self._resolve_prompt_context(session, run, system_prompts)
        return session, active, messages, prompt_context

    def local_prompt_context(self) -> PromptContext:
        if self.prompt_context_resolver is not None:
            return self.prompt_context_resolver()
        return PromptContext(system_prompt=self.system_prompt)

    async def _resolve_prompt_context(
        self, session: SessionRecord, run: RunRecord, registry: SystemPromptRegistry | None
    ) -> PromptContext:
        try:
            personal = await run_blocking(self.local_prompt_context)
        except ConfigError as exc:
            message = f"Cannot prepare system instructions: {exc}"
            await self.events.emit(
                run,
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
        if registry is None:
            return personal
        evaluation = await registry.evaluate(session.workspace)
        await self._check_provider_failures(run, evaluation, "system_prompt_provider_failed")
        return replace(personal, fragments=(*personal.fragments, *evaluation.fragments))

    def project(
        self,
        *,
        session: SessionRecord,
        messages: list[MessageRecord],
        active: CompactionRecord | None,
        tools: list[ToolDefinition],
        adapter: RuntimeAdapter,
        skill_instructions: str,
        prompt_context: PromptContext,
    ) -> ContextView:
        complete_messages, excluded = project_tool_exchanges(
            project_compacted_messages(messages, active)
        )
        fixed_messages = (
            compacted_skill_messages(
                [
                    self.store.load_message(session.session_id, identity)
                    for identity in active.retained_skill_message_ids
                ]
            )
            if active is not None
            else []
        )
        complete_messages = [
            message.model_copy(update={"provider_continuation": None})
            if (artifact := message.provider_continuation) is not None
            and (
                artifact.provider_id != adapter.model.provider_id
                or artifact.model_id != adapter.model.model_id
            )
            else message
            for message in complete_messages
        ]
        source_messages = project_recovery_messages(complete_messages)
        with self._prepared_files(session, source_messages, adapter) as (files, media_state):
            effective_messages = files.request_messages(media_state.elided_ids(source_messages))
        view = self.build_view(
            messages=effective_messages,
            source_messages=messages,
            tools=tools,
            fixed_messages=fixed_messages,
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
                "covered_message_count": active.covered_message_count if active else 0,
                "history_diagnostics": (
                    [{"code": "incomplete_tool_exchange", "message_ids": list(excluded)}]
                    if excluded
                    else []
                ),
            }
        )
        return view.model_copy(
            update={
                "context_metadata": metadata,
                "selection": media_state.model_copy(
                    update={"compaction_id": active.compaction_id if active else None}
                ),
            }
        )

    def build_view(
        self,
        *,
        messages: Sequence[MessageRecord | ModelMessage],
        source_messages: Sequence[MessageRecord] = (),
        tools: list[ToolDefinition],
        fixed_messages: Sequence[ModelMessage] = (),
        adapter: RuntimeAdapter,
        prompt_context: PromptContext,
        skill_instructions: str = "",
    ) -> ContextView:
        """Build history or new model messages, referencing only explicit canonical sources."""
        return self.context_builder.build(
            messages=messages,
            source_messages=source_messages,
            tools=tools,
            system_prompt=prompt_context.system_prompt,
            system_prompt_fragments=prompt_context.fragments,
            fixed_messages=fixed_messages,
            additional_skill_instructions=skill_instructions,
            budget=adapter.model.context_budget,
            supports_inline_system_messages=adapter.model.supports_inline_system_messages,
        )

    def materialize_history(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        adapter: RuntimeAdapter,
    ) -> list[MessageRecord]:
        with self._prepared_files(session, messages, adapter) as (files, selection):
            return files.materialize(selection.elided_ids(messages))

    @contextmanager
    def _prepared_files(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        adapter: RuntimeAdapter,
    ) -> Iterator[tuple[PreparedFileInputs, ContextSelection]]:
        with self.store.session_read(session.session_id):
            yield self.media_context.prepare(
                messages,
                session=session,
                input_modalities=adapter.model.input_modalities,
                input_budget_tokens=adapter.model.context_budget.input_tokens,
            )
