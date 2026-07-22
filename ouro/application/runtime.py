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
from ouro.application.queued_inputs import (
    QueuedInput,
    QueuedInputMode,
    QueuedInputQueue,
    QueuedInputSource,
)
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
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.session_models import (
    CurrentSessionContextSnapshot,
    CurrentSessionContextStatus,
)
from ouro.application.session_recovery import SessionRecoveryService
from ouro.application.sessions import SessionService
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.application.usage import UsageService
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentCatalogSnapshot, AgentWaitResolution, EffectiveToolRef
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionCommandResult, CompactionStrategyRegistry
from ouro.core.context import ContextBuilder
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.context_snapshots import context_view_digest
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import CompactionError, ConfigError, ResourceAttachmentError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.inner_loop import (
    InnerAgentLoop,
    InnerLoopPorts,
    TurnBoundaryInputResult,
    TurnCompletionPolicyResult,
)
from ouro.core.interactions import (
    SESSION_INTERACTION_MODES,
    SessionInteractionMode,
)
from ouro.core.messages import MessageRecord, ProviderContinuationArtifact
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    AttributedSessionInput,
    HostSessionInput,
    SessionControlActivation,
    SessionControlTransitionResult,
)
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
from ouro.core.tools import PermissionMode, tool_definition_contract_hash
from ouro.core.turn_policies import TurnCompletionCandidate, TurnPolicyRegistry
from ouro.prompts import DEFAULT_SYSTEM_PROMPT, render_agent_skills_prompt
from ouro.sessions import RuntimeSessionSnapshot, SessionLock, SessionStore
from ouro.skills import SkillMetadata
from ouro.tools import ToolExecutor, ToolRegistry, ToolResult
from ouro.tools.builtin_agents import agent_control_tools

