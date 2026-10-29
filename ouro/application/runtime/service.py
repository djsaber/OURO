from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import JsonValue

from ouro.application.agents.definitions import AgentDefinitionService
from ouro.application.agents.recovery import AgentRecoveryService
from ouro.application.context.compaction import ContextPreparationService
from ouro.application.context.prompts import PromptContextResolver
from ouro.application.context.summary import LLMSummaryCompactionStrategy
from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.contracts.models import AvailableModel, ModelReasoningState
from ouro.application.contracts.sessions import (
    SessionHistoryAnchor,
    SessionObservationSnapshot,
    SessionUndoResult,
    SessionView,
)
from ouro.application.events import (
    RuntimeEventFilter,
    RuntimeEventRecorder,
    RuntimeEventSink,
    RuntimeEventSinkRegistration,
)
from ouro.application.harness import capture_harness
from ouro.application.inputs.queue import (
    QueuedInput,
    QueuedInputMode,
    QueuedInputQueue,
    QueuedInputSource,
    QueuedUserInputView,
    input_message_events,
    project_pending_user_inputs,
    project_queued_user_input,
    same_queued_input,
    validate_host_input,
)
from ouro.application.inputs.requests import UserInputRequest
from ouro.application.inputs.skills import SkillActivationService, SkillInvocationRequest
from ouro.application.models.catalog import (
    FixedRuntimeAdapterCatalog,
    RuntimeAdapterCatalog,
    close_runtime_adapters,
)
from ouro.application.models.requests import ModelRequestPorts, RunModelRequests
from ouro.application.provider_auth.service import ProviderAuthService
from ouro.application.runs.continuations import RunContinuationService, RunContinuationSettlement
from ouro.application.runs.drivers import NativeRunDriverContext
from ouro.application.runs.lifecycle import (
    ACTIVE_RUN_STATUSES,
    DelegationStartRevoked,
    RunLifecycle,
    SessionContinuationSuperseded,
    abort_run_state,
    find_active_run,
    maybe_active_run,
    request_run_abort_state,
    require_current_continuation,
    reserve_run,
)
from ouro.application.runs.ownership import RunExecutionOwnership, RunReservation
from ouro.application.runs.recovery import RunRecoveryService
from ouro.application.runs.tool_batches import load_tool_batch
from ouro.application.runs.turns import NativeRunTurns
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.resources import RunResources
from ouro.application.session_controls import SessionControlContractError, SessionControlCoordinator
from ouro.application.sessions.editing import continuable_run
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.sessions.service import SessionService
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.application.usage import UsageService
from ouro.auth.credentials import CredentialService
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentWaitRequest, AgentWaitResolution
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.attachment_input import separate_attachments
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal, settle_owned_tasks
from ouro.core.compaction import (
    CompactionAbortedError,
    CompactionCommandResult,
    CompactionStrategyRegistry,
)
from ouro.core.context import ContextBuilder, ContextMetrics, compose_system_prompt
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
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.inner_loop import (
    InnerAgentLoop,
    TurnCompletionPolicyResult,
)
from ouro.core.input_content import (
    InputAttachment,
    InputContent,
    InputFile,
    InputFileReference,
)
from ouro.core.inputs import InputRecord
from ouro.core.interactions import (
    SESSION_INTERACTION_MODES,
    SessionInteractionMode,
)
from ouro.core.messages import (
    InputMessageReference,
    MessageRecord,
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
from ouro.core.run_budget import RunBudgetPolicy, RunPauseReason
from ouro.core.run_drivers import DEFAULT_DRIVER_ID, DriverInputDelivery, RunDriverBinding
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    AttributedSessionInput,
    AttributedSessionInputBatch,
    DelegatedTaskInput,
    HostSessionInput,
    SessionControlActivation,
    SessionControlState,
    SessionControlTransitionResult,
    SessionInputRequest,
    SessionInputRole,
)
from ouro.core.session_titles import normalize_session_title, with_session_title
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunRecord,
    SessionContinuationRequest,
    SessionRecord,
)
from ouro.core.tool_batch_transitions import resolve_tool_approval, tool_batch_suspension
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tool_facts import ToolCallIdentity
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.tool_requests import (
    DriverToolBatchRequest,
    ModelToolBatchRequest,
    ToolBatchHandlingResult,
)
from ouro.core.tools import PermissionMode, ToolDefinition
from ouro.core.turn_policies import TurnCompletionCandidate, TurnPolicyRegistry
from ouro.plugins.run_drivers import run_driver_invocation
from ouro.prompts import DEFAULT_SYSTEM_PROMPT, render_agent_skills_prompt
from ouro.sessions import RuntimeSessionSnapshot, SessionStore
from ouro.skills import SkillMetadata
from ouro.tools import ToolExecutor, ToolRegistry, ToolResult
from ouro.tools.builtin.agents import agent_control_tools

if TYPE_CHECKING:
    from ouro.application.resources import ResourceService
    from ouro.application.tool_calls import AgentControlExecutor
    from ouro.plugins.catalog.snapshot import ExtensionCatalogLease
    from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
    from ouro.plugins.providers import ProviderBinding


_LOG = logging.getLogger(__name__)


class _QueuedInputSelectionChanged(RuntimeError):
    """A queued-input snapshot changed before its durable consumption boundary."""


