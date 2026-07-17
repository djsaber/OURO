from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from ouro.application.compaction import (
    ContextPreparationService,
    LLMSummaryCompactionStrategy,
)
from ouro.application.events import RuntimeEventRecorder, RuntimeEventSink
from ouro.application.models import (
    AvailableModel,
    FixedRuntimeAdapterCatalog,
    RuntimeAdapterCatalog,
    close_runtime_adapters,
)
from ouro.application.queued_inputs import QueuedInput, QueuedInputMode, QueuedInputQueue
from ouro.application.recovery import RunRecoveryService
from ouro.application.run_continuations import RunContinuationService
from ouro.application.runs import (
    ACTIVE_RUN_STATUSES,
    RunLifecycle,
    clear_pending_metadata,
    find_active_run,
    find_run,
    maybe_active_run,
)
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_recovery import SessionRecoveryService
from ouro.application.sessions import SessionService
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.application.usage import UsageService
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionCommandResult, CompactionStrategyRegistry
from ouro.core.context import ContextBuilder
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import CompactionError, ConfigError, ResourceAttachmentError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.inner_loop import InnerAgentLoop, InnerLoopPorts, TurnBoundaryInputResult
from ouro.core.messages import MessageRecord, ProviderContinuationArtifact
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import AttributedSessionInput
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunOutcome,
    RunRecord,
    RunStatus,
    SessionRecord,
)
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.tools import PermissionMode
from ouro.prompts import DEFAULT_SYSTEM_PROMPT, render_agent_skills_prompt
from ouro.sessions import RuntimeSessionSnapshot, SessionLock, SessionStore
from ouro.skills import SkillMetadata
from ouro.tools import ToolExecutor, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from ouro.application.resources import ResourceService
    from ouro.plugins.catalog import ExtensionCatalogLease
    from ouro.plugins.runtime import PluginRuntimeResource


