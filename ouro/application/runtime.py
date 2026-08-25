from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import JsonValue

from ouro.application.compaction import (
    ContextPreparationService,
    LLMSummaryCompactionStrategy,
)
from ouro.application.dto import Diagnostic
from ouro.application.events import RuntimeEventRecorder, RuntimeEventSink
from ouro.application.models import (
    AvailableModel,
    FixedRuntimeAdapterCatalog,
    ModelReasoningState,
    RuntimeAdapterCatalog,
    close_runtime_adapters,
)
from ouro.application.queued_inputs import (
    QueuedInput,
    QueuedInputMode,
    QueuedInputQueue,
    QueuedInputSource,
    QueuedUserInputView,
)
from ouro.application.recovery import RunRecoveryService
from ouro.application.run_continuations import (
    RunContinuationService,
    RunContinuationSettlement,
)
from ouro.application.run_ownership import RunExecutionOwnership
from ouro.application.runs import (
    ACTIVE_RUN_STATUSES,
    RunLifecycle,
    abort_run_state,
    find_active_run,
    find_run,
    maybe_active_run,
    request_run_abort_state,
)
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.session_recovery import SessionRecoveryService
from ouro.application.sessions import SessionService
from ouro.application.skill_activations import SkillActivationService, SkillInvocationRequest
from ouro.application.supervisor_models import HostInputReceipt
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.application.usage import UsageService
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentCatalogSnapshot,
    AgentWaitResolution,
    DelegatedConstraints,
    EffectiveToolRef,
)
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import (
    CompactionAbortedError,
    CompactionCommandResult,
    CompactionStrategyRegistry,
)
from ouro.core.context import ContextBuilder, ContextMetrics
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import (
    CompactionError,
    ConfigError,
    PersistenceError,
    PromptRejectedError,
    ResourceAttachmentError,
    SessionBusyError,
)
from ouro.core.events import EventRecord
from ouro.core.inner_loop import (
    InnerAgentLoop,
    InnerLoopContext,
    InnerLoopPorts,
    InnerLoopRunBoundaries,
    InnerLoopTurnBoundaries,
    TurnBoundaryInputResult,
    TurnCompletionPolicyResult,
)
from ouro.core.interactions import (
    SESSION_INTERACTION_MODES,
    SessionInteractionMode,
)
from ouro.core.message_roles import MessageRole
from ouro.core.messages import (
    MessageRecord,
    ModelResponseRecord,
    ProviderContinuationArtifact,
    SkillActivationContent,
)
from ouro.core.observers import (
    Observation,
    ObserverRegistry,
    RunFinishedObservation,
    RunOutcomeView,
)
from ouro.core.prompt_policies import (
    PromptAttachmentMetadata,
    PromptPolicyCall,
    PromptPolicyRegistry,
    PromptSkillActivationMetadata,
)
from ouro.core.reasoning import ReasoningRequest
from ouro.core.redaction import SensitiveValueRedactor, redact_text
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    AttributedSessionInput,
    AttributedSessionInputBatch,
    DelegatedTaskInput,
    HostSessionInput,
    SessionControlActivation,
    SessionControlTransitionResult,
    SessionInputRequest,
    SessionInputRole,
)
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunRecord,
    SessionRecord,
)
from ouro.core.tool_batch_transitions import resolve_tool_approval, tool_batch_suspension
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.tools import PermissionMode, ToolDefinition, tool_definition_contract_hash
from ouro.core.turn_policies import TurnCompletionCandidate, TurnPolicyRegistry
from ouro.prompts import DEFAULT_SYSTEM_PROMPT, render_agent_skills_prompt
from ouro.sessions import RuntimeSessionSnapshot, SessionStore
from ouro.skills import SkillMetadata
from ouro.tools import ToolExecutor, ToolRegistry, ToolResult
from ouro.tools.builtin_agents import agent_control_tools

if TYPE_CHECKING:
    from ouro.application.agent_definitions import AgentDefinitionService
    from ouro.application.resources import ResourceService
    from ouro.application.tool_calls import AgentControlExecutor
    from ouro.plugins.catalog import ExtensionCatalogLease
    from ouro.plugins.runtime import PluginRuntimeResource


class _QueuedInputSelectionChanged(RuntimeError):
    """A queued-input snapshot changed before its durable consumption boundary."""


def _append_system_prompt(system_prompt: str, fragment: str) -> str:
    if not fragment:
        return system_prompt
    if not system_prompt:
        return fragment
    if system_prompt.endswith("\n\n"):
        separator = ""
    elif system_prompt.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{system_prompt}{separator}{fragment}"