if TYPE_CHECKING:
    from ouro.application.agent_definitions import AgentDefinitionService
    from ouro.application.resources import ResourceService
    from ouro.application.tool_calls import AgentControlExecutor
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
        context_providers: ContextProviderRegistry | None = None,
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
        turn_policies: TurnPolicyRegistry | None = None,
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
        self.session_controls = SessionControlCoordinator(store)
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
        self.context_providers = context_providers or ContextProviderRegistry()
        self.continuations = continuations or RunContinuationRegistry()
        self.tool_policies = tool_policies or ToolPolicyRegistry()
        self.turn_policies = turn_policies or TurnPolicyRegistry()
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
            context_providers=self.context_providers,
            events=self.event_recorder,
            context_builder=self.context_builder,
            strategies=self.compaction_strategies,
            config=self.compaction_config,
            system_prompt=self.system_prompt,
            append_context_message=self.run_lifecycle.append_synthetic_input,
            project_model_messages=(
                resources.project_model_messages if resources is not None else None
            ),
        )
        self._abort_signals: dict[str, AbortSignal] = {}
        self._extension_catalog: PluginRuntimeResource | None = None
        self._extension_snapshots: dict[str, ExtensionCatalogLease] = {}
        self._session_control_snapshots: dict[str, frozenset[str]] = {}
        self._agent_definitions: AgentDefinitionService | None = None
        self._agent_controls: AgentControlExecutor | None = None
        self._run_agent_catalogs: dict[str, AgentCatalogSnapshot] = {}
        self._run_tool_caps: dict[str, frozenset[str]] = {}
        self._run_tool_refs: dict[str, tuple[EffectiveToolRef, ...]] = {}

    def attach_extension_catalog(self, catalog: PluginRuntimeResource) -> None:
        if self._extension_catalog is not None and self._extension_catalog is not catalog:
            raise RuntimeError("runtime already has an extension catalog")
        self._extension_catalog = catalog
        self.event_recorder.set_hook_registry_resolver(self._event_hooks_for_run)

    def attach_agent_definitions(self, service: AgentDefinitionService) -> None:
        if self._agent_definitions is not None and self._agent_definitions is not service:
            raise RuntimeError("runtime already has an AgentDefinition service")
        self._agent_definitions = service

    def attach_agent_controls(self, controls: AgentControlExecutor) -> None:
        if self._agent_controls is not None and self._agent_controls is not controls:
            raise RuntimeError("runtime already has an Agent control executor")
        self._agent_controls = controls

    def _event_hooks_for_run(
        self,
        run_id: str | None,
    ) -> tuple[HookRegistry, Callable[[], None]]:
        if run_id is None:
            return self.hooks.scoped(lambda: set()), lambda: None
        snapshot = self._extension_snapshots.get(run_id)
        if snapshot is None:
            return self.hooks, lambda: None
        retained = snapshot.fork()
        return (
            retained.hooks.scoped(lambda: self._active_controller_ids_for_run(run_id)),
            retained.close,
        )

    def _tools_for_run(self, run_id: str) -> ToolRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.tools if snapshot is not None else self.tools
        scoped = registry.scoped(lambda: self._active_controller_ids_for_run(run_id))
        cap = self._run_tool_caps.get(run_id)
        selected = scoped if cap is None else scoped.selected(cap)
        for reference in self._run_tool_refs.get(run_id, ()):
            definition = selected.get(reference.public_name).definition
            metadata_hash = definition.metadata.get("execution_contract_hash")
            execution_hash = (
                metadata_hash
                if isinstance(metadata_hash, str) and metadata_hash
                else tool_definition_contract_hash(definition)
            )
            if (
                execution_hash != reference.execution_contract_hash
                or definition.source != reference.source
                or definition.effect != reference.effect
            ):
                raise RuntimeError(
                    f"delegated tool contract is unavailable or changed: {reference.public_name}"
                )
        catalog = self._run_agent_catalogs.get(run_id)
        if catalog is None or self._agent_controls is None:
            return selected
        return selected.overlay(agent_control_tools(catalog))

    def _hooks_for_run(self, run_id: str) -> HookRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.hooks if snapshot is not None else self.hooks
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _context_providers_for_run(self, run_id: str) -> ContextProviderRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.context_providers if snapshot is not None else self.context_providers
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

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
        registry = snapshot.continuations if snapshot is not None else self.continuations
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _tool_policies_for_run(self, run_id: str) -> ToolPolicyRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.tool_policies if snapshot is not None else self.tool_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _turn_policies_for_run(self, run_id: str) -> TurnPolicyRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.turn_policies if snapshot is not None else self.turn_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _active_controller_ids_for_run(self, run_id: str) -> set[str]:
        return set(self._session_control_snapshots.get(run_id, frozenset()))

    def _session_controllers_for_run(self, run_id: str):
        from ouro.plugins.session_controllers import PluginSessionControllerRegistry

        snapshot = self._extension_snapshots.get(run_id)
        return (
            snapshot.session_controllers
            if snapshot is not None
            else PluginSessionControllerRegistry()
        )

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
            artifact_root=(
                str(self.environment.artifact_root)
                if self.environment.artifact_root is not None
                else None
            ),
            is_aborted=self._is_run_aborted,
            tool_policies=self._tool_policies_for_run(run_id),
            session_controls=self.session_controls,
            session_controllers=self._session_controllers_for_run(run_id),
            agent_controls=self._agent_controls,
        )

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        self.event_recorder.add_event_sink(event_sink)

    def session_control_activations(
        self,
        session_id: str,
    ) -> tuple[SessionControlActivation, ...]:
        session = self.store.load_session_record(session_id)
        return tuple(item.model_copy(deep=True) for item in session.controls.activations)

    def reconcile_interrupted_session_controls(self) -> tuple[str, ...]:
        reconciled: list[str] = []
        for entry in self.sessions.catalog().entries:
            if self.session_controls.reconcile_interrupted(entry.session_id):
                reconciled.append(entry.session_id)
        return tuple(reconciled)

    async def release_session_control(
        self,
        *,
        session_id: str,
        activation_id: str,
        force: bool = False,
    ) -> SessionControlTransitionResult:
        activation = next(
            (
                item
                for item in self.session_control_activations(session_id)
                if item.activation_id == activation_id
            ),
            None,
        )
        if activation is None:
            return self.session_controls.force_release(
                session_id=session_id,
                activation_id=activation_id,
            )
        if force:
            return self.session_controls.force_release(
                session_id=session_id,
                activation_id=activation_id,
            )
        if self._extension_catalog is None:
            raise RuntimeError(
                "Session controller Catalog is unavailable; retry with force release"
            )
        lease = self._extension_catalog.snapshot()
        try:
            return await self.session_controls.apply(
                AttributedSessionControlTransition(
                    action="release",
                    activation_id=activation.activation_id,
                    plugin_id=activation.plugin_id,
                    generation_id=activation.generation_id,
                    execution_contract_hash=activation.execution_contract_hash,
                    reason="host_control_command",
                ),
                session_id=session_id,
                controllers=lease.session_controllers,
                workspace=self.default_workspace,
            )
        except Exception as exc:
            raise RuntimeError(
                "Session controller cleanup could not be confirmed; retry with force release"
            ) from exc
        finally:
            lease.close()

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
        self._session_control_snapshots.clear()
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

    def current_context(self, session_id: str) -> CurrentSessionContextSnapshot:
        """Project the Session's current durable context without starting a model request."""

        snapshot = self.store.load_runtime_session(session_id)
        selection = self.models.resolve(
            snapshot.session.current_model or self.models.default_model().key
        )
        adapter = self.models.adapter_for(selection.key)
        catalog = (
            self._extension_catalog.snapshot() if self._extension_catalog is not None else None
        )
        try:
            tools = catalog.tools if catalog is not None else self.tools
            skills = catalog.skills if catalog is not None else self.skills
            view = self.context_preparation.project_current(
                session_id=session_id,
                messages=snapshot.messages,
                tools=tools.definitions(),
                adapter=adapter,
                skill_instructions=render_agent_skills_prompt(skills),
            )
        finally:
            if catalog is not None:
                catalog.close()

        compaction_id = view.context_metadata.get("compaction_id")
        return CurrentSessionContextSnapshot(
            status=CurrentSessionContextStatus(
                session_id=session_id,
                provider_id=adapter.model.provider_id,
                endpoint_id=adapter.model.endpoint_id,
                model_id=adapter.model.model_id,
                projected_at=utc_now(),
                metrics=view.metrics,
                active_compaction_id=(compaction_id if isinstance(compaction_id, str) else None),
            ),
            provider_context=view.provider_context(),
            context_digest=context_view_digest(view),
        )

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
        interaction_mode: SessionInteractionMode = "interactive",
    ) -> SessionRecord:
        """Create an idle Session before a Host starts asynchronous input work."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        selected_model = (
            self.resolve_model(initial_model)
            if initial_model is not None
            else self.models.default_model()
        )
        created = self.sessions.create(
            title=title,
            current_model=selected_model.key,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
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

    def set_session_interaction_mode(
        self,
        session_id: str,
        interaction_mode: SessionInteractionMode,
    ) -> SessionRecord:
        """Persist how future tool input requests are resolved in a Session."""

        self.sessions.set_interaction_mode(session_id, interaction_mode)
        return self.store.load_session_record(session_id)

    def set_session_afk_mode(self, session_id: str, *, enabled: bool) -> SessionRecord:
        """Apply or clear the AFK policy preset for an idle Session."""

        self.sessions.set_afk_mode(session_id, enabled=enabled)
        return self.store.load_session_record(session_id)

    @staticmethod
    def _validate_requested_session_policies(
        session: SessionRecord,
        *,
        approval_mode: SessionApprovalMode | None,
        interaction_mode: SessionInteractionMode | None,
    ) -> None:
        if approval_mode is not None and approval_mode != session.approval_mode:
            raise SessionBusyError(
                "cannot change approval mode while the session has an active run: "
                f"{session.session_id}"
            )
        if interaction_mode is not None and interaction_mode != session.interaction_mode:
            raise SessionBusyError(
                "cannot change interaction mode while the session has an active run: "
                f"{session.session_id}"
            )

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
        interaction_mode: SessionInteractionMode | None = None,
        resource_refs: list[str] | tuple[str, ...] | None = None,
        _drain_queued_inputs: bool = True,
        _queued_inputs: list[QueuedInput] | None = None,
        _input_attribution: AttributedSessionInput | HostSessionInput | None = None,
        _prepared_run: RunRecord | None = None,
    ) -> RunResult:
        if approval_mode is not None and approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if interaction_mode is not None and interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        if initial_model is not None and session_id is not None:
            raise ValueError("initial_model can only be set when creating a session")
        if _prepared_run is not None:
            if session_id is None or _prepared_run.session_id != session_id:
                raise ValueError("prepared run must target the supplied existing session")
            if _prepared_run.status != "queued":
                raise ValueError("prepared run must be queued")
            if not isinstance(_input_attribution, HostSessionInput):
                raise ValueError("prepared run requires HostSessionInput attribution")
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
                interaction_mode=interaction_mode or "interactive",
            )
            snapshot = RuntimeSessionSnapshot(session=session)
        else:
            snapshot = self._load_runtime_snapshot(
                session_id,
                recover=(_prepared_run is None and not self._is_session_run_locked(session_id)),
            )
            session = snapshot.session
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(session, input_resource_refs)
            if active_run is not None and (
                _prepared_run is None or active_run.run_id != _prepared_run.run_id
            ):
                self._validate_requested_session_policies(
                    session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
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

        queued_synthetic_input = next(
            (item for item in (_queued_inputs or []) if item.source != "user"),
            None,
        )
        synthetic_source = (
            _attributed_input_source(_input_attribution)
            if _input_attribution is not None
            else (queued_synthetic_input.source if queued_synthetic_input is not None else None)
        )
        run = _prepared_run or RunRecord(
            session_id=session.session_id,
            status="running",
            origin=(
                synthetic_source
                if synthetic_source is not None
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
                _attributed_input_initiator(_input_attribution)
                if _input_attribution is not None
                else _queued_input_initiator(queued_synthetic_input)
            ),
            trigger=synthetic_source or "user",
        )
        run_lock = self._acquire_session_run_lock(session.session_id, run.run_id)
        if run_lock is None:
            snapshot = self.store.load_runtime_session(session.session_id)
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(snapshot.session, input_resource_refs)
            if active_run is not None and (
                _prepared_run is None or active_run.run_id != _prepared_run.run_id
            ):
                self._validate_requested_session_policies(
                    snapshot.session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
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
            if locked_active_run is not None and (
                _prepared_run is None or locked_active_run.run_id != _prepared_run.run_id
            ):
                self._validate_requested_session_policies(
                    session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
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
            policy_updates: dict[str, object] = {}
            if approval_mode is not None and approval_mode != session.approval_mode:
                policy_updates["approval_mode"] = approval_mode
            if interaction_mode is not None and interaction_mode != session.interaction_mode:
                policy_updates["interaction_mode"] = interaction_mode
            if policy_updates:
                with self.store.session_mutation(session.session_id):
                    latest_session_record = self.store.load_session_record(session.session_id)
                    now = utc_now()
                    session = latest_session_record.model_copy(
                        update={
                            **policy_updates,
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
                    source=(
                        _attributed_input_source(_input_attribution)
                        if _input_attribution is not None
                        else "user"
                    ),
                    host_source_id=(
                        _input_attribution.host_source_id
                        if isinstance(_input_attribution, HostSessionInput)
                        else None
                    ),
                    plugin_id=(
                        _input_attribution.plugin_id if _input_attribution is not None else None
                    )
                    if isinstance(_input_attribution, AttributedSessionInput)
                    else None,
                    contribution_id=(
                        _input_attribution.contribution_id
                        if isinstance(_input_attribution, AttributedSessionInput)
                        else None
                    ),
                    generation_id=(
                        _input_attribution.generation_id
                        if isinstance(_input_attribution, AttributedSessionInput)
                        else None
                    ),
                    execution_contract_hash=(
                        _input_attribution.execution_contract_hash
                        if isinstance(_input_attribution, AttributedSessionInput)
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
            self._restore_run_tool_cap(run)
            if _prepared_run is not None and "extension_generations" in run.metadata:
                await self._ensure_extension_snapshot(run)
            else:
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
                    event_type=_session_input_event_type(queued_input.source),
                    payload={
                        "message_id": input_message.message_id,
                        "queued_input_id": queued_input.queue_id if is_queued else None,
                        "source": queued_input.source,
                        "host_source_id": queued_input.host_source_id,
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

    async def run_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput,
    ) -> RunResult:
        """Execute an already durable queued Run with attributed Host input."""

        return await self.run(
            session_input.prompt,
            session_id=run.session_id,
            title=session_input.title,
            queue_mode=session_input.mode,
            _input_attribution=session_input,
            _prepared_run=run,
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
        context_providers = self._context_providers_for_run(run.run_id)
        compaction = self._compaction_for_run(run.run_id)
        tool_calls = self._tool_calls_for_run(run.run_id)
        loop = InnerAgentLoop(
            adapter=adapter,
            tools=tools,
            run_budget=self._run_budget_for_run(run),
            ports=InnerLoopPorts(
                prepare_context=partial(
                    self.context_preparation.prepare,
                    adapter=adapter,
                    hooks=hooks,
                    context_providers=context_providers,
                    strategies=compaction,
                    skill_instructions=self._instructions_for_run(run),
                ),
                emit=self.event_recorder.emit,
                record_hook_failures=self.event_recorder.record_hook_failures,
                handle_tool_batch=tool_calls.handle_batch,
                record_usage=self.run_lifecycle.record_usage,
                record_context_completion=self.context_preparation.record_provider_completion,
                aborted_result=self._aborted_result,
                save_waiting_state=self.run_lifecycle.save_waiting_state,
                complete_run=self.run_lifecycle.complete,
                fail_run=self.run_lifecycle.fail,
                pause_run=self.run_lifecycle.pause,
                consume_turn_boundary_inputs=self._consume_turn_boundary_inputs,
                evaluate_turn_completion=partial(
                    self._evaluate_turn_completion,
                    policies=self._turn_policies_for_run(run.run_id),
                ),
                begin_model_round=(
                    self._begin_model_round if self._extension_catalog is not None else None
                ),
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

    def _run_budget_for_run(self, run: RunRecord) -> RunBudgetPolicy:
        raw = run.metadata.get("run_budget")
        if raw is None:
            return self.run_budget
        if not isinstance(raw, dict):
            raise ValueError("run has invalid delegated budget metadata")
        return RunBudgetPolicy.model_validate(raw)

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

    async def submit_host_input(
        self,
        session_input: HostSessionInput,
        *,
        session_id: str | None,
    ) -> RunResult:
        """Submit an OURO-owned synthetic input through the canonical Run boundary."""

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
        attribution: AttributedSessionInput | HostSessionInput | None = None,
    ) -> tuple[SessionRecord, list[EventRecord]]:
        session, queued_input, queue_size = self.queued_inputs.enqueue(
            snapshot=snapshot,
            prompt=prompt,
            title=title,
            mode=mode,
            resource_refs=resource_refs,
            queue_id=(attribution.queue_id if isinstance(attribution, HostSessionInput) else None),
            source=_attributed_input_source(attribution) if attribution is not None else "user",
            host_source_id=(
                attribution.host_source_id if isinstance(attribution, HostSessionInput) else None
            ),
            plugin_id=(
                attribution.plugin_id if isinstance(attribution, AttributedSessionInput) else None
            ),
            contribution_id=(
                attribution.contribution_id
                if isinstance(attribution, AttributedSessionInput)
                else None
            ),
            generation_id=(
                attribution.generation_id
                if isinstance(attribution, AttributedSessionInput)
                else None
            ),
            execution_contract_hash=(
                attribution.execution_contract_hash
                if isinstance(attribution, AttributedSessionInput)
                else None
            ),
            caused_by_run_id=(active_run.run_id if attribution is not None else None),
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
                "host_source_id": queued_input.host_source_id,
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
                    "host_source_id": item.host_source_id,
                    "plugin_id": item.plugin_id,
                    "contribution_id": item.contribution_id,
                },
            )
        return TurnBoundaryInputResult(sequence=sequence, messages=appended)

    async def _evaluate_turn_completion(
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
        policies: TurnPolicyRegistry,
    ) -> TurnCompletionPolicyResult:
        evaluation = await policies.evaluate(
            TurnCompletionCandidate(
                session_id=session.session_id,
                run_id=run.run_id,
                turn_id=turn_id,
                assistant_text=assistant_text,
                assistant_reasoning=assistant_reasoning,
            )
        )
        sequence = await self.event_recorder.record_turn_policy_failures(
            emitted,
            sequence,
            evaluation.failures,
        )
        if evaluation.failures:
            failure = evaluation.failures[-1]
            return TurnCompletionPolicyResult(
                sequence=sequence,
                blocked_reason=(
                    f"turn policy failed: {failure.plugin_id}:{failure.policy_id}: "
                    f"{failure.error_type}"
                ),
            )
        if not evaluation.should_continue:
            return TurnCompletionPolicyResult(sequence=sequence)

        assistant_message = self.run_lifecycle.append_assistant_message(
            session=session,
            run=run,
            text=assistant_text,
            reasoning_content=assistant_reasoning,
            provider_continuation=assistant_continuation,
            sequence=len(messages) + 1,
            turn_id=turn_id,
        )
        controllers = [
            {
                "plugin_id": item.plugin_id,
                "policy_id": item.policy_id,
                "generation_id": item.generation_id,
            }
            for item in evaluation.decisions
        ]
        feedback_message = self.run_lifecycle.append_synthetic_input(
            session=session,
            run=run,
            prompt=evaluation.feedback,
            sequence=len(messages) + 2,
            metadata={
                "session_input_source": "turn_policy",
                "turn_id": turn_id,
                "controllers": controllers,
            },
        )
        sequence = await self.event_recorder.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="turn_policy_continued",
            payload={
                "turn_id": turn_id,
                "policy_ids": [
                    f"{item.plugin_id}/turn_policies/{item.policy_id}"
                    for item in evaluation.decisions
                ],
            },
        )
        return TurnCompletionPolicyResult(
            sequence=sequence,
            messages=[assistant_message, feedback_message],
        )

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
        if item.source != "user":
            if item.resource_refs:
                raise ResourceAttachmentError(
                    "synthetic session inputs cannot attach user resources"
                )
            if item.source == "host":
                metadata["host_source_id"] = item.host_source_id
                metadata["caused_by_run_id"] = item.caused_by_run_id
            else:
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
                    "resolved_by": "user",
                    "resolver_id": None,
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
            batch = await self._tool_calls_for_run(run.run_id).resolve_user_answer(
                batch,
                session=session,
                tool_call_id=waiting_call.request.id,
                answer=answer,
                workspace=session.workspace,
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
                    "resolution": {
                        "kind": "answer",
                        "value": answer,
                        "resolved_by": "user",
                        "resolver_id": None,
                    },
                    "resolved_by": "user",
                    "resolver_id": None,
                },
            )
            if resolved_call.status == "waiting_user" and resolved_call.input_request is not None:
                sequence = await self.event_recorder.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="ask_user_requested",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": batch.turn_id,
                        "tool_call_id": resolved_call.request.id,
                        "tool_name": resolved_call.request.name,
                        "request": resolved_call.input_request.data,
                        "question": resolved_call.input_request.data,
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

    async def resume_agent_wait(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        resolution: AgentWaitResolution,
        result: ToolResult,
    ) -> RunResult:
        """Resolve one durable child join and continue the original parent Run."""

        snapshot = self._load_runtime_snapshot(session_id, recover=True)
        run = find_run(snapshot.runs, run_id)
        if run.status != "waiting_child":
            raise ValueError(f"run is not waiting for a child: {run.status}")
        run_lock = self._acquire_session_run_lock(session_id, run_id)
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run_id}")
        try:
            snapshot = self.store.load_runtime_session(session_id)
            session = snapshot.session
            run = find_run(snapshot.runs, run_id)
            if run.status != "waiting_child" or run.active_tool_batch is None:
                raise ValueError("parent Run no longer has a durable child wait")
            batch = run.active_tool_batch
            waiting_call = next(
                (
                    call
                    for call in batch.calls
                    if call.request.id == tool_call_id
                    and call.status == "waiting_child"
                    and call.agent_wait is not None
                ),
                None,
            )
            if waiting_call is None or waiting_call.agent_wait is None:
                raise ValueError("parent ToolCall no longer has a pending child wait")
            if result.tool_call_id != tool_call_id or result.tool_name != waiting_call.request.name:
                raise ValueError("Agent wait result identity does not match the original ToolCall")
            resolved_wait = waiting_call.agent_wait.model_copy(
                update={"state": "resolved", "resolution": resolution}
            )
            calls = [
                call.model_copy(
                    update={
                        "status": "completed",
                        "agent_wait": resolved_wait,
                        "result": result,
                    }
                )
                if call.request.id == tool_call_id
                else call
                for call in batch.calls
            ]
            batch = batch.model_copy(update={"calls": calls, "updated_at": utc_now()})
            await self._ensure_extension_snapshot(run)
            sequence = self.store.latest_event_sequence(session_id)
            emitted: list[EventRecord] = []
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session_id,
                run_id=run_id,
                event_type="agent_wait_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "tool_call_id": tool_call_id,
                    "wait_id": resolved_wait.wait_id,
                    "resolution": resolution.model_dump(mode="json"),
                },
            )
            resumed = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                emitted=emitted,
                sequence=sequence,
            )
        finally:
            self._release_session_run_lock(run_lock)
        return await self._finalize_run_boundary(resumed, drain_queued_inputs=True)

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
            if handling.status in {"waiting_user", "waiting_child"}:
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
            self._session_control_snapshots.pop(run.run_id, None)
            self._run_agent_catalogs.pop(run.run_id, None)
            self._run_tool_caps.pop(run.run_id, None)
            self._run_tool_refs.pop(run.run_id, None)
            if snapshot is not None:
                snapshot.close()

    def _release_extension_snapshot(self, run_id: str) -> None:
        snapshot = self._extension_snapshots.pop(run_id, None)
        self._session_control_snapshots.pop(run_id, None)
        self._run_agent_catalogs.pop(run_id, None)
        self._run_tool_caps.pop(run_id, None)
        self._run_tool_refs.pop(run_id, None)
        if snapshot is not None:
            snapshot.close()

    def _restore_run_tool_cap(self, run: RunRecord) -> None:
        if "effective_tool_names" not in run.metadata:
            return
        raw = run.metadata.get("effective_tool_names")
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("run has invalid effective tool names")
        self._run_tool_caps[run.run_id] = frozenset(raw)
        raw_refs = run.metadata.get("effective_tool_refs", [])
        if not isinstance(raw_refs, list) or any(not isinstance(item, dict) for item in raw_refs):
            raise ValueError("run has invalid effective tool references")
        self._run_tool_refs[run.run_id] = tuple(
            EffectiveToolRef.model_validate(item) for item in raw_refs
        )

    def _instructions_for_run(self, run: RunRecord) -> str:
        instructions = [self._skill_instructions_for_run(run.run_id)]
        agent_prompt = run.metadata.get("agent_system_prompt")
        if agent_prompt is not None and not isinstance(agent_prompt, str):
            raise ValueError("run has invalid agent system prompt")
        if agent_prompt:
            instructions.append(agent_prompt)
        return "\n\n".join(item.strip() for item in instructions if item.strip())

    def _begin_model_round(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        model_round: int,
    ) -> RunRecord:
        del session
        metadata = dict(run.metadata)
        snapshot = self._extension_snapshots.get(run.run_id)
        metadata["session_control_snapshot"] = self._session_control_snapshot(
            run.session_id,
            run_id=run.run_id,
            model_round=model_round,
            catalog=snapshot,
        )
        update: dict[str, object] = {"metadata": metadata}
        if run.kind == "agent" and snapshot is not None:
            update["continuation"] = self._continuation_checkpoint(run, snapshot)
        updated = run.model_copy(update=update)
        self.store.save_run(updated)
        return updated

    def _session_control_snapshot(
        self,
        session_id: str,
        *,
        run_id: str,
        model_round: int,
        catalog: ExtensionCatalogLease | None = None,
    ) -> dict[str, object]:
        controls = self.store.load_session_record(session_id).controls
        activations = [
            {
                "activation_id": activation.activation_id,
                "controller_id": activation.controller_id,
                "revision": activation.revision,
                "generation_id": activation.generation_id,
                "execution_contract_hash": activation.execution_contract_hash,
            }
            for activation in controls.activations
            if activation.phase == "active"
        ]
        eligible_controller_ids: set[str] = set()
        for activation in controls.activations:
            if activation.phase != "active":
                continue
            if catalog is not None:
                try:
                    controller = catalog.session_controllers.get(activation.controller_id)
                except KeyError:
                    continue
                if controller.execution_contract_hash != activation.execution_contract_hash:
                    continue
            eligible_controller_ids.add(activation.controller_id)
        self._session_control_snapshots[run_id] = frozenset(eligible_controller_ids)
        return {
            "revision": controls.revision,
            "model_round": model_round,
            "activations": activations,
        }

    def _restore_session_control_snapshot(self, run: RunRecord) -> None:
        raw = run.metadata.get("session_control_snapshot", {})
        if not isinstance(raw, dict):
            raise ValueError("run has invalid Session control snapshot metadata")
        activations = raw.get("activations", [])
        if not isinstance(activations, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("controller_id"), str)
            for item in activations
        ):
            raise ValueError("run has invalid Session control activation metadata")
        catalog = self._extension_snapshots.get(run.run_id)
        eligible: set[str] = set()
        for item in activations:
            controller_id = str(item["controller_id"])
            if catalog is not None:
                try:
                    controller = catalog.session_controllers.get(controller_id)
                except KeyError:
                    continue
                if controller.execution_contract_hash != item.get("execution_contract_hash"):
                    continue
            eligible.add(controller_id)
        self._session_control_snapshots[run.run_id] = frozenset(eligible)

    def _attach_extension_snapshot(self, run: RunRecord) -> RunRecord:
        metadata = dict(run.metadata)
        metadata.setdefault("run_budget", self.run_budget.model_dump(mode="json"))
        update: dict[str, object] = {"metadata": metadata}
        if self._extension_catalog is not None:
            snapshot = self._extension_catalog.snapshot()
            self._extension_snapshots[run.run_id] = snapshot
            metadata["session_control_snapshot"] = self._session_control_snapshot(
                run.session_id,
                run_id=run.run_id,
                model_round=0,
                catalog=snapshot,
            )
            metadata["extension_catalog_id"] = snapshot.catalog_id
            metadata["extension_generations"] = [
                generation.model_dump(mode="json", exclude_none=True)
                for generation in snapshot.generations
            ]
            if run.kind == "agent":
                update["continuation"] = self._continuation_checkpoint(run, snapshot)
        session = self.store.load_session_record(run.session_id)
        if (
            run.kind == "agent"
            and session.session_kind == "primary"
            and self._agent_definitions is not None
        ):
            catalog = self._agent_definitions.catalog_snapshot(self._tools_for_run(run.run_id))
            update["agent_catalog"] = catalog
            self._run_agent_catalogs[run.run_id] = catalog
        return run.model_copy(update=update)

    def _continuation_checkpoint(
        self,
        run: RunRecord,
        snapshot: ExtensionCatalogLease,
    ) -> RunContinuationCheckpoint | None:
        generations = {generation.generation_id: generation for generation in snapshot.generations}
        activations = {
            activation.controller_id: activation
            for activation in self.store.load_session_record(run.session_id).controls.activations
            if activation.phase == "active"
        }
        controllers: list[RunContinuationControllerRef] = []
        for registration in self._continuations_for_run(run.run_id).registered():
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
            activation = (
                activations.get(registration.active_when_controller_id)
                if registration.active_when_controller_id is not None
                else None
            )
            if registration.active_when_controller_id is not None and activation is None:
                continue
            assert generation.execution_contract_hash is not None
            controllers.append(
                RunContinuationControllerRef(
                    plugin_id=registration.plugin_id,
                    policy_id=registration.policy_id,
                    version=generation.version,
                    plugin_class=generation.plugin_class,
                    release_id=generation.release_id,
                    package_digest=generation.package_digest,
                    builtin_contract_hash=generation.builtin_contract_hash,
                    generation_id=generation.generation_id,
                    contract_hash=contract_hash,
                    execution_contract_hash=generation.execution_contract_hash,
                    active_when_controller_id=registration.active_when_controller_id,
                    activation_id=(activation.activation_id if activation is not None else None),
                )
            )
        if not controllers:
            return None
        if run.continuation is None or run.continuation.state == "resolved":
            return RunContinuationCheckpoint(controllers=controllers)
        return run.continuation.model_copy(update={"controllers": controllers})

    async def _ensure_extension_snapshot(self, run: RunRecord) -> None:
        self._restore_run_tool_cap(run)
        if run.agent_catalog is not None:
            self._run_agent_catalogs[run.run_id] = run.agent_catalog
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
        self._restore_session_control_snapshot(run)


def _attributed_input_source(
    attribution: AttributedSessionInput | HostSessionInput,
) -> QueuedInputSource:
    return "host" if isinstance(attribution, HostSessionInput) else "plugin"


def _attributed_input_initiator(
    attribution: AttributedSessionInput | HostSessionInput,
) -> str:
    if isinstance(attribution, HostSessionInput):
        return attribution.host_source_id
    return attribution.plugin_id


def _queued_input_initiator(item: QueuedInput | None) -> str | None:
    if item is None:
        return None
    if item.source == "host":
        return item.host_source_id
    if item.source == "plugin":
        return item.continuation_decision_id or item.plugin_id
    return None


def _session_input_event_type(source: QueuedInputSource) -> str:
    if source == "host":
        return "host_message_added"
    if source == "plugin":
        return "plugin_message_added"
    return "user_message_added"


def _title_from_prompt(prompt: str) -> str:
    stripped = " ".join(prompt.split())
    return stripped[:60] if stripped else "New session"