class RuntimeService:
    """Coordinate runs with static instructions or an injected per-request resolver.

    When supplied, prompt_context_resolver owns the full personal system prompt;
    system_prompt and additional_system_prompt remain startup values only.
    """

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
        prompt_context_resolver: PromptContextResolver | None = None,
        event_sink: RuntimeEventSink | None = None,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
        plugin_permission_overrides: Mapping[str, PermissionMode] | None = None,
        tool_execution_config: ToolExecutionConfig | None = None,
        compaction_config: CompactionConfig | None = None,
        compaction_strategies: CompactionStrategyRegistry | None = None,
        compaction_adapter: RuntimeAdapter | None = None,
        compaction_adapter_error: str | None = None,
        compaction_adapter_resolver: Callable[[], RuntimeAdapter] | None = None,
        continuations: RunContinuationRegistry | None = None,
        prompt_policies: PromptPolicyRegistry | None = None,
        tool_policies: ToolPolicyRegistry | None = None,
        turn_policies: TurnPolicyRegistry | None = None,
        queued_input_config: QueuedInputConfig | None = None,
        skills: tuple[SkillMetadata, ...] = (),
        skill_catalog_resolver: Callable[[], tuple[SkillMetadata, ...]] | None = None,
        resources: ResourceService | None = None,
        usage_service: UsageService | None = None,
        default_workspace: str | Path | None = None,
        environment: RuntimeEnvironment | None = None,
        owned_auxiliary_adapters: tuple[RuntimeAdapter, ...] = (),
        provider_auth: ProviderAuthService | None = None,
        credential_service: CredentialService | None = None,
    ) -> None:
        if (provider_auth is None) != (credential_service is None):
            raise ValueError(
                "runtime authentication requires both application and credential services"
            )
        self.provider_auth = provider_auth
        self._credential_service = credential_service
        self.store = store
        self.environment = environment or RuntimeEnvironment()
        self.resources = resources
        self.usage = usage_service
        self.sessions = SessionService(store, resources=resources, usage_service=usage_service)
        self.session_recovery = SessionRecoveryService(store, usage_service=usage_service)
        self.session_controls = SessionControlCoordinator(store, usage_service=usage_service)
        self.queued_inputs = QueuedInputQueue(store)
        self.queued_input_config = queued_input_config or QueuedInputConfig()
        self.adapter = adapter
        self.models = model_catalog or FixedRuntimeAdapterCatalog(adapter)
        self._owned_auxiliary_adapters = owned_auxiliary_adapters
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self.tools = tools
        self.skills = tuple(skill.model_copy(deep=True) for skill in skills)
        self.skill_catalog_resolver = skill_catalog_resolver
        self._skill_refresh_lock = asyncio.Lock()
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
            usage_service=usage_service,
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
        self.additional_system_prompt = additional_system_prompt or ""
        self.compaction_config = compaction_config or CompactionConfig()
        self.compaction_strategies = compaction_strategies or CompactionStrategyRegistry()
        configured_adapter_error = compaction_adapter_error
        if (
            self.compaction_config.model.has_configured_route
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
                    configured_adapter_resolver=compaction_adapter_resolver,
                    model_config=self.compaction_config.model,
                    runtime_adapter_resolver=self._adapter_for_session_id,
                    prepare_requests=self.run_lifecycle.prepare_summary_requests,
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
            system_prompt=compose_system_prompt(
                self.system_prompt,
                self.additional_system_prompt,
            ),
            prompt_context_resolver=prompt_context_resolver,
            append_context_message=self.run_lifecycle.append_input_message,
            usage_service=usage_service,
            prepare_model_messages=(
                resources.prepare_model_messages if resources is not None else None
            ),
        )
        self.run_ownership = RunExecutionOwnership(store)
        self._extension_catalog: PluginRuntimeResource | None = None
        self._extensions_bound = False
        self._run_resources: dict[str, RunResources] = {}
        self._agent_definitions: AgentDefinitionService | None = None
        self._agent_controls: AgentControlExecutor | None = None

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
        for resources in self._run_resources.values():
            if resources.tool_calls is None:
                continue
            resources.tool_calls.executor.replace_permission_overrides(
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

    async def bind_provider(self, protocol: str) -> ProviderBinding:
        if self._extension_catalog is None:
            raise ConfigError("plugin provider runtime is unavailable")
        return await self._extension_catalog.bind_provider(protocol)

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
            self.event_recorder.set_runtime_event_observer_matcher(self._has_runtime_event_observer)

    def _has_runtime_event_observer(
        self,
        run_id: str | None,
        event_type: str,
    ) -> bool:
        snapshot = self._extension_snapshot_for_run(run_id)
        if snapshot is not None:
            assert run_id is not None
            return snapshot.observers.has_registration(
                "runtime_event",
                runtime_event_type=event_type,
                active_controller_ids=lambda: self._active_controller_ids_for_run(run_id),
            )
        if self._extension_catalog is not None:
            # Application events without a retained Run snapshot resolve against
            # the current immutable Catalog at publication time.
            return True
        return self.observers.has_registration(
            "runtime_event",
            runtime_event_type=event_type,
        )

    def _observers_for_observation(
        self,
        observation: Observation,
    ) -> tuple[ObserverRegistry, Callable[[], None]]:
        run_id = observation.run_id
        snapshot = self._extension_snapshot_for_run(run_id)
        if snapshot is not None:
            catalog = snapshot.catalog
        elif self._extension_catalog is not None:
            catalog = self._extension_catalog.catalog()
        else:
            return self.observers.scoped(lambda: set()), lambda: None
        retained = catalog.bind_observers(
            observation,
            active_controller_ids=(
                lambda: self._active_controller_ids_for_run(run_id) if run_id else set()
            ),
        )
        return retained.value, retained.close

    def _tools_for_run(self, run_id: str) -> ToolRegistry:
        resources = self._run_resources.get(run_id)
        snapshot = resources.snapshot if resources is not None else None
        registry = snapshot.tools if snapshot is not None else self.tools
        if snapshot is None and resources is not None and resources.host_tools is not None:
            registry = resources.host_tools
        scoped = registry.scoped(lambda: self._active_controller_ids_for_run(run_id))
        cap = resources.tool_names if resources is not None else None
        selected = scoped if cap is None else scoped.selected(cap.intersection(scoped.names()))
        if resources is not None and resources.tool_refs:
            ceilings = {reference.public_name: reference for reference in resources.tool_refs}
            selected = selected.selected(
                definition.name
                for definition in selected.definitions()
                if definition.name in ceilings
                and ceilings[definition.name].permits(
                    AgentDefinitionService.tool_reference(selected, definition)
                )
            )
        catalog = resources.agent_catalog if resources is not None else None
        if catalog is None or self._agent_controls is None:
            return selected
        return selected.overlay(agent_control_tools(catalog))

    def _context_providers_for_run(self, run_id: str) -> ContextProviderRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.context_providers if snapshot is not None else self.context_providers
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _prompt_policies_for_run(self, run_id: str) -> PromptPolicyRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.prompt_policies if snapshot is not None else self.prompt_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _skills_for_run(self, run_id: str) -> tuple[SkillMetadata, ...]:
        snapshot = self._extension_snapshot_for_run(run_id)
        return snapshot.skills if snapshot is not None else self.skills

    def _skill_instructions_for_run(self, run_id: str) -> str:
        return render_agent_skills_prompt(self._skills_for_run(run_id))

    async def _resolve_skill_activations_for_run(
        self,
        run_id: str,
        requests: tuple[SkillInvocationRequest, ...],
    ) -> tuple[SkillActivationContent, ...]:
        if not requests:
            return ()
        return await run_blocking(
            self.skill_activations.resolve_user_activations,
            self._skills_for_run(run_id),
            requests,
        )

    def _compaction_for_run(self, run_id: str) -> CompactionStrategyRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        return snapshot.compaction if snapshot is not None else self.compaction_strategies

    def _continuations_for_run(
        self,
        run_id: str,
        *,
        active_only: bool = True,
    ) -> RunContinuationRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.continuations if snapshot is not None else self.continuations
        if not active_only:
            return registry
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _tool_policies_for_run(self, run_id: str) -> ToolPolicyRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.tool_policies if snapshot is not None else self.tool_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _turn_policies_for_run(self, run_id: str) -> TurnPolicyRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.turn_policies if snapshot is not None else self.turn_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _active_controller_ids_for_run(self, run_id: str) -> set[str]:
        resources = self._run_resources.get(run_id)
        return set(resources.controller_ids) if resources is not None else set()

    def _session_controllers_for_run(self, run_id: str):
        from ouro.plugins.session_controllers import PluginSessionControllerRegistry

        snapshot = self._extension_snapshot_for_run(run_id)
        return (
            snapshot.session_controllers
            if snapshot is not None
            else PluginSessionControllerRegistry()
        )

    def _tool_calls_for_run(self, run_id: str) -> ToolCallCoordinator:
        resources = self._run_resources.get(run_id)
        if resources is None:
            resources = RunResources()
            self._run_resources[run_id] = resources
        if resources.tool_calls is not None:
            return resources.tool_calls
        tools = self._tools_for_run(run_id)
        delegated = resources.constraints
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
                    for reference in resources.tool_refs
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
            readable_directory=self.resources.directory_for if self.resources else None,
            tool_policies=self._tool_policies_for_run(run_id),
            session_controls=self.session_controls,
            session_controllers=self._session_controllers_for_run(run_id),
            agent_controls=self._agent_controls,
            refresh_controls=lambda controls: self._bind_control_state(run_id, controls),
        )
        resources.tool_calls = coordinator
        return coordinator

    def add_event_sink(
        self,
        event_sink: RuntimeEventSink,
        *,
        event_filter: RuntimeEventFilter | None = None,
    ) -> RuntimeEventSinkRegistration:
        return self.event_recorder.add_event_sink(event_sink, event_filter=event_filter)

    async def drain_events(self) -> None:
        """Wait until every event observer has processed the published prefix."""

        await self.event_recorder.drain()

    def session_control_activations(
        self,
        session_id: str,
    ) -> tuple[SessionControlActivation, ...]:
        session = self.store.load_session_record(session_id)
        return tuple(item.model_copy(deep=True) for item in session.controls.activations)

    async def reconcile_pending_run_continuations(
        self,
        session_id: str | None = None,
    ) -> tuple[str, ...]:
        """Replay unfinished settlements for a target or explicit full maintenance."""

        settled_run_ids: list[str] = []
        session_ids = (
            (session_id,)
            if session_id is not None
            else tuple(entry.session_id for entry in await run_blocking(self.store.list_sessions))
        )
        for candidate_id in session_ids:
            for run in await run_blocking(self.store.load_pending_runs, candidate_id):
                if not self._run_continuation_is_pending(run):
                    continue
                try:
                    settlement = await self._settle_run_continuation(run)
                    if not settlement.settled:
                        continue
                    settled_run_ids.append(run.run_id)
                    await self.event_recorder.publish_committed(settlement.events)
                finally:
                    self._release_run_resources(run.run_id)
        return tuple(settled_run_ids)

    async def reconcile_execution_session(
        self,
        session_id: str,
    ) -> RuntimeSessionSnapshot:
        """Recover execution state for exactly one Session."""

        snapshot = await run_blocking(self.session_recovery.recover_runtime, session_id)
        settled = await self.reconcile_pending_run_continuations(session_id)
        return (
            await run_blocking(self.store.load_runtime_session, session_id) if settled else snapshot
        )

    async def reconcile_session(self, session_id: str) -> tuple[Diagnostic, ...]:
        """Recover only the target Session and its bounded Agent aggregate."""

        _, diagnostics = await self._reconcile_session(session_id)
        return diagnostics

    async def _reconcile_session(
        self,
        session_id: str,
    ) -> tuple[RuntimeSessionSnapshot, tuple[Diagnostic, ...]]:
        """Return the post-reconciliation target snapshot for execution."""

        session = await run_blocking(self.store.load_session_record, session_id)
        if self._agent_controls is None:
            return await self.reconcile_execution_session(session_id), ()

        diagnostics: list[Diagnostic] = []
        root_session_id = session.agent_root_session_id
        target_snapshot: RuntimeSessionSnapshot | None = None
        for candidate_id in await run_blocking(self._agent_controls.recovery_session_ids, session):
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
        return await run_blocking(self.store.load_runtime_session, session_id), tuple(diagnostics)

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
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await settle_owned_tasks(self._close_task)
        self._close_task.result()

    async def _close(self) -> None:
        errors: list[Exception] = []
        if self.provider_auth is not None:
            try:
                await self.provider_auth.close()
            except Exception as exc:
                errors.append(exc)
        for run_id in tuple(self._run_resources):
            try:
                self._release_run_resources(run_id)
            except Exception as exc:  # noqa: BLE001 - close every owned resource.
                errors.append(exc)
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
        if self._credential_service is not None:
            try:
                await self._credential_service.close()
            except Exception as exc:
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

    async def refresh_skills(self, skills: tuple[SkillMetadata, ...] | None = None) -> None:
        """Resolve and publish directory skills at a use boundary, without polling."""

        async with self._skill_refresh_lock:
            if skills is None:
                if self.skill_catalog_resolver is None:
                    return
                skills = await run_blocking(self.skill_catalog_resolver)
            if self._extension_catalog is not None:
                await self._extension_catalog.replace_base_skills(skills)
            self.skills = tuple(skill.model_copy(deep=True) for skill in skills)

    async def available_skills(self) -> tuple[SkillMetadata, ...]:
        """Return current directory and active plugin skills for one Host query."""

        await self.refresh_skills()
        await self._synchronize_extensions()
        if self._extension_catalog is None:
            return self.skill_activations.list_for_host(self.skills)
        lease = self._extension_catalog.snapshot()
        try:
            return self.skill_activations.list_for_host(lease.skills)
        finally:
            lease.close()

    def pending_user_inputs(self, session_id: str) -> tuple[QueuedUserInputView, ...]:
        """Return direct user inputs still waiting at a Run or model-turn boundary."""

        with self.store.session_read(session_id):
            session = self.store.load_session_record(session_id)
            return project_pending_user_inputs(
                session, self.queued_inputs.eligible_session(session)
            )

    async def cancel_pending_user_input(
        self,
        session_id: str,
        queue_id: str,
    ) -> QueuedUserInputView | None:
        """Cancel one direct user input if it has not crossed a durable boundary."""

        result = await run_blocking(
            self.queued_inputs.cancel_user_input, session_id=session_id, queue_id=queue_id
        )
        if result is None:
            return None
        session, cancelled, events = result
        await self.event_recorder.publish_committed(events)
        return project_queued_user_input(cancelled, waiting_on_run_id=session.active_run_id)

    def current_model(self, session_id: str | None = None) -> AvailableModel:
        if session_id is None:
            return self.models.default_model()
        session = self.store.load_session_record(session_id)
        return self.models.resolve(session.current_model or self.models.default_model().key)

    def session_context_metrics(self, session_id: str) -> ContextMetrics | None:
        """Read durable usage, projecting current model limits when resolvable.

        Model configuration is optional presentation metadata here. An unresolved
        route retains the recorded limits; it does not invalidate Session history.
        """

        metrics = self.sessions.effective_context_metrics(session_id)
        if metrics is None:
            return None
        try:
            model = self.current_model(session_id)
        except ConfigError:
            return metrics
        return metrics.with_budget(model.context_budget)

    def observation_snapshot(
        self,
        session_id: str,
        *,
        history_limit: int | None = None,
        history_anchor: SessionHistoryAnchor | None = None,
    ) -> SessionObservationSnapshot:
        """Read display facts, then resolve optional model limits outside the read boundary."""

        snapshot = self.sessions.observation_snapshot(
            session_id, history_limit=history_limit, history_anchor=history_anchor
        )
        metrics = snapshot.effective_context_metrics
        if metrics is not None:
            try:
                model = self.models.resolve(
                    snapshot.session.current_model or self.models.default_model().key
                )
            except ConfigError:
                pass
            else:
                snapshot = snapshot.model_copy(
                    update={"effective_context_metrics": metrics.with_budget(model.context_budget)}
                )
        return snapshot

    def resolve_model(self, selector: str) -> AvailableModel:
        """Resolve and validate a model route before it is attached to a session."""

        model = self.models.resolve(selector)
        self.models.adapter_for(model.key)
        return model

    def configured_default_model(self) -> AvailableModel:
        model = self.models.configured_default_model()
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

        return self.reasoning_for_session(self.sessions.view(session_id))

    def reasoning_for_session(self, session: SessionView) -> ModelReasoningState:
        """Project an observed Session's model settings without rereading or opening an adapter."""
        model = self.models.resolve(session.current_model or self.models.default_model().key)
        return self._model_reasoning_state(model, session.reasoning_request)

    def create_session(
        self,
        *,
        title: str | None = None,
        _session_id: str | None = None,
        namespace: str = "default",
        initial_model: str | None = None,
        reasoning_request: ReasoningRequest | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
        workspace: str | Path | None = None,
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
            namespace=namespace,
            title=title,
            current_model=selected_model.key,
            reasoning_request=selected_reasoning,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            workspace=(
                str(Path(workspace).expanduser().resolve())
                if workspace is not None
                else self.default_workspace
            ),
        )
        return self.store.load_session_record(created.session.session_id)

    def prepare_run(
        self,
        run: RunRecord,
        *,
        model: str,
        reasoning: ReasoningRequest,
        workspace: str,
        approval_mode: SessionApprovalMode,
        interaction_mode: SessionInteractionMode,
        title: str | None = None,
    ) -> RunRecord:
        """Atomically bind execution settings and reserve a queued application input."""

        selected = self.resolve_model(model)
        self._model_reasoning_state(selected, reasoning)
        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"Invalid approval mode: {approval_mode}")
        if interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"Invalid interaction mode: {interaction_mode}")
        if title is not None:
            title = normalize_session_title(title)

        def configure(session: SessionRecord) -> SessionRecord:
            resolved_workspace = str(Path(workspace).expanduser().resolve())
            if title is not None:
                session = with_session_title(session, title, source="user")
            return session.model_copy(
                update={
                    "current_model": selected.key,
                    "reasoning_request": reasoning,
                    "workspace": resolved_workspace,
                    "workspace_trusted": session.workspace_trusted
                    and session.workspace == resolved_workspace,
                    "approval_mode": approval_mode,
                    "interaction_mode": interaction_mode,
                }
            )

        return reserve_run(
            self.store,
            run.model_copy(update={"model": selected.key}),
            configure_session=configure,
        )

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
            model = self.models.resolve(session.current_model or self.models.default_model().key)
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

    async def accept_user_input(self, request: UserInputRequest) -> HostInputReceipt:
        """Accept ordinary user content durably, without acquiring execution ownership."""
        session_id = request.session_id or request.new_session_id
        assert session_id is not None
        existing = await run_blocking(self._user_input_receipt, session_id, request)
        if existing is not None:
            return existing
        if request.skill_invocations:
            await self.refresh_skills()
        await self._synchronize_extensions()
        if request.session_id is None:
            session, _, prospective = await self._accept_new_user_input(
                prompt=request.prompt,
                new_session_id=session_id,
                input_id=request.input_id,
                title=request.title,
                initial_model=request.initial_model,
                initial_reasoning_request=request.initial_reasoning_request,
                approval_mode=request.approval_mode,
                interaction_mode=request.interaction_mode,
                workspace=request.workspace,
                queue_mode=request.queue_mode,
                skill_invocations=request.skill_invocations,
                input_resource_refs=request.input_resource_refs,
                attachments=request.attachments,
                input_driver=self._select_input_driver(None),
            )
            # Accepted input carries the policy/skill facts; scheduling does not retain a lease.
            self._release_run_resources(prospective.run_id)
        else:
            snapshot = await self._load_submission_snapshot(session_id)
            session = snapshot.session
            if any(
                run.history_epoch == session.history_epoch
                and self._run_continuation_is_pending(run)
                for run in snapshot.runs
            ):
                raise SessionBusyError("Session has an unresolved terminal Run continuation")
            active = maybe_active_run(snapshot.runs, history_epoch=session.history_epoch)
            self._validate_model_resource_refs(
                session, request.input_resource_refs, request.attachments
            )
            if active is not None:
                self._validate_requested_session_policies(
                    session,
                    approval_mode=request.approval_mode,
                    interaction_mode=request.interaction_mode,
                )
            else:
                updates = {
                    name: value
                    for name, value in (
                        ("approval_mode", request.approval_mode),
                        ("interaction_mode", request.interaction_mode),
                    )
                    if value is not None and value != getattr(session, name)
                }
                if updates:
                    session = await run_blocking(
                        self._persist_session_policy_updates, session_id, updates, require_idle=True
                    )
                    snapshot = snapshot.model_copy(update={"session": session})
            await self._queue_input(
                snapshot=snapshot,
                active_run=active,
                prompt=request.prompt,
                skill_invocations=request.skill_invocations,
                title=request.title,
                mode=request.queue_mode,
                resource_refs=request.input_resource_refs,
                attachments=request.attachments,
                input_id=request.input_id,
                driver=self._select_input_driver(None),
            )
        receipt = await run_blocking(self._user_input_receipt, session_id, request)
        assert receipt is not None
        return receipt

    def _user_input_receipt(
        self, session_id: str, request: UserInputRequest
    ) -> HostInputReceipt | None:
        with self.store.session_read(session_id):
            try:
                record = self.store.load_input(session_id, request.input_id)
            except FileNotFoundError:
                if request.session_id is not None:
                    raise
                return None
            if record is None:
                return None
            item = record.item
            if (
                item.source != "user"
                or item.prompt != request.prompt
                or item.title != request.title
                or item.mode != request.queue_mode
                or item.attachments != request.attachments
                or item.resource_refs != request.input_resource_refs
                or [
                    (skill.name, skill.text_offset, skill.order) for skill in item.skill_activations
                ]
                != [
                    (skill.name, skill.text_offset, skill.order)
                    for skill in request.skill_invocations
                ]
            ):
                raise ValueError("Input id is already bound to another input")
            # Consumption removes the queue item and writes its message atomically.
            # A still-pending input cannot have a canonical message to search for.
            state = self.store.load_input_state(session_id, item.queue_id)
            if state is None:
                raise PersistenceError("accepted input has no lifecycle state")
            if state.status in {"cancelled", "withdrawn"}:
                raise ValueError("Input was already accepted and has been withdrawn or cancelled")
            message = (
                self.store.load_message(session_id, state.message_ids[-1])
                if state.message_ids
                else None
            )
            return HostInputReceipt(
                session_id=session_id,
                queue_id=item.queue_id,
                accepted_at=item.queued_at,
                message_id=message.message_id if message is not None else None,
                run_id=message.run_id if message is not None else None,
            )

    async def _accept_new_user_input(
        self,
        *,
        prompt: str,
        new_session_id: str | None,
        input_id: str | None,
        title: str | None,
        initial_model: str | None,
        initial_reasoning_request: ReasoningRequest | None,
        approval_mode: SessionApprovalMode | None,
        interaction_mode: SessionInteractionMode | None,
        workspace: str | Path | None,
        queue_mode: QueuedInputMode,
        skill_invocations: tuple[SkillInvocationRequest, ...],
        input_resource_refs: tuple[str, ...],
        attachments: tuple[InputFile, ...],
        input_driver: RunDriverBinding | None,
    ) -> tuple[SessionRecord, QueuedInput, RunRecord]:
        selected_model = (
            self.resolve_model(initial_model)
            if initial_model is not None
            else self.models.default_model()
        )
        selected_reasoning = initial_reasoning_request or ReasoningRequest()
        self._model_reasoning_state(selected_model, selected_reasoning)
        candidate_session = SessionRecord(
            session_id=new_session_id or new_id("ses"),
            workspace=(
                str(Path(workspace).expanduser().resolve())
                if workspace is not None
                else self.default_workspace
            ),
            current_model=selected_model.key,
            reasoning_request=selected_reasoning,
            approval_mode=approval_mode or "inherit",
            interaction_mode=interaction_mode or "interactive",
        )
        candidate_session = with_session_title(candidate_session, title)
        if attachments:
            if self.resources is None:
                raise ResourceAttachmentError("File inputs require a configured ResourceService")
            self.resources.validate_file_inputs(attachments, candidate_session.workspace)
        preapproved_new_run = RunRecord(
            session_id=candidate_session.session_id,
            status="running",
            origin="user",
            trigger="user",
            driver=input_driver or RunDriverBinding(),
        )
        if self._extension_catalog is not None:
            self._run_resources[preapproved_new_run.run_id] = RunResources(
                snapshot=self._extension_catalog.snapshot()
            )
        try:
            resolved_skill_activations = await self._resolve_skill_activations_for_run(
                preapproved_new_run.run_id, skill_invocations
            )
            candidate_input = QueuedInput(
                queue_id=input_id or new_id("queued"),
                prompt=prompt,
                driver=input_driver,
                attachments=attachments,
                resource_refs=input_resource_refs,
                title=title,
                mode=queue_mode,
                queued_at=utc_now(),
                source="user",
                role="user",
                scaffold=False,
                semantic_tag=None,
                skill_activations=resolved_skill_activations,
            )
            await self._evaluate_prompt_policies(
                session=candidate_session,
                run=preapproved_new_run,
                harness=HarnessCallSnapshot(
                    session_id=candidate_session.session_id,
                    harness_version=candidate_session.harness_version,
                    history_epoch=candidate_session.history_epoch,
                    run_id=preapproved_new_run.run_id,
                    state=HarnessState(),
                    usage_tokens={} if self.usage is not None else None,
                ),
                inputs=[candidate_input],
            )
            extension_snapshot = self._extension_snapshot_for_run(preapproved_new_run.run_id)
            first_input = candidate_input.model_copy(
                update={
                    "prompt_policy_checked": True,
                    "prospective_run_id": preapproved_new_run.run_id,
                    "extension_catalog_id": extension_snapshot.catalog_id
                    if extension_snapshot
                    else None,
                    "extension_generations": tuple(
                        generation.model_dump(mode="json", exclude_none=True)
                        for generation in extension_snapshot.generations
                    )
                    if extension_snapshot
                    else (),
                }
            )
            session = await run_blocking(self._accept_initial_input, candidate_session, first_input)
        except BaseException:
            self._release_run_resources(preapproved_new_run.run_id)
            raise
        return session, first_input, preapproved_new_run

    async def run(
        self,
        prompt: str,
        *,
        skill_invocations: tuple[SkillInvocationRequest, ...] = (),
        skill_invocation: SkillInvocationRequest | None = None,
        session_id: str | None = None,
        new_session_id: str | None = None,
        input_id: str | None = None,
        title: str | None = None,
        initial_model: str | None = None,
        initial_reasoning_request: ReasoningRequest | None = None,
        queue_mode: QueuedInputMode = "follow_up",
        approval_mode: SessionApprovalMode | None = None,
        interaction_mode: SessionInteractionMode | None = None,
        workspace: str | Path | None = None,
        resource_refs: list[str] | tuple[str, ...] | None = None,
        attachments: tuple[InputFile, ...] = (),
        driver_id: str | None = None,
        _drain_queued_inputs: bool = True,
        _queued_inputs: list[QueuedInput] | None = None,
        _input_attribution: (
            AttributedSessionInput | HostSessionInput | DelegatedTaskInput | None
        ) = None,
        _prepared_run: RunRecord | None = None,
        _reservation: RunReservation | None = None,
        _continuation_request: SessionContinuationRequest | None = None,
        _on_provider_stream_start: Callable[[], None] | None = None,
        on_session_created: Callable[[SessionRecord], None] | None = None,
        on_input_accepted: Callable[[], None] | None = None,
    ) -> RunResult:
        if new_session_id is not None and (
            session_id is not None or _input_attribution is not None
        ):
            raise ValueError("new_session_id requires a new direct-user Session")
        if approval_mode is not None and approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if interaction_mode is not None and interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        if initial_model is not None and session_id is not None:
            raise ValueError("initial_model can only be set when creating a session")
        if initial_reasoning_request is not None and session_id is not None:
            raise ValueError("initial_reasoning_request can only be set when creating a session")
        if workspace is not None and session_id is not None:
            raise ValueError("workspace can only be set when creating a session")
        if skill_invocation is not None:
            if skill_invocations:
                raise ValueError("use either skill_invocation or skill_invocations")
            skill_invocations = (skill_invocation,)
        if skill_invocations:
            arguments = skill_invocations[0].arguments
            if (prompt and prompt != arguments) or any(
                item.arguments != arguments for item in skill_invocations
            ):
                raise ValueError("prompt must match the skill invocation arguments")
            prompt = arguments
            if len({item.name for item in skill_invocations}) != len(skill_invocations):
                raise ValueError("skill invocations must have unique names")
        queued_skill_activation = any(
            bool(item.skill_activations) for item in (_queued_inputs or ())
        )
        if (
            not prompt.strip()
            and not skill_invocations
            and not queued_skill_activation
            and not resource_refs
            and not attachments
            and not any(item.resource_refs or item.attachments for item in (_queued_inputs or ()))
        ):
            raise ValueError("run input requires a prompt or skill invocation")
        if skill_invocations and _input_attribution is not None:
            raise ValueError("skill invocation is only valid on direct user input")
        if skill_invocations and queue_mode != "follow_up":
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
        content = InputContent(text=prompt, attachments=attachments)
        if len(attachments) > 16:
            raise ResourceAttachmentError("A message can contain at most 16 attachments")
        input_resource_refs = tuple(dict.fromkeys(resource_refs or content.resource_refs))
        if attachments and input_resource_refs != content.resource_refs:
            raise ResourceAttachmentError("Attachment identities must match resource references")
        if input_resource_refs and session_id is None and new_session_id is None:
            raise ResourceAttachmentError(
                "resource attachments require an existing Session or a reserved namespace; "
                "pass session_id or new_session_id"
            )
        if skill_invocations:
            await self.refresh_skills()
        await self._synchronize_extensions()
        preapproved_new_run: RunRecord | None = None
        attributed_driver = _input_attribution.driver_id if _input_attribution is not None else None
        if driver_id is not None and attributed_driver not in {None, driver_id}:
            raise ValueError("Run Driver selection conflicts with the attributed input")
        driver_id = driver_id or attributed_driver
        if driver_id is not None and (_prepared_run is not None or _queued_inputs):
            raise ValueError("accepted work already has a Driver binding")
        if driver_id is not None and queue_mode != "follow_up":
            raise ValueError("Driver selection cannot steer an existing Run")
        input_driver = (
            _queued_inputs[0].driver if _queued_inputs else self._select_input_driver(driver_id)
        )
        resolved_skill_activations: tuple[SkillActivationContent, ...] = ()
        if session_id is None:
            if _input_attribution is None:
                session, first_input, preapproved_new_run = await self._accept_new_user_input(
                    prompt=prompt,
                    new_session_id=new_session_id,
                    input_id=input_id,
                    title=title,
                    initial_model=initial_model,
                    initial_reasoning_request=initial_reasoning_request,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
                    workspace=workspace,
                    queue_mode=queue_mode,
                    skill_invocations=skill_invocations,
                    input_resource_refs=input_resource_refs,
                    attachments=attachments,
                    input_driver=input_driver,
                )
                resolved_skill_activations = first_input.skill_activations
                _queued_inputs = [first_input]
            else:
                session = await run_blocking(
                    self.create_session,
                    title=title,
                    initial_model=initial_model,
                    reasoning_request=initial_reasoning_request,
                    approval_mode=approval_mode or "inherit",
                    interaction_mode=interaction_mode or "interactive",
                    workspace=workspace,
                )
            snapshot = RuntimeSessionSnapshot(session=session)
            if on_session_created is not None:
                on_session_created(session)
            if _input_attribution is None and on_input_accepted is not None:
                on_input_accepted()
                on_input_accepted = None
        else:
            session_locked = await run_blocking(
                self.run_ownership.is_locked,
                session_id,
            )
            should_reconcile = _prepared_run is None and not session_locked
            if should_reconcile:
                snapshot, reconciliation_diagnostics = await self._reconcile_session(session_id)
                self._raise_incompatible_target(
                    snapshot.session,
                    reconciliation_diagnostics,
                )
            else:
                snapshot = await run_blocking(
                    self._load_runtime_snapshot,
                    session_id,
                    recover=False,
                )
            session = snapshot.session
            require_current_continuation(session, _continuation_request)
            if (
                isinstance(_input_attribution, HostSessionInput)
                and _input_attribution.history_epoch is not None
                and _input_attribution.history_epoch != session.history_epoch
            ):
                raise SessionCommitConflict("Host input belongs to withdrawn Session history")
            pending_continuation = next(
                (
                    candidate
                    for candidate in snapshot.runs
                    if candidate.history_epoch == snapshot.session.history_epoch
                    and self._run_continuation_is_pending(candidate)
                ),
                None,
            )
            if pending_continuation is not None:
                raise SessionBusyError(
                    "session has an unresolved terminal Run continuation: "
                    f"{pending_continuation.run_id}"
                )
            active_run = maybe_active_run(
                snapshot.runs, history_epoch=snapshot.session.history_epoch
            )
            self._validate_model_resource_refs(
                session,
                input_resource_refs,
                attachments,
            )
            if active_run is not None and (
                _prepared_run is None or active_run.run_id != _prepared_run.run_id
            ):
                if _queued_inputs is not None:
                    return RunResult(session=session, run=active_run)
                self._validate_requested_session_policies(
                    session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
                )
                queued_session, _ = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    skill_invocations=skill_invocations,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attachments=attachments,
                    attribution=_input_attribution,
                    driver=input_driver,
                    on_input_accepted=on_input_accepted,
                    input_id=input_id,
                )
                return RunResult(session=queued_session, run=active_run)
        if isinstance(_input_attribution, DelegatedTaskInput) and (
            session.session_kind != "subagent"
            or session.delegation_id != _input_attribution.delegation_id
        ):
            raise ValueError("delegated task input must target its child Session")

        queued_attributed_input = next(
            (item for item in (_queued_inputs or []) if item.source != "user"),
            None,
        )
        attributed_source = (
            _attributed_input_source(_input_attribution)
            if _input_attribution is not None
            else (queued_attributed_input.source if queued_attributed_input is not None else None)
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
                history_epoch=session.history_epoch,
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
        if input_driver is not None:
            run = run.model_copy(update={"driver": input_driver})
        run_lock = await self.run_ownership.acquire(
            session.session_id,
            run.run_id,
            reservation=_reservation,
        )
        if run_lock is None:
            if _queued_inputs is not None:
                await self.run_ownership.wait_for_run_start(session.session_id)
            snapshot = await run_blocking(
                self.store.load_runtime_session,
                session.session_id,
            )
            require_current_continuation(snapshot.session, _continuation_request)
            active_run = maybe_active_run(
                snapshot.runs, history_epoch=snapshot.session.history_epoch
            )
            self._validate_model_resource_refs(
                snapshot.session,
                input_resource_refs,
                attachments,
            )
            if active_run is not None and (
                _prepared_run is None or active_run.run_id != _prepared_run.run_id
            ):
                if _queued_inputs is not None:
                    return RunResult(session=snapshot.session, run=active_run)
                self._validate_requested_session_policies(
                    snapshot.session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
                )
                queued_session, _ = await self._queue_input(
                    snapshot=snapshot,
                    active_run=active_run,
                    prompt=prompt,
                    skill_invocations=skill_invocations,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attachments=attachments,
                    attribution=_input_attribution,
                    driver=input_driver,
                    on_input_accepted=on_input_accepted,
                    input_id=input_id,
                )
                return RunResult(session=queued_session, run=active_run)
            if _queued_inputs is not None:
                # The competing owner released before publishing, or already completed.
                # Re-select against current durable inputs before trying execution again.
                raise _QueuedInputSelectionChanged()
            raise SessionBusyError(f"session is already locked: {session.session_id}")
        abort_signal: AbortSignal | None = None
        result: RunResult | None = None
        try:
            latest_session = await run_blocking(
                self.store.load_session_record,
                session.session_id,
            )
            locked_snapshot = (
                snapshot
                if latest_session == snapshot.session
                else await run_blocking(
                    self.store.load_runtime_session,
                    session.session_id,
                )
            )
            locked_snapshot = await self.run_recovery.reconcile(locked_snapshot)
            session = locked_snapshot.session
            require_current_continuation(session, _continuation_request)
            locked_active_run = maybe_active_run(
                locked_snapshot.runs, history_epoch=locked_snapshot.session.history_epoch
            )
            if locked_active_run is not None and (
                _prepared_run is None or locked_active_run.run_id != _prepared_run.run_id
            ):
                if _queued_inputs is not None:
                    return RunResult(session=session, run=locked_active_run)
                self._validate_requested_session_policies(
                    session,
                    approval_mode=approval_mode,
                    interaction_mode=interaction_mode,
                )
                queued_session, _ = await self._queue_input(
                    snapshot=locked_snapshot,
                    active_run=locked_active_run,
                    prompt=prompt,
                    skill_invocations=skill_invocations,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attachments=attachments,
                    attribution=_input_attribution,
                    driver=input_driver,
                    on_input_accepted=on_input_accepted,
                    input_id=input_id,
                )
                return RunResult(
                    session=queued_session,
                    run=locked_active_run,
                )
            policy_updates: dict[str, object] = {}
            if approval_mode is not None and approval_mode != session.approval_mode:
                policy_updates["approval_mode"] = approval_mode
            if interaction_mode is not None and interaction_mode != session.interaction_mode:
                policy_updates["interaction_mode"] = interaction_mode
            if policy_updates:
                session = await run_blocking(
                    self._persist_session_policy_updates,
                    session.session_id,
                    policy_updates,
                )
                locked_snapshot = locked_snapshot.model_copy(update={"session": session})
            session, active_adapter = await run_blocking(self._resolve_session_adapter, session)
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            if preapproved_new_run is not None:
                run = await self._bind_extension_snapshot(
                    run,
                    self._extension_snapshot_for_run(run.run_id),
                )
            elif _prepared_run is not None and "extension_generations" in run.metadata:
                await self._ensure_extension_snapshot(run)
            else:
                run = await self._attach_extension_snapshot(run)
            if skill_invocations and not resolved_skill_activations:
                resolved_skill_activations = await self._resolve_skill_activations_for_run(
                    run.run_id, skill_invocations
                )
            pending_inputs = _queued_inputs or [
                QueuedInput(
                    queue_id=input_id or new_id("queued"),
                    history_epoch=session.history_epoch,
                    prompt=prompt,
                    driver=input_driver,
                    skill_activations=resolved_skill_activations,
                    title=title,
                    mode=queue_mode,
                    resource_refs=input_resource_refs,
                    attachments=attachments,
                    queued_at=utc_now(),
                    source=(
                        _attributed_input_source(_input_attribution)
                        if _input_attribution is not None
                        else "user"
                    ),
                    role=_input_role(_input_attribution),
                    scaffold=_input_scaffold(_input_attribution),
                    semantic_tag=(
                        _input_attribution.semantic_tag if _input_attribution is not None else None
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
            if skill_invocations and (
                len(pending_inputs) != 1 or pending_inputs[0].source != "user"
            ):
                raise ValueError("skill invocation requires one direct user input")
            for pending_input in pending_inputs:
                self._validate_model_resource_refs(
                    session,
                    pending_input.resource_refs,
                    pending_input.attachments,
                )
            if preapproved_new_run is None:
                await self._evaluate_prompt_policies(
                    session=session,
                    run=run,
                    inputs=pending_inputs,
                )
            is_queued_run = _queued_inputs is not None
            if is_queued_run:
                session, run, input_messages, committed_events = await run_blocking(
                    self._start_queued_run,
                    session=session,
                    run=run,
                    snapshot=locked_snapshot,
                    inputs=pending_inputs,
                    continuation_request=_continuation_request,
                )
            else:
                input_messages = []
                for item in pending_inputs:
                    input_messages.extend(
                        await run_blocking(
                            self._create_session_input_messages,
                            session=session,
                            run=run,
                            item=item,
                            sequence=session.message_count + len(input_messages) + 1,
                            is_queued=False,
                        )
                    )
                session, run, committed_events = await run_blocking(
                    self.run_lifecycle.start,
                    session=session,
                    run=run,
                    snapshot=locked_snapshot,
                    input_messages=tuple(input_messages),
                    accepted_inputs=tuple(
                        InputRecord(session_id=session.session_id, item=item)
                        for item in pending_inputs
                    ),
                    consumed_inputs=tuple(item.queue_id for item in pending_inputs),
                    events=input_message_events(run, pending_inputs, input_messages, queued=False),
                )

            await self.event_recorder.publish_committed(committed_events)
            sequence = committed_events[-1].sequence
            for _ in pending_inputs:
                if on_input_accepted is not None:
                    on_input_accepted()

            abort_signal = await self.run_ownership.activate_signal(
                session.session_id,
                run.run_id,
            )
            result = await self._continue_run_driver(
                session=session,
                run=run,
                sequence=sequence,
                abort_signal=abort_signal,
                adapter=active_adapter,
                on_provider_stream_start=_on_provider_stream_start,
            )
        finally:
            try:
                if abort_signal is not None:
                    await self.run_ownership.release_signal(run.run_id, abort_signal)
            finally:
                if result is None:
                    self._release_run_resources(run.run_id)
                await self.run_ownership.release(run.run_id, run_lock)
        assert result is not None
        return await self._finalize_run_boundary(
            result,
            drain_queued_inputs=_drain_queued_inputs,
        )

    async def undo(
        self, session_id: str, *, message_id: str | None = None, queue_id: str | None = None
    ) -> SessionUndoResult:
        """Own the history decision through cancellation, then settle its local work."""
        task = asyncio.create_task(
            self._undo_and_settle(session_id, message_id=message_id, queue_id=queue_id)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned_tasks(task)
            return task.result()

    async def _undo_and_settle(
        self, session_id: str, *, message_id: str | None, queue_id: str | None
    ) -> SessionUndoResult:
        result = await run_blocking(
            self.sessions.undo, session_id, message_id=message_id, queue_id=queue_id
        )
        if result.removed_count:
            if self._agent_controls is not None:
                await self._agent_controls.reconcile_session(result.session)
        return result

    async def continue_session(
        self, *, session_id: str, request: SessionContinuationRequest | None = None
    ) -> RunResult | None:
        """Continue retained dialogue or consume its first pending input, without adding input."""
        snapshot = await self.reconcile_execution_session(session_id)
        if request is not None and not request.is_current(snapshot.session):
            return None
        active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
        if active is not None:
            if not active.abort_requested:
                if request is not None:
                    await run_blocking(
                        self.run_lifecycle.settle_continuation_request, session_id, request
                    )
                return RunResult(session=snapshot.session, run=active)
            await self.settle_run_abort(session_id=session_id, run_id=active.run_id)
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
        pending = await run_blocking(self._pending_queued_inputs, snapshot)
        try:
            if pending:
                return await self.consume_host_input(
                    HostInputReceipt(
                        session_id=session_id,
                        queue_id=pending[0].queue_id,
                        accepted_at=pending[0].queued_at,
                    ),
                    _continuation_request=request,
                )
            if snapshot.session.message_count:
                return await self.continue_run(
                    session_id=session_id, allow_completed=True, _continuation_request=request
                )
        except SessionContinuationSuperseded:
            return None
        if request is not None:
            await run_blocking(self.run_lifecycle.settle_continuation_request, session_id, request)
        return None

    async def continue_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        history_epoch: int | None = None,
        allow_completed: bool = False,
        _continuation_request: SessionContinuationRequest | None = None,
    ) -> RunResult:
        """Resume retained work without adding input or binding a provider route.

        Execution ownership and history epochs govern checkpoint reuse. The
        ordinary model loop binds current Session settings at its next request.
        """

        snapshot = await run_blocking(self.session_recovery.recover_runtime, session_id)
        require_current_continuation(snapshot.session, _continuation_request)
        if history_epoch is not None and history_epoch != snapshot.session.history_epoch:
            raise SessionCommitConflict("Continuation belongs to withdrawn Session history")
        source = await run_blocking(
            continuable_run, self.store, snapshot, allow_completed=allow_completed
        )
        if run_id is not None and source.run_id != run_id:
            raise SessionBusyError("The requested continuation is no longer the latest run")
        fresh = (
            source.history_epoch != snapshot.session.history_epoch or source.status == "completed"
        )
        run = (
            RunRecord(
                session_id=session_id,
                history_epoch=snapshot.session.history_epoch,
                origin="user",
                trigger="continue",
            )
            if fresh
            else source
        )
        lock = await self.run_ownership.acquire_parked(
            session_id,
            run.run_id,
            expected_status=source.status,
            source_run_id=source.run_id,
        )
        if lock is None:
            raise SessionBusyError("The session already has an execution owner")
        signal: AbortSignal | None = None
        result: RunResult | None = None
        started = False
        try:
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            require_current_continuation(snapshot.session, _continuation_request)
            latest_run = await run_blocking(
                continuable_run, self.store, snapshot, allow_completed=allow_completed
            )
            if (
                latest_run.run_id != source.run_id
                or run.history_epoch != snapshot.session.history_epoch
            ):
                raise SessionBusyError(
                    "The latest run changed before continuation acquired ownership"
                )
            if self._run_continuation_is_pending(latest_run):
                settlement = await self._settle_run_continuation(latest_run)
                if settlement.settled:
                    await self.event_recorder.publish_committed(settlement.events)
                if self._run_continuation_is_pending(settlement.run):
                    detail = (
                        "; ".join(settlement.diagnostics)
                        or "terminal policy settlement is still pending"
                    )
                    raise ValueError(f"Cannot continue until terminal state is settled: {detail}")
                snapshot = await run_blocking(self.store.load_runtime_session, session_id)
                latest_run = await run_blocking(
                    continuable_run, self.store, snapshot, allow_completed=allow_completed
                )
            if fresh:
                if snapshot.session.session_kind == "subagent":
                    if self._agent_controls is None:
                        raise ValueError("Child continuation requires the Agent service")
                    run = await self._agent_controls.prepare_history_continuation(run)
                    snapshot = await run_blocking(self.store.load_runtime_session, session_id)
                run = await self._attach_extension_snapshot(run)
            else:
                run = latest_run
                await self._ensure_extension_snapshot(run)
                snapshot = await self.run_recovery.reconcile(
                    snapshot, run_ids=frozenset({run.run_id})
                )
                run = await run_blocking(continuable_run, self.store, snapshot)
            metadata = dict(run.metadata)
            metadata.pop("pause", None)
            metadata.pop("abort_reason", None)
            run = run.model_copy(
                update={
                    "ended_at": None,
                    "error": None,
                    "error_code": None,
                    "abort_requested": False,
                    "pending_approval_id": None,
                    "active_tool_batch": run.active_tool_batch,
                    "phase": "model_streaming",
                    "continuation": None,
                    "metadata": metadata,
                }
            )
            session, run, committed_events = await run_blocking(
                self.run_lifecycle.start,
                session=snapshot.session,
                run=run,
                snapshot=snapshot,
                resume=not fresh,
                continuation_request=_continuation_request,
            )
            started = True
            await self.event_recorder.publish_committed(committed_events)
            sequence = committed_events[-1].sequence
            if not fresh and self._agent_controls is not None:
                await self._agent_controls.continue_children(run)
            signal = await self.run_ownership.activate_signal(session_id, run.run_id)
            result = await self._continue_run_driver(
                session=session,
                run=run,
                sequence=sequence,
                abort_signal=signal,
                adapter=self.adapter,
                resuming_driver=not fresh,
            )
        except Exception as exc:  # noqa: BLE001 - continuation owns its durable failure boundary.
            if not started:
                raise
            result = await self.run_lifecycle.fail(
                session=session,
                run=run,
                error=f"run continuation failed: {exc}",
                final_text="",
                had_tool_error=False,
            )
        finally:
            try:
                if signal is not None:
                    await self.run_ownership.release_signal(run.run_id, signal)
            finally:
                if result is None:
                    self._release_run_resources(run.run_id)
                await self.run_ownership.release(run.run_id, lock)
        return await self._finalize_run_boundary(result, drain_queued_inputs=True)

    async def run_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput | DelegatedTaskInput,
        *,
        reservation: RunReservation | None = None,
    ) -> RunResult:
        """Execute an already durable queued Run with typed input attribution."""

        queued_inputs = None
        if isinstance(session_input, HostSessionInput) and session_input.queue_id is not None:
            accepted = await run_blocking(
                self.store.load_input, run.session_id, session_input.queue_id
            )
            if accepted is None or accepted.item.prospective_run_id != run.run_id:
                raise PersistenceError("prepared Host input does not belong to its queued Run")
            validate_host_input(accepted, session_input)
            queued_inputs = [accepted.item]
        try:
            return await self.run(
                session_input.prompt,
                session_id=run.session_id,
                title=session_input.title,
                queue_mode=session_input.mode,
                resource_refs=session_input.resource_refs,
                _input_attribution=session_input,
                _prepared_run=run,
                _queued_inputs=queued_inputs,
                _reservation=reservation,
            )
        except DelegationStartRevoked as exc:
            return await self.abort_run(
                session_id=run.session_id, run_id=run.run_id, reason=str(exc)
            )

    async def _continue_run_driver(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        sequence: int,
        abort_signal: AbortSignal,
        adapter: RuntimeAdapter,
        on_provider_stream_start: Callable[[], None] | None = None,
        resuming_driver: bool = False,
        resume_turn_id: str | None = None,
    ) -> RunResult:
        try:
            model_requests = RunModelRequests(
                adapter=adapter,
                tool_definitions=lambda: self._tools_for_run(run.run_id).definitions(),
                run_budget=self._run_budget_for_run(run),
                ports=ModelRequestPorts(
                    prepare_context=lambda *args, **kwargs: self.context_preparation.prepare(
                        *args,
                        context_providers=self._context_providers_for_run(run.run_id),
                        strategies=self._compaction_for_run(run.run_id),
                        skill_instructions=self._instructions_for_run(run),
                        **kwargs,
                    ),
                    record_settlement=self.context_preparation.record_request_settlement,
                    append_draft=self.store.append_response_draft,
                    record_retry=self.store.record_request_retry,
                    resolve_model_request=self._resolve_model_request,
                    record_usage=self.run_lifecycle.record_usage,
                    run_usage=self.usage.run_usage if self.usage is not None else None,
                    begin_model_round=self._bind_run_capabilities,
                    commit_model_request=self._commit_model_request,
                    publish_committed=self.event_recorder.publish_committed,
                    shared_budget_reason=(
                        self._shared_budget_reason if self._agent_controls is not None else None
                    ),
                    notify_provider_stream_start=on_provider_stream_start,
                    emit=self.event_recorder.emit,
                ),
            )
            context = NativeRunDriverContext(
                session=session,
                run=run,
                sequence=sequence,
                abort_signal=abort_signal,
                request_model=model_requests.execute,
                request_tools=self._execute_model_tool_batch,
                request_driver_tools=self._execute_driver_tool_batch,
                consume_inputs=self._consume_driver_inputs,
                runs=self.run_lifecycle,
                workspace=session.workspace
                or self._tool_calls_for_run(run.run_id).default_workspace,
                workspace_only=self._tool_calls_for_run(run.run_id).execution_config.workspace_only,
                resume_turn_id=resume_turn_id,
            )
            if run.driver.is_default:
                driver = InnerAgentLoop(
                    turns=NativeRunTurns(
                        context,
                        evaluate_completion=lambda *args, **kwargs: self._evaluate_turn_completion(
                            *args,
                            policies=self._turn_policies_for_run(run.run_id),
                            **kwargs,
                        ),
                    )
                )
                with run_driver_invocation(DEFAULT_DRIVER_ID, context):
                    result = await context.invoke(driver, resuming=resuming_driver)
            else:
                if self._extension_catalog is None:
                    raise ValueError("Run driver catalog is unavailable")
                async with self._extension_catalog.bind_driver(run.driver) as binding:
                    result = await context.invoke(binding.value.handler, resuming=resuming_driver)
        except Exception as exc:  # noqa: BLE001 - settle Run driver initialization failures.
            result = await self.run_lifecycle.fail(
                session=session,
                run=run,
                error=(
                    f"Run driver initialization failed: {type(exc).__name__}: "
                    f"{redact_text(str(exc))}"
                ),
                final_text="",
                had_tool_error=False,
            )
        return await self._finalize_requested_abort(result)

    async def _execute_driver_tool_batch(
        self, request: DriverToolBatchRequest
    ) -> ToolBatchHandlingResult:
        run = await self._bind_run_capabilities(
            session=request.session, run=request.run, model_round=request.run.model_round_count
        )
        session = await run_blocking(self.store.load_session_record, run.session_id)
        return await self._tool_calls_for_run(run.run_id).handle_driver_batch(
            request.sequence,
            session=session,
            run=run,
            tool_calls=request.calls,
            turn_id=request.turn_id,
            abort_signal=request.abort_signal,
            checkpoint=request.checkpoint,
        )

    async def _execute_model_tool_batch(
        self, request: ModelToolBatchRequest
    ) -> ToolBatchHandlingResult:
        session = await run_blocking(self.store.load_session_record, request.session.session_id)
        response = request.response
        return await self._tool_calls_for_run(request.run.run_id).handle_batch(
            request.sequence,
            session,
            request.run,
            list(response.tool_calls),
            turn_id=request.turn_id,
            assistant_text=response.text,
            assistant_reasoning=response.reasoning,
            assistant_model_response=response.model_response,
            assistant_continuation=response.continuation,
            abort_signal=request.abort_signal,
            driver_checkpoint=request.driver_checkpoint,
        )

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
        resources = self._run_resources[run.run_id]
        if resources.budget is not None:
            return resources.budget
        raw = run.metadata.get("run_budget")
        if raw is None:
            return self.run_budget
        if not isinstance(raw, dict):
            raise ValueError("run has invalid delegated budget metadata")
        return RunBudgetPolicy.model_validate(raw)

    async def _shared_budget_reason(
        self,
        run: RunRecord,
        next_model_round: int,
    ) -> RunPauseReason | None:
        controls = self._agent_controls
        if controls is None:
            return None
        return await run_blocking(controls.budget_reason, run, next_model_round)

    async def compact(self, *, session_id: str) -> CompactionCommandResult:
        """Manually compact a session through the same durable summary pipeline as auto mode."""

        snapshot = await run_blocking(self._load_runtime_snapshot, session_id, recover=True)
        if (
            maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
            is not None
        ):
            raise SessionBusyError(f"session already has an active run: {session_id}")
        run = RunRecord(
            session_id=session_id,
            history_epoch=snapshot.session.history_epoch,
            kind="compaction",
            trigger="manual_compaction",
            status="running",
        )
        run_lock = await self.run_ownership.acquire(
            session_id,
            run.run_id,
        )
        if run_lock is None:
            raise SessionBusyError(f"session is already locked: {session_id}")
        abort_signal = None
        terminal_run = run
        try:
            sequence = 0
            abort_signal = await self.run_ownership.activate_signal(session_id, run.run_id)
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            if (
                maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
                is not None
            ):
                raise SessionBusyError(f"session already has an active run: {session_id}")
            active_session, active_adapter = await run_blocking(
                self._resolve_session_adapter, snapshot.session
            )
            run = run.model_copy(update={"model": active_adapter.model.model_id})
            await self.refresh_skills()
            run = await self._attach_extension_snapshot(run)
            terminal_run = run
            active_session, run, committed_events = await run_blocking(
                self.run_lifecycle.start,
                session=active_session,
                run=run,
                snapshot=snapshot,
            )
            await self.event_recorder.publish_committed(committed_events)
            sequence = committed_events[-1].sequence
            try:
                execution = await self.context_preparation.compact(
                    sequence,
                    session=active_session,
                    run=run,
                    tools=self._tools_for_run(run.run_id).definitions(),
                    added_context=[],
                    trigger="manual",
                    force=True,
                    abort_signal=abort_signal,
                    adapter=active_adapter,
                    strategies=self._compaction_for_run(run.run_id),
                    skill_instructions=self._instructions_for_run(run),
                )
            except CompactionAbortedError:
                cancelled = await self._finalize_requested_abort(
                    RunResult(
                        session=await run_blocking(self.store.load_session_record, session_id),
                        run=await run_blocking(self.store.load_run, session_id, run.run_id),
                    )
                )
                terminal_run = cancelled.run
                return CompactionCommandResult(
                    session=cancelled.session,
                    run=cancelled.run,
                )
            except Exception as exc:
                failed = await self.run_lifecycle.fail(
                    session=active_session,
                    run=run,
                    error=str(exc),
                    final_text="",
                    had_tool_error=False,
                )
                terminal_run = failed.run
                if isinstance(exc, CompactionError):
                    raise
                raise CompactionError(f"manual compaction failed: {exc}") from exc
            completed = await self.run_lifecycle.complete(
                session=active_session,
                run=run,
                final_text="",
                had_tool_error=False,
            )
            completed = await self._finalize_requested_abort(completed)
            terminal_run = completed.run
            return CompactionCommandResult(
                session=completed.session,
                run=completed.run,
                compaction=execution.record,
                skipped=execution.skipped,
            )
        finally:
            try:
                try:
                    if abort_signal is not None:
                        await self.run_ownership.release_signal(run.run_id, abort_signal)
                finally:
                    await self.run_ownership.release(run.run_id, run_lock)
                if terminal_run.status in ACTIVE_RUN_STATUSES:
                    self._finish_run_observers(terminal_run)
                else:
                    await self._finalize_run_boundary(
                        RunResult(
                            session=await run_blocking(self.store.load_session_record, session_id),
                            run=terminal_run,
                        ),
                        drain_queued_inputs=False,
                    )
            finally:
                self._release_run_resources(run.run_id)

    async def abort_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        reason: str = "User aborted",
        _reservation: RunReservation | None = None,
    ) -> RunResult:
        """Request cancellation once, then settle that durable intent."""
        _, run, events = await run_blocking(
            request_run_abort_state,
            self.store,
            session_id=session_id,
            run_id=run_id,
            reason=reason,
        )
        await self.event_recorder.publish_committed(events)
        result = await self.settle_run_abort(
            session_id=session_id, run_id=run.run_id, _reservation=_reservation
        )
        return result

    async def settle_run_abort(
        self,
        *,
        session_id: str,
        run_id: str,
        _reservation: RunReservation | None = None,
    ) -> RunResult:
        """Settle an accepted cancellation without cancelling a resumed execution."""
        signal = self.run_ownership.signal_for(run_id)
        session, run = await run_blocking(self._load_abort_target, session_id, run_id)
        if run.status not in ACTIVE_RUN_STATUSES or not run.abort_requested:
            return RunResult(session=session, run=run)
        if signal is not None and signal is self.run_ownership.signal_for(run_id):
            self.run_ownership.record_abort_state(session, run)
        has_active_owner = await run_blocking(self.run_ownership.is_locked, session_id)
        if has_active_owner and not (
            _reservation is not None and _reservation.owns(session_id, run.run_id)
        ):
            await self.run_ownership.wait_for_abort_settlement(session_id, run.run_id)
            session, run = await run_blocking(
                self._load_abort_target,
                session_id,
                run.run_id,
            )
            if run.status not in ACTIVE_RUN_STATUSES or not run.abort_requested:
                return RunResult(session=session, run=run)
        result = await self._finalize_requested_abort(RunResult(session=session, run=run))
        if result.run.status == "aborted":
            return await self._finalize_run_boundary(result, drain_queued_inputs=False)
        return result

    def _load_abort_target(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[SessionRecord, RunRecord]:
        return self.store.load_session_record(session_id), self.store.load_run(session_id, run_id)

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

        await self._synchronize_extensions()
        snapshot = await self._load_submission_snapshot(session_id)
        active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
        queued_session, queued = await self._queue_input_batch(
            snapshot=snapshot,
            active_run=active,
            batch=batch,
        )
        if active is not None:
            return RunResult(session=queued_session, run=active)
        target_ids = {item.queue_id for item in queued}
        while True:
            snapshot = await self._load_submission_snapshot(session_id)
            active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
            if active is not None:
                return RunResult(session=snapshot.session, run=active)
            if snapshot.session.history_epoch != queued_session.history_epoch:
                raise SessionCommitConflict(
                    "Plugin input batch belongs to withdrawn Session history"
                )
            pending = await run_blocking(self._pending_queued_inputs, snapshot)
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
            consumed = await run_blocking(
                self.queued_inputs.are_consumed,
                session_id,
                target_ids,
            )
            if consumed or result.run.status != "completed":
                return result
        raise PersistenceError("accepted plugin input batch is missing from queue and history")

    async def submit_host_input(
        self,
        session_input: HostSessionInput,
        *,
        session_id: str,
    ) -> HostInputReceipt:
        """Durably accept an OURO-owned scaffold input without awaiting execution."""

        await self._synchronize_extensions()
        session = await run_blocking(self.store.load_session_record, session_id)
        self._validate_model_resource_refs(
            session,
            session_input.resource_refs,
        )
        receipt, _, _, events = await run_blocking(
            self.queued_inputs.accept_host,
            session_id,
            session_input,
            select_driver=self._select_input_driver,
        )
        await self.event_recorder.publish_committed(events)
        return receipt

    async def consume_host_input(
        self,
        receipt: HostInputReceipt,
        *,
        _continuation_request: SessionContinuationRequest | None = None,
        _on_provider_stream_start: Callable[[], None] | None = None,
    ) -> RunResult:
        """Consume a previously accepted Session input through the ordinary Run path."""

        while True:
            snapshot = await self._load_submission_snapshot(receipt.session_id)
            require_current_continuation(snapshot.session, _continuation_request)
            if (
                await run_blocking(
                    self.queued_inputs.find_session, snapshot.session, receipt.queue_id
                )
                is None
            ):
                break
            active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
            if active is not None:
                return RunResult(session=snapshot.session, run=active)
            pending = await run_blocking(self._pending_queued_inputs, snapshot)
            if not pending:
                break
            try:
                return await self.run(
                    pending[0].prompt,
                    session_id=receipt.session_id,
                    title=pending[0].title,
                    _queued_inputs=pending,
                    _continuation_request=_continuation_request,
                    _on_provider_stream_start=_on_provider_stream_start,
                )
            except _QueuedInputSelectionChanged:
                continue
        return await run_blocking(self._consumed_host_input_result, receipt)

    def _consumed_host_input_result(self, receipt: HostInputReceipt) -> RunResult:
        with self.store.session_read(receipt.session_id):
            message = self.store.find_queued_input_message(receipt.session_id, receipt.queue_id)
            if message is not None and message.run_id is not None:
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
        skill_invocations: tuple[SkillInvocationRequest, ...] = (),
        title: str | None,
        mode: QueuedInputMode,
        resource_refs: tuple[str, ...] = (),
        attachments: tuple[InputFile, ...] = (),
        attribution: (AttributedSessionInput | HostSessionInput | DelegatedTaskInput | None) = None,
        on_input_accepted: Callable[[], None] | None = None,
        driver: RunDriverBinding | None = None,
        input_id: str | None = None,
    ) -> tuple[SessionRecord, QueuedInput]:
        if (
            isinstance(attribution, HostSessionInput)
            and attribution.history_epoch is not None
            and attribution.history_epoch != snapshot.session.history_epoch
        ):
            raise SessionCommitConflict("Host input belongs to withdrawn Session history")
        source = _attributed_input_source(attribution) if attribution is not None else "user"
        role = _input_role(attribution)
        scaffold = _input_scaffold(attribution)
        if skill_invocations and (source != "user" or mode != "follow_up"):
            raise ValueError("skill invocation requires direct follow-up user input")
        queue_id = (
            attribution.queue_id
            if isinstance(attribution, HostSessionInput) and attribution.queue_id is not None
            else input_id or new_id("queued")
        )
        prompt_policy_checked = source == "user"
        prospective_run_id: str | None = None
        extension_snapshot: ExtensionCatalogLease | None = None
        extension_catalog_id: str | None = None
        extension_generations: tuple[dict[str, object], ...] = ()
        resolved_skill_activations: tuple[SkillActivationContent, ...] = ()
        if prompt_policy_checked:
            child_run = active_run if snapshot.session.session_kind == "subagent" else None
            if snapshot.session.session_kind == "subagent" and child_run is None:
                raise PersistenceError("Child prompt binding requires an active delegated Run")
            prospective = RunRecord(
                session_id=snapshot.session.session_id,
                history_epoch=snapshot.session.history_epoch,
                status="running",
                origin=source,
                driver=driver or RunDriverBinding(),
                parent_run_id=child_run.parent_run_id if child_run is not None else None,
                initiator_id=child_run.initiator_id if child_run is not None else None,
                metadata=(
                    {
                        name: child_run.metadata[name]
                        for name in (
                            "root_session_id",
                            "delegation_id",
                            "agent_id",
                            "agent_definition_contract_hash",
                        )
                    }
                    if child_run is not None
                    else {}
                ),
            )
            prospective_run_id = prospective.run_id
            prospective = await self._attach_extension_snapshot(prospective)
            try:
                resolved_skill_activations = await self._resolve_skill_activations_for_run(
                    prospective.run_id, skill_invocations
                )
                await self._evaluate_prompt_policies(
                    session=snapshot.session,
                    run=prospective,
                    inputs=[
                        QueuedInput(
                            queue_id=queue_id,
                            prompt=prompt,
                            driver=driver,
                            skill_activations=resolved_skill_activations,
                            title=title,
                            mode=mode,
                            resource_refs=resource_refs,
                            attachments=attachments,
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
                )
                extension_snapshot = self._detach_extension_snapshot(prospective.run_id)
                if extension_snapshot is not None:
                    extension_catalog_id = extension_snapshot.catalog_id
                    extension_generations = tuple(
                        generation.model_dump(mode="json", exclude_none=True)
                        for generation in extension_snapshot.generations
                    )
            except BaseException:
                self._release_run_resources(prospective.run_id)
                if extension_snapshot is not None:
                    extension_snapshot.close()
                raise
        try:
            session, queued_input, _, events = await run_blocking(
                self.queued_inputs.enqueue,
                snapshot=snapshot,
                prompt=prompt,
                skill_activations=resolved_skill_activations,
                driver=driver,
                title=title,
                mode=mode,
                resource_refs=resource_refs,
                attachments=attachments,
                queue_id=queue_id,
                source=source,
                role=role,
                scaffold=scaffold,
                semantic_tag=(attribution.semantic_tag if attribution is not None else None),
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
                history_bound=(
                    isinstance(attribution, HostSessionInput)
                    and attribution.history_epoch is not None
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
            extension_snapshot.close()
        if on_input_accepted is not None:
            on_input_accepted()
        await self.event_recorder.publish_committed(events)
        return session, queued_input

    async def _queue_input_batch(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        active_run: RunRecord | None,
        batch: AttributedSessionInputBatch,
    ) -> tuple[SessionRecord, tuple[QueuedInput, ...]]:
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
        session, queued, _, _, events = await run_blocking(
            self.queued_inputs.enqueue_batch,
            snapshot=snapshot,
            inputs=requests,
            source="plugin",
            plugin_id=first.plugin_id,
            contribution_id=first.contribution_id,
            generation_id=first.generation_id,
            execution_contract_hash=first.execution_contract_hash,
            driver=self._select_input_driver(first.driver_id),
            coalesce_key=batch.coalesce_key,
            caused_by_run_id=active_run.run_id if active_run is not None else None,
        )
        await self.event_recorder.publish_committed(events)
        return session, queued

    async def _drain_queued_input_queue(self, session_id: str, history_epoch: int) -> None:
        while True:
            # A successor may already be accepted but not yet executing. This
            # completion path observes current work; it must not recover it.
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            if snapshot.session.history_epoch != history_epoch:
                return
            if (
                maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
                is not None
            ):
                return
            pending = await run_blocking(self._pending_queued_inputs, snapshot)
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
        pending_units = self.queued_inputs.eligible_units(snapshot)
        if not pending_units:
            return []
        if self.queued_input_config.consumption_mode == "one_by_one":
            return list(pending_units[0])
        first = pending_units[0][0]
        compatible_units: list[tuple[QueuedInput, ...]] = []
        for unit in pending_units:
            item = unit[0]
            if any(member.driver != first.driver for member in unit):
                break
            if first.prompt_policy_checked:
                matches = all(self._can_group_queued_inputs(first, member) for member in unit)
            else:
                matches = not item.prompt_policy_checked
            if not matches:
                break
            compatible_units.append(unit)
        return [item for unit in compatible_units for item in unit]

    @staticmethod
    def _can_group_queued_inputs(first: QueuedInput, item: QueuedInput) -> bool:
        if first.driver != item.driver:
            return False
        if first.skill_activations or item.skill_activations:
            return first.queue_id == item.queue_id
        return item.prompt_policy_checked == first.prompt_policy_checked

    def _select_input_driver(self, driver_id: str | None) -> RunDriverBinding | None:
        if driver_id is None:
            return None
        if driver_id == DEFAULT_DRIVER_ID:
            return RunDriverBinding()
        if self._extension_catalog is None:
            raise ValueError("plugin Run drivers require an extension catalog")
        return self._extension_catalog.select_driver(driver_id)

    async def _consume_driver_inputs(
        self,
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        turn_id: str,
    ) -> DriverInputDelivery:
        latest_session = await run_blocking(
            self.store.load_session_record,
            session.session_id,
        )
        units = await run_blocking(
            self.queued_inputs.eligible_units_session, latest_session, mode="steer"
        )
        if self.queued_input_config.consumption_mode == "one_by_one":
            units = units[:1]
        eligible = [item for unit in units for item in unit]
        if not eligible:
            return DriverInputDelivery(sequence=sequence)

        current = self._extension_catalog.snapshot() if self._extension_catalog else None
        try:
            await self._evaluate_prompt_policies(
                session=latest_session,
                run=run,
                inputs=eligible,
                catalog_snapshot=current,
            )
        finally:
            if current is not None:
                current.close()

        appended: list[MessageRecord] = []
        for unit in units:
            consumed, events = await run_blocking(
                self._consume_queued_turn_inputs,
                session=session,
                run=run,
                inputs=unit,
                turn_id=turn_id,
            )
            if not consumed:
                continue
            appended.extend(consumed)
            await self.event_recorder.publish_committed(events)
            if events:
                sequence = events[-1].sequence
        return DriverInputDelivery(sequence=sequence, messages=appended)

    async def _evaluate_turn_completion(
        self,
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        assistant_text: str,
        assistant_reasoning: str,
        turn_id: str,
        policies: TurnPolicyRegistry,
    ) -> TurnCompletionPolicyResult:
        evaluation = await policies.evaluate(
            TurnCompletionCandidate(
                harness=await run_blocking(
                    capture_harness,
                    self.store,
                    session.session_id,
                    run=run,
                    usage_service=self.usage,
                )
                if policies.registered()
                else None,
                session_id=session.session_id,
                run_id=run.run_id,
                turn_id=turn_id,
                assistant_text=assistant_text,
                assistant_reasoning=assistant_reasoning,
            )
        )
        sequence = await self.event_recorder.record_turn_policy_failures(
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

        controllers = [
            {
                "plugin_id": item.plugin_id,
                "policy_id": item.policy_id,
                "generation_id": item.generation_id,
            }
            for item in evaluation.decisions
        ]
        message = await run_blocking(
            self.run_lifecycle.create_input_message,
            sequence=0,
            session=session,
            run=run,
            prompt=evaluation.feedback,
            role="system",
            scaffold=True,
            metadata={
                "session_input_source": "turn_policy",
                "turn_id": turn_id,
                "controllers": controllers,
            },
            semantic_tag="turn-policy-feedback",
        )
        events = await run_blocking(
            self.run_lifecycle.append_messages,
            session=session,
            run=run,
            messages=[message],
            events=(
                EventRecord(
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
                ),
            ),
        )
        await self.event_recorder.publish_committed(events)
        if events:
            sequence = events[-1].sequence
        return TurnCompletionPolicyResult(
            sequence=sequence,
            should_continue=True,
        )

    def _accept_initial_input(self, session: SessionRecord, item: QueuedInput) -> SessionRecord:
        """Publish the first durable input and adopt its existing files in one boundary."""
        with self.store.session_mutation(session.session_id):
            try:
                self.store.load_session_record(session.session_id)
            except FileNotFoundError:
                record = None
            else:
                record = self.store.load_input(session.session_id, item.queue_id)
            if record is not None:
                if not same_queued_input(record.item, item):
                    raise ValueError("Input id is already bound to another input")
                state = self.store.load_input_state(session.session_id, item.queue_id)
                if state is None:
                    raise PersistenceError("accepted input has no lifecycle state")
                if state.status in {"cancelled", "withdrawn"}:
                    raise ValueError(
                        "Input was already accepted and has been withdrawn or cancelled"
                    )
                return self.store.load_session_record(session.session_id)
            references = (
                self.resources.prepared_references(session.session_id)
                if self.resources is not None
                else None
            )
            available = {record.resource_id for record in references or ()}
            if not set(item.resource_refs) <= available:
                raise ResourceAttachmentError("Input contains a foreign prepared resource")
            accepted = session.model_copy(
                update={
                    "pending_input_ids": (item.queue_id,),
                    "updated_at": utc_now(),
                }
            )
            harness = HarnessVersion(
                session_id=session.session_id,
                version_id=session.harness_version,
                state=HarnessState(controls=session.controls),
            )
            session = accepted
            self.store.create_session_snapshot(
                session,
                [],
                harness_versions=[harness],
                inputs=(InputRecord(session_id=session.session_id, item=item),),
                resource_references=references,
                prepared_resources=references is not None,
            )
        self.store.refresh_catalog(session.session_id)
        return session

    def _persist_session_policy_updates(
        self,
        session_id: str,
        updates: Mapping[str, object],
        *,
        require_idle: bool = False,
    ) -> SessionRecord:
        """Persist one live policy transition and its catalog projection."""

        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            if require_idle and (
                latest.active_run_id is not None or self.run_ownership.is_locked(session_id)
            ):
                raise SessionBusyError("Session became active while changing input policies")
            now = utc_now()
            session = latest.model_copy(
                update={
                    **updates,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            self.store.save_session(session)
        self.store.refresh_catalog(session_id)
        return session

    def _create_session_input_messages(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        item: QueuedInput,
        sequence: int,
        is_queued: bool,
    ) -> list[MessageRecord]:
        metadata: dict[str, object] = {
            "session_input_source": item.source,
            "session_input_mode": item.mode,
        }
        if item.driver is not None:
            metadata["input_driver_id"] = item.driver.driver_id
        if item.source == "delegation":
            metadata["delegation_id"] = item.delegation_id
            if item.caused_by_run_id is not None:
                metadata["caused_by_run_id"] = item.caused_by_run_id
        if is_queued:
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
        message = self.run_lifecycle.create_input_message(
            session=session,
            run=run,
            prompt=item.prompt,
            role=item.role,
            scaffold=item.scaffold,
            skill_activations=item.skill_activations,
            sequence=sequence,
            resource_refs=item.resource_refs,
            metadata=metadata,
            semantic_tag=item.semantic_tag,
        )
        message.input_ref = InputMessageReference(
            input_id=item.queue_id, attachments_from_input=bool(item.attachments)
        )

        attachments = item.attachments
        if item.source == "user" and (item.resource_refs or attachments):
            assert self.resources is not None
            if not attachments:
                attachments = tuple(
                    InputAttachment(
                        resource_id=resource_id,
                        name=self.resources.show_resource(
                            session.session_id, resource_id, verify=False
                        ).source.name,
                        text_offset=len(item.prompt),
                    )
                    for resource_id in item.resource_refs
                )
            messages = separate_attachments(message, attachments)
            resources = self._run_resources.get(run.run_id)
            constraints = resources.constraints if resources is not None else None
            workspace_only = self._tool_execution_config.workspace_only or (
                constraints is not None and not constraints.allow_external_paths
            )
            messages[0] = self.resources.prepare_input(
                messages[0],
                input_modalities=self._adapter_for_session_id(
                    session.session_id
                ).model.input_modalities,
                workspace=session.workspace,
                workspace_only=workspace_only,
            )
            return messages
        return [message]

    def _start_queued_run(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        snapshot: RuntimeSessionSnapshot,
        inputs: list[QueuedInput],
        continuation_request: SessionContinuationRequest | None = None,
    ) -> tuple[SessionRecord, RunRecord, list[MessageRecord], tuple[EventRecord, ...]]:
        """Cross one selected queue batch into canonical history under one mutation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            current = {
                item.queue_id: item for item in self.queued_inputs.eligible_session(latest_session)
            }
            if any(current.get(item.queue_id) != item for item in inputs):
                raise _QueuedInputSelectionChanged
            messages: list[MessageRecord] = []
            for item in inputs:
                messages.extend(
                    self._create_session_input_messages(
                        session=latest_session,
                        run=run,
                        item=item,
                        sequence=latest_session.message_count + len(messages) + 1,
                        is_queued=True,
                    )
                )
            started_session, started_run, events = self.run_lifecycle.start(
                session=latest_session,
                run=run,
                snapshot=snapshot,
                input_messages=tuple(messages),
                consumed_inputs=tuple(item.queue_id for item in inputs),
                events=input_message_events(run, inputs, messages, queued=True),
                continuation_request=continuation_request,
            )
        return started_session, started_run, messages, events

    def _consume_queued_turn_inputs(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        inputs: tuple[QueuedInput, ...],
        turn_id: str,
    ) -> tuple[list[MessageRecord], tuple[EventRecord, ...]]:
        """Consume one complete unit and its queue identities in one durable decision."""

        with self.store.session_mutation(session.session_id):
            latest = self.store.load_session_record(session.session_id)
            current = {item.queue_id: item for item in self.queued_inputs.eligible_session(latest)}
            if any(current.get(item.queue_id) != item for item in inputs):
                return [], ()
            messages: list[MessageRecord] = []
            for item in inputs:
                messages.extend(
                    self._create_session_input_messages(
                        session=latest,
                        run=run,
                        item=item,
                        sequence=latest.message_count + len(messages) + 1,
                        is_queued=True,
                    )
                )
            events = self.run_lifecycle.append_messages(
                session=latest,
                run=run,
                messages=messages,
                consumed_inputs=tuple(item.queue_id for item in inputs),
                events=input_message_events(run, inputs, messages, queued=True, turn_id=turn_id),
            )
            return messages, events

    def _validate_model_resource_refs(
        self,
        session: SessionRecord,
        resource_refs: tuple[str, ...],
        attachments: tuple[InputFile, ...] = (),
    ) -> None:
        if not resource_refs and not attachments:
            return
        if self.resources is None:
            raise ResourceAttachmentError(
                "resource attachments require a configured ResourceService"
            )
        self.resources.validate_file_inputs(attachments, session.workspace)
        self.resources.validate_model_references(session.session_id, resource_refs)

    async def _evaluate_prompt_policies(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        inputs: list[QueuedInput],
        harness: HarnessCallSnapshot | None = None,
        catalog_snapshot: ExtensionCatalogLease | None = None,
    ) -> None:
        snapshot = catalog_snapshot or self._extension_snapshot_for_run(run.run_id)
        if snapshot is not None:
            self._session_control_snapshot(
                session.session_id,
                run_id=run.run_id,
                model_round=0,
                catalog=snapshot,
                session_record=session,
            )
            active = {activation.controller_id for activation in session.controls.activations}
            registry = snapshot.prompt_policies.scoped(lambda: active)
        else:
            registry = self._prompt_policies_for_run(run.run_id)
        if not registry.registered():
            return
        execution_contracts = {
            generation.plugin_id: generation.execution_contract_hash
            for generation in (snapshot.generations if snapshot is not None else ())
            if generation.execution_contract_hash is not None
        }
        if harness is None:
            harness = await run_blocking(
                capture_harness,
                self.store,
                session.session_id,
                run=run,
                usage_service=self.usage,
            )
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
                        name=resource.source.name,
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
            attachments += tuple(
                PromptAttachmentMetadata(
                    kind="reference",
                    resource_id=None,
                    media_type=None,
                    size=None,
                    name=file.name,
                    path=file.path,
                )
                for file in item.attachments
                if isinstance(file, InputFileReference)
            )
            # Preserve the single-skill policy contract while accepting the input atomically.
            for activation in item.skill_activations or (None,):
                evaluation = await registry.evaluate(
                    PromptPolicyCall(
                        harness=harness,
                        session_id=session.session_id,
                        prospective_run_id=run.run_id,
                        prompt=item.prompt,
                        input_source=item.source,
                        workspace=session.workspace or self.default_workspace,
                        attachments=attachments,
                        skill_activation=(
                            PromptSkillActivationMetadata(
                                name=activation.name,
                                source=activation.source,
                                location=activation.location,
                                content_digest=activation.content_digest,
                            )
                            if activation is not None
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

    async def _load_submission_snapshot(self, session_id: str) -> RuntimeSessionSnapshot:
        """Load one Host submission snapshot without blocking the orchestration loop."""

        locked = await run_blocking(self.run_ownership.is_locked, session_id)
        return await run_blocking(
            self._load_runtime_snapshot,
            session_id,
            recover=not locked,
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
        snapshot = await run_blocking(
            self._load_runtime_snapshot,
            session_id,
            recover=True,
        )
        session = snapshot.session
        run = (
            await run_blocking(self.store.load_run, session_id, run_id)
            if run_id
            else find_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
        )
        if run.status != "waiting_approval":
            raise ValueError(f"run is not waiting for approval: {run.status}")
        batch = await run_blocking(load_tool_batch, self.store, run)
        if batch is None:
            raise ValueError("run has no active tool batch")
        resolved_approval_id = approval_id or run.pending_approval_id
        if resolved_approval_id is None:
            raise ValueError("run has no pending approval")
        run_lock = await self.run_ownership.acquire_parked(
            session.session_id,
            run.run_id,
            expected_status="waiting_approval",
        )
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run.run_id}")
        try:
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            session = snapshot.session
            run = await run_blocking(self.store.load_run, session_id, run.run_id)
            if run.status != "waiting_approval":
                raise ValueError(f"run is not waiting for approval: {run.status}")
            batch = await run_blocking(load_tool_batch, self.store, run)
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
            pending_events: list[EventRecord] = []
            pending_events.append(
                EventRecord(
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
            )
            session, run, batch, committed_events = await run_blocking(
                self.run_lifecycle.save_batch_state,
                session=session,
                run=run,
                batch=batch,
                status=suspension.status if suspension is not None else "running",
                phase=suspension.run_phase if suspension is not None else "tool_execution",
                events=tuple(pending_events),
            )
            await self.event_recorder.publish_committed(committed_events)
            assert batch is not None
            sequence = 0
            if suspension is not None:
                result = RunResult(session=session, run=run)
            else:
                result = await self._continue_resolved_batch(
                    snapshot=snapshot,
                    session=session,
                    run=run,
                    batch=batch,
                    sequence=sequence,
                )
        finally:
            await self.run_ownership.release(run.run_id, run_lock)
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
        snapshot = await run_blocking(
            self._load_runtime_snapshot,
            session_id,
            recover=True,
        )
        session = snapshot.session
        run = await run_blocking(self.store.load_run, session_id, run_id)
        if run.status != "waiting_user":
            raise ValueError(f"run is not waiting for user input: {run.status}")
        batch = await run_blocking(load_tool_batch, self.store, run)
        if batch is None:
            raise ValueError("run has no active tool batch")
        waiting_call = next((call for call in batch.calls if call.status == "waiting_user"), None)
        if waiting_call is None:
            raise ValueError("run has no tool call waiting for user input")
        run_lock = await self.run_ownership.acquire_parked(
            session.session_id,
            run.run_id,
            expected_status="waiting_user",
        )
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run.run_id}")
        try:
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            session = snapshot.session
            run = await run_blocking(self.store.load_run, session_id, run.run_id)
            if run.status != "waiting_user":
                raise ValueError(f"run is not waiting for user input: {run.status}")
            batch = await run_blocking(load_tool_batch, self.store, run)
            if batch is None:
                raise ValueError("run has no active tool batch")
            waiting_call = next(
                (call for call in batch.calls if call.status == "waiting_user"),
                None,
            )
            if waiting_call is None:
                raise ValueError("run has no tool call waiting for user input")
            await self._ensure_extension_snapshot(run)
            answer_harness = await run_blocking(
                capture_harness,
                self.store,
                session.session_id,
                run=run,
                usage_service=self.usage,
            )
            sequence = 0
            batch, sequence = await self._tool_calls_for_run(run.run_id).resolve_user_answer(
                batch,
                sequence=sequence,
                run=run,
                session=session,
                tool_call_id=waiting_call.request.id,
                answer=answer,
                harness=answer_harness,
                workspace=session.workspace,
            )
            suspension = tool_batch_suspension(batch)
            pending_events: list[EventRecord] = []
            resolved_call = next(
                call for call in batch.calls if call.request.id == waiting_call.request.id
            )
            pending_events.append(
                EventRecord(
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
            )
            if resolved_call.status == "waiting_user" and resolved_call.input_request is not None:
                pending_events.append(
                    EventRecord(
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
                )
            session, run, batch, committed_events = await run_blocking(
                self.run_lifecycle.save_batch_state,
                session=session,
                run=run,
                batch=batch,
                harness=answer_harness,
                events=tuple(pending_events),
                status=suspension.status if suspension is not None else "running",
                phase=suspension.run_phase if suspension is not None else "tool_execution",
            )
            await self.event_recorder.publish_committed(committed_events)
            if committed_events:
                sequence = committed_events[-1].sequence
            assert batch is not None
            result = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                sequence=sequence,
            )
        finally:
            await self.run_ownership.release(run.run_id, run_lock)
        # Keep the same post-lock Run boundary as normal runs and approval
        # recovery; a follow-up must acquire a fresh Session run lock.
        return await self._finalize_run_boundary(result, drain_queued_inputs=True)

    async def resume_agent_wait(
        self,
        *,
        session_id: str,
        identity: ToolCallIdentity,
        resolution: AgentWaitResolution,
    ) -> RunResult:
        """Resolve one durable child join and continue the original parent Run."""

        run_id, tool_call_id = identity.run_id, identity.tool_call_id
        snapshot = await run_blocking(
            self._load_runtime_snapshot,
            session_id,
            recover=True,
        )
        run = await run_blocking(self.store.load_run, session_id, run_id)
        if run.status != "waiting_child":
            raise ValueError(f"run is not waiting for a child: {run.status}")
        run_lock = await self.run_ownership.acquire_parked(
            session_id,
            run_id,
            expected_status="waiting_child",
        )
        if run_lock is None:
            raise SessionBusyError(f"run is already active: {run_id}")
        try:
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            session = snapshot.session
            run = await run_blocking(self.store.load_run, session_id, run_id)
            if run.status != "waiting_child" or run.active_tool_batch is None:
                raise ValueError("parent Run no longer has a durable child wait")
            if run.active_tool_batch.batch_id != identity.batch_id:
                raise ValueError("Agent wait belongs to another native batch")
            batch = await run_blocking(load_tool_batch, self.store, run)
            assert batch is not None
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
            resolved_wait = AgentWaitRequest.model_validate(
                {
                    **waiting_call.agent_wait.model_dump(),
                    "state": "resolved",
                    "resolution": resolution,
                }
            )
            outcome = await run_blocking(
                self.store.load_tool_outcome, session_id, identity, waiting_call.attempt
            )
            if outcome is None:
                raise PersistenceError("Agent wait has no recorded child observation")
            result = ToolResult.model_validate(outcome.value.model_dump())
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
            batch = batch.model_copy(
                update={"calls": calls, "phase": "executing", "updated_at": utc_now()}
            )
            pending_events: list[EventRecord] = []
            pending_events.append(
                EventRecord(
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
            )
            pending_events.append(
                EventRecord(
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
                        "outcome_attempt": waiting_call.attempt,
                    },
                )
            )
            session, run, batch, committed_events = await run_blocking(
                self.run_lifecycle.save_batch_state,
                session=session,
                run=run,
                batch=batch,
                status="running",
                phase="tool_execution",
                changed_call_ids=frozenset({tool_call_id}),
                events=tuple(pending_events),
            )
            await self.event_recorder.publish_committed(committed_events)
            assert batch is not None
            sequence = 0
            resumed = await self._continue_resolved_batch(
                snapshot=snapshot,
                session=session,
                run=run,
                batch=batch,
                sequence=sequence,
            )
        finally:
            await self.run_ownership.release(run.run_id, run_lock)
        return await self._finalize_run_boundary(resumed, drain_queued_inputs=True)

    async def _continue_resolved_batch(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        sequence: int,
    ) -> RunResult:
        suspension = tool_batch_suspension(batch)
        if suspension is not None:
            batch = batch.model_copy(
                update={"phase": suspension.batch_phase, "updated_at": utc_now()}
            )
            session, run, batch, committed_events = await run_blocking(
                self.run_lifecycle.save_batch_state,
                session=session,
                run=run,
                batch=batch,
                status=suspension.status,
                phase=suspension.run_phase,
            )
            await self.event_recorder.publish_committed(committed_events)
            if committed_events:
                sequence = committed_events[-1].sequence
            assert batch is not None
            if suspension.status == "waiting_child":
                if self._agent_controls is None or suspension.call.agent_wait is None:
                    raise RuntimeError("Agent control executor is unavailable")
                await self._agent_controls.wait_parked(
                    session=session,
                    run=run,
                    batch=batch,
                    tool_call_id=suspension.call.request.id,
                    wait=suspension.call.agent_wait,
                )
            return RunResult(session=session, run=run)

        active_signal = await self.run_ownership.activate_signal(
            session.session_id,
            run.run_id,
        )
        try:
            try:
                await self._ensure_extension_snapshot(run)
                handling = await self._tool_calls_for_run(run.run_id).execute_batch(
                    sequence,
                    session=session,
                    run=run,
                    batch=batch,
                    abort_signal=active_signal,
                )
            except Exception as exc:  # noqa: BLE001 - coordinator is a runtime boundary.
                latest = await run_blocking(
                    self.store.load_runtime_session,
                    session.session_id,
                )
                latest_run = await run_blocking(self.store.load_run, session.session_id, run.run_id)
                failed = await self.run_lifecycle.fail(
                    session=latest.session,
                    run=latest_run,
                    error=f"tool batch handling failed: {exc}",
                    final_text="",
                    had_tool_error=False,
                )
                return await self._finalize_requested_abort(failed)
            latest = await run_blocking(
                self._load_runtime_snapshot,
                session.session_id,
                recover=True,
            )
            session = latest.session
            run = await run_blocking(self.store.load_run, session.session_id, run.run_id)
            abort_result = self.run_ownership.aborted_result(
                session_id=session.session_id,
                run_id=run.run_id,
                final_text="",
                had_tool_error=any(item.is_error for item in handling.results),
            )
            if abort_result is not None:
                return await self._finalize_requested_abort(abort_result)
            if handling.status in {"waiting_approval", "waiting_user", "waiting_child"}:
                return RunResult(
                    session=session,
                    run=run,
                    had_tool_error=any(item.is_error for item in handling.results),
                )
            session, active_adapter = await run_blocking(self._resolve_session_adapter, session)
            resumed = await self._continue_run_driver(
                session=session,
                run=run,
                sequence=handling.sequence,
                abort_signal=active_signal,
                adapter=active_adapter,
                resuming_driver=True,
                resume_turn_id=batch.turn_id,
            )
        finally:
            await self.run_ownership.release_signal(run.run_id, active_signal)
        return resumed

    async def _finalize_requested_abort(self, result: RunResult) -> RunResult:
        """Publish terminal abort only after the active owner has settled its work."""

        session, aborted_run, events = await run_blocking(
            self._settle_requested_abort_state,
            result.run.session_id,
            result.run.run_id,
        )
        await self.event_recorder.publish_committed(events)
        return result.model_copy(update={"session": session, "run": aborted_run})

    def _settle_requested_abort_state(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]]:
        with self.store.session_mutation(session_id):
            latest_run = self.store.load_run(session_id, run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or not latest_run.abort_requested:
                return (
                    self.store.load_session_record(latest_run.session_id),
                    latest_run,
                    (),
                )
            raw_reason = latest_run.metadata.get("abort_reason")
            reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "User aborted"
            return abort_run_state(
                self.store,
                session_id=latest_run.session_id,
                run_id=latest_run.run_id,
                reason=reason,
            )

    async def _finalize_run_boundary(
        self,
        result: RunResult,
        *,
        drain_queued_inputs: bool,
    ) -> RunResult:
        run = result.run
        usage = await run_blocking(self.run_lifecycle.usage_for_run, run)
        if run.status not in ACTIVE_RUN_STATUSES:
            try:
                await self._settle_terminal_run(run)
                settlement = await self._settle_run_continuation(
                    run,
                    final_text=result.final_text,
                )
                run = settlement.run
                if settlement.settled:
                    await self.event_recorder.publish_committed(settlement.events)
                outcome = RunOutcomeView(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    status=run.status,
                    origin=run.origin,
                    final_text=result.final_text,
                    error=run.error,
                    usage=MappingProxyType(usage or {}),
                )
                await self.event_recorder.observe(
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
                "usage": usage,
                "session": await run_blocking(
                    self.store.load_session_record,
                    run.session_id,
                ),
            }
        )
        if (
            drain_queued_inputs
            and run.status == "completed"
            and not self._run_continuation_is_pending(run)
        ):
            await self._drain_queued_input_queue(run.session_id, run.history_epoch)
        return updated

    async def _settle_run_continuation(
        self,
        run: RunRecord,
        *,
        final_text: str = "",
    ) -> RunContinuationSettlement:
        if not self._run_continuation_is_pending(run):
            return RunContinuationSettlement(run=run)

        withdrawn = await run_blocking(
            RunContinuationService(
                store=self.store,
                registry=self.continuations,
                queue=self.queued_inputs,
            ).settle_withdrawn,
            run,
        )
        if withdrawn is not None:
            return withdrawn
        checkpoint = run.continuation
        assert checkpoint is not None
        if checkpoint.settlement_plan is None:
            try:
                await self._ensure_extension_snapshot(run)
            except Exception as exc:  # noqa: BLE001 - keep an unaccepted decision retryable.
                diagnostic = (
                    f"current environment binding failed for Run {run.run_id}: "
                    f"{type(exc).__name__}: {redact_text(str(exc))}"
                )
                _LOG.warning(diagnostic)
                return RunContinuationSettlement(run=run, diagnostics=(diagnostic,))

        return await RunContinuationService(
            store=self.store,
            registry=self._continuations_for_run(run.run_id, active_only=False),
            queue=self.queued_inputs,
            session_controls=self.session_controls,
            session_controllers=self._session_controllers_for_run(run.run_id),
            usage_service=self.usage,
        ).settle(
            session_id=run.session_id,
            run_id=run.run_id,
            final_text=final_text,
        )

    @staticmethod
    def _run_continuation_is_pending(run: RunRecord) -> bool:
        checkpoint = run.continuation
        return (
            run.kind == "agent"
            and run.status not in ACTIVE_RUN_STATUSES
            and checkpoint is not None
            and checkpoint.state != "resolved"
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
        if run.status not in ACTIVE_RUN_STATUSES:
            self.event_recorder.finish_run_observers(run.run_id)
        if run.status != "running":
            self._release_run_resources(run.run_id)

    def _release_run_resources(self, run_id: str) -> None:
        # Drop the Run owner before invoking a lease callback that may fail.
        snapshot = self._detach_extension_snapshot(run_id)
        if snapshot is not None:
            snapshot.close()

    def _detach_extension_snapshot(self, run_id: str) -> ExtensionCatalogLease | None:
        resources = self._run_resources.pop(run_id, None)
        return resources.take_snapshot() if resources is not None else None

    def _extension_snapshot_for_run(self, run_id: str | None) -> ExtensionCatalogLease | None:
        resources = self._run_resources.get(run_id) if run_id is not None else None
        return resources.snapshot if resources is not None else None

    def _instructions_for_run(self, run: RunRecord) -> str:
        instructions = [self._skill_instructions_for_run(run.run_id)]
        agent_prompt = self._run_resources[run.run_id].agent_prompt
        if agent_prompt:
            instructions.append(agent_prompt)
        return "\n\n".join(item.strip() for item in instructions if item.strip())

    async def _synchronize_extensions(self) -> None:
        if self._extension_catalog is not None:
            await self._extension_catalog.synchronize()

    async def _bind_run_capabilities(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        model_round: int,
    ) -> RunRecord:
        await self.refresh_skills()
        await self._synchronize_extensions()
        return await run_blocking(
            self._capture_run_capabilities, session=session, run=run, model_round=model_round
        )

    def _capture_run_capabilities(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        model_round: int,
    ) -> RunRecord:
        # A completed model/tool turn no longer owns an executable environment.
        # The next request and its resulting calls share one current binding.
        resources = self._run_resources[run.run_id]
        if self._extension_catalog is not None:
            current_snapshot = self._extension_catalog.snapshot()
            previous = resources.snapshot
            resources.snapshot = current_snapshot
            if previous is not None:
                previous.close()
        else:
            resources.host_tools = self.tools.scoped(
                lambda: self._active_controller_ids_for_run(run.run_id)
            )
        resources.tool_calls = None
        with self.store.session_mutation(run.session_id):
            latest = self.store.load_run(run.session_id, run.run_id)
            session = self.store.load_session_record(run.session_id)
            if latest.history_epoch != session.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            if latest.abort_requested or latest.status not in ACTIVE_RUN_STATUSES:
                return latest
            metadata = dict(latest.metadata)
            snapshot = self._extension_snapshot_for_run(latest.run_id)
            metadata["session_control_snapshot"] = self._session_control_snapshot(
                latest.session_id,
                run_id=latest.run_id,
                model_round=model_round,
                catalog=snapshot,
                session_record=session,
            )
            self._run_resources[latest.run_id].controller_ids = frozenset(
                item["controller_id"]
                for item in metadata["session_control_snapshot"]["activations"]
            )
            update: dict[str, object] = {"metadata": metadata}
            if latest.kind == "agent" and snapshot is not None:
                update["continuation"] = self._continuation_checkpoint(latest, snapshot, session)
                metadata["extension_catalog_id"] = snapshot.catalog_id
                metadata["extension_generations"] = [
                    generation.model_dump(mode="json", exclude_none=True)
                    for generation in snapshot.generations
                ]
            if (
                latest.kind == "agent"
                and session.session_kind == "primary"
                and self._agent_definitions is not None
            ):
                catalog = self._agent_definitions.catalog_snapshot(
                    self._tools_for_run(latest.run_id),
                    permission_overrides=self._permission_overrides,
                    plugin_permission_overrides=self._plugin_permission_overrides,
                    approval_mode=session.approval_mode,
                )
                if self._agent_controls is not None:
                    catalog = self._agent_controls.constrain_catalog(catalog)
                update["agent_catalog"] = catalog
                resources.agent_catalog = catalog
            updated = latest.model_copy(update=update)
            self.store.save_run(updated)
            return updated

    def _commit_model_request(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        context_id: str,
        event: EventRecord,
    ) -> tuple[RunRecord, tuple[EventRecord, ...]]:
        """Record possible dispatch and the Run counter together before network IO."""

        del session
        with self.store.session_mutation(run.session_id):
            latest = self.store.load_run(run.session_id, run.run_id)
            current = self.store.load_session_record(run.session_id)
            if latest.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            if latest.abort_requested or latest.status not in ACTIVE_RUN_STATUSES:
                return latest, ()
            if current.active_run_id != run.run_id:
                raise SessionCommitConflict("Run no longer owns request dispatch")
            header = self.store.load_latest_context_header(run.session_id)
            if (
                header is None
                or header.context_id != context_id
                or latest.current_request_id != context_id
            ):
                raise SessionCommitConflict("prepared request is no longer current")
            if header.started_at is not None:
                raise SessionCommitConflict("request dispatch is already recorded")
            updated = latest.model_copy(
                update={
                    "model_round_count": latest.model_round_count + 1,
                    "model": run.model,
                    "model_endpoint_id": run.model_endpoint_id,
                }
            )
            committed = self.store.commit_session(
                SessionCommit(
                    before=current,
                    runs=(updated,),
                    request_header=header.model_copy(update={"started_at": utc_now()}),
                    events=(event,),
                )
            )
            return updated, committed.events

    def _session_control_snapshot(
        self,
        session_id: str,
        *,
        run_id: str,
        model_round: int,
        catalog: ExtensionCatalogLease | None = None,
        session_record: SessionRecord | None = None,
    ) -> dict[str, object]:
        controls = (session_record or self.store.load_session_record(session_id)).controls
        activations = [
            {
                "activation_id": activation.activation_id,
                "controller_id": activation.controller_id,
                "revision": activation.revision,
                "generation_id": activation.generation_id,
                "execution_contract_hash": activation.execution_contract_hash,
                "usage_scope": activation.usage_scope,
            }
            for activation in controls.activations
        ]
        for activation in controls.activations:
            try:
                controller = (
                    catalog.session_controllers.get(activation.controller_id)
                    if catalog is not None
                    else None
                )
            except KeyError:
                controller = None
            if controller is None or (
                controller.execution_contract_hash != activation.execution_contract_hash
            ):
                raise SessionControlContractError(
                    f"Session controller contract is unavailable: {activation.controller_id}"
                )
        return {
            "revision": controls.revision,
            "model_round": model_round,
            "activations": activations,
        }

    def _bind_control_state(self, run_id: str, controls: SessionControlState) -> str | None:
        resources = self._run_resources[run_id]
        snapshot = resources.snapshot
        eligible: set[str] = set()
        unavailable: list[str] = []
        for activation in controls.activations:
            try:
                controller = (
                    snapshot.session_controllers.get(activation.controller_id) if snapshot else None
                )
            except KeyError:
                controller = None
            if (
                controller is None
                or controller.execution_contract_hash != activation.execution_contract_hash
            ):
                unavailable.append(activation.controller_id)
            else:
                eligible.add(activation.controller_id)
        resources.controller_ids = frozenset(eligible)
        return (
            "Required Session controls are unavailable: " + ", ".join(unavailable)
            if unavailable
            else None
        )

    async def _attach_extension_snapshot(self, run: RunRecord) -> RunRecord:
        await self._synchronize_extensions()
        snapshot = (
            self._extension_catalog.snapshot() if self._extension_catalog is not None else None
        )
        return await self._bind_extension_snapshot(run, snapshot)

    async def _bind_extension_snapshot(
        self,
        run: RunRecord,
        snapshot: ExtensionCatalogLease | None,
    ) -> RunRecord:
        resources = self._run_resources.get(run.run_id)
        if resources is None:
            resources = RunResources()
            self._run_resources[run.run_id] = resources
        resources.snapshot = snapshot
        if snapshot is None:
            resources.host_tools = self.tools.scoped(
                lambda: self._active_controller_ids_for_run(run.run_id)
            )
        try:
            session = await run_blocking(self.store.load_session_record, run.session_id)
            relation = await run_blocking(
                AgentRecoveryService(self.store).load_run_delegation, session, run
            )
            resources.bind_delegation(relation)
            metadata = dict(run.metadata)
            if relation is None:
                metadata.setdefault("run_budget", self.run_budget.model_dump(mode="json"))
            update: dict[str, object] = {"metadata": metadata}
            if snapshot is not None:
                metadata["session_control_snapshot"] = self._session_control_snapshot(
                    run.session_id,
                    run_id=run.run_id,
                    model_round=0,
                    catalog=snapshot,
                    session_record=session,
                )
                resources.controller_ids = frozenset(
                    activation.controller_id for activation in session.controls.activations
                )
                metadata["extension_catalog_id"] = snapshot.catalog_id
                metadata["extension_generations"] = [
                    generation.model_dump(mode="json", exclude_none=True)
                    for generation in snapshot.generations
                ]
                if run.kind == "agent":
                    update["continuation"] = self._continuation_checkpoint(run, snapshot, session)
            return run.model_copy(update=update)
        except BaseException:
            self._release_run_resources(run.run_id)
            raise

    def _continuation_checkpoint(
        self,
        run: RunRecord,
        snapshot: ExtensionCatalogLease,
        session_record: SessionRecord | None = None,
    ) -> RunContinuationCheckpoint | None:
        generations = {generation.generation_id: generation for generation in snapshot.generations}
        activations = {
            activation.controller_id: activation
            for activation in (
                session_record or self.store.load_session_record(run.session_id)
            ).controls.activations
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
        return previous.model_copy(update={"controllers": controllers})

    async def _ensure_extension_snapshot(self, run: RunRecord) -> None:
        resources = self._run_resources.get(run.run_id)
        if resources is None:
            resources = RunResources()
        session = await run_blocking(self.store.load_session_record, run.session_id)
        relation = await run_blocking(
            AgentRecoveryService(self.store).load_run_delegation, session, run
        )
        resources.bind_delegation(relation)
        if run.agent_catalog is not None:
            resources.agent_catalog = run.agent_catalog
        if resources.snapshot is not None or resources.host_tools is not None:
            return
        await self._synchronize_extensions()
        snapshot = self._extension_catalog.snapshot() if self._extension_catalog else None
        resources.snapshot = snapshot
        if snapshot is None:
            resources.host_tools = self.tools.scoped(
                lambda: self._active_controller_ids_for_run(run.run_id)
            )
        self._run_resources[run.run_id] = resources
        try:
            self._bind_control_state(run.run_id, session.controls)
        except BaseException:
            self._release_run_resources(run.run_id)
            raise


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