class RuntimeService:
    def __init__(
        self,
        *,
        store: SessionStore,
        adapter: RuntimeAdapter,
        model_catalog: RuntimeAdapterCatalog | None = None,
        tools: ToolRegistry,
        observers: ObserverRegistry | None = None,
        context_providers: ContextProviderRegistry | None = None,
        context_builder: ContextBuilder | None = None,
        run_budget: RunBudgetPolicy | None = None,
        system_prompt: str | None = None,
        additional_system_prompt: str | None = None,
        event_sink: RuntimeEventSink | None = None,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
        plugin_permission_overrides: Mapping[str, PermissionMode] | None = None,
        tool_execution_config: ToolExecutionConfig | None = None,
        compaction_config: CompactionConfig | None = None,
        compaction_strategies: CompactionStrategyRegistry | None = None,
        compaction_adapter: RuntimeAdapter | None = None,
        compaction_adapter_error: str | None = None,
        continuations: RunContinuationRegistry | None = None,
        prompt_policies: PromptPolicyRegistry | None = None,
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
        self.skill_activations = SkillActivationService()
        self.default_workspace = (
            str(Path(default_workspace).expanduser().resolve())
            if default_workspace is not None
            else None
        )
        self._permission_overrides = dict(permission_overrides or {})
        self._plugin_permission_overrides = dict(plugin_permission_overrides or {})
        self._tool_execution_config = tool_execution_config or ToolExecutionConfig()
        self.observers = observers or ObserverRegistry()
        self.context_providers = context_providers or ContextProviderRegistry()
        self.continuations = continuations or RunContinuationRegistry()
        self.prompt_policies = prompt_policies or PromptPolicyRegistry()
        self.tool_policies = tool_policies or ToolPolicyRegistry()
        self.turn_policies = turn_policies or TurnPolicyRegistry()
        self.event_recorder = RuntimeEventRecorder(
            store=store,
            observers=self.observers,
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
        self.additional_system_prompt = (additional_system_prompt or "").strip()
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
            context_providers=self.context_providers,
            events=self.event_recorder,
            context_builder=self.context_builder,
            strategies=self.compaction_strategies,
            config=self.compaction_config,
            system_prompt=_append_system_prompt(
                self.system_prompt,
                self.additional_system_prompt,
            ),
            append_context_message=self.run_lifecycle.append_input_message,
            project_model_messages=(
                resources.project_model_messages if resources is not None else None
            ),
        )
        self.run_ownership = RunExecutionOwnership(store)
        self._extension_catalog: PluginRuntimeResource | None = None
        self._extensions_bound = False
        self._extension_snapshots: dict[str, ExtensionCatalogLease] = {}
        self._queued_extension_snapshots: dict[str, ExtensionCatalogLease] = {}
        self._session_control_snapshots: dict[str, frozenset[str]] = {}
        self._agent_definitions: AgentDefinitionService | None = None
        self._agent_controls: AgentControlExecutor | None = None
        self._run_agent_catalogs: dict[str, AgentCatalogSnapshot] = {}
        self._run_tool_caps: dict[str, frozenset[str]] = {}
        self._run_tool_refs: dict[str, tuple[EffectiveToolRef, ...]] = {}
        self._run_delegated_constraints: dict[str, DelegatedConstraints] = {}
        self._run_tool_coordinators: dict[str, ToolCallCoordinator] = {}

    def tool_permission_overrides(self) -> dict[str, PermissionMode]:
        """Return the permission policy currently published to tool execution."""

        return dict(self._permission_overrides)

    def plugin_permission_overrides(self) -> dict[str, PermissionMode]:
        """Return plugin-wide permission overrides published to tool execution."""

        return dict(self._plugin_permission_overrides)

    def replace_tool_permission_overrides(
        self,
        permission_overrides: Mapping[str, PermissionMode],
        plugin_permission_overrides: Mapping[str, PermissionMode] | None = None,
    ) -> None:
        """Apply a permission policy to future checks, including active Runs."""

        self._validate_permission_overrides("tool", permission_overrides)
        if plugin_permission_overrides is not None:
            self._validate_permission_overrides("plugin", plugin_permission_overrides)
        published_tools = dict(permission_overrides)
        published_plugins = (
            dict(plugin_permission_overrides)
            if plugin_permission_overrides is not None
            else dict(self._plugin_permission_overrides)
        )
        self._permission_overrides = published_tools
        self._plugin_permission_overrides = published_plugins
        for coordinator in self._run_tool_coordinators.values():
            coordinator.executor.replace_permission_overrides(
                published_tools,
                published_plugins,
            )

    @staticmethod
    def _validate_permission_overrides(
        scope: str,
        overrides: Mapping[str, PermissionMode],
    ) -> None:
        invalid = {
            name: mode
            for name, mode in overrides.items()
            if not name or mode not in ("auto", "approval", "deny")
        }
        if invalid:
            raise ValueError(f"invalid {scope} permission overrides: {invalid}")

    def bind_extensions(
        self,
        *,
        catalog: PluginRuntimeResource | None = None,
        agent_definitions: AgentDefinitionService | None = None,
        agent_controls: AgentControlExecutor | None = None,
    ) -> None:
        """Bind one complete Host extension assembly before executing Runs."""

        if self._extensions_bound:
            raise RuntimeError("runtime extensions are already bound")
        if catalog is None and agent_definitions is None and agent_controls is None:
            raise ValueError("runtime extension binding cannot be empty")
        if agent_controls is not None and agent_definitions is None:
            raise ValueError("Agent controls require the matching AgentDefinition service")
        self._extension_catalog = catalog
        self._agent_definitions = agent_definitions
        self._agent_controls = agent_controls
        self._extensions_bound = True
        if catalog is not None:
            self.event_recorder.set_observer_registry_resolver(self._observers_for_observation)

    def _observers_for_observation(
        self,
        observation: Observation,
    ) -> tuple[ObserverRegistry, Callable[[], None]]:
        run_id = observation.run_id
        snapshot = self._extension_snapshots.get(run_id) if run_id is not None else None
        if snapshot is not None:
            retained = snapshot.fork()
        elif self._extension_catalog is not None:
            retained = self._extension_catalog.snapshot()
        else:
            return self.observers.scoped(lambda: set()), lambda: None
        return (
            retained.observers.scoped(
                lambda: self._active_controller_ids_for_run(run_id) if run_id else set()
            ),
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

    def _context_providers_for_run(self, run_id: str) -> ContextProviderRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.context_providers if snapshot is not None else self.context_providers
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _prompt_policies_for_run(self, run_id: str) -> PromptPolicyRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.prompt_policies if snapshot is not None else self.prompt_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _skills_for_run(self, run_id: str) -> tuple[SkillMetadata, ...]:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.skills if snapshot is not None else self.skills

    def _skill_instructions_for_run(self, run_id: str) -> str:
        return render_agent_skills_prompt(self._skills_for_run(run_id))

    def _resolve_skill_activation_for_run(
        self,
        run_id: str,
        request: SkillInvocationRequest,
    ) -> SkillActivationContent:
        return self.skill_activations.resolve_user_activation(
            self._skills_for_run(run_id),
            request,
        )

    def _compaction_for_run(self, run_id: str) -> CompactionStrategyRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        return snapshot.compaction if snapshot is not None else self.compaction_strategies

    def _continuations_for_run(
        self,
        run_id: str,
        *,
        active_only: bool = True,
    ) -> RunContinuationRegistry:
        snapshot = self._extension_snapshots.get(run_id)
        registry = snapshot.continuations if snapshot is not None else self.continuations
        if not active_only:
            return registry
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
        existing = self._run_tool_coordinators.get(run_id)
        if existing is not None:
            return existing
        tools = self._tools_for_run(run_id)
        delegated = self._run_delegated_constraints.get(run_id)
        execution_config = self._tool_execution_config
        if delegated is not None and not delegated.allow_external_paths:
            execution_config = execution_config.model_copy(update={"workspace_only": True})
        coordinator = ToolCallCoordinator(
            executor=ToolExecutor(
                tools,
                permission_overrides=self._permission_overrides,
                plugin_permission_overrides=self._plugin_permission_overrides,
                permission_floors={
                    reference.public_name: reference.minimum_permission_mode
                    for reference in self._run_tool_refs.get(run_id, ())
                },
            ),
            registry=tools,
            events=self.event_recorder,
            runs=self.run_lifecycle,
            execution_config=execution_config,
            default_workspace=self.default_workspace,
            artifact_root=(
                str(self.environment.artifact_root)
                if self.environment.artifact_root is not None
                else None
            ),
            is_aborted=self.run_ownership.is_aborted,
            tool_policies=self._tool_policies_for_run(run_id),
            session_controls=self.session_controls,
            session_controllers=self._session_controllers_for_run(run_id),
            agent_controls=self._agent_controls,
        )
        self._run_tool_coordinators[run_id] = coordinator
        return coordinator

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        self.event_recorder.add_event_sink(event_sink)

    async def drain_events(self) -> None:
        """Wait until every event observer has processed the published prefix."""

        await self.event_recorder.drain()

    def session_control_activations(
        self,
        session_id: str,
    ) -> tuple[SessionControlActivation, ...]:
        session = self.store.load_session_record(session_id)
        return tuple(item.model_copy(deep=True) for item in session.controls.activations)

    def reconcile_interrupted_session_controls(
        self,
        session_id: str | None = None,
    ) -> tuple[str, ...]:
        """Reconcile a target Session, or every Session for explicit maintenance."""

        reconciled: list[str] = []
        session_ids = (
            (session_id,)
            if session_id is not None
            else tuple(entry.session_id for entry in self.sessions.catalog().entries)
        )
        for candidate_id in session_ids:
            if self.session_controls.reconcile_interrupted(candidate_id):
                reconciled.append(candidate_id)
        return tuple(reconciled)

    async def reconcile_pending_run_continuations(
        self,
        session_id: str | None = None,
    ) -> tuple[str, ...]:
        """Replay unfinished settlements for a target or explicit full maintenance."""

        settled_run_ids: list[str] = []
        session_ids = (
            (session_id,)
            if session_id is not None
            else tuple(entry.session_id for entry in self.store.list_sessions())
        )
        for candidate_id in session_ids:
            for run in self.store.load_runs(candidate_id):
                checkpoint = run.continuation
                if (
                    run.kind != "agent"
                    or run.status in ACTIVE_RUN_STATUSES
                    or checkpoint is None
                    or checkpoint.state == "resolved"
                ):
                    continue
                snapshot_error: str | None = None
                try:
                    await self._ensure_extension_snapshot(run)
                except Exception as exc:  # noqa: BLE001 - recovery must fail closed
                    snapshot_error = type(exc).__name__
                try:
                    settlement = await RunContinuationService(
                        store=self.store,
                        registry=self._continuations_for_run(run.run_id, active_only=False),
                        queue=self.queued_inputs,
                        session_controls=self.session_controls,
                        session_controllers=self._session_controllers_for_run(run.run_id),
                    ).settle(
                        session_id=run.session_id,
                        run_id=run.run_id,
                    )
                    if not settlement.settled:
                        continue
                    settled_run_ids.append(run.run_id)
                    diagnostics = (
                        (f"extension snapshot recovery failed: {snapshot_error}",)
                        if snapshot_error is not None
                        else ()
                    )
                    await self._emit_continuation_resolution(
                        settlement,
                        [],
                        extra_diagnostics=diagnostics,
                    )
                finally:
                    self._release_extension_snapshot(run.run_id)
        return tuple(settled_run_ids)

    async def reconcile_execution_session(
        self,
        session_id: str,
    ) -> RuntimeSessionSnapshot:
        """Recover execution state for exactly one Session."""

        self.reconcile_interrupted_session_controls(session_id)
        snapshot = self.session_recovery.recover_runtime(session_id)
        settled = await self.reconcile_pending_run_continuations(session_id)
        return self.store.load_runtime_session(session_id) if settled else snapshot

    async def reconcile_session(self, session_id: str) -> tuple[Diagnostic, ...]:
        """Recover only the target Session and its bounded Agent aggregate."""

        _, diagnostics = await self._reconcile_session(session_id)
        return diagnostics

    async def _reconcile_session(
        self,
        session_id: str,
    ) -> tuple[RuntimeSessionSnapshot, tuple[Diagnostic, ...]]:
        """Return the post-reconciliation target snapshot for execution."""

        session = self.store.load_session_record(session_id)
        if self._agent_controls is None:
            return await self.reconcile_execution_session(session_id), ()

        diagnostics: list[Diagnostic] = []
        root_session_id = session.agent_root_session_id
        target_snapshot: RuntimeSessionSnapshot | None = None
        for candidate_id in self._agent_controls.recovery_session_ids(session):
            try:
                recovered = await self.reconcile_execution_session(candidate_id)
                if candidate_id == session_id:
                    target_snapshot = recovered
            except FileNotFoundError:
                if candidate_id in {root_session_id, session_id}:
                    raise
            except PersistenceError as exc:
                if candidate_id in {root_session_id, session_id}:
                    raise
                diagnostics.append(
                    self._execution_recovery_diagnostic(
                        root_session_id=root_session_id,
                        session_id=candidate_id,
                        error=exc,
                    )
                )
        diagnostics.extend(await self._agent_controls.reconcile_session(session))
        if target_snapshot is None:
            raise FileNotFoundError(session_id)
        return self.store.load_runtime_session(session_id), tuple(diagnostics)

    @staticmethod
    def _execution_recovery_diagnostic(
        *,
        root_session_id: str,
        session_id: str,
        error: PersistenceError,
    ) -> Diagnostic:
        return Diagnostic(
            code="session_runtime_incompatible",
            severity="warning",
            component="runtime.recovery",
            message=(
                f"Session {session_id} was excluded from Agent aggregate recovery because "
                f"its persisted execution data is incompatible: {error}"
            ),
            session_id=session_id,
            recoverable=True,
            suggested_action="Inspect the retained Session data before an explicit retry.",
            metadata={
                "root_session_id": root_session_id,
                "error_type": type(error).__name__,
            },
        )

    @staticmethod
    def _raise_incompatible_target(
        session: SessionRecord,
        diagnostics: tuple[Diagnostic, ...],
    ) -> None:
        affected_session_ids = {session.session_id, session.agent_root_session_id}
        incompatible = next(
            (
                diagnostic
                for diagnostic in diagnostics
                if diagnostic.code == "session_runtime_incompatible"
                and diagnostic.session_id in affected_session_ids
            ),
            None,
        )
        if incompatible is not None:
            raise PersistenceError(incompatible.message)

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
        for snapshot in self._queued_extension_snapshots.values():
            snapshot.close()
        self._queued_extension_snapshots.clear()
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

    def available_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Return the tools published by the current Host and plugin catalog."""

        if self._extension_catalog is None:
            definitions = self.tools.definitions()
            return tuple(definition.model_copy(deep=True) for definition in definitions)
        lease = self._extension_catalog.snapshot()
        try:
            return tuple(
                definition.model_copy(deep=True) for definition in lease.tools.definitions()
            )
        finally:
            lease.close()

    def available_skills(self) -> tuple[SkillMetadata, ...]:
        """Return the current Host view without exposing discovery to interface layers."""

        if self._extension_catalog is None:
            return self.skill_activations.list_for_host(self.skills)
        lease = self._extension_catalog.snapshot()
        try:
            return self.skill_activations.list_for_host(lease.skills)
        finally:
            lease.close()

    def pending_user_inputs(self, session_id: str) -> tuple[QueuedUserInputView, ...]:
        """Return direct user inputs still waiting at a Run or model-turn boundary."""

        session = self.store.load_session_record(session_id)
        return tuple(
            _queued_user_input_view(item)
            for item in self.queued_inputs.eligible_session(session)
            if item.source == "user"
        )

    async def cancel_pending_user_input(
        self,
        session_id: str,
        queue_id: str,
    ) -> QueuedUserInputView | None:
        """Cancel one direct user input if it has not crossed a durable boundary."""

        cancelled: QueuedInput | None = None
        with self.store.session_mutation(session_id):
            snapshot = self.store.load_runtime_session(session_id)
            pending = self.queued_inputs.find_session(snapshot.session, queue_id)
            if pending is None or pending.source != "user":
                return None
            if self._queued_input_is_durable(snapshot.messages, queue_id):
                self.queued_inputs.acknowledge(snapshot, queue_id)
            else:
                cancelled = self.queued_inputs.cancel_user_input(
                    session_id=session_id,
                    queue_id=queue_id,
                )
        self._release_queued_extension_snapshot(queue_id)
        if cancelled is None:
            return None
        session = self.store.load_session_record(session_id)
        await self.event_recorder.publish(
            session_id=session_id,
            run_id=session.active_run_id,
            event_type="queued_input_cancelled",
            payload={
                "queue_id": cancelled.queue_id,
                "mode": cancelled.mode,
                "source": cancelled.source,
            },
        )
        return _queued_user_input_view(cancelled)

    def current_model(self, session_id: str | None = None) -> AvailableModel:
        if session_id is None:
            return self.models.default_model()
        session = self.store.load_session_record(session_id)
        return self.models.resolve(session.current_model or self.models.default_model().key)

    def session_context_metrics(self, session_id: str) -> ContextMetrics | None:
        """Project existing context usage against the Session's selected model limits."""

        metrics = self.sessions.effective_context_metrics(session_id)
        if metrics is None:
            return None
        model = self.current_model(session_id)
        return metrics.with_model_limits(
            context_window_tokens=model.context_window,
            reserved_output_tokens=model.max_output_tokens,
        )

    def resolve_model(self, selector: str) -> AvailableModel:
        """Resolve and validate a model route before it is attached to a session."""

        model = self.models.resolve(selector)
        self.models.adapter_for(model.key)
        return model

    def model_reasoning(
        self,
        selector: str,
        request: ReasoningRequest | None = None,
    ) -> ModelReasoningState:
        """Resolve one draft reasoning intent against a selectable model route."""

        model = self.resolve_model(selector)
        return self._model_reasoning_state(model, request or ReasoningRequest())

    def session_reasoning(self, session_id: str) -> ModelReasoningState:
        """Return the persisted and effective reasoning state for one Session."""

        session = self.store.load_session_record(session_id)
        model = self.models.resolve(session.current_model or self.models.default_model().key)
        return self._model_reasoning_state(model, session.reasoning_request)

    def create_session(
        self,
        *,
        title: str,
        _session_id: str | None = None,
        initial_model: str | None = None,
        reasoning_request: ReasoningRequest | None = None,
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
        selected_reasoning = reasoning_request or ReasoningRequest()
        self._model_reasoning_state(selected_model, selected_reasoning)
        created = self.sessions.create(
            session_id=_session_id,
            title=title,
            current_model=selected_model.key,
            reasoning_request=selected_reasoning,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            workspace=self.default_workspace,
        )
        return self.store.load_session_record(created.session.session_id)

    def switch_session_model(
        self,
        session_id: str,
        selector: str,
        *,
        reasoning_request: ReasoningRequest | None = None,
    ) -> SessionRecord:
        """Atomically persist an executable model route and reasoning pair.

        Omitting ``reasoning_request`` preserves the Session intent and therefore
        rejects an incompatible route. Passing an explicit request replaces both
        settings in one mutation, which lets interfaces reset or stage reasoning
        when the user deliberately changes model capabilities.
        """

        model = self.resolve_model(selector)

        def validate(session: SessionRecord) -> None:
            selected_reasoning = (
                session.reasoning_request if reasoning_request is None else reasoning_request
            )
            self._model_reasoning_state(model, selected_reasoning)

        updates: dict[str, object] = {"current_model": model.key}
        if reasoning_request is not None:
            updates["reasoning_request"] = reasoning_request

        return self._update_live_session_settings(
            session_id,
            updates,
            validate=validate,
        )

    def set_session_reasoning(
        self,
        session_id: str,
        reasoning_request: ReasoningRequest,
    ) -> SessionRecord:
        """Persist reasoning intent for the next model request boundary."""

        def validate(session: SessionRecord) -> None:
            model = self.models.resolve(
                session.current_model or self.models.default_model().key
            )
            self._model_reasoning_state(model, reasoning_request)

        return self._update_live_session_settings(
            session_id,
            {"reasoning_request": reasoning_request},
            validate=validate,
        )

    def _model_reasoning_state(
        self,
        model: AvailableModel,
        request: ReasoningRequest,
    ) -> ModelReasoningState:
        adapter = self.models.adapter_for(model.key)
        bound = self._bind_reasoning(adapter, request, model.key)
        return ModelReasoningState(
            model=model,
            capability=adapter.model.reasoning,
            requested=request,
            effective=bound.model.reasoning_resolution.effective,
        )

    @staticmethod
    def _bind_reasoning(
        adapter: RuntimeAdapter,
        request: ReasoningRequest,
        model_key: str,
    ) -> RuntimeAdapter:
        # A default Session intent preserves an explicit request already bound by
        # a one-shot runtime composition; non-default Session intent takes precedence.
        selected = adapter.model.reasoning_request if request == ReasoningRequest() else request
        try:
            return adapter.with_reasoning(selected)
        except ValueError as exc:
            raise ConfigError(f"reasoning setting is not supported by {model_key}: {exc}") from exc

    def set_session_approval_mode(
        self,
        session_id: str,
        approval_mode: SessionApprovalMode,
    ) -> SessionRecord:
        """Persist the approval policy used by future tool calls in a session."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        return self._update_live_session_settings(
            session_id,
            {"approval_mode": approval_mode},
        )

    def set_session_interaction_mode(
        self,
        session_id: str,
        interaction_mode: SessionInteractionMode,
    ) -> SessionRecord:
        """Persist how future tool input requests are resolved in a Session."""

        if interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        return self._update_live_session_settings(
            session_id,
            {"interaction_mode": interaction_mode},
        )

    def set_session_policies(
        self,
        session_id: str,
        *,
        approval_mode: SessionApprovalMode,
        interaction_mode: SessionInteractionMode,
    ) -> SessionRecord:
        """Persist policies used by the Session's next tool interaction atomically."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        return self._update_live_session_settings(
            session_id,
            {
                "approval_mode": approval_mode,
                "interaction_mode": interaction_mode,
            },
        )

    def set_session_afk_mode(self, session_id: str, *, enabled: bool) -> SessionRecord:
        """Apply or clear the AFK policy preset for future Session interactions."""

        return self.set_session_policies(
            session_id,
            approval_mode="auto" if enabled else "inherit",
            interaction_mode="unattended" if enabled else "interactive",
        )

    def _update_live_session_settings(
        self,
        session_id: str,
        changes: Mapping[str, object],
        *,
        validate: Callable[[SessionRecord], None] | None = None,
    ) -> SessionRecord:
        """Update request-bound Session settings without contending for Run ownership."""

        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if validate is not None:
                validate(session)
            now = utc_now()
            updated = session.model_copy(
                update={
                    **changes,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            self.store.save_session(updated)
        self.store.refresh_catalog(session_id)
        return updated

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
    ) -> tuple[SessionRecord, RuntimeAdapter]:
        with self.store.session_mutation(session.session_id):
            latest = self.store.load_session_record(session.session_id)
            selection = self.models.resolve(latest.current_model or self.models.default_model().key)
            adapter = self._bind_reasoning(
                self.models.adapter_for(selection.key),
                latest.reasoning_request,
                selection.key,
            )
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
        self.store.refresh_catalog(session.session_id)
        return session, adapter

    def _adapter_for_session_id(self, session_id: str) -> RuntimeAdapter:
        session = self.store.load_session_record(session_id)
        _, adapter = self._resolve_session_adapter(session)
        return adapter

    async def run(
        self,
        prompt: str,
        *,
        skill_invocation: SkillInvocationRequest | None = None,
        session_id: str | None = None,
        title: str | None = None,
        initial_model: str | None = None,
        initial_reasoning_request: ReasoningRequest | None = None,
        queue_mode: QueuedInputMode = "follow_up",
        approval_mode: SessionApprovalMode | None = None,
        interaction_mode: SessionInteractionMode | None = None,
        resource_refs: list[str] | tuple[str, ...] | None = None,
        _drain_queued_inputs: bool = True,
        _queued_inputs: list[QueuedInput] | None = None,
        _input_attribution: (
            AttributedSessionInput | HostSessionInput | DelegatedTaskInput | None
        ) = None,
        _prepared_run: RunRecord | None = None,
    ) -> RunResult:
        if approval_mode is not None and approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if interaction_mode is not None and interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        if initial_model is not None and session_id is not None:
            raise ValueError("initial_model can only be set when creating a session")
        if initial_reasoning_request is not None and session_id is not None:
            raise ValueError("initial_reasoning_request can only be set when creating a session")
        if skill_invocation is not None:
            if prompt and prompt != skill_invocation.arguments:
                raise ValueError("prompt must match the skill invocation arguments")
            prompt = skill_invocation.arguments
        queued_skill_activation = any(
            item.skill_activation is not None for item in (_queued_inputs or ())
        )
        if not prompt.strip() and skill_invocation is None and not queued_skill_activation:
            raise ValueError("run input requires a prompt or skill invocation")
        if skill_invocation is not None and _input_attribution is not None:
            raise ValueError("skill invocation is only valid on direct user input")
        if skill_invocation is not None and queue_mode != "follow_up":
            raise ValueError("skill invocation cannot steer an active run")
        if isinstance(_input_attribution, DelegatedTaskInput) and _prepared_run is None:
            raise ValueError("delegated task input requires a prepared child run")
        if _prepared_run is not None:
            if session_id is None or _prepared_run.session_id != session_id:
                raise ValueError("prepared run must target the supplied existing session")
            if _prepared_run.status != "queued":
                raise ValueError("prepared run must be queued")
            if not isinstance(_input_attribution, HostSessionInput | DelegatedTaskInput):
                raise ValueError("prepared run requires Host or delegated-task attribution")
            if isinstance(_input_attribution, DelegatedTaskInput) and (
                _prepared_run.metadata.get("delegation_id") != _input_attribution.delegation_id
            ):
                raise ValueError("prepared child run does not match delegated task identity")
        input_resource_refs = tuple(dict.fromkeys(resource_refs or ()))
        if input_resource_refs and session_id is None:
            raise ResourceAttachmentError(
                "resource attachments require an existing session; import the resource "
                "and pass its session with session_id"
            )
        preapproved_new_run: RunRecord | None = None
        resolved_skill_activation: SkillActivationContent | None = None
        if session_id is None:
            if _input_attribution is None:
                selected_model = (
                    self.resolve_model(initial_model)
                    if initial_model is not None
                    else self.models.default_model()
                )
                selected_reasoning = initial_reasoning_request or ReasoningRequest()
                self._model_reasoning_state(selected_model, selected_reasoning)
                candidate_session = SessionRecord(
                    title=title or _title_from_input(prompt, skill_invocation),
                    workspace=self.default_workspace,
                    current_model=selected_model.key,
                    reasoning_request=selected_reasoning,
                    approval_mode=approval_mode or "inherit",
                    interaction_mode=interaction_mode or "interactive",
                )
                preapproved_new_run = RunRecord(
                    session_id=candidate_session.session_id,
                    status="running",
                    origin="user",
                    trigger="user",
                )
                if self._extension_catalog is not None:
                    self._extension_snapshots[preapproved_new_run.run_id] = (
                        self._extension_catalog.snapshot()
                    )
                try:
                    if skill_invocation is not None:
                        resolved_skill_activation = self._resolve_skill_activation_for_run(
                            preapproved_new_run.run_id,
                            skill_invocation,
                        )
                    await self._evaluate_prompt_policies(
                        session=candidate_session,
                        run=preapproved_new_run,
                        inputs=[
                            QueuedInput(
                                queue_id=new_id("queued"),
                                prompt=prompt,
                                title=title,
                                mode="follow_up",
                                queued_at=utc_now(),
                                source="user",
                                role="user",
                                scaffold=False,
                                semantic_tag=None,
                                skill_activation=resolved_skill_activation,
                            )
                        ],
                    )
                    session = self.create_session(
                        title=candidate_session.title,
                        _session_id=candidate_session.session_id,
                        initial_model=selected_model.key,
                        reasoning_request=candidate_session.reasoning_request,
                        approval_mode=candidate_session.approval_mode,
                        interaction_mode=candidate_session.interaction_mode,
                    )
                except BaseException:
                    self._release_extension_snapshot(preapproved_new_run.run_id)
                    raise
            else:
                session = self.create_session(
                    title=title or _title_from_input(prompt, skill_invocation),
                    initial_model=initial_model,
                    reasoning_request=initial_reasoning_request,
                    approval_mode=approval_mode or "inherit",
                    interaction_mode=interaction_mode or "interactive",
                )
            snapshot = RuntimeSessionSnapshot(session=session)
        else:
            should_reconcile = _prepared_run is None and not self.run_ownership.is_locked(
                session_id
            )
            if should_reconcile:
                snapshot, reconciliation_diagnostics = await self._reconcile_session(session_id)
                self._raise_incompatible_target(
                    snapshot.session,
                    reconciliation_diagnostics,
                )
            else:
                snapshot = self._load_runtime_snapshot(
                    session_id,
                    recover=False,
                )
            session = snapshot.session
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(
                session,
                input_resource_refs,
                message_role=_input_role(_input_attribution),
            )
            if active_run is not None and (
                _prepared_run is None or active_run.run_id != _prepared_run.run_id
            ):
                self._validate_requested_session_policies(
                    session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
                )
                queued_session, _, queued_events = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    skill_invocation=skill_invocation,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attribution=_input_attribution,
                )
                return RunResult(session=queued_session, run=active_run, events=queued_events)
        if isinstance(_input_attribution, DelegatedTaskInput) and (
            session.session_kind != "subagent"
            or session.delegation_id != _input_attribution.delegation_id
        ):
            raise ValueError("delegated task input must target its child Session")
        emitted: list[EventRecord] = []

        queued_attributed_input = next(
            (item for item in (_queued_inputs or []) if item.source != "user"),
            None,
        )
        attributed_source = (
            _attributed_input_source(_input_attribution)
            if _input_attribution is not None
            else (
                queued_attributed_input.source if queued_attributed_input is not None else None
            )
        )
        run = (
            _prepared_run
            or preapproved_new_run
            or RunRecord(
                run_id=(
                    _queued_inputs[0].prospective_run_id
                    if _queued_inputs
                    and _queued_inputs[0].prompt_policy_checked
                    and _queued_inputs[0].prospective_run_id is not None
                    else new_id("run")
                ),
                session_id=session.session_id,
                status="running",
                origin=(
                    attributed_source
                    if attributed_source is not None
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
                    else _queued_input_initiator(queued_attributed_input)
                ),
                trigger=attributed_source or "user",
            )
        )
        run_lock = self.run_ownership.acquire(session.session_id, run.run_id)
        if run_lock is None:
            snapshot = self.store.load_runtime_session(session.session_id)
            active_run = maybe_active_run(snapshot.runs)
            self._validate_model_resource_refs(
                snapshot.session,
                input_resource_refs,
                message_role=_input_role(_input_attribution),
            )
            if active_run is not None and (
                _prepared_run is None or active_run.run_id != _prepared_run.run_id
            ):
                self._validate_requested_session_policies(
                    snapshot.session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
                )
                queued_session, _, queued_events = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    skill_invocation=skill_invocation,
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
                queued_session, _, queued_events = await self._queue_input(
                    snapshot=locked_snapshot,
                    active_run=locked_active_run,
                    prompt=prompt,
                    skill_invocation=skill_invocation,
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
                self.store.refresh_catalog(session.session_id)
            session, active_adapter = self._resolve_session_adapter(session)
            existing_messages = locked_snapshot.messages
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            self._restore_run_tool_cap(run)
            if preapproved_new_run is not None:
                run = self._bind_extension_snapshot(
                    run,
                    self._extension_snapshots.get(run.run_id),
                )
            elif _queued_inputs and _queued_inputs[0].prompt_policy_checked:
                run = await self._attach_queued_extension_snapshot(run, _queued_inputs)
            elif _prepared_run is not None and "extension_generations" in run.metadata:
                await self._ensure_extension_snapshot(run)
            else:
                run = self._attach_extension_snapshot(run)
            if skill_invocation is not None and resolved_skill_activation is None:
                resolved_skill_activation = self._resolve_skill_activation_for_run(
                    run.run_id,
                    skill_invocation,
                )
            pending_inputs = _queued_inputs or [
                QueuedInput(
                    queue_id=new_id("queued"),
                    prompt=prompt,
                    skill_activation=resolved_skill_activation,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    queued_at=utc_now(),
                    source=(
                        _attributed_input_source(_input_attribution)
                        if _input_attribution is not None
                        else "user"
                    ),
                    role=_input_role(_input_attribution),
                    scaffold=_input_scaffold(_input_attribution),
                    semantic_tag=(
                        _input_attribution.semantic_tag
                        if _input_attribution is not None
                        else None
                    ),
                    host_source_id=(
                        _input_attribution.host_source_id
                        if isinstance(_input_attribution, HostSessionInput)
                        else None
                    ),
                    delegation_id=(
                        _input_attribution.delegation_id
                        if isinstance(_input_attribution, DelegatedTaskInput)
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
            if skill_invocation is not None and (
                len(pending_inputs) != 1 or pending_inputs[0].source != "user"
            ):
                raise ValueError("skill invocation requires one direct user input")
            for pending_input in pending_inputs:
                self._validate_model_resource_refs(
                    session,
                    pending_input.resource_refs,
                    adapter=active_adapter,
                    message_role=pending_input.role,
                )
            if preapproved_new_run is None and not (
                _queued_inputs and _queued_inputs[0].prompt_policy_checked
            ):
                await self._evaluate_prompt_policies(
                    session=session,
                    run=run,
                    inputs=pending_inputs,
                )
            is_queued_run = _queued_inputs is not None
            if is_queued_run:
                session, run, input_messages = self._start_queued_run(
                    session=session,
                    run=run,
                    snapshot=locked_snapshot,
                    inputs=pending_inputs,
                    existing_message_count=len(existing_messages),
                )
            else:
                session, run = self.run_lifecycle.start(
                    session=session,
                    run=run,
                    snapshot=locked_snapshot,
                )
                input_messages = []

            sequence = self.store.latest_event_sequence(session.session_id)
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_started",
                payload={"trigger": run.trigger, "origin": run.origin},
            )

            for index, queued_input in enumerate(pending_inputs):
                if is_queued_run:
                    input_message = input_messages[index]
                else:
                    input_message = self._append_session_input(
                        session=session,
                        run=run,
                        item=queued_input,
                        sequence=len(existing_messages) + len(input_messages) + 1,
                        is_queued=False,
                    )
                    input_messages.append(input_message)
                sequence = await self.event_recorder.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type=_session_input_event_type(queued_input.source),
                    payload={
                        "message_id": input_message.message_id,
                        "queued_input_id": (queued_input.queue_id if is_queued_run else None),
                        "source": queued_input.source,
                        "role": queued_input.role,
                        "scaffold": queued_input.scaffold,
                        "semantic_tag": queued_input.semantic_tag,
                        "host_source_id": queued_input.host_source_id,
                        "plugin_id": queued_input.plugin_id,
                        "contribution_id": queued_input.contribution_id,
                        "resource_refs": list(queued_input.resource_refs),
                        "skill_name": (
                            queued_input.skill_activation.name
                            if queued_input.skill_activation is not None
                            else None
                        ),
                        "skill_content_digest": (
                            queued_input.skill_activation.content_digest
                            if queued_input.skill_activation is not None
                            else None
                        ),
                    },
                )

            abort_signal = self.run_ownership.activate_signal(session.session_id, run.run_id)
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
                self.run_ownership.release_signal(run.run_id, abort_signal)
            if result is None:
                self._release_extension_snapshot(run.run_id)
            self.run_ownership.release(run_lock)
        assert result is not None
        return await self._finalize_run_boundary(
            result,
            drain_queued_inputs=_drain_queued_inputs,
        )

    async def run_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput | DelegatedTaskInput,
    ) -> RunResult:
        """Execute an already durable queued Run with typed input attribution."""

        return await self.run(
            session_input.prompt,
            session_id=run.session_id,
            title=session_input.title,
            queue_mode=session_input.mode,
            resource_refs=session_input.resource_refs,
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
        tool_calls = self._tool_calls_for_run(run.run_id)
        tools = tool_calls.registry
        context_providers = self._context_providers_for_run(run.run_id)
        compaction = self._compaction_for_run(run.run_id)
        loop = InnerAgentLoop(
            adapter=adapter,
            tool_definitions=tools.definitions,
            run_budget=self._run_budget_for_run(run),
            ports=InnerLoopPorts(
                context=InnerLoopContext(
                    prepare=partial(
                        self.context_preparation.prepare,
                        context_providers=context_providers,
                        strategies=compaction,
                        skill_instructions=self._instructions_for_run(run),
                    ),
                    record_completion=self.context_preparation.record_provider_completion,
                    resolve_model_request=self._resolve_model_request,
                ),
                runs=InnerLoopRunBoundaries(
                    record_usage=self.run_lifecycle.record_usage,
                    aborted_result=self.run_ownership.aborted_result,
                    save_waiting_state=self.run_lifecycle.save_waiting_state,
                    complete=self.run_lifecycle.complete,
                    fail=self.run_lifecycle.fail,
                    pause=self.run_lifecycle.pause,
                ),
                turns=InnerLoopTurnBoundaries(
                    consume_inputs=self._consume_turn_boundary_inputs,
                    evaluate_completion=partial(
                        self._evaluate_turn_completion,
                        policies=self._turn_policies_for_run(run.run_id),
                    ),
                    begin_model_round=(
                        self._begin_model_round if self._extension_catalog is not None else None
                    ),
                    commit_model_request=self._commit_model_request,
                    shared_budget_reason=(
                        self._agent_controls.budget_reason
                        if self._agent_controls is not None
                        else None
                    ),
                ),
                emit=self.event_recorder.emit,
                handle_tool_batch=tool_calls.handle_batch,
                refresh_session=self.store.load_session_record,
            ),
        )
        result = await loop.run(
            session=session,
            run=run,
            messages=messages,
            sequence=sequence,
            emitted=emitted,
            final_parts=final_parts,
            tool_results=tool_results,
            abort_signal=abort_signal,
        )
        return await self._finalize_requested_abort(result)

    def _resolve_model_request(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        model_round: int,
    ) -> tuple[SessionRecord, RuntimeAdapter]:
        """Bind the latest Session route immediately before one provider request."""

        del run, model_round
        return self._resolve_session_adapter(session)

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
        run_lock = self.run_ownership.acquire(session_id, run.run_id)
        if run_lock is None:
            raise SessionBusyError(f"session is already locked: {session_id}")
        emitted: list[EventRecord] = []
        sequence = self.store.latest_event_sequence(session_id)
        abort_signal = self.run_ownership.activate_signal(session_id, run.run_id)
        terminal_run = run
        try:
            snapshot = self.store.load_runtime_session(session_id)
            if maybe_active_run(snapshot.runs) is not None:
                raise SessionBusyError(f"session already has an active run: {session_id}")
            active_session, active_adapter = self._resolve_session_adapter(snapshot.session)
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
            except CompactionAbortedError:
                cancelled = await self._finalize_requested_abort(
                    RunResult(
                        session=self.store.load_session_record(session_id),
                        run=self.store.load_run(session_id, run.run_id),
                        events=emitted,
                    )
                )
                terminal_run = cancelled.run
                return CompactionCommandResult(
                    session=cancelled.session,
                    run=cancelled.run,
                    events=emitted,
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
            completed = await self._finalize_requested_abort(completed)
            terminal_run = completed.run
            return CompactionCommandResult(
                session=completed.session,
                run=completed.run,
                compaction=execution.record,
                events=emitted,
                skipped=execution.skipped,
            )
        finally:
            self.run_ownership.release_signal(run.run_id, abort_signal)
            self.run_ownership.release(run_lock)
            if terminal_run.status in ACTIVE_RUN_STATUSES:
                self._finish_run_observers(terminal_run)
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
        session = self.store.load_session_record(session_id)
        runs = self.store.load_runs(session_id)
        run = find_run(runs, run_id) if run_id else find_active_run(runs)
        if run.status not in ACTIVE_RUN_STATUSES:
            return RunResult(session=session, run=run)

        emitted: list[EventRecord] = []
        has_active_owner = self.run_ownership.is_locked(session_id)
        signal = self.run_ownership.signal_for(run.run_id)
        if has_active_owner:
            session, run, changed = request_run_abort_state(
                self.store,
                session_id=session_id,
                run_id=run.run_id,
                reason=reason,
            )
            if changed:
                await self.event_recorder.emit(
                    emitted,
                    self.store.latest_event_sequence(session_id),
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="run_abort_requested",
                    payload={"reason": reason},
                )
            if signal is not None:
                signal.abort(reason)
            await self.run_ownership.wait_for_settlement(session_id, run.run_id)
            session = self.store.load_session_record(session_id)
            run = self.store.load_run(session_id, run.run_id)
            if run.status not in ACTIVE_RUN_STATUSES:
                return RunResult(session=session, run=run, events=emitted)

        session, run, changed = abort_run_state(
            self.store,
            session_id=session_id,
            run_id=run.run_id,
            reason=reason,
        )
        if not changed:
            return RunResult(session=session, run=run)
        await self.event_recorder.emit(
            emitted,
            self.store.latest_event_sequence(session_id),
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_aborted",
            payload={"reason": reason},
        )
        result = RunResult(session=session, run=run, events=emitted)
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

    async def submit_plugin_input_batch(
        self,
        batch: AttributedSessionInputBatch,
        *,
        session_id: str,
    ) -> RunResult:
        """Durably accept and consume one attributed atomic input unit."""

        snapshot = self._load_runtime_snapshot(
            session_id,
            recover=not self.run_ownership.is_locked(session_id),
        )
        active = maybe_active_run(snapshot.runs)
        queued_session, queued, _ = await self._queue_input_batch(
            snapshot=snapshot,
            active_run=active,
            batch=batch,
        )
        if active is not None:
            return RunResult(session=queued_session, run=active)
        target_ids = {item.queue_id for item in queued}
        while True:
            snapshot = self._load_runtime_snapshot(
                session_id,
                recover=not self.run_ownership.is_locked(session_id),
            )
            active = maybe_active_run(snapshot.runs)
            if active is not None:
                return RunResult(session=snapshot.session, run=active)
            pending = self._pending_queued_inputs(snapshot)
            if not pending:
                break
            try:
                result = await self.run(
                    pending[0].prompt,
                    session_id=session_id,
                    title=pending[0].title,
                    _queued_inputs=pending,
                )
            except _QueuedInputSelectionChanged:
                continue
            consumed_ids = {
                str(message.metadata.get("queued_input_id"))
                for message in self.store.load_runtime_session(session_id).messages
            }
            if target_ids <= consumed_ids or result.run.status != "completed":
                return result
        raise PersistenceError("accepted plugin input batch is missing from queue and history")

    async def submit_host_input(
        self,
        session_input: HostSessionInput,
        *,
        session_id: str,
    ) -> HostInputReceipt:
        """Durably accept an OURO-owned scaffold input without awaiting execution."""

        snapshot = self._load_runtime_snapshot(
            session_id,
            recover=not self.run_ownership.is_locked(session_id),
        )
        self._validate_model_resource_refs(
            snapshot.session,
            session_input.resource_refs,
            message_role=session_input.role,
        )
        _, queued_input, _ = await self._queue_input(
            snapshot=snapshot,
            active_run=maybe_active_run(snapshot.runs),
            prompt=session_input.prompt,
            title=session_input.title,
            mode=session_input.mode,
            resource_refs=session_input.resource_refs,
            attribution=session_input,
        )
        return HostInputReceipt(
            session_id=session_id,
            queue_id=queued_input.queue_id,
            accepted_at=queued_input.queued_at,
        )

    async def consume_host_input(self, receipt: HostInputReceipt) -> RunResult:
        """Consume a previously accepted Host input through the ordinary Run path."""

        while True:
            snapshot = self._load_runtime_snapshot(
                receipt.session_id,
                recover=not self.run_ownership.is_locked(receipt.session_id),
            )
            active = maybe_active_run(snapshot.runs)
            if active is not None:
                return RunResult(session=snapshot.session, run=active)
            pending = self._pending_queued_inputs(snapshot)
            if not pending:
                break
            try:
                return await self.run(
                    pending[0].prompt,
                    session_id=receipt.session_id,
                    title=pending[0].title,
                    _queued_inputs=pending,
                )
            except _QueuedInputSelectionChanged:
                continue
        for message in snapshot.messages:
            if (
                message.metadata.get("queued_input_id") == receipt.queue_id
                and message.run_id is not None
            ):
                return RunResult(
                    session=self.store.load_session_record(receipt.session_id),
                    run=self.store.load_run(receipt.session_id, message.run_id),
                )
        raise PersistenceError(
            f"accepted Host input is missing from queue and canonical history: {receipt.queue_id}"
        )

    async def _queue_input(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        active_run: RunRecord | None,
        prompt: str,
        skill_invocation: SkillInvocationRequest | None = None,
        title: str | None,
        mode: QueuedInputMode,
        resource_refs: tuple[str, ...] = (),
        attribution: (AttributedSessionInput | HostSessionInput | DelegatedTaskInput | None) = None,
    ) -> tuple[SessionRecord, QueuedInput, list[EventRecord]]:
        source = _attributed_input_source(attribution) if attribution is not None else "user"
        role = _input_role(attribution)
        scaffold = _input_scaffold(attribution)
        if skill_invocation is not None and (source != "user" or mode != "follow_up"):
            raise ValueError("skill invocation requires direct follow-up user input")
        queue_id = (
            attribution.queue_id
            if isinstance(attribution, HostSessionInput) and attribution.queue_id is not None
            else new_id("queued")
        )
        prompt_policy_checked = source == "user"
        prospective_run_id: str | None = None
        extension_snapshot: ExtensionCatalogLease | None = None
        extension_catalog_id: str | None = None
        extension_generations: tuple[dict[str, object], ...] = ()
        resolved_skill_activation: SkillActivationContent | None = None
        if prompt_policy_checked:
            prospective = RunRecord(
                session_id=snapshot.session.session_id,
                status="running",
                origin=source,
            )
            prospective_run_id = prospective.run_id
            controller_run_id: str | None = None
            if mode == "steer" and active_run is not None:
                await self._ensure_extension_snapshot(active_run)
                active_snapshot = self._extension_snapshots.get(active_run.run_id)
                if active_snapshot is not None:
                    extension_snapshot = active_snapshot.fork()
                controller_run_id = active_run.run_id
            else:
                prospective = self._attach_extension_snapshot(prospective)
            try:
                if skill_invocation is not None:
                    resolved_skill_activation = self._resolve_skill_activation_for_run(
                        prospective.run_id,
                        skill_invocation,
                    )
                await self._evaluate_prompt_policies(
                    session=snapshot.session,
                    run=prospective,
                    inputs=[
                        QueuedInput(
                            queue_id=queue_id,
                            prompt=prompt,
                            skill_activation=resolved_skill_activation,
                            title=title,
                            mode=mode,
                            resource_refs=resource_refs,
                            queued_at=utc_now(),
                            source=source,
                            role=role,
                            scaffold=scaffold,
                            semantic_tag=(
                                attribution.semantic_tag if attribution is not None else None
                            ),
                            delegation_id=(
                                attribution.delegation_id
                                if isinstance(attribution, DelegatedTaskInput)
                                else None
                            ),
                        )
                    ],
                    catalog_snapshot=extension_snapshot,
                    controller_run_id=controller_run_id,
                )
                if controller_run_id is None:
                    extension_snapshot = self._detach_extension_snapshot(prospective.run_id)
                if extension_snapshot is not None:
                    extension_catalog_id = extension_snapshot.catalog_id
                    extension_generations = tuple(
                        generation.model_dump(mode="json", exclude_none=True)
                        for generation in extension_snapshot.generations
                    )
            except BaseException:
                self._release_extension_snapshot(prospective.run_id)
                if extension_snapshot is not None:
                    extension_snapshot.close()
                raise
        try:
            session, queued_input, queue_size = self.queued_inputs.enqueue(
                snapshot=snapshot,
                prompt=prompt,
                skill_activation=resolved_skill_activation,
                title=title,
                mode=mode,
                resource_refs=resource_refs,
                queue_id=queue_id,
                source=source,
                role=role,
                scaffold=scaffold,
                semantic_tag=(
                    attribution.semantic_tag
                    if attribution is not None
                    else None
                ),
                host_source_id=(
                    attribution.host_source_id
                    if isinstance(attribution, HostSessionInput)
                    else None
                ),
                delegation_id=(
                    attribution.delegation_id
                    if isinstance(attribution, DelegatedTaskInput)
                    else None
                ),
                plugin_id=(
                    attribution.plugin_id
                    if isinstance(attribution, AttributedSessionInput)
                    else None
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
                caused_by_run_id=(
                    active_run.run_id
                    if active_run is not None and attribution is not None
                    else None
                ),
                prompt_policy_checked=prompt_policy_checked,
                prospective_run_id=prospective_run_id,
                extension_catalog_id=extension_catalog_id,
                extension_generations=extension_generations,
            )
        except BaseException:
            if extension_snapshot is not None:
                extension_snapshot.close()
            raise
        if extension_snapshot is not None:
            retained = self._queued_extension_snapshots.setdefault(
                queued_input.queue_id,
                extension_snapshot,
            )
            if retained is not extension_snapshot:
                extension_snapshot.close()
        emitted: list[EventRecord] = []
        event_type = (
            "follow_up_input_queued"
            if queued_input.mode == "follow_up"
            else "steering_input_queued"
        )
        payload = {
            "queue_id": queued_input.queue_id,
            "mode": queued_input.mode,
            "queue_size": queue_size,
            "resource_refs": list(queued_input.resource_refs),
            "source": queued_input.source,
            "role": queued_input.role,
            "scaffold": queued_input.scaffold,
            "semantic_tag": queued_input.semantic_tag,
            "host_source_id": queued_input.host_source_id,
            "delegation_id": queued_input.delegation_id,
            "plugin_id": queued_input.plugin_id,
            "contribution_id": queued_input.contribution_id,
            "skill_name": (
                queued_input.skill_activation.name
                if queued_input.skill_activation is not None
                else None
            ),
            "skill_content_digest": (
                queued_input.skill_activation.content_digest
                if queued_input.skill_activation is not None
                else None
            ),
        }
        if active_run is None:
            emitted.append(
                await self.event_recorder.publish(
                    session_id=session.session_id,
                    run_id=None,
                    event_type=event_type,
                    payload=payload,
                )
            )
        else:
            await self.event_recorder.emit(
                emitted,
                self.store.latest_event_sequence(session.session_id),
                session_id=session.session_id,
                run_id=active_run.run_id,
                event_type=event_type,
                payload=payload,
            )
        return session, queued_input, emitted

    async def _queue_input_batch(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        active_run: RunRecord | None,
        batch: AttributedSessionInputBatch,
    ) -> tuple[SessionRecord, tuple[QueuedInput, ...], list[EventRecord]]:
        first = batch.inputs[0]
        requests = tuple(
            SessionInputRequest(
                prompt=item.prompt,
                mode=item.mode,
                title=item.title,
                role=item.role,
                scaffold=item.scaffold,
                semantic_tag=item.semantic_tag,
            )
            for item in batch.inputs
        )
        session, queued, queue_size, superseded = self.queued_inputs.enqueue_batch(
            snapshot=snapshot,
            inputs=requests,
            source="plugin",
            plugin_id=first.plugin_id,
            contribution_id=first.contribution_id,
            generation_id=first.generation_id,
            execution_contract_hash=first.execution_contract_hash,
            coalesce_key=batch.coalesce_key,
            caused_by_run_id=active_run.run_id if active_run is not None else None,
        )
        for item in superseded:
            self._release_queued_extension_snapshot(item.queue_id)
        emitted: list[EventRecord] = []
        payload = {
            "batch_id": queued[0].batch_id,
            "queue_ids": [item.queue_id for item in queued],
            "batch_size": len(queued),
            "mode": queued[0].mode,
            "queue_size": queue_size,
            "plugin_id": first.plugin_id,
            "contribution_id": first.contribution_id,
            "coalesce_key": batch.coalesce_key,
            "superseded_queue_ids": [item.queue_id for item in superseded],
        }
        if active_run is None:
            emitted.append(
                await self.event_recorder.publish(
                    session_id=session.session_id,
                    run_id=None,
                    event_type="session_input_batch_queued",
                    payload=payload,
                )
            )
        else:
            await self.event_recorder.emit(
                emitted,
                self.store.latest_event_sequence(session.session_id),
                session_id=session.session_id,
                run_id=active_run.run_id,
                event_type="session_input_batch_queued",
                payload=payload,
            )
        return session, queued, emitted

    async def _drain_queued_input_queue(self, session_id: str) -> None:
        while True:
            snapshot = self._load_runtime_snapshot(
                session_id,
                recover=not self.run_ownership.is_locked(session_id),
            )
            if maybe_active_run(snapshot.runs) is not None:
                return
            pending = self._pending_queued_inputs(snapshot)
            if not pending:
                return
            try:
                result = await self.run(
                    pending[0].prompt,
                    session_id=session_id,
                    title=pending[0].title,
                    _drain_queued_inputs=False,
                    _queued_inputs=pending,
                )
            except _QueuedInputSelectionChanged:
                continue
            if result.run.status != "completed":
                return

    def _pending_queued_inputs(self, snapshot: RuntimeSessionSnapshot) -> list[QueuedInput]:
        units = self.queued_inputs.eligible_units(snapshot)
        pending_units: list[tuple[QueuedInput, ...]] = []
        for unit in units:
            durable = [
                self._queued_input_is_durable(snapshot.messages, item.queue_id) for item in unit
            ]
            if all(durable):
                self.queued_inputs.acknowledge_many(
                    session_id=snapshot.session.session_id,
                    queue_ids=tuple(item.queue_id for item in unit),
                )
                for item in unit:
                    self._release_queued_extension_snapshot(item.queue_id)
                continue
            pending_units.append(unit)
        if not pending_units:
            return []
        if self.queued_input_config.consumption_mode == "one_by_one":
            return list(pending_units[0])
        first = pending_units[0][0]
        compatible_units: list[tuple[QueuedInput, ...]] = []
        for unit in pending_units:
            item = unit[0]
            if first.prompt_policy_checked:
                matches = all(self._same_approved_queue_contract(first, member) for member in unit)
            else:
                matches = not item.prompt_policy_checked
            if not matches:
                break
            compatible_units.append(unit)
        return [item for unit in compatible_units for item in unit]

    @staticmethod
    def _same_approved_queue_contract(first: QueuedInput, item: QueuedInput) -> bool:
        if first.skill_activation is not None or item.skill_activation is not None:
            return first.queue_id == item.queue_id
        return (
            item.prompt_policy_checked
            and item.extension_catalog_id == first.extension_catalog_id
            and item.extension_generations == first.extension_generations
        )

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
        assistant_model_response: ModelResponseRecord | None,
        assistant_continuation: ProviderContinuationArtifact | None,
        turn_id: str,
    ) -> TurnBoundaryInputResult:
        snapshot = self.store.load_runtime_session(session.session_id)
        units = self.queued_inputs.eligible_units(snapshot, mode="steer")
        if self.queued_input_config.consumption_mode == "one_by_one":
            units = units[:1]
        eligible = [item for unit in units for item in unit]
        if not eligible:
            return TurnBoundaryInputResult(sequence=sequence)

        appended: list[MessageRecord] = []
        if assistant_text or assistant_reasoning or assistant_continuation is not None:
            assistant_message = self.run_lifecycle.append_assistant_message(
                session=session,
                run=run,
                text=assistant_text,
                reasoning_content=assistant_reasoning,
                model_response=assistant_model_response,
                provider_continuation=assistant_continuation,
                sequence=len(messages) + 1,
                turn_id=turn_id,
            )
            if not any(message.message_id == assistant_message.message_id for message in messages):
                appended.append(assistant_message)

        for item in eligible:
            input_message, newly_consumed = self._consume_queued_turn_input(
                session=session,
                run=run,
                item=item,
                sequence=len(messages) + len(appended) + 1,
            )
            if input_message is None:
                continue
            self._release_queued_extension_snapshot(item.queue_id)
            if not any(
                message.message_id == input_message.message_id for message in (*messages, *appended)
            ):
                appended.append(input_message)
            if not newly_consumed:
                continue
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="queued_input_consumed",
                payload={
                    "queue_id": item.queue_id,
                    "message_id": input_message.message_id,
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
        assistant_model_response: ModelResponseRecord | None,
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
            model_response=assistant_model_response,
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
        feedback_message = self.run_lifecycle.append_input_message(
            session=session,
            run=run,
            prompt=evaluation.feedback,
            role="system",
            scaffold=True,
            sequence=len(messages) + 2,
            metadata={
                "session_input_source": "turn_policy",
                "turn_id": turn_id,
                "controllers": controllers,
            },
            semantic_tag="turn-policy-feedback",
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
        message = self._create_session_input_message(
            session=session,
            run=run,
            item=item,
            sequence=sequence,
            is_queued=is_queued,
        )
        self.run_lifecycle.append_messages(session=session, run=run, messages=[message])
        return message

    def _create_session_input_message(
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
        if item.source == "delegation":
            metadata["delegation_id"] = item.delegation_id
            if item.caused_by_run_id is not None:
                metadata["caused_by_run_id"] = item.caused_by_run_id
        if is_queued:
            metadata["queued_input_id"] = item.queue_id
            metadata["queued_input_mode"] = item.mode
        if item.batch_id is not None:
            metadata.update(
                {
                    "session_input_batch_id": item.batch_id,
                    "session_input_batch_index": item.batch_index,
                    "session_input_batch_size": item.batch_size,
                    "session_input_coalesce_key": item.coalesce_key,
                }
            )
        if item.source == "plugin" and item.resource_refs:
            raise ResourceAttachmentError(
                "plugin session inputs cannot attach Host resource references"
            )
        if item.source == "host":
            metadata["host_source_id"] = item.host_source_id
            metadata["caused_by_run_id"] = item.caused_by_run_id
        elif item.source == "plugin":
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
        return self.run_lifecycle.create_input_message(
            session=session,
            run=run,
            prompt=item.prompt,
            role=item.role,
            scaffold=item.scaffold,
            skill_activation=item.skill_activation,
            sequence=sequence,
            resource_refs=item.resource_refs,
            metadata=metadata,
            semantic_tag=item.semantic_tag,
        )

    def _start_queued_run(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        snapshot: RuntimeSessionSnapshot,
        inputs: list[QueuedInput],
        existing_message_count: int,
    ) -> tuple[SessionRecord, RunRecord, list[MessageRecord]]:
        """Cross one selected queue batch into canonical history under one mutation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            current = {
                item.queue_id: item for item in self.queued_inputs.eligible_session(latest_session)
            }
            if any(current.get(item.queue_id) != item for item in inputs):
                raise _QueuedInputSelectionChanged
            started_session, started_run = self.run_lifecycle.start(
                session=session,
                run=run,
                snapshot=snapshot,
            )
            messages = [
                self._create_session_input_message(
                    session=started_session,
                    run=started_run,
                    item=item,
                    sequence=existing_message_count + index + 1,
                    is_queued=True,
                )
                for index, item in enumerate(inputs)
            ]
            self.run_lifecycle.append_messages(
                session=started_session,
                run=started_run,
                messages=messages,
            )
            self.queued_inputs.acknowledge_many(
                session_id=session.session_id,
                queue_ids=tuple(item.queue_id for item in inputs),
            )
        return started_session, started_run, messages

    def _consume_queued_turn_input(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        item: QueuedInput,
        sequence: int,
    ) -> tuple[MessageRecord | None, bool]:
        """Atomically choose cancellation or durable turn-boundary consumption."""

        with self.store.session_mutation(session.session_id):
            latest = self.store.load_runtime_session(session.session_id)
            if self.queued_inputs.find_session(latest.session, item.queue_id) != item:
                return None, False
            durable = next(
                (
                    message
                    for message in latest.messages
                    if message.metadata.get("queued_input_id") == item.queue_id
                ),
                None,
            )
            if durable is not None:
                if item.batch_id is None:
                    self.queued_inputs.acknowledge(latest, item.queue_id)
                elif item.batch_size is not None and item.batch_index == item.batch_size - 1:
                    self.queued_inputs.acknowledge_batch(
                        session_id=session.session_id,
                        batch_id=item.batch_id,
                    )
                return durable, False
            message = self._append_session_input(
                session=session,
                run=run,
                item=item,
                sequence=sequence,
                is_queued=True,
            )
            if item.batch_id is None:
                latest = self.store.load_runtime_session(session.session_id)
                self.queued_inputs.acknowledge(latest, item.queue_id)
            elif item.batch_size is not None and item.batch_index == item.batch_size - 1:
                self.queued_inputs.acknowledge_batch(
                    session_id=session.session_id,
                    batch_id=item.batch_id,
                )
            return message, True

    def _validate_model_resource_refs(
        self,
        session: SessionRecord,
        resource_refs: tuple[str, ...],
        *,
        adapter: RuntimeAdapter | None = None,
        message_role: MessageRole = "user",
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
            message_role=message_role,
        )

    async def _evaluate_prompt_policies(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        inputs: list[QueuedInput],
        catalog_snapshot: ExtensionCatalogLease | None = None,
        controller_run_id: str | None = None,
    ) -> None:
        scope_run_id = controller_run_id or run.run_id
        snapshot = catalog_snapshot or self._extension_snapshots.get(run.run_id)
        registry = (
            snapshot.prompt_policies.scoped(
                lambda: self._active_controller_ids_for_run(scope_run_id)
            )
            if snapshot is not None
            else self._prompt_policies_for_run(scope_run_id)
        )
        if not registry.registered():
            return
        execution_contracts = {
            generation.plugin_id: generation.execution_contract_hash
            for generation in (snapshot.generations if snapshot is not None else ())
            if generation.execution_contract_hash is not None
        }
        for item in inputs:
            if item.source != "user":
                continue
            attachments: tuple[PromptAttachmentMetadata, ...] = ()
            if item.resource_refs:
                assert self.resources is not None
                attachments = tuple(
                    PromptAttachmentMetadata(
                        resource_id=resource.resource_id,
                        media_type=resource.media_type,
                        size=resource.size,
                    )
                    for resource in (
                        self.resources.show_resource(
                            session.session_id,
                            resource_id,
                            verify=False,
                        )
                        for resource_id in item.resource_refs
                    )
                )
            evaluation = await registry.evaluate(
                PromptPolicyCall(
                    session_id=session.session_id,
                    prospective_run_id=run.run_id,
                    prompt=item.prompt,
                    input_source=item.source,
                    workspace=self.default_workspace,
                    attachments=attachments,
                    skill_activation=(
                        PromptSkillActivationMetadata(
                            name=item.skill_activation.name,
                            source=item.skill_activation.source,
                            location=item.skill_activation.location,
                            content_digest=item.skill_activation.content_digest,
                        )
                        if item.skill_activation is not None
                        else None
                    ),
                    catalog_id=snapshot.catalog_id if snapshot is not None else None,
                    execution_contracts=execution_contracts,
                )
            )
            if evaluation.allowed:
                continue
            prompt_redactor = SensitiveValueRedactor((item.prompt,))
            if evaluation.rejected_by is not None:
                rejected = evaluation.rejected_by
                raise PromptRejectedError(
                    prompt_redactor.redact(evaluation.reason or "prompt rejected"),
                    plugin_id=rejected.plugin_id,
                    policy_id=rejected.policy_id,
                )
            failure = evaluation.failures[0]
            raise PromptRejectedError(
                evaluation.reason or "prompt policy evaluation failed",
                plugin_id=failure.plugin_id,
                policy_id=failure.policy_id,
                policy_failed=True,
                failure_error_type=failure.error_type,
                failure_message=prompt_redactor.redact(failure.message),
            )

    @staticmethod
    def _queued_input_is_durable(messages: list[MessageRecord], queue_id: str) -> bool:
        return any(
            message.metadata.get("queued_input_id") == queue_id
            for message in messages
        )

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
        run_lock = self.run_ownership.acquire(session.session_id, run.run_id)
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
            batch = resolve_tool_approval(
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
            suspension = tool_batch_suspension(batch)
            session, run = self.run_lifecycle.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status=suspension.status if suspension is not None else "running",
                phase=suspension.run_phase if suspension is not None else "tool_execution",
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
            if suspension is not None:
                result = RunResult(session=session, run=run, events=emitted)
            else:
                try:
                    await self._ensure_extension_snapshot(run)
                except Exception as exc:  # noqa: BLE001 - settle the resolved interaction.
                    error = (
                        "tool batch continuation failed after approval resolution: "
                        f"{type(exc).__name__}: {redact_text(str(exc))}"
                    )
                    result = await self.run_lifecycle.fail(
                        emitted,
                        sequence,
                        session=session,
                        run=run,
                        error=error,
                        final_text="",
                        tool_results=[],
                    )
                else:
                    result = await self._continue_resolved_batch(
                        snapshot=snapshot,
                        session=session,
                        run=run,
                        batch=batch,
                        emitted=emitted,
                        sequence=sequence,
                    )
        finally:
            self.run_ownership.release(run_lock)
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
        run_lock = self.run_ownership.acquire(session.session_id, run.run_id)
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
            self.run_ownership.release(run_lock)
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
        run_lock = self.run_ownership.acquire(session_id, run_id)
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
                        "status": "failed" if result.is_error else "completed",
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
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session_id,
                run_id=run_id,
                event_type="tool_call_completed",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "tool_call_id": result.tool_call_id,
                    "tool_name": result.tool_name,
                    "status": "failed" if result.is_error else "completed",
                    "is_error": result.is_error,
                    "source_index": waiting_call.request.source_index,
                    "result_data": result.structured,
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
            self.run_ownership.release(run_lock)
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
        suspension = tool_batch_suspension(batch)
        if suspension is not None:
            batch = batch.model_copy(
                update={"phase": suspension.batch_phase, "updated_at": utc_now()}
            )
            session, run = self.run_lifecycle.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status=suspension.status,
                phase=suspension.run_phase,
            )
            sequence = await self.event_recorder.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_status_changed",
                payload={
                    "to_status": suspension.status,
                    "status": suspension.status,
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                },
            )
            return RunResult(session=session, run=run, events=emitted)

        active_signal = self.run_ownership.activate_signal(session.session_id, run.run_id)
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
                return await self._finalize_requested_abort(failed)
            latest = self._load_runtime_snapshot(session.session_id, recover=True)
            session = latest.session
            run = find_run(latest.runs, run.run_id)
            abort_result = self.run_ownership.aborted_result(
                session_id=session.session_id,
                run_id=run.run_id,
                final_text="",
                emitted=emitted,
                tool_results=handling.results,
            )
            if abort_result is not None:
                return await self._finalize_requested_abort(abort_result)
            if handling.status in {"waiting_approval", "waiting_user", "waiting_child"}:
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
                assistant_model_response=None,
                assistant_continuation=None,
                turn_id=batch.turn_id,
            )
            latest = self.store.load_runtime_session(session.session_id)
            session = latest.session
            run = find_run(latest.runs, run.run_id)
            session, active_adapter = self._resolve_session_adapter(session)
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
            self.run_ownership.release_signal(run.run_id, active_signal)
        return resumed

    async def _finalize_requested_abort(self, result: RunResult) -> RunResult:
        """Publish terminal abort only after the active owner has settled its work."""

        latest_run = self.store.load_run(result.run.session_id, result.run.run_id)
        if latest_run.status not in ACTIVE_RUN_STATUSES or not latest_run.abort_requested:
            return result.model_copy(
                update={
                    "session": self.store.load_session_record(latest_run.session_id),
                    "run": latest_run,
                }
            )
        raw_reason = latest_run.metadata.get("abort_reason")
        reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "User aborted"
        session, aborted_run, changed = abort_run_state(
            self.store,
            session_id=latest_run.session_id,
            run_id=latest_run.run_id,
            reason=reason,
        )
        if changed:
            await self.event_recorder.emit(
                result.events,
                self.store.latest_event_sequence(latest_run.session_id),
                session_id=latest_run.session_id,
                run_id=latest_run.run_id,
                event_type="run_aborted",
                payload={"reason": reason},
            )
        return result.model_copy(update={"session": session, "run": aborted_run})

    async def _finalize_run_boundary(
        self,
        result: RunResult,
        *,
        drain_queued_inputs: bool,
    ) -> RunResult:
        run = result.run
        if run.status not in ACTIVE_RUN_STATUSES:
            try:
                await self._settle_terminal_run(run)
                continuation_service = RunContinuationService(
                    store=self.store,
                    registry=self._continuations_for_run(run.run_id, active_only=False),
                    queue=self.queued_inputs,
                    session_controls=self.session_controls,
                    session_controllers=self._session_controllers_for_run(run.run_id),
                )
                settlement = await continuation_service.settle(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    final_text=result.final_text,
                )
                run = settlement.run
                if settlement.settled:
                    await self._emit_continuation_resolution(settlement, result.events)
                outcome = RunOutcomeView(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    status=run.status,
                    origin=run.origin,
                    final_text=result.final_text,
                    error=run.error,
                    usage=MappingProxyType(dict(run.usage)),
                )
                self.event_recorder.observe(
                    RunFinishedObservation(
                        session_id=run.session_id,
                        run_id=run.run_id,
                        outcome=outcome,
                    )
                )
            finally:
                self._finish_run_observers(run)
        else:
            self._finish_run_observers(run)

        updated = result.model_copy(
            update={
                "run": run,
                "session": self.store.load_session_record(run.session_id),
            }
        )
        if drain_queued_inputs and run.status == "completed":
            await self._drain_queued_input_queue(run.session_id)
        return updated

    async def _emit_continuation_resolution(
        self,
        settlement: RunContinuationSettlement,
        events: list[EventRecord],
        *,
        extra_diagnostics: tuple[str, ...] = (),
    ) -> None:
        run = settlement.run
        checkpoint = run.continuation
        if checkpoint is None:
            return
        await self.event_recorder.emit(
            events,
            self.store.latest_event_sequence(run.session_id),
            session_id=run.session_id,
            run_id=run.run_id,
            event_type="run_continuation_resolved",
            payload={
                "decision_id": checkpoint.decision_id,
                "action": checkpoint.action,
                "controller_id": checkpoint.selected_controller_id,
                "reason": checkpoint.reason,
                "queued": settlement.queued,
                "failure_count": len(settlement.failures),
                "failures": [failure.model_dump(mode="json") for failure in settlement.failures],
                "diagnostics": [*settlement.diagnostics, *extra_diagnostics],
            },
        )

    async def _settle_terminal_run(self, run: RunRecord) -> None:
        """Run product semantics after the terminal record is durable.

        Agent lifecycle settlement is part of the Runtime boundary, not an
        observational event callback. The operation is intentionally idempotent:
        an explicit abort may settle here and then pass through the ordinary Run
        finalizer again when its active owner unwinds.
        """

        if run.status in ACTIVE_RUN_STATUSES or self._agent_controls is None:
            return
        await self._agent_controls.settle_terminal_run(run)

    def _finish_run_observers(self, run: RunRecord) -> None:
        self.run_lifecycle.finish_run(run)
        if run.status not in ACTIVE_RUN_STATUSES:
            self.event_recorder.finish_run_observers(run.run_id)
            snapshot = self._extension_snapshots.pop(run.run_id, None)
            self._session_control_snapshots.pop(run.run_id, None)
            self._run_agent_catalogs.pop(run.run_id, None)
            self._run_tool_caps.pop(run.run_id, None)
            self._run_tool_refs.pop(run.run_id, None)
            self._run_delegated_constraints.pop(run.run_id, None)
            self._run_tool_coordinators.pop(run.run_id, None)
            if snapshot is not None:
                snapshot.close()

    def _release_extension_snapshot(self, run_id: str) -> None:
        snapshot = self._detach_extension_snapshot(run_id)
        if snapshot is not None:
            snapshot.close()

    def _release_queued_extension_snapshot(self, queue_id: str) -> None:
        snapshot = self._queued_extension_snapshots.pop(queue_id, None)
        if snapshot is not None:
            snapshot.close()

    def _detach_extension_snapshot(self, run_id: str) -> ExtensionCatalogLease | None:
        snapshot = self._extension_snapshots.pop(run_id, None)
        self._session_control_snapshots.pop(run_id, None)
        self._run_agent_catalogs.pop(run_id, None)
        self._run_tool_caps.pop(run_id, None)
        self._run_tool_refs.pop(run_id, None)
        self._run_delegated_constraints.pop(run_id, None)
        self._run_tool_coordinators.pop(run_id, None)
        return snapshot

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
        raw_constraints = run.metadata.get("delegated_constraints", {})
        if not isinstance(raw_constraints, dict):
            raise ValueError("run has invalid delegated constraints")
        self._run_delegated_constraints[run.run_id] = DelegatedConstraints.model_validate(
            raw_constraints
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

    def _commit_model_request(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        model_round: int,
    ) -> RunRecord:
        """Persist one provider request after context preparation succeeds."""

        del session, model_round
        updated = run.model_copy(update={"model_round_count": run.model_round_count + 1})
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
        snapshot = (
            self._extension_catalog.snapshot() if self._extension_catalog is not None else None
        )
        return self._bind_extension_snapshot(run, snapshot)

    def _bind_extension_snapshot(
        self,
        run: RunRecord,
        snapshot: ExtensionCatalogLease | None,
    ) -> RunRecord:
        metadata = dict(run.metadata)
        metadata.setdefault("run_budget", self.run_budget.model_dump(mode="json"))
        update: dict[str, object] = {"metadata": metadata}
        if snapshot is not None:
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
            catalog = self._agent_definitions.catalog_snapshot(
                self._tools_for_run(run.run_id),
                permission_overrides=self._permission_overrides,
                plugin_permission_overrides=self._plugin_permission_overrides,
                approval_mode=session.approval_mode,
            )
            if self._agent_controls is not None:
                catalog = self._agent_controls.constrain_catalog(catalog)
            update["agent_catalog"] = catalog
            self._run_agent_catalogs[run.run_id] = catalog
        return run.model_copy(update=update)

    async def _attach_queued_extension_snapshot(
        self,
        run: RunRecord,
        inputs: list[QueuedInput],
    ) -> RunRecord:
        first = inputs[0]
        if not first.prompt_policy_checked or first.prospective_run_id != run.run_id:
            raise ValueError("queued prompt policy approval does not match prospective run")
        if any(not self._same_approved_queue_contract(first, item) for item in inputs[1:]):
            raise ValueError("approved queued inputs do not share one extension Catalog")
        snapshot = self._queued_extension_snapshots.pop(first.queue_id, None)
        for item in inputs[1:]:
            extra = self._queued_extension_snapshots.pop(item.queue_id, None)
            if extra is not None:
                extra.close()
        if snapshot is None and first.extension_catalog_id is not None:
            if self._extension_catalog is None:
                raise RuntimeError("approved queued input extension Catalog is unavailable")
            snapshot = await self._extension_catalog.snapshot_for(
                list(first.extension_generations),
                catalog_id=first.extension_catalog_id,
            )
        return self._bind_extension_snapshot(run, snapshot)

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
        previous = run.continuation
        if previous is None or previous.state == "resolved":
            if not controllers:
                return None
            return RunContinuationCheckpoint(controllers=controllers)
        existing_identities = {
            (
                controller.plugin_id,
                controller.policy_id,
                controller.execution_contract_hash,
                controller.contract_hash,
            )
            for controller in previous.controllers
        }
        retained = [*previous.controllers]
        retained.extend(
            controller
            for controller in controllers
            if (
                controller.plugin_id,
                controller.policy_id,
                controller.execution_contract_hash,
                controller.contract_hash,
            )
            not in existing_identities
        )
        return previous.model_copy(update={"controllers": retained})

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


def _queued_user_input_view(item: QueuedInput) -> QueuedUserInputView:
    return QueuedUserInputView(
        queue_id=item.queue_id,
        prompt=item.prompt,
        mode=item.mode,
        queued_at=item.queued_at,
        skill_name=(item.skill_activation.name if item.skill_activation is not None else None),
    )


def _attributed_input_source(
    attribution: AttributedSessionInput | HostSessionInput | DelegatedTaskInput,
) -> QueuedInputSource:
    if isinstance(attribution, HostSessionInput):
        return "host"
    if isinstance(attribution, DelegatedTaskInput):
        return "delegation"
    return "plugin"


def _attributed_input_initiator(
    attribution: AttributedSessionInput | HostSessionInput | DelegatedTaskInput,
) -> str:
    if isinstance(attribution, HostSessionInput):
        return attribution.host_source_id
    if isinstance(attribution, DelegatedTaskInput):
        return f"ouro.subagents/{attribution.delegation_id}"
    return attribution.plugin_id


def _input_role(
    attribution: AttributedSessionInput | HostSessionInput | DelegatedTaskInput | None,
) -> SessionInputRole:
    if attribution is None:
        return "user"
    return attribution.role


def _input_scaffold(
    attribution: AttributedSessionInput | HostSessionInput | DelegatedTaskInput | None,
) -> bool:
    if attribution is None:
        return False
    return attribution.scaffold


def _queued_input_initiator(item: QueuedInput | None) -> str | None:
    if item is None:
        return None
    if item.source == "host":
        return item.host_source_id
    if item.source == "plugin":
        return item.continuation_decision_id or item.plugin_id
    if item.source == "delegation" and item.delegation_id is not None:
        return f"ouro.subagents/{item.delegation_id}"
    return None


def _session_input_event_type(source: QueuedInputSource) -> str:
    if source == "host":
        return "host_message_added"
    if source == "plugin":
        return "plugin_message_added"
    return "user_message_added"


def _title_from_input(
    prompt: str,
    skill_invocation: SkillInvocationRequest | None,
) -> str:
    stripped = " ".join(prompt.split())
    if stripped:
        return stripped[:60]
    if skill_invocation is not None:
        return f"Skill: {skill_invocation.name}"[:60]
    return "New session"
