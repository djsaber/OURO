from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from ouro.application.context.media import MediaContextPreparation, ModelMessagePreparer
from ouro.application.context.prompts import PromptContext, PromptContextResolver
from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
from ouro.application.inputs.skills import compacted_skill_context
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
    AttributedContextFragment,
    ContextFragment,
    ContextProviderCall,
    ContextProviderRegistry,
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
from ouro.core.message_frames import project_tool_exchanges
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import (
    SemanticTag,
)
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.observers import ContextPreparedObservation, FrozenJson, freeze_json
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
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
        metadata: dict[str, object],
        semantic_tag: SemanticTag | None = None,
    ) -> MessageRecord: ...


@dataclass(frozen=True)
class PreparedContext:
    """One loaded selection and its request projection, reused within this preparation."""

    session: SessionRecord
    active: CompactionRecord | None
    messages: list[MessageRecord]
    view: ContextView
    added_context: Sequence[str | ContextFragment]
    prompt_context: PromptContext
    sequence: int


class ContextPreparationService:
    """Build and record model context without selecting or executing a maintenance policy."""

    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        context_providers: ContextProviderRegistry,
        events: RuntimeEventRecorder,
        context_builder: ContextBuilder,
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
        self.system_prompt = system_prompt
        self.prompt_context_resolver = prompt_context_resolver
        self.append_context_message = append_context_message
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
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        tools: list[ToolDefinition],
        model_round: int,
        adapter: RuntimeAdapter | None = None,
        context_providers: ContextProviderRegistry | None = None,
        skill_instructions: str = "",
        prompt: str = "",
    ) -> PreparedContext:
        session, active, messages = await run_blocking(self.load_history, session.session_id)
        prompt_context = await self.resolve_prompt_context(sequence, session, run)
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
                message_count=session.message_count,
                model_round=model_round,
                workspace=session.workspace,
            )
        )
        sequence = await self.events.record_context_provider_failures(
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
        view = await run_blocking(
            self.project,
            session=session,
            messages=messages,
            active=active,
            tools=tools,
            added_context=added_context,
            adapter=active_adapter,
            skill_instructions=skill_instructions,
            prompt_context=prompt_context,
        )

        return PreparedContext(
            session=session,
            active=active,
            messages=messages,
            view=view,
            added_context=added_context,
            prompt_context=prompt_context,
            sequence=sequence,
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
            previous = self.store.find_context_provider_message(
                session.session_id, run.run_id, delivery_key
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
                message_count=len(view.provider_context.messages),
                tool_count=len(view.provider_context.tools),
                model_round=model_round,
                context_metadata=metadata,
            )
        )

    async def finalize(
        self,
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
        model_round: int,
        adapter: RuntimeAdapter,
        view: ContextView,
    ) -> tuple[ContextView, int]:
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

    async def resolve_prompt_context(
        self,
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

    def project(
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
    ) -> ContextView:
        complete_messages, excluded = project_tool_exchanges(
            project_compacted_messages(messages, active)
        )
        # The durable compaction boundary fixes this prefix, including after recovery.
        added_context = [
            *added_context,
            *compacted_skill_context(
                [
                    self.store.load_message(session.session_id, identity)
                    for identity in active.retained_skill_message_ids
                ]
                if active is not None
                else []
            ),
        ]
        # Artifact source identity bounds the budget candidate. Protocol and
        # account compatibility belong to the codec. A retained response does
        # not depend on its Run's execution authority, including on a branch.
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
        effective_messages, media_state = self.materialize_messages(
            source_messages,
            adapter,
            session=session,
            history=source_messages,
        )
        view = self.build_view(
            messages=effective_messages,
            source_messages=messages,
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
        added_context: Sequence[str | ContextFragment],
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
            added_context=added_context,
            additional_skill_instructions=skill_instructions,
            budget=adapter.model.context_budget,
        )

    def materialize_messages(
        self,
        messages: list[MessageRecord],
        adapter: RuntimeAdapter,
        *,
        session: SessionRecord,
        history: list[MessageRecord],
    ) -> tuple[list[MessageRecord], ContextSelection]:
        with self.store.session_read(session.session_id):
            return self.media_context.project(
                messages,
                session=session,
                history=history,
                input_modalities=adapter.model.input_modalities,
                input_budget_tokens=adapter.model.context_budget.input_tokens,
            )