class RuntimeService:
    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        model_catalog: RuntimeAdapterCatalog | None = None,
        tools: ToolRegistry,
        hooks: HookRegistry | None = None,
        context_builder: ContextBuilder | None = None,
        run_budget: RunBudgetPolicy | None = None,
        system_prompt: str | None = None,
        event_sink: RuntimeEventSink | None = None,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
        tool_execution_config: ToolExecutionConfig | None = None,
        compaction_config: CompactionConfig | None = None,
        compaction_strategies: CompactionStrategyRegistry | None = None,
        compaction_adapter: RuntimeAdapter | None = None,
        compaction_adapter_error: str | None = None,
        continuations: RunContinuationRegistry | None = None,
        tool_policies: ToolPolicyRegistry | None = None,
        queued_input_config: QueuedInputConfig | None = None,
        skills: tuple[SkillMetadata, ...] = (),
        resources: ResourceService | None = None,
        usage_service: UsageService | None = None,
        default_workspace: str | Path | None = None,
        environment: RuntimeEnvironment | None = None,
        owned_auxiliary_adapters: tuple[RuntimeAdapter, ...] = (),
    ) -> None:
        self.store = store
        self.environment = environment or RuntimeEnvironment()
        self.resources = resources
        self.usage = usage_service
        self.sessions = SessionService(store, resources=resources)
        self.session_recovery = SessionRecoveryService(store)
        self.queued_inputs = QueuedInputQueue(store)
        self.queued_input_config = queued_input_config or QueuedInputConfig()
        self.adapter = adapter
        self.models = model_catalog or FixedRuntimeAdapterCatalog(adapter)
        self._owned_auxiliary_adapters = owned_auxiliary_adapters
        self._closed = False
        self.tools = tools
        self.skills = tuple(skill.model_copy(deep=True) for skill in skills)
        self.default_workspace = (
            str(Path(default_workspace).expanduser().resolve())
            if default_workspace is not None
            else None
        )
        self._permission_overrides = dict(permission_overrides or {})
        self._tool_execution_config = tool_execution_config or ToolExecutionConfig()
        self.hooks = hooks or HookRegistry()
        self.continuations = continuations or RunContinuationRegistry()
        self.tool_policies = tool_policies or ToolPolicyRegistry()
        self.event_recorder = RuntimeEventRecorder(
            store=store,
            hooks=self.hooks,
            event_sink=event_sink,
        )
        self.run_lifecycle = RunLifecycle(
            store=store,
            events=self.event_recorder,
            usage_service=usage_service,
            validate_resource_refs=(
                resources.validate_references if resources is not None else None
            ),
        )
        self.run_recovery = RunRecoveryService(
            store=store,
            runs=self.run_lifecycle,
            events=self.event_recorder,
        )
        self.context_builder = context_builder or ContextBuilder()
        self.run_budget = run_budget or RunBudgetPolicy()
        self.system_prompt = DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt
        self.compaction_config = compaction_config or CompactionConfig()
        self.compaction_strategies = compaction_strategies or CompactionStrategyRegistry()
        configured_adapter_error = compaction_adapter_error
        if (
            self.compaction_config.model.has_dedicated_endpoint
            and compaction_adapter is None
            and configured_adapter_error is None
        ):
            configured_adapter_error = (
                "configured endpoint unavailable: no compaction adapter was supplied"
            )
        try:
            self.compaction_strategies.get(LLMSummaryCompactionStrategy.strategy_id)
        except KeyError:
            self.compaction_strategies.register(
                LLMSummaryCompactionStrategy(
                    adapter,
                    configured_adapter=compaction_adapter,
                    configured_adapter_error=configured_adapter_error,
                    model_config=self.compaction_config.model,
                    runtime_adapter_resolver=self._adapter_for_session_id,
                    usage_recorder=self.run_lifecycle.record_usage_for_run,
                )
            )
        self.context_preparation = ContextPreparationService(
            store=store,
            adapter=adapter,
            hooks=self.hooks,
            events=self.event_recorder,
            context_builder=self.context_builder,
            strategies=self.compaction_strategies,
            config=self.compaction_config,
            system_prompt=self.system_prompt,
            project_model_messages=(
                resources.project_model_messages if resources is not None else None
            ),
        )
        self._abort_signals: dict[str, AbortSignal] = {}
        self._extension_catalog: PluginRuntimeResource | None = None
        self._extension_snapshots: dict[str, ExtensionCatalogLease] = {}

    def attach_extension_catalog(self, catalog: PluginRuntimeResource) -> None:
        if self._extension_catalog is not None and self._extension_catalog is not catalog:
            raise RuntimeError("runtime already has an extension catalog")
        self._extension_catalog = catalog
        self.event_recorder.set_hook_registry_resolver(self._event_hooks_for_run)

    def _event_hooks_for_run(
        self,
        run_id: str | None,
    ) -> tuple[HookRegistry, Callable[[], None]]:
        if run_id is None:
            return self.hooks, lambda: None
        snapshot = self._extension_snapshots.get(run_id)
        if snapshot is None:
            return self.hooks, lambda: None
        retained = snapshot.fork()
        return retained.hooks, retained.close

    def _tools_for_run(self, run_id: str) -> ToolRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.tools if snapshot is not None else self.tools

    def _hooks_for_run(self, run_id: str) -> HookRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.hooks if snapshot is not None else self.hooks

    def _skills_for_run(self, run_id: str) -> tuple[SkillMetadata, ...]:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.skills if snapshot is not None else self.skills

    def _skill_instructions_for_run(self, run_id: str) -> str:
        return render_agent_skills_prompt(self._skills_for_run(run_id))

    def _compaction_for_run(self, run_id: str) -> CompactionStrategyRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.compaction if snapshot is not None else self.compaction_strategies

    def _continuations_for_run(self, run_id: str) -> RunContinuationRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.continuations if snapshot is not None else self.continuations

    def _tool_policies_for_run(self, run_id: str) -> ToolPolicyRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.tool_policies if snapshot is not None else self.tool_policies

    def _tool_calls_for_run(self, run_id: str) -> ToolCallCoordinator:
        tools = self._tools_for_run(run_id)
        return ToolCallCoordinator(
            executor=ToolExecutor(
                tools,
                permission_overrides=self._permission_overrides,
            ),
            registry=tools,
            hooks=self._hooks_for_run(run_id),
            events=self.event_recorder,
            runs=self.run_lifecycle,
            execution_config=self._tool_execution_config,
            default_workspace=self.default_workspace,
            is_aborted=self._is_run_aborted,
            tool_policies=self._tool_policies_for_run(run_id),
        )

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        self.event_recorder.add_event_sink(event_sink)

    async def __aenter__(self) -> RuntimeService:
        if self._closed:
            raise RuntimeError("runtime is closed")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for snapshot in self._extension_snapshots.values():
            snapshot.close()
        self._extension_snapshots.clear()
        try:
            await self.event_recorder.close()
        except Exception as exc:  # noqa: BLE001 - close every owned resource.
            errors.append(exc)
        try:
            await self.models.close()
        except Exception as exc:  # noqa: BLE001 - close every owned resource.
            errors.append(exc)
        try:
            await close_runtime_adapters(self._owned_auxiliary_adapters)
        except Exception as exc:  # noqa: BLE001 - close every owned resource.
            errors.append(exc)
        if errors:
            raise ExceptionGroup("runtime shutdown failed", errors)

    def available_models(self) -> list[AvailableModel]:
        """Return enabled model endpoints from the current system configuration."""

        return self.models.list_models()

    def current_model(self, session_id: str | None = None) -> AvailableModel:
        if session_id is None:
            return self.models.default_model()
        session = self.store.load_session_record(session_id)
        return self.models.resolve(session.current_model or self.models.default_model().key)

    def resolve_model(self, selector: str) -> AvailableModel:
        """Resolve and validate a model route before it is attached to a session."""

        model = self.models.resolve(selector)
        self.models.adapter_for(model.key)
        return model

    def create_session(
        self,
        *,
        title: str,
        initial_model: str | None = None,
        approval_mode: SessionApprovalMode = "inherit",
    ) -> SessionRecord:
        """Create an idle Session before a Host starts asynchronous input work."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        selected_model = (
            self.resolve_model(initial_model)
            if initial_model is not None
            else self.models.default_model()
        )
        created = self.sessions.create(
            title=title,
            current_model=selected_model.key,
            approval_mode=approval_mode,
            workspace=self.default_workspace,
        )
        return self.store.load_session_record(created.session.session_id)

    def switch_session_model(self, session_id: str, selector: str) -> SessionRecord:
        """Persist an idle session's model route without changing other sessions."""

        lock = self._acquire_session_run_lock(session_id, new_id("model"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            with self.store.session_mutation(session_id):
                snapshot = self.store.load_runtime_session(session_id)
                if maybe_active_run(snapshot.runs) is not None:
                    raise SessionBusyError(f"session already has an active run: {session_id}")
                normalized = selector.strip()
                model = next(
                    (item for item in self.models.list_models() if item.key == normalized),
                    None,
                )
                if model is None:
                    raise ConfigError(f"model not found: {selector}")
                self.models.adapter_for(model.key)
                session = snapshot.session.model_copy(
                    update={
                        "current_model": model.key,
                        "updated_at": utc_now(),
                        "last_activity_at": utc_now(),
                    }
                )
                self.store.save_session(session)
            self.store.refresh_catalog(session.session_id)
            return session
        finally:
            self._release_session_run_lock(lock)

    def set_session_approval_mode(
        self,
        session_id: str,
        approval_mode: SessionApprovalMode,
    ) -> SessionRecord:
        """Persist the approval policy used by future tool calls in a session."""

        self.sessions.set_approval_mode(session_id, approval_mode)
        return self.store.load_session_record(session_id)

    def _resolve_session_adapter(
        self,
        session: SessionRecord,
        *,
        snapshot: RuntimeSessionSnapshot | None = None,
    ) -> tuple[SessionRecord, RuntimeAdapter]:
        with self.store.session_mutation(session.session_id):
            latest = self.store.load_session_record(session.session_id)
            selection = self.models.resolve(latest.current_model or self.models.default_model().key)
            adapter = self.models.adapter_for(selection.key)
            if latest.current_model == selection.key:
                return latest, adapter
            session = latest.model_copy(
                update={
                    "current_model": selection.key,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                }
            )
            self.store.save_session(session)
        self.store.refresh_catalog(
            session.session_id,
            snapshot=(
                snapshot.model_copy(update={"session": session}) if snapshot is not None else None
            ),
        )
        return session, adapter

    def _adapter_for_session_id(self, session_id: str) -> RuntimeAdapter:
        session = self.store.load_session_record(session_id)
        _, adapter = self._resolve_session_adapter(session)
        return adapter

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
        initial_model: str | None = None,
        queue_mode: QueuedInputMode = "follow_up",
        approval_mode: SessionApprovalMode | None = None,
        resource_refs: list[str] | tuple[str, ...] | None = None,
        _drain_queued_inputs: bool = True,
        _queued_inputs: list[QueuedInput] | None = None,
        _input_attribution: AttributedSessionInput | None = None,
    ) -> RunResult:
        if approval_mode is not None and approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if initial_model is not None and session_id is not None:
            raise ValueError("initial_model can only be set when creating a session")
        input_resource_refs = tuple(dict.fromkeys(resource_refs or ()))
        if input_resource_refs and session_id is None:
            raise ResourceAttachmentError(
                "resource attachments require an existing session; import the resource "
                "and pass its session with session_id"
            )
        if session_id is None:
            session = self.create_session(
                title=title or _title_from_prompt(prompt),
                initial_model=initial_model,
                approval_mode=approval_mode or "inherit",
            )
            snapshot = RuntimeSessionSnapshot(session=session)
        else:
            snapshot = self._load_runtime_snapshot(
                session_id,
                recover=not self._is_session_run_locked(session_id),
            )
            session = snapshot.session
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(session, input_resource_refs)
            if active_run is not None:
                if approval_mode is not None and approval_mode != session.approval_mode:
                    raise SessionBusyError(
                        "cannot change approval mode while the session has an active run: "
                        f"{session_id}"
                    )
                queued_session, queued_events = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attribution=_input_attribution,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
        emitted: list[EventRecord] = []

        queued_origin = bool(
            _queued_inputs and any(item.source == "plugin" for item in _queued_inputs)
        )
        run = RunRecord(
            session_id=session.session_id,
            status="running",
            origin=(
                "plugin"
                if _input_attribution is not None or queued_origin
                else ("follow_up" if _queued_inputs else "user")
            ),
            parent_run_id=(
                next(
                    (
                        item.caused_by_run_id
                        for item in (_queued_inputs or [])
                        if item.caused_by_run_id is not None
                    ),
                    None,
                )
            ),
            initiator_id=(
                _input_attribution.plugin_id
                if _input_attribution is not None
                else next(
                    (
                        item.continuation_decision_id or item.plugin_id
                        for item in (_queued_inputs or [])
                        if item.source == "plugin"
                    ),
                    None,
                )
            ),
            trigger="plugin" if _input_attribution is not None or queued_origin else "user",
        )
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            snapshot = self.store.load_runtime_session(session.session_id)
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(snapshot.session, input_resource_refs)
            if active_run is not None:
                if approval_mode is not None and approval_mode != snapshot.session.approval_mode:
                    raise SessionBusyError(
                        "cannot change approval mode while the session has an active run: "
                        f"{session.session_id}"
                    )
                queued_session, queued_events = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attribution=_input_attribution,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
            raise SessionBusyError(f"session is already locked: {session.session_id}")
        abort_signal: AbortSignal | None = None
        result: RunResult | None = None
        try:
            latest_session = self.store.load_session_record(session.session_id)
            locked_snapshot = (
                snapshot
                if latest_session == snapshot.session
                else self.store.load_runtime_session(session.session_id)
            )
            locked_snapshot = await self.run_recovery.reconcile(locked_snapshot)
            session = locked_snapshot.session
            locked_active_run = maybe_active_run(locked_snapshot.runs)
            if locked_active_run is not None:
                if approval_mode is not None and approval_mode != session.approval_mode:
                    raise SessionBusyError(
                        "cannot change approval mode while the session has an active run: "
                        f"{session.session_id}"
                    )
                queued_session, queued_events = await self._queue_input(
                    snapshot=locked_snapshot,
                    active_run=locked_active_run,
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attribution=_input_attribution,
                )
                return RunResult(
                    session=queued_session,
                    run=locked_active_run,
                    events=queued_events,
                )
            if approval_mode is not None and approval_mode != session.approval_mode:
                with self.store.session_mutation(session.session_id):
                    latest_session_record = self.store.load_session_record(session.session_id)
                    now = utc_now()
                    session = latest_session_record.model_copy(
                        update={
                            "approval_mode": approval_mode,
                            "updated_at": now,
                            "last_activity_at": now,
                        }
                    )
                    self.store.save_session(session)
                locked_snapshot = locked_snapshot.model_copy(update={"session": session})
                self.store.refresh_catalog(
                    session.session_id,
                    snapshot=locked_snapshot,
                )
            session, active_adapter = self._resolve_session_adapter(
                session,
                snapshot=locked_snapshot,
            )
            existing_messages = locked_snapshot.messages
            pending_inputs = _queued_inputs or [
                QueuedInput(
                    prompt=prompt,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    source="plugin" if _input_attribution is not None else "user",
                    plugin_id=(
                        _input_attribution.plugin_id if _input_attribution is not None else None
                    ),
                    contribution_id=(
                        _input_attribution.contribution_id
                        if _input_attribution is not None
                        else None
                    ),
                    generation_id=(
                        _input_attribution.generation_id if _input_attribution is not None else None
                    ),
                    execution_contract_hash=(
                        _input_attribution.execution_contract_hash
                        if _input_attribution is not None
                        else None
                    ),
                )
            ]
            for pending_input in pending_inputs:
                self._validate_model_resource_refs(
                    session,
                    pending_input.resource_refs,
                    adapter=active_adapter,
                )
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            run = self._attach_extension_snapshot(run)
            session, run = self.run_lifecycle.start(
                session=session,
                run=run,
                snapshot=locked_snapshot,
            )

            sequence = self.store.latest_event_sequence(session.session_id)
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_started",
                payload={"trigger": run.trigger, "origin": run.origin},
            )

            input_messages: list[MessageRecord] = []
            for queued_input in pending_inputs:
                is_queued = _queued_inputs is not None
                input_message = self._append_session_input(
                    session=session,
                    run=run,
                    item=queued_input,
                    sequence=len(existing_messages) + len(input_messages) + 1,
                    is_queued=is_queued,
                )
                input_messages.append(input_message)
                if is_queued:
                    latest = self.store.load_runtime_session(session.session_id)
                    self.queued_inputs.acknowledge(latest, queued_input.queue_id)
                sequence = await self.event_recorder.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type=(
                        "plugin_message_added"
                        if queued_input.source == "plugin"
                        else "user_message_added"
                    ),
                    payload={
                        "message_id": input_message.message_id,
                        "queued_input_id": queued_input.queue_id if is_queued else None,
                        "source": queued_input.source,
                        "plugin_id": queued_input.plugin_id,
                        "contribution_id": queued_input.contribution_id,
                        "resource_refs": list(queued_input.resource_refs),
                    },
                )

            abort_signal = self._activate_abort_signal(session.session_id, run.run_id)
            result = await self._continue_model_loop(
                session=session,
                run=run,
                messages=[*existing_messages, *input_messages],
                sequence=sequence,
                emitted=emitted,
                abort_signal=abort_signal,
                adapter=active_adapter,
            )
        finally:
            if abort_signal is not None:
                self._release_abort_signal(run.run_id, abort_signal)
            if result is None:
                self._release_extension_snapshot(run.run_id)
            self._release_session_run_lock(run_lock)
        assert result is not None
        return await self._finalize_run_boundary(
            result,
            drain_queued_inputs=_drain_queued_inputs,
        )

    async def _continue_model_loop(
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
        adapter: RuntimeAdapter,
    ) -> RunResult:
        tools = self._tools_for_run(run.run_id)
        hooks = self._hooks_for_run(run.run_id)
        compaction = self._compaction_for_run(run.run_id)
        tool_calls = self._tool_calls_for_run(run.run_id)
        loop = InnerAgentLoop(
            adapter=adapter,
            tools=tools,
            run_budget=self.run_budget,
            ports=InnerLoopPorts(
                prepare_context=partial(
                    self.context_preparation.prepare,
                    adapter=adapter,
                    hooks=hooks,
                    strategies=compaction,
                    skill_instructions=self._skill_instructions_for_run(run.run_id),
                ),
                emit=self.event_recorder.emit,
                record_hook_failures=self.event_recorder.record_hook_failures,
                handle_tool_batch=tool_calls.handle_batch,
                record_usage=self.run_lifecycle.record_usage,
                aborted_result=self._aborted_result,
                save_waiting_state=self.run_lifecycle.save_waiting_state,
                complete_run=self.run_lifecycle.complete,
                fail_run=self.run_lifecycle.fail,
                pause_run=self.run_lifecycle.pause,
                consume_turn_boundary_inputs=self._consume_turn_boundary_inputs,
            ),
        )
        return await loop.run(
            session=session,
            run=run,
            messages=messages,
            sequence=sequence,
            emitted=emitted,
            final_parts=final_parts,
            tool_results=tool_results,
            abort_signal=abort_signal,
        )

    async def compact(self, *, session_id: str) -> CompactionCommandResult:
        """Manually compact a session through the same durable summary pipeline as auto mode."""

        snapshot = self._load_runtime_snapshot(session_id, recover=True)
        if maybe_active_run(snapshot.runs) is not None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        run = RunRecord(
            session_id=session_id,
            kind="compaction",
            trigger="manual_compaction",
            status="running",
        )
        run_lock = self._acquire_session_run_lock(session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"session is already locked: {session_id}")
        emitted: list[EventRecord] = []
        sequence = self.store.latest_event_sequence(session_id)
        abort_signal = self._activate_abort_signal(session_id, run.run_id)
        terminal_run = run
        try:
            snapshot = self.store.load_runtime_session(session_id)
            if maybe_active_run(snapshot.runs) is not None:
                raise SessionBusyError(f"session already has an active run: {session_id}")
            active_session, active_adapter = self._resolve_session_adapter(
                snapshot.session,
                snapshot=snapshot,
            )
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            run = self._attach_extension_snapshot(run)
            terminal_run = run
            active_session, run = self.run_lifecycle.start(
                session=active_session,
                run=run,
                snapshot=snapshot,
            )
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session_id,
                run_id=run.run_id,
                event_type="run_started",
                payload={"trigger": "manual_compaction", "kind": "compaction"},
            )
            try:
                execution = await self.context_preparation.compact(
                    emitted,
                    sequence,
                    session=active_session,
                    run=run,
                    messages=snapshot.messages,
                    tools=self._tools_for_run(run.run_id).definitions(),
                    added_context=[],
                    trigger="manual",
                    force=True,
                    abort_signal=abort_signal,
                    adapter=active_adapter,
                    strategies=self._compaction_for_run(run.run_id),
                    skill_instructions=self._skill_instructions_for_run(run.run_id),
                )
            except Exception as exc:
                failed = await self.run_lifecycle.fail(
                    emitted,
                    self.store.latest_event_sequence(session_id),
                    session=active_session,
                    run=run,
                    error=str(exc),
                    final_text="",
                    tool_results=[],
                )
                terminal_run = failed.run
                idle_session = failed.session.model_copy(
                    update={
                        "status": "idle",
                        "active_run_id": None,
                        "updated_at": utc_now(),
                        "last_activity_at": utc_now(),
                    }
                )
                self.store.save_session(idle_session)
                self.store.refresh_catalog(idle_session.session_id)
                if isinstance(exc, CompactionError):
                    raise
                raise CompactionError(f"manual compaction failed: {exc}") from exc
            completed = await self.run_lifecycle.complete(
                emitted,
                execution.sequence,
                session=active_session,
                run=run,
                messages=snapshot.messages,
                final_text="",
                tool_results=[],
            )
            terminal_run = completed.run
            return CompactionCommandResult(
                session=completed.session,
                run=completed.run,
                compaction=execution.record,
                events=emitted,
                skipped=execution.skipped,
            )
        finally:
            self._release_abort_signal(run.run_id, abort_signal)
            self._release_session_run_lock(run_lock)
            if terminal_run.status in ACTIVE_RUN_STATUSES:
                self._finish_run_hooks(terminal_run)
            else:
                await self._finalize_run_boundary(
                    RunResult(
                        session=self.store.load_session_record(session_id),
                        run=terminal_run,
                        events=emitted,
                    ),
                    drain_queued_inputs=False,
                )
            self._release_extension_snapshot(run.run_id)

    async def abort_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        reason: str = "User aborted",
    ) -> RunResult:
        snapshot = self.store.load_runtime_session(session_id)
        run = find_run(snapshot.runs, run_id) if run_id else find_active_run(snapshot.runs)
        if run.status not in ACTIVE_RUN_STATUSES:
            return RunResult(session=snapshot.session, run=run)

        emitted: list[EventRecord] = []
        signal = self._abort_signals.get(run.run_id)
        has_active_owner = signal is not None
        if signal is not None:
            signal.abort(reason)
        with self.store.session_mutation(session_id):
            snapshot = self.store.load_runtime_session(session_id)
            run = find_run(snapshot.runs, run.run_id)
            if run.status not in ACTIVE_RUN_STATUSES:
                return RunResult(session=snapshot.session, run=run)
            metadata = clear_pending_metadata(run.metadata)
            metadata["abort_reason"] = reason
            run = run.model_copy(
                update={
                    "status": "aborted",
                    "ended_at": utc_now(),
                    "pending_approval_id": None,
                    "abort_requested": True,
                    "error": reason,
                    "metadata": metadata,
                }
            )
            session = snapshot.session.model_copy(
                update={
                    "status": "idle",
                    "active_run_id": None,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                    "last_run_id": run.run_id,
                }
            )
            self.store.save_run(run)
            self.store.save_session(session)
        self.store.refresh_catalog(session.session_id)
        await self.event_recorder.emit(
            emitted,
            self.store.latest_event_sequence(session_id),
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_aborted",
            payload={"reason": reason},
        )
        result = RunResult(session=session, run=run, events=emitted)
        if has_active_owner:
            return result
        return await self._finalize_run_boundary(result, drain_queued_inputs=False)

    async def submit_plugin_input(
        self,
        session_input: AttributedSessionInput,
        *,
        session_id: str | None,
    ) -> RunResult:
        """Submit an attributed extension input through the canonical Run boundary."""

        return await self.run(
            session_input.prompt,
            session_id=session_id,
            title=session_input.title,
            queue_mode=session_input.mode,
            _input_attribution=session_input,
        )

    async def _queue_input(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        active_run: RunRecord,
        prompt: str,
        title: str | None,
        mode: QueuedInputMode,
        resource_refs: tuple[str, ...] = (),
        attribution: AttributedSessionInput | None = None,
    ) -> tuple[SessionRecord, list[EventRecord]]:
        session, queued_input, queue_size = self.queued_inputs.enqueue(
            snapshot=snapshot,
            prompt=prompt,
            title=title,
            mode=mode,
            resource_refs=resource_refs,
            source="plugin" if attribution is not None else "user",
            plugin_id=attribution.plugin_id if attribution is not None else None,
            contribution_id=(attribution.contribution_id if attribution is not None else None),
            generation_id=attribution.generation_id if attribution is not None else None,
            execution_contract_hash=(
                attribution.execution_contract_hash if attribution is not None else None
            ),
            caused_by_run_id=active_run.run_id if attribution is not None else None,
        )
        emitted: list[EventRecord] = []
        sequence = await self.event_recorder.emit(
            emitted,
            self.store.latest_event_sequence(session.session_id),
            session_id=session.session_id,
            run_id=active_run.run_id,
            event_type=(
                "follow_up_input_queued"
                if queued_input.mode == "follow_up"
                else "steering_input_queued"
            ),
            payload={
                "queue_id": queued_input.queue_id,
                "mode": queued_input.mode,
                "queue_size": queue_size,
                "resource_refs": list(queued_input.resource_refs),
                "source": queued_input.source,
                "plugin_id": queued_input.plugin_id,
                "contribution_id": queued_input.contribution_id,
            },
        )
        del sequence
        return session, emitted

    async def _drain_queued_input_queue(self, session_id: str) -> None:
        while True:
            snapshot = self._load_runtime_snapshot(
                session_id,
                recover=not self._is_session_run_locked(session_id),
            )
            if maybe_active_run(snapshot.runs) is not None:
                return
            limit = 1 if self.queued_input_config.consumption_mode == "one_by_one" else None
            queued = self.queued_inputs.eligible(snapshot, limit=limit)
            if not queued:
                return
            pending = [
                item
                for item in queued
                if not self._queued_input_is_durable(snapshot.messages, item.queue_id)
            ]
            for item in queued:
                if item not in pending:
                    self.queued_inputs.acknowledge(snapshot, item.queue_id)
            if not pending:
                continue
            result = await self.run(
                pending[0].prompt,
                session_id=session_id,
                title=pending[0].title,
                _drain_queued_inputs=False,
                _queued_inputs=pending,
            )
            if result.run.status != "completed":
                return

    async def _consume_turn_boundary_inputs(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        assistant_text: str,
        assistant_reasoning: str,
        assistant_continuation: ProviderContinuationArtifact | None,
        turn_id: str,
    ) -> TurnBoundaryInputResult:
        snapshot = self.store.load_runtime_session(session.session_id)
        limit = 1 if self.queued_input_config.consumption_mode == "one_by_one" else None
        eligible = self.queued_inputs.eligible(snapshot, mode="steer", limit=limit)
        if not eligible:
            return TurnBoundaryInputResult(sequence=sequence)

        appended: list[MessageRecord] = []
        if assistant_text or assistant_reasoning or assistant_continuation is not None:
            assistant_message = self.run_lifecycle.append_assistant_message(
                session=session,
                run=run,
                text=assistant_text,
                reasoning_content=assistant_reasoning,
                provider_continuation=assistant_continuation,
                sequence=len(messages) + 1,
                turn_id=turn_id,
            )
            if not any(message.message_id == assistant_message.message_id for message in messages):
                appended.append(assistant_message)

        for item in eligible:
            latest = self.store.load_runtime_session(session.session_id)
            if self._queued_input_is_durable(latest.messages, item.queue_id):
                self.queued_inputs.acknowledge(latest, item.queue_id)
                durable = next(
                    (
                        message
                        for message in latest.messages
                        if message.metadata.get("queued_input_id") == item.queue_id
                        or message.metadata.get("queued_follow_up_id") == item.queue_id
                    ),
                    None,
                )
                if durable is not None and not any(
                    message.message_id == durable.message_id for message in messages
                ):
                    appended.append(durable)
                continue
            input_message = self._append_session_input(
                session=session,
                run=run,
                item=item,
                sequence=len(messages) + len(appended) + 1,
                is_queued=True,
            )
            appended.append(input_message)
            latest = self.store.load_runtime_session(session.session_id)
            self.queued_inputs.acknowledge(latest, item.queue_id)
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="queued_input_consumed",
                payload={
                    "queue_id": item.queue_id,
                    "mode": item.mode,
                    "turn_id": turn_id,
                    "source": item.source,
                    "plugin_id": item.plugin_id,
                    "contribution_id": item.contribution_id,
                },
            )
        return TurnBoundaryInputResult(sequence=sequence, messages=appended)

    def _append_session_input(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        item: QueuedInput,
        sequence: int,
        is_queued: bool,
    ) -> MessageRecord:
        metadata: dict[str, object] = {
            "session_input_source": item.source,
            "session_input_mode": item.mode,
        }
        if is_queued:
            metadata["queued_input_id"] = item.queue_id
            metadata["queued_input_mode"] = item.mode
        if item.source == "plugin":
            if item.resource_refs:
                raise ResourceAttachmentError(
                    "extension-produced session inputs cannot attach user resources"
                )
            metadata.update(
                {
                    "plugin_id": item.plugin_id,
                    "contribution_id": item.contribution_id,
                    "generation_id": item.generation_id,
                    "execution_contract_hash": item.execution_contract_hash,
                    "caused_by_run_id": item.caused_by_run_id,
                    "continuation_decision_id": item.continuation_decision_id,
                }
            )
            return self.run_lifecycle.append_synthetic_input(
                session=session,
                run=run,
                prompt=item.prompt,
                sequence=sequence,
                metadata=metadata,
            )
        return self.run_lifecycle.append_user_message(
            session=session,
            run=run,
            prompt=item.prompt,
            sequence=sequence,
            resource_refs=item.resource_refs,
            metadata=metadata,
        )

    def _validate_model_resource_refs(
        self,
        session: SessionRecord,
        resource_refs: tuple[str, ...],
        *,
        adapter: RuntimeAdapter | None = None,
    ) -> None:
        if not resource_refs:
            return
        if self.resources is None:
            raise ResourceAttachmentError(
                "resource attachments require a configured ResourceService"
            )
        active_adapter = adapter
        if active_adapter is None:
            selection = self.models.resolve(
                session.current_model or self.models.default_model().key
            )
            active_adapter = self.models.adapter_for(selection.key)
        self.resources.validate_model_references(
            session.session_id,
            resource_refs,
            supports_vision=active_adapter.model.supports_vision,
        )

    @staticmethod
    def _queued_input_is_durable(messages: list[MessageRecord], queue_id: str) -> bool:
        return any(
            message.metadata.get("queued_input_id") == queue_id
            or message.metadata.get("queued_follow_up_id") == queue_id
            for message in messages
        )

    def _acquire_session_run_lock(self, session_id: str, run_id: str) -> SessionLock | None:
        return self.store.acquire_session_lock(session_id, run_id)

    def _release_session_run_lock(self, lock: SessionLock | None) -> None:
        if lock is not None:
            lock.release()

    def _is_session_run_locked(self, session_id: str) -> bool:
        return self.store.is_session_locked(session_id)

    def _load_runtime_snapshot(
        self,
        session_id: str,
        *,
        recover: bool,
    ) -> RuntimeSessionSnapshot:
        if recover:
            return self.session_recovery.recover_runtime(session_id)
        return self.store.load_runtime_session(session_id)

    async def approve_tool(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        approval_id: str | None = None,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunResult:
        snapshot = self._load_runtime_snapshot(session_id, recover=True)
        session = snapshot.session
        run = find_run(snapshot.runs, run_id) if run_id else find_active_run(snapshot.runs)
        if run.status != "waiting_approval":
            raise ValueError(f"run is not waiting for approval: {run.status}")
        batch = run.active_tool_batch
        if batch is None:
            raise ValueError("run has no active tool batch")
        resolved_approval_id = approval_id or run.pending_approval_id
        if resolved_approval_id is None:
            raise ValueError("run has no pending approval")
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run.run_id}")
        try:
            snapshot = self.store.load_runtime_session(session_id)
            session = snapshot.session
            run = find_run(snapshot.runs, run.run_id)
            if run.status != "waiting_approval":
                raise ValueError(f"run is not waiting for approval: {run.status}")
            batch = run.active_tool_batch
            if batch is None:
                raise ValueError("run has no active tool batch")
            resolved_approval_id = approval_id or run.pending_approval_id
            if resolved_approval_id is None:
                raise ValueError("run has no pending approval")
            await self._ensure_extension_snapshot(run)
            batch = self._tool_calls_for_run(run.run_id).resolve_approval(
                batch,
                approval_id=resolved_approval_id,
                approved=approved,
                reason=reason,
                arguments_override=arguments_override,
            )
            resolved_call = next(
                call
                for call in batch.calls
                if call.approval is not None and call.approval.approval_id == resolved_approval_id
            )
            sequence = self.store.latest_event_sequence(session_id)
            emitted: list[EventRecord] = []
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "approval_id": resolved_approval_id,
                    "tool_call_id": resolved_call.request.id,
                    "tool_call_status": resolved_call.status,
                    "approved": approved,
                    "reason": None if approved else reason,
                },
            )
            result = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                emitted=emitted,
                sequence=sequence,
            )
        finally:
            self._release_session_run_lock(run_lock)
        # Queue draining starts a new Run and therefore must occur outside the
        # completed interaction's non-reentrant Session run lock.
        return await self._finalize_run_boundary(result, drain_queued_inputs=True)

    async def answer_user(
        self,
        *,
        session_id: str,
        run_id: str,
        answer: JsonValue,
    ) -> RunResult:
        snapshot = self._load_runtime_snapshot(session_id, recover=True)
        session = snapshot.session
        run = find_run(snapshot.runs, run_id)
        if run.status != "waiting_user":
            raise ValueError(f"run is not waiting for user input: {run.status}")
        batch = run.active_tool_batch
        if batch is None:
            raise ValueError("run has no active tool batch")
        waiting_call = next((call for call in batch.calls if call.status == "waiting_user"), None)
        if waiting_call is None:
            raise ValueError("run has no tool call waiting for user input")
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run.run_id}")
        try:
            snapshot = self.store.load_runtime_session(session_id)
            session = snapshot.session
            run = find_run(snapshot.runs, run.run_id)
            if run.status != "waiting_user":
                raise ValueError(f"run is not waiting for user input: {run.status}")
            batch = run.active_tool_batch
            if batch is None:
                raise ValueError("run has no active tool batch")
            waiting_call = next(
                (call for call in batch.calls if call.status == "waiting_user"),
                None,
            )
            if waiting_call is None:
                raise ValueError("run has no tool call waiting for user input")
            await self._ensure_extension_snapshot(run)
            batch = self._tool_calls_for_run(run.run_id).resolve_user_answer(
                batch,
                tool_call_id=waiting_call.request.id,
                answer=answer,
            )
            resolved_call = next(
                call for call in batch.calls if call.request.id == waiting_call.request.id
            )
            sequence = self.store.latest_event_sequence(session_id)
            emitted: list[EventRecord] = []
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "tool_call_id": waiting_call.request.id,
                    "tool_call_status": resolved_call.status,
                    "answer": answer,
                },
            )
            result = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                emitted=emitted,
                sequence=sequence,
            )
        finally:
            self._release_session_run_lock(run_lock)
        # Keep the same post-lock Run boundary as normal runs and approval
        # recovery; a follow-up must acquire a fresh Session run lock.
        return await self._finalize_run_boundary(result, drain_queued_inputs=True)

    async def _continue_resolved_batch(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        emitted: list[EventRecord],
        sequence: int,
    ) -> RunResult:
        await self._ensure_extension_snapshot(run)
        pending_approvals = batch.pending_approvals
        waiting_user = next((call for call in batch.calls if call.status == "waiting_user"), None)
        if pending_approvals or waiting_user is not None:
            status: RunStatus = "waiting_approval" if pending_approvals else "waiting_user"
            session, run = self.run_lifecycle.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status=status,
                phase="human_interaction",
            )
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_status_changed",
                payload={
                    "to_status": status,
                    "status": status,
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                },
            )
            return RunResult(session=session, run=run, events=emitted)

        active_signal = self._activate_abort_signal(session.session_id, run.run_id)
        try:
            try:
                handling = await self._tool_calls_for_run(run.run_id).execute_batch(
                    emitted,
                    sequence,
                    session=session,
                    run=run,
                    batch=batch,
                    message_sequence=len(snapshot.messages) + 1,
                    abort_signal=active_signal,
                )
            except Exception as exc:  # noqa: BLE001 - coordinator is a runtime boundary.
                latest = self.store.load_runtime_session(session.session_id)
                latest_run = find_run(latest.runs, run.run_id)
                failed = await self.run_lifecycle.fail(
                    emitted,
                    self.store.latest_event_sequence(session.session_id),
                    session=latest.session,
                    run=latest_run,
                    error=f"tool batch handling failed: {exc}",
                    final_text="",
                    tool_results=[],
                )
                return failed
            latest = self._load_runtime_snapshot(session.session_id, recover=True)
            session = latest.session
            run = find_run(latest.runs, run.run_id)
            abort_result = self._aborted_result(
                session_id=session.session_id,
                run_id=run.run_id,
                final_text="",
                emitted=emitted,
                tool_results=handling.results,
            )
            if abort_result is not None:
                return abort_result
            if handling.status == "waiting_user":
                return RunResult(
                    session=session,
                    run=run,
                    events=emitted,
                    tool_results=handling.results,
                )
            boundary = await self._consume_turn_boundary_inputs(
                emitted,
                handling.sequence,
                session=session,
                run=run,
                messages=latest.messages,
                assistant_text="",
                assistant_reasoning="",
                assistant_continuation=None,
                turn_id=batch.turn_id,
            )
            latest = self.store.load_runtime_session(session.session_id)
            session = latest.session
            run = find_run(latest.runs, run.run_id)
            session, active_adapter = self._resolve_session_adapter(
                session,
                snapshot=latest,
            )
            resumed = await self._continue_model_loop(
                session=session,
                run=run,
                messages=latest.messages,
                sequence=boundary.sequence,
                emitted=emitted,
                tool_results=handling.results,
                abort_signal=active_signal,
                adapter=active_adapter,
            )
        finally:
            self._release_abort_signal(run.run_id, active_signal)
        return resumed

    def _is_run_aborted(self, session_id: str, run_id: str) -> bool:
        return self._abort_state(session_id, run_id) is not None

    def _abort_state(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[SessionRecord, RunRecord] | None:
        try:
            run = self.store.load_run(session_id, run_id)
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None
        if run.status == "aborted" or run.abort_requested:
            try:
                return self.store.load_session_record(session_id), run
            except Exception:  # noqa: BLE001 - abort polling must not break active runs.
                return None
        return None

    def _aborted_result(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str,
        emitted: list[EventRecord],
        tool_results: list[ToolResult],
    ) -> RunResult | None:
        abort_state = self._abort_state(session_id, run_id)
        if abort_state is None:
            return None
        session, run = abort_state
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _activate_abort_signal(self, session_id: str, run_id: str) -> AbortSignal:
        signal = AbortSignal(lambda: self._abort_state(session_id, run_id) is not None)
        self._abort_signals[run_id] = signal
        return signal

    def _release_abort_signal(self, run_id: str, signal: AbortSignal) -> None:
        if self._abort_signals.get(run_id) is signal:
            self._abort_signals.pop(run_id, None)

    async def _finalize_run_boundary(
        self,
        result: RunResult,
        *,
        drain_queued_inputs: bool,
    ) -> RunResult:
        run = result.run
        if run.status not in ACTIVE_RUN_STATUSES:
            try:
                continuation_service = RunContinuationService(
                    store=self.store,
                    registry=self._continuations_for_run(run.run_id),
                    queue=self.queued_inputs,
                )
                settlement = await continuation_service.settle(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    final_text=result.final_text,
                )
                run = settlement.run
                if settlement.settled and run.continuation is not None:
                    await self.event_recorder.emit(
                        result.events,
                        self.store.latest_event_sequence(run.session_id),
                        session_id=run.session_id,
                        run_id=run.run_id,
                        event_type="run_continuation_resolved",
                        payload={
                            "decision_id": run.continuation.decision_id,
                            "action": run.continuation.action,
                            "controller_id": run.continuation.selected_controller_id,
                            "reason": run.continuation.reason,
                            "queued": settlement.queued,
                            "failure_count": len(settlement.failures),
                            "failures": [
                                failure.model_dump(mode="json") for failure in settlement.failures
                            ],
                            "diagnostics": list(settlement.diagnostics),
                        },
                    )
                outcome = RunOutcome(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    status=run.status,
                    origin=run.origin,
                    final_text=result.final_text,
                    error=run.error,
                    usage=dict(run.usage),
                )
                dispatch = await self._hooks_for_run(run.run_id).dispatch(
                    HookContext(
                        hook_name="after_run",
                        session_id=run.session_id,
                        run_id=run.run_id,
                        data={"outcome": outcome.model_dump(mode="json")},
                        run_outcome=outcome,
                    )
                )
                if dispatch.failures:
                    await self.event_recorder.record_hook_failures(
                        result.events,
                        self.store.latest_event_sequence(run.session_id),
                        dispatch.failures,
                    )
            finally:
                self._finish_run_hooks(run)
        else:
            self._finish_run_hooks(run)

        updated = result.model_copy(
            update={
                "run": run,
                "session": self.store.load_session_record(run.session_id),
            }
        )
        if drain_queued_inputs and run.status == "completed":
            await self._drain_queued_input_queue(run.session_id)
        return updated

    def _finish_run_hooks(self, run: RunRecord) -> None:
        self.run_lifecycle.finish_run(run)
        if run.status not in ACTIVE_RUN_STATUSES:
            self._hooks_for_run(run.run_id).finish_run(run.run_id)
            snapshot = self._extension_snapshots.pop(run.run_id, None)
            if snapshot is not None:
                snapshot.close()

    def _release_extension_snapshot(self, run_id: str) -> None:
        snapshot = self._extension_snapshots.pop(run_id, None)
        if snapshot is not None:
            snapshot.close()

    def _attach_extension_snapshot(self, run: RunRecord) -> RunRecord:
        if self._extension_catalog is None:
            return run
        snapshot = self._extension_catalog.snapshot()
        self._extension_snapshots[run.run_id] = snapshot
        metadata = dict(run.metadata)
        metadata["extension_catalog_id"] = snapshot.catalog_id
        metadata["extension_generations"] = [
            generation.model_dump(mode="json", exclude_none=True)
            for generation in snapshot.generations
        ]
        update: dict[str, object] = {"metadata": metadata}
        if run.kind == "agent":
            generations = {
                generation.generation_id: generation for generation in snapshot.generations
            }
            controllers: list[RunContinuationControllerRef] = []
            for registration in snapshot.continuations.registered():
                generation_id = registration.generation_id
                generation = generations.get(generation_id or "")
                if generation is None or generation.plugin_id != registration.plugin_id:
                    raise RuntimeError(
                        "continuation policy is not owned by the active extension generation: "
                        f"{registration.plugin_id}:{registration.policy_id}"
                    )
                canonical_id = (
                    f"{registration.plugin_id}/continuation_policies/{registration.policy_id}"
                )
                contract_hash = snapshot.contract_hash(canonical_id)
                if contract_hash is None:
                    raise RuntimeError(f"continuation policy contract is missing: {canonical_id}")
                if (
                    registration.execution_contract_hash != generation.execution_contract_hash
                    or registration.contract_hash != contract_hash
                ):
                    raise RuntimeError(
                        f"continuation policy execution contract is inconsistent: {canonical_id}"
                    )
                assert generation.execution_contract_hash is not None
                controllers.append(
                    RunContinuationControllerRef(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        version=generation.version,
                        package_digest=generation.package_digest,
                        generation_id=generation.generation_id,
                        contract_hash=contract_hash,
                        execution_contract_hash=generation.execution_contract_hash,
                    )
                )
            if controllers:
                update["continuation"] = RunContinuationCheckpoint(controllers=controllers)
        return run.model_copy(update=update)

    async def _ensure_extension_snapshot(self, run: RunRecord) -> None:
        if self._extension_catalog is None or run.run_id in self._extension_snapshots:
            return
        raw = run.metadata.get("extension_generations", [])
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("run has invalid extension generation metadata")
        raw_catalog_id = run.metadata.get("extension_catalog_id")
        catalog_id = raw_catalog_id if isinstance(raw_catalog_id, str) else None
        self._extension_snapshots[run.run_id] = await self._extension_catalog.snapshot_for(
            raw,
            catalog_id=catalog_id,
        )


def _title_from_prompt(prompt: str) -> str:
    stripped = " ".join(prompt.split())
    return stripped[:60] if stripped else "New session"
