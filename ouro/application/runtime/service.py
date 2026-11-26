from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Container, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import JsonValue

from ouro.application.agents.definitions import AgentDefinitionService
from ouro.application.agents.recovery import AgentRecoveryService
from ouro.application.context.compaction import ContextCompactionService
from ouro.application.context.preparation import ContextPreparationService
from ouro.application.context.prompts import PromptContextResolver
from ouro.application.context.summary import LLMSummaryCompactionStrategy
from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.contracts.models import AvailableModel, ModelReasoningState
from ouro.application.contracts.sessions import (
    SessionHistoryAnchor,
    SessionObservationSnapshot,
    SessionUndoResult,
)
from ouro.application.events import (
    RuntimeEventFilter,
    RuntimeEventRecorder,
    RuntimeEventSink,
    RuntimeEventSinkRegistration,
)
from ouro.application.harness import capture_harness
from ouro.application.inputs.admission import InputAdmissionService
from ouro.application.inputs.queue import (
    QueuedInput,
    QueuedInputMode,
    QueuedInputQueue,
    QueuedUserInputView,
    input_message_events,
    project_pending_user_inputs,
    project_queued_user_input,
    validate_host_input,
)
from ouro.application.inputs.requests import (
    IncomingRunInput,
    InputSessionSettings,
    PreparedUserInput,
    UserInputRequest,
    queued_run,
)
from ouro.application.inputs.skills import SkillActivationService, SkillInvocationRequest
from ouro.application.inputs.source_history import freeze_source_history, prepend_source_history
from ouro.application.models.catalog import (
    FixedRuntimeAdapterCatalog,
    RuntimeAdapterCatalog,
    SessionModels,
    close_runtime_adapters,
)
from ouro.application.models.requests import ModelRequestPorts
from ouro.application.provider_auth.service import ProviderAuthService
from ouro.application.runs.continuations import RunContinuationService, RunContinuationSettlement
from ouro.application.runs.execution import RunDriverExecution
from ouro.application.runs.interactions import ParkedRunExecution
from ouro.application.runs.lifecycle import (
    ACTIVE_RUN_STATUSES,
    DelegationStartRevoked,
    RunLifecycle,
    SessionContinuationSuperseded,
    maybe_active_run,
    request_run_abort_state,
    require_current_continuation,
    reserve_run,
)
from ouro.application.runs.ownership import RunExecutionOwnership, RunReservation
from ouro.application.runs.recovery import RunRecoveryService
from ouro.application.runs.start import QueuedInputSelectionChanged, RunStartExecution
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.resources import RunResources, RuntimeRegistries
from ouro.application.session_controls import (
    SessionControlCoordinator,
    require_available_controls,
)
from ouro.application.sessions.editing import continuable_run
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.sessions.service import SessionService
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.application.usage import UsageService
from ouro.auth.credentials import CredentialService
from ouro.config.compaction import CompactionConfig, CompactionModelConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.runtime import RuntimeSettings
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentWaitResolution
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.attachment_input import separate_attachments
from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal, settle_owned_tasks
from ouro.core.compaction import (
    CompactionAbortedError,
    CompactionCommandResult,
    CompactionStrategyRegistry,
)
from ouro.core.context import ContextBuilder, ContextMetrics, compose_system_prompt
from ouro.core.context_providers import ContextProviderRegistry, RequestProviders
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import (
    CompactionError,
    ConfigError,
    PersistenceError,
    ResourceAttachmentError,
    SessionBusyError,
)
from ouro.core.events import EventRecord
from ouro.core.input_content import (
    InputAttachment,
    InputContent,
    InputFile,
)
from ouro.core.inputs import InputRecord, SourceHistory
from ouro.core.interactions import (
    SESSION_INTERACTION_MODES,
    SessionInteractionMode,
)
from ouro.core.messages import (
    InputMessageReference,
    MessageRecord,
)
from ouro.core.observers import (
    Observation,
    ObserverRegistry,
    RunFinishedObservation,
    RunOutcomeView,
)
from ouro.core.prompt_policies import (
    PromptPolicyRegistry,
)
from ouro.core.reasoning import ReasoningRequest
from ouro.core.redaction import redact_text
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.run_drivers import DEFAULT_DRIVER_ID, RunDriverBinding
from ouro.core.run_transitions import prepare_run_continuation
from ouro.core.session_commits import InputAlreadyAccepted, SessionCommitConflict
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    AttributedSessionInput,
    AttributedSessionInputBatch,
    DelegatedTaskInput,
    HostSessionInput,
    SessionControlActivation,
    SessionControlState,
    SessionControlTransitionResult,
)
from ouro.core.session_ports import RuntimeSessionSnapshot, SessionStore
from ouro.core.session_titles import normalize_session_title, with_session_title
from ouro.core.state import (
    TOOL_WAIT_RUN_STATUSES,
    RunContinuationCheckpoint,
    RunRecord,
    SessionContinuationRequest,
    SessionRecord,
)
from ouro.core.system_prompts import SystemPromptRegistry
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tool_facts import ToolCallIdentity, ToolQuestion
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.tools import PermissionMode, ToolDefinition
from ouro.core.turn_policies import (
    TurnCompletionCandidate,
    TurnPolicyEvaluation,
    TurnPolicyRegistry,
)
from ouro.prompts import DEFAULT_SYSTEM_PROMPT, render_agent_skills_prompt
from ouro.skills import SkillMetadata
from ouro.tools import ToolExecutor, ToolRegistry

if TYPE_CHECKING:
    from ouro.application.agents.service import AgentService
    from ouro.application.resources import ResourceService
    from ouro.application.runtime.extensions import RuntimeCapabilities, RuntimeExtensions


_LOG = logging.getLogger(__name__)


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
        system_prompt_providers: SystemPromptRegistry | None = None,
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
        compaction_adapter_resolver: Callable[[CompactionModelConfig], RuntimeAdapter]
        | None = None,
        settings_resolver: Callable[[], RuntimeSettings] | None = None,
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
        self.settings_resolver = settings_resolver
        self._applied_settings: RuntimeSettings | None = None
        self.queued_input_config = queued_input_config or QueuedInputConfig()
        self._session_scheduler: Callable[[str, int], Awaitable[None]] | None = None
        self._advancing_sessions: set[str] = set()
        self.adapter = adapter
        self.session_models = SessionModels(
            model_catalog or FixedRuntimeAdapterCatalog(adapter), store, self.sessions
        )
        self._owned_auxiliary_adapters = owned_auxiliary_adapters
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self.skill_activations = SkillActivationService(skills, skill_catalog_resolver)
        self.default_workspace = (
            str(Path(default_workspace).expanduser().resolve())
            if default_workspace is not None
            else None
        )
        self._permission_overrides = dict(permission_overrides or {})
        self._plugin_permission_overrides = dict(plugin_permission_overrides or {})
        self._tool_execution_config = tool_execution_config or ToolExecutionConfig()
        self.registries = RuntimeRegistries(
            tools=tools,
            observers=observers or ObserverRegistry(),
            request_providers=RequestProviders(
                system_prompt_providers or SystemPromptRegistry(),
                context_providers or ContextProviderRegistry(),
            ),
            continuations=continuations or RunContinuationRegistry(),
            prompt_policies=prompt_policies or PromptPolicyRegistry(),
            tool_policies=tool_policies or ToolPolicyRegistry(),
            turn_policies=turn_policies or TurnPolicyRegistry(),
            compaction=compaction_strategies or CompactionStrategyRegistry(),
        )
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
                    runtime_adapter_resolver=self.session_models.adapter_for_session_id,
                    prepare_requests=self.run_lifecycle.prepare_summary_requests,
                )
            )
        self.context_preparation = ContextPreparationService(
            store=store,
            adapter=adapter,
            providers_for_run=self._request_providers_for_run,
            events=self.event_recorder,
            context_builder=self.context_builder,
            system_prompt=compose_system_prompt(
                self.system_prompt,
                self.additional_system_prompt,
            ),
            prompt_context_resolver=prompt_context_resolver,
            append_context_messages=self.run_lifecycle.append_messages,
            usage_service=usage_service,
            prepare_model_messages=(
                resources.prepare_model_messages if resources is not None else None
            ),
        )
        self.context_compaction = ContextCompactionService(
            preparation=self.context_preparation,
            store=store,
            events=self.event_recorder,
            strategies=self.compaction_strategies,
            config=self.compaction_config,
            usage_service=usage_service,
        )
        self.run_ownership = RunExecutionOwnership(store)
        self.parked_runs = ParkedRunExecution(
            ownership=self.run_ownership,
            runs=self.run_lifecycle,
            recovery=self.session_recovery,
            continue_batch=self._continue_resolved_batch,
            finish=self._finalize_run_boundary,
            release_resources=self._release_run_resources,
            bind_run=self._ensure_extension_snapshot,
            tool_calls_for_run=self._tool_calls_for_run,
        )
        self._extension_catalog: RuntimeExtensions | None = None
        self._extensions_bound = False
        self._run_resources: dict[str, RunResources] = {}
        self._agent_definitions: AgentDefinitionService | None = None
        self._agent_controls: AgentService | None = None
        self.input_admission = InputAdmissionService(
            store,
            resources=resources,
            usage_service=usage_service,
            models=self.session_models,
            queue=self.queued_inputs,
            events=self.event_recorder,
            recovery=self.session_recovery,
            skill_activations=self.skill_activations,
            refresh_configuration=self.refresh_configuration,
            prompt_policies=self.prompt_policies,
            default_workspace=self.default_workspace,
        )
        self.driver_execution = RunDriverExecution(
            runs=self.run_lifecycle,
            model_ports=ModelRequestPorts(
                prepare_context=lambda *, run, **kwargs: self.context_compaction.prepare(
                    run=run,
                    strategies=self._compaction_for_run(run.run_id),
                    skill_instructions=self._instructions_for_run(run),
                    **kwargs,
                ),
                record_settlement=self.context_preparation.record_request_settlement,
                append_draft=store.append_response_draft,
                record_retry=store.record_request_retry,
                resolve_adapter=self.session_models.bind_session,
                record_usage=self.run_lifecycle.record_usage,
                run_usage=usage_service.run_usage if usage_service is not None else None,
                begin_model_round=self._bind_run_capabilities,
                prepare_configuration=self._prepare_request_configuration,
                commit_model_request=self.run_lifecycle.commit_model_request,
                events=self.event_recorder,
            ),
            budget_for_run=self._run_budget_for_run,
            tools_for_run=self._tools_for_run,
            tool_calls_for_run=self._tool_calls_for_run,
            refresh_configuration=self.refresh_configuration,
            consume_inputs=self._consume_driver_inputs,
            evaluate_completion=self._evaluate_turn_completion,
            default_workspace=self.default_workspace,
        )

        self.run_start = RunStartExecution(
            self.run_ownership,
            self.run_recovery,
            self.session_models,
            self.driver_execution,
            release_resources=self._release_run_resources,
            finalize=self._finalize_run_boundary,
        )

    @property
    def models(self) -> RuntimeAdapterCatalog:
        return self.session_models.catalog

    @models.setter
    def models(self, catalog: RuntimeAdapterCatalog) -> None:
        self.session_models.catalog = catalog

    @property
    def tools(self) -> ToolRegistry:
        return self.registries.tools

    @property
    def observers(self) -> ObserverRegistry:
        return self.registries.observers

    @property
    def context_providers(self) -> ContextProviderRegistry:
        return self.registries.request_providers.observations

    @property
    def system_prompt_providers(self) -> SystemPromptRegistry:
        return self.registries.request_providers.system_prompts

    @property
    def compaction_strategies(self) -> CompactionStrategyRegistry:
        return self.registries.compaction

    @property
    def prompt_policies(self) -> PromptPolicyRegistry:
        return self.registries.prompt_policies

    async def refresh_configuration(self) -> None:
        """Publish settings for subsequent operations without mutating their snapshots."""
        if self.settings_resolver is None:
            return
        settings = await run_blocking(self.settings_resolver)
        if settings is self._applied_settings:
            return
        self._permission_overrides = dict(settings.permissions.tools)
        self._plugin_permission_overrides = dict(settings.permissions.plugins)
        self._tool_execution_config = settings.tool_execution
        self.run_budget = settings.run_budget
        self.queued_input_config = settings.queued_inputs
        self.compaction_config = settings.compaction
        self.context_compaction.config = settings.compaction
        self._applied_settings = settings

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

    def bind_extensions(
        self,
        *,
        catalog: RuntimeExtensions | None = None,
        agent_definitions: AgentDefinitionService | None = None,
        agent_controls: AgentService | None = None,
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
        self.input_admission.extensions = catalog
        self.input_admission.reconcile_agent = (
            agent_controls.reconcile_session if agent_controls else None
        )
        if agent_controls is not None:
            self.driver_execution.model_ports = replace(
                self.driver_execution.model_ports, shared_budget_reason=agent_controls.budget_reason
            )
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
        catalog: RuntimeCapabilities | RuntimeExtensions
        if snapshot is not None:
            catalog = snapshot
        elif self._extension_catalog is not None:
            catalog = self._extension_catalog
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
        def active_controller_ids() -> set[str]:
            return self._active_controller_ids_for_run(run_id)

        resources = self._run_resources.get(run_id)
        if resources is None:
            return self.tools.scoped(active_controller_ids)
        return resources.select_tools(self.tools, active_controller_ids)

    def _request_providers_for_run(self, run_id: str) -> tuple[RequestProviders, Container[str]]:
        resources = self._run_resources.get(run_id)
        if resources is None:
            return self.registries.request_providers, ()
        snapshot = resources.snapshot
        providers = (
            snapshot.request_providers
            if snapshot is not None
            else self.registries.request_providers
        )
        return providers, resources.controller_ids

    def _skills_for_run(self, run_id: str) -> tuple[SkillMetadata, ...]:
        snapshot = self._extension_snapshot_for_run(run_id)
        return snapshot.skills if snapshot is not None else self.skill_activations.base_skills

    def _skill_instructions_for_run(self, run_id: str) -> str:
        return render_agent_skills_prompt(self._skills_for_run(run_id))

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
        registry = snapshot.continuations if snapshot is not None else self.registries.continuations
        if not active_only:
            return registry
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _tool_policies_for_run(self, run_id: str) -> ToolPolicyRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.tool_policies if snapshot is not None else self.registries.tool_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _turn_policies_for_run(self, run_id: str) -> TurnPolicyRegistry:
        snapshot = self._extension_snapshot_for_run(run_id)
        registry = snapshot.turn_policies if snapshot is not None else self.registries.turn_policies
        return registry.scoped(lambda: self._active_controller_ids_for_run(run_id))

    def _active_controller_ids_for_run(self, run_id: str) -> set[str]:
        resources = self._run_resources.get(run_id)
        return set(resources.controller_ids) if resources is not None else set()

    def _session_controllers_for_run(self, run_id: str):
        from ouro.core.session_controller_registry import SessionControllerRegistry

        snapshot = self._extension_snapshot_for_run(run_id)
        return snapshot.session_controllers if snapshot is not None else SessionControllerRegistry()

    def _tool_calls_for_run(self, run_id: str) -> ToolCallCoordinator:
        resources = self._run_resources.get(run_id)
        if resources is None:
            resources = RunResources()
            self._run_resources[run_id] = resources
        execution_config = self._tool_execution_config
        current = resources.tool_calls
        if (
            current is not None
            and current.execution_config == execution_config
            and current.executor.permission_overrides == self._permission_overrides
            and current.executor.plugin_permission_overrides == self._plugin_permission_overrides
        ):
            return current
        tools = self._tools_for_run(run_id)
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
            is_aborted=self.run_ownership.is_aborted,
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
                if not run.has_pending_continuation:
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
        if snapshot.pending_continuation and await self.reconcile_pending_run_continuations(
            session_id
        ):
            return await run_blocking(self.store.load_runtime_session, session_id)
        return snapshot

    async def reconcile_session(self, session_id: str) -> tuple[Diagnostic, ...]:
        """Recover only the target Session and its bounded Agent aggregate."""

        _, diagnostics = await self._reconcile_session(session_id)
        return diagnostics

    async def _reconcile_session(
        self,
        target: str | SessionRecord,
    ) -> tuple[RuntimeSessionSnapshot, tuple[Diagnostic, ...]]:
        """Return the post-reconciliation target snapshot for execution."""

        session_id = target.session_id if isinstance(target, SessionRecord) else target
        if self._agent_controls is None:
            return await self.reconcile_execution_session(session_id), ()

        session = (
            target
            if isinstance(target, SessionRecord)
            else await run_blocking(self.store.load_session_record, session_id)
        )
        return await self._agent_controls.reconcile_for_execution(
            session, self.reconcile_execution_session
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

        await self.skill_activations.refresh(self._extension_catalog, skills)

    async def available_skills(self) -> tuple[SkillMetadata, ...]:
        """Return current directory and active plugin skills for one Host query."""

        await self.refresh_skills()
        await self._synchronize_extensions()
        if self._extension_catalog is None:
            return self.skill_activations.list_for_host(self.skill_activations.base_skills)
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
        return self.session_models.current_model(session_id)

    def source_progress(self, session_id: str) -> SourceHistory | None:
        budget = self.current_model(session_id).context_budget.input_tokens
        return freeze_source_history(self.store, session_id, input_budget=budget)

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
        return self.session_models.resolve_model(selector)

    def session_reasoning(self, session_id: str) -> ModelReasoningState:
        return self.session_models.session_reasoning(session_id)

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
        self.session_models.reasoning_state(selected_model, selected_reasoning)
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
        self.session_models.reasoning_state(selected, reasoning)
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
        return self.session_models.switch_session_model(
            session_id, selector, reasoning_request=reasoning_request
        )

    def set_session_reasoning(
        self,
        session_id: str,
        reasoning_request: ReasoningRequest,
    ) -> SessionRecord:
        return self.session_models.set_session_reasoning(session_id, reasoning_request)

    def set_session_approval_mode(
        self,
        session_id: str,
        approval_mode: SessionApprovalMode,
    ) -> SessionRecord:
        """Persist the approval policy used by future tool calls in a session."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        return self.sessions.update_settings(
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
        return self.sessions.update_settings(
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
        return self.sessions.update_settings(
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

    async def accept_user_input(self, request: UserInputRequest) -> HostInputReceipt:
        receipt, _ = await self.input_admission.accept(request)
        return receipt

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
        on_provider_stream_start: Callable[[], None] | None = None,
        on_session_created: Callable[[SessionRecord], None] | None = None,
        on_input_accepted: Callable[[], None] | None = None,
    ) -> RunResult:
        """Submit user input and own its execution until the next Run boundary."""
        if skill_invocation is not None:
            if skill_invocations:
                raise ValueError("use either skill_invocation or skill_invocations")
            skill_invocations = (skill_invocation,)
        if skill_invocations and not prompt:
            prompt = skill_invocations[0].arguments
        if len(attachments) > 16:
            raise ResourceAttachmentError("A message can contain at most 16 attachments")
        attachment_refs = InputContent(text=prompt, attachments=attachments).resource_refs
        refs = tuple(dict.fromkeys(resource_refs or attachment_refs))
        if attachments and refs != attachment_refs:
            raise ResourceAttachmentError("Attachment identities must match resource references")
        if refs and session_id is None and new_session_id is None:
            raise ResourceAttachmentError(
                "resource attachments require an existing Session or a reserved namespace; "
                "pass session_id or new_session_id"
            )
        if driver_id is not None and queue_mode != "follow_up":
            raise ValueError("Driver selection cannot steer an existing Run")
        request = UserInputRequest(
            prompt=prompt,
            session_id=session_id,
            new_session_id=new_session_id,
            input_id=input_id or new_id("queued"),
            title=title,
            initial_model=initial_model,
            initial_reasoning_request=initial_reasoning_request,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            workspace=str(workspace) if workspace is not None else None,
            queue_mode=queue_mode,
            skill_invocations=skill_invocations,
            resource_refs=refs,
            attachments=attachments,
        )
        admitted: PreparedUserInput | HostInputReceipt
        receipt = None
        target: str | SessionRecord | None = session_id
        if session_id is not None and input_id is not None and self._agent_controls is not None:
            existing = await run_blocking(
                self.input_admission.receipt_or_session, session_id, request, driver_id=driver_id
            )
            if isinstance(existing, SessionRecord):
                target = existing
            else:
                receipt = existing
        elif input_id is not None and (destination_id := session_id or new_session_id) is not None:
            receipt = await run_blocking(
                self.input_admission.receipt, destination_id, request, driver_id=driver_id
            )
        if receipt is None:
            if refs:
                await self.refresh_configuration()
            await self.input_admission.refresh_sources(request)
            driver = self._select_input_driver(driver_id)
            if target is not None:
                snapshot = await self._input_target(target)
                try:
                    incoming = IncomingRunInput(request, driver)
                    return await self._start_inputs(
                        snapshot,
                        incoming.new_run(snapshot.session),
                        incoming,
                        on_input_accepted=on_input_accepted,
                        on_provider_stream_start=on_provider_stream_start,
                    )
                except InputAlreadyAccepted as conflict:
                    if conflict.input_id != request.input_id:
                        raise
                    receipt = await run_blocking(
                        self.input_admission.receipt,
                        snapshot.session.session_id,
                        request,
                        driver_id=driver_id,
                    )
                    if receipt is None:
                        raise
                admitted = receipt
            else:
                initial = await self.input_admission.accept_new(request, driver)
                admitted = initial if isinstance(initial, PreparedUserInput) else initial[0]
        else:
            admitted = receipt
        destination = (
            admitted.session.session_id
            if isinstance(admitted, PreparedUserInput)
            else admitted.session_id
        )
        try:
            if on_session_created is not None and session_id is None:
                session = (
                    admitted.session
                    if isinstance(admitted, PreparedUserInput)
                    else await run_blocking(self.store.load_session_record, destination)
                )
                on_session_created(session)
            if on_input_accepted is not None:
                on_input_accepted()
            if isinstance(admitted, PreparedUserInput):
                try:
                    return await self._start_inputs(
                        RuntimeSessionSnapshot(session=admitted.session),
                        admitted.run,
                        (admitted.item,),
                        initial=admitted,
                        on_provider_stream_start=on_provider_stream_start,
                    )
                except QueuedInputSelectionChanged:
                    pass
            elif admitted.run_id is not None:
                return await run_blocking(self.sessions.run_result, destination, admitted.run_id)
        finally:
            if isinstance(admitted, PreparedUserInput):
                admitted.close()
        result = await self._consume_pending_input(
            destination,
            request.input_id,
            direct_first=session_id is None,
            on_provider_stream_start=on_provider_stream_start,
        )
        if result is not None:
            return result
        receipt = await run_blocking(
            self.input_admission.receipt, destination, request, driver_id=driver_id
        )
        if receipt is None or receipt.run_id is None:
            raise PersistenceError("Accepted input has no execution result")
        return await run_blocking(self.sessions.run_result, destination, receipt.run_id)

    async def _input_target(self, target: str | SessionRecord) -> RuntimeSessionSnapshot:
        session_id = target.session_id if isinstance(target, SessionRecord) else target
        if await run_blocking(self.run_ownership.is_locked, session_id):
            return await run_blocking(self.store.load_runtime_session, session_id)
        snapshot, diagnostics = await self._reconcile_session(target)
        self._raise_incompatible_target(snapshot.session, diagnostics)
        return snapshot

    async def _start_inputs(
        self,
        snapshot: RuntimeSessionSnapshot,
        run: RunRecord,
        inputs: IncomingRunInput | tuple[QueuedInput, ...],
        *,
        initial: PreparedUserInput | None = None,
        reservation: RunReservation | None = None,
        continuation_request: SessionContinuationRequest | None = None,
        on_provider_stream_start: Callable[[], None] | None = None,
        on_input_accepted: Callable[[], None] | None = None,
    ) -> RunResult:
        incoming = inputs if isinstance(inputs, IncomingRunInput) else None
        accepted = inputs if isinstance(inputs, tuple) else ()
        settings = incoming.settings if incoming else InputSessionSettings()
        if incoming is not None:
            await self.input_admission.validate_resources(
                snapshot.session, incoming.resource_refs, incoming.attachments
            )
        elif run.status == "queued":
            await self.input_admission.validate_resources(
                snapshot.session, accepted[0].resource_refs
            )

        async def enqueue(current: RuntimeSessionSnapshot, active: RunRecord) -> RunResult:
            assert incoming is not None
            settings.apply(current.session, executing=True)
            session, _ = await self.input_admission.enqueue(
                current, active, incoming, on_input_accepted=on_input_accepted
            )
            await self.advance_session(session.session_id, session.history_epoch)
            return RunResult(session=session, run=active)

        async def start(
            locked_snapshot: RuntimeSessionSnapshot, session: SessionRecord, run: RunRecord
        ) -> tuple[SessionRecord, RunRecord]:
            session = settings.apply(session)
            if initial is not None:
                run = await self._bind_extension_snapshot(run, initial.take_snapshot())
            elif run.status == "queued" and run.has_recorded_extension_binding:
                await self._ensure_extension_snapshot(run)
            else:
                run = await self._attach_extension_snapshot(run)
            catalog = self._extension_snapshot_for_run(run.run_id)
            if incoming is not None:
                pending_inputs = [
                    await self.input_admission.materialize(incoming, session.history_epoch, catalog)
                ]
            else:
                pending_inputs = list(accepted)
            for pending_input in pending_inputs:
                await self.input_admission.validate_resources(
                    session,
                    pending_input.resource_refs,
                    pending_input.attachments,
                )
            if initial is None:
                await self.input_admission.evaluate_policies(
                    session=session,
                    run=run,
                    inputs=pending_inputs,
                    catalog=catalog,
                )
            if accepted:
                session, run, input_messages, committed_events = await run_blocking(
                    self._start_queued_run,
                    session=session,
                    run=run,
                    snapshot=locked_snapshot,
                    inputs=pending_inputs,
                    continuation_request=continuation_request,
                )
            else:
                input_messages, input_events = await run_blocking(
                    self._prepare_input_messages,
                    session=session,
                    run=run,
                    inputs=pending_inputs,
                    is_queued=False,
                )
                session, run, committed_events = await run_blocking(
                    self.run_lifecycle.start,
                    session=session,
                    session_settings=settings,
                    run=run,
                    snapshot=locked_snapshot,
                    input_messages=tuple(input_messages),
                    accepted_inputs=tuple(
                        InputRecord(session_id=session.session_id, item=item)
                        for item in pending_inputs
                    ),
                    consumed_inputs=tuple(item.queue_id for item in pending_inputs),
                    events=input_events,
                )

            await self.event_recorder.publish_committed(committed_events)
            for _ in pending_inputs:
                if on_input_accepted is not None:
                    on_input_accepted()

            return session, run

        return await self.run_start.execute(
            snapshot,
            run,
            start=start,
            enqueue=enqueue if incoming is not None else None,
            extensions=self._extension_catalog,
            reservation=reservation,
            continuation_request=continuation_request,
            on_provider_stream_start=on_provider_stream_start,
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
        pending = await self._pending_queued_inputs(snapshot)
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
            if latest_run.has_pending_continuation:
                settlement = await self._settle_run_continuation(latest_run)
                if settlement.settled:
                    await self.event_recorder.publish_committed(settlement.events)
                if settlement.run.has_pending_continuation:
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
            run = prepare_run_continuation(run)
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
            if not fresh and self._agent_controls is not None:
                await self._agent_controls.continue_children(run)
            signal = await self.run_ownership.activate_signal(session_id, run.run_id)
            result = await self.driver_execution.execute(
                extensions=self._extension_catalog,
                session=session,
                run=run,
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
        return await self._finalize_run_boundary(result)

    async def run_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput | DelegatedTaskInput,
        *,
        reservation: RunReservation | None = None,
    ) -> RunResult:
        """Execute an already durable queued Run with typed input attribution."""

        if run.status != "queued":
            raise ValueError("prepared run must be queued")
        if isinstance(session_input, DelegatedTaskInput) and (
            run.metadata.get("delegation_id") != session_input.delegation_id
        ):
            raise ValueError("prepared child run does not match delegated task identity")
        if session_input.driver_id is not None:
            raise ValueError("accepted work already has a Driver binding")
        inputs: IncomingRunInput | tuple[QueuedInput, ...] = IncomingRunInput(session_input, None)
        if isinstance(session_input, HostSessionInput) and session_input.queue_id is not None:
            accepted = await run_blocking(
                self.store.load_input, run.session_id, session_input.queue_id
            )
            if accepted is None or accepted.item.prospective_run_id != run.run_id:
                raise PersistenceError("prepared Host input does not belong to its queued Run")
            validate_host_input(accepted, session_input)
            inputs = (accepted.item,)
        try:
            if session_input.resource_refs or (
                isinstance(inputs, tuple) and any(item.attachments for item in inputs)
            ):
                await self.refresh_configuration()
            await self._synchronize_extensions()
            snapshot = await run_blocking(self.store.load_runtime_session, run.session_id)
            if (
                isinstance(session_input, HostSessionInput)
                and session_input.history_epoch is not None
                and session_input.history_epoch != snapshot.session.history_epoch
            ):
                raise SessionCommitConflict("Host input belongs to withdrawn Session history")
            if isinstance(session_input, DelegatedTaskInput) and (
                snapshot.session.session_kind != "subagent"
                or snapshot.session.delegation_id != session_input.delegation_id
            ):
                raise ValueError("delegated task input must target its child Session")
            if isinstance(inputs, tuple) and inputs[0].driver is not None:
                run = run.model_copy(update={"driver": inputs[0].driver})
            return await self._start_inputs(snapshot, run, inputs, reservation=reservation)
        except DelegationStartRevoked as exc:
            return await self.abort_run(
                session_id=run.session_id, run_id=run.run_id, reason=str(exc)
            )

    async def _prepare_request_configuration(self, run: RunRecord) -> RunBudgetPolicy:
        """Publish request settings and persist a changed root budget before preflight."""
        await self.refresh_configuration()
        policy = self._run_budget_for_run(run)
        if self.settings_resolver is not None and self._run_resources[run.run_id].budget is None:
            serialized = policy.model_dump(mode="json")
            if run.metadata.get("run_budget") != serialized:
                await run_blocking(self.run_lifecycle.save_budget, run, serialized)
        return policy

    def _run_budget_for_run(self, run: RunRecord) -> RunBudgetPolicy:
        resources = self._run_resources[run.run_id]
        if resources.budget is not None:
            return resources.budget
        if self.settings_resolver is not None:
            return self.run_budget
        return run.budget_policy or self.run_budget

    async def compact(self, *, session_id: str) -> CompactionCommandResult:
        """Manually compact a session through the same durable summary pipeline as auto mode."""

        snapshot = await run_blocking(self.session_recovery.recover_runtime, session_id)
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
            abort_signal = await self.run_ownership.activate_signal(session_id, run.run_id)
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            if (
                maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
                is not None
            ):
                raise SessionBusyError(f"session already has an active run: {session_id}")
            active_session, active_adapter = await run_blocking(
                self.session_models.bind_session, snapshot.session.session_id
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
            try:
                execution = await self.context_compaction.compact(
                    session=active_session,
                    run=run,
                    tools=self._tools_for_run(run.run_id).definitions(),
                    trigger="manual",
                    force=True,
                    abort_signal=abort_signal,
                    adapter=active_adapter,
                    strategies=self._compaction_for_run(run.run_id),
                    skill_instructions=self._instructions_for_run(run),
                )
            except CompactionAbortedError:
                cancelled = await self.run_lifecycle.finalize_requested_abort(
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
            completed = await self.run_lifecycle.finalize_requested_abort(completed)
            terminal_run = completed.run
            return CompactionCommandResult(
                session=completed.session,
                run=completed.run,
                compaction=execution.record,
                skipped=execution.record is None,
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
        result = await self.run_lifecycle.finalize_requested_abort(
            RunResult(session=session, run=run)
        )
        if result.run.status == "aborted":
            return await self._finalize_run_boundary(result)
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

        await self._synchronize_extensions()
        incoming = IncomingRunInput(
            session_input, self._select_input_driver(session_input.driver_id)
        )
        snapshot = (
            await self._input_target(session_id)
            if session_id is not None
            else RuntimeSessionSnapshot(
                session=await run_blocking(self.create_session, title=session_input.title)
            )
        )
        return await self._start_inputs(snapshot, incoming.new_run(snapshot.session), incoming)

    async def submit_plugin_input_batch(
        self,
        batch: AttributedSessionInputBatch,
        *,
        session_id: str,
    ) -> RunResult:
        """Durably accept and consume one attributed atomic input unit."""

        await self._synchronize_extensions()
        snapshot = await run_blocking(self.session_recovery.submission_snapshot, session_id)
        active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
        queued_session, queued = await self._queue_input_batch(
            snapshot=snapshot,
            active_run=active,
            batch=batch,
        )
        if active is not None:
            await self.advance_session(queued_session.session_id, queued_session.history_epoch)
            return RunResult(session=queued_session, run=active)
        target_ids = {item.queue_id for item in queued}
        while True:
            snapshot = await run_blocking(self.session_recovery.submission_snapshot, session_id)
            active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
            if active is not None:
                return RunResult(session=snapshot.session, run=active)
            if snapshot.session.history_epoch != queued_session.history_epoch:
                raise SessionCommitConflict(
                    "Plugin input batch belongs to withdrawn Session history"
                )
            pending = await self._pending_queued_inputs(snapshot)
            if not pending:
                break
            try:
                result = await self._run_queued_inputs(
                    session_id,
                    pending,
                )
            except QueuedInputSelectionChanged:
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
        """Durably accept an OURO-owned harness input without awaiting execution."""

        await self._synchronize_extensions()
        session = await run_blocking(self.store.load_session_record, session_id)
        await self.input_admission.validate_resources(
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
    ) -> RunResult:
        """Consume a previously accepted Session input through the ordinary Run path."""

        result = await self._consume_pending_input(
            receipt.session_id, receipt.queue_id, continuation_request=_continuation_request
        )
        if result is not None:
            return result
        return await run_blocking(self._consumed_host_input_result, receipt)

    async def _consume_pending_input(
        self,
        session_id: str,
        queue_id: str,
        *,
        direct_first: bool = False,
        continuation_request: SessionContinuationRequest | None = None,
        on_provider_stream_start: Callable[[], None] | None = None,
    ) -> RunResult | None:
        while True:
            snapshot = await run_blocking(self.session_recovery.submission_snapshot, session_id)
            require_current_continuation(snapshot.session, continuation_request)
            item = await run_blocking(self.queued_inputs.find_session, snapshot.session, queue_id)
            if item is None:
                return None
            active = maybe_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)
            if active is not None:
                await self.advance_session(session_id, snapshot.session.history_epoch)
                return RunResult(session=snapshot.session, run=active)
            try:
                pending = await self._pending_queued_inputs(snapshot)
                if not pending:
                    return None
                selected = any(member.queue_id == queue_id for member in pending)
                result = await self._run_queued_inputs(
                    session_id,
                    pending,
                    direct_first=direct_first and selected,
                    continuation_request=continuation_request,
                    on_provider_stream_start=on_provider_stream_start if selected else None,
                )
                if selected or result.run.status in ACTIVE_RUN_STATUSES:
                    return result
            except QueuedInputSelectionChanged:
                continue

    def _consumed_host_input_result(self, receipt: HostInputReceipt) -> RunResult:
        with self.store.session_read(receipt.session_id):
            message = self.store.find_queued_input_message(receipt.session_id, receipt.queue_id)
            if message is not None and message.run_id is not None:
                return self.sessions.run_result(receipt.session_id, message.run_id)
        raise PersistenceError(
            f"accepted Host input is missing from queue and canonical history: {receipt.queue_id}"
        )

    async def _queue_input_batch(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        active_run: RunRecord | None,
        batch: AttributedSessionInputBatch,
    ) -> tuple[SessionRecord, tuple[QueuedInput, ...]]:
        first = batch.inputs[0]
        session, queued, _, _, events = await run_blocking(
            self.queued_inputs.enqueue_batch,
            snapshot=snapshot,
            inputs=batch.inputs,
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

    def bind_session_scheduler(self, schedule: Callable[[str, int], Awaitable[None]]) -> None:
        """Hand execution-boundary wakeups to the Application scheduling owner."""
        self._session_scheduler = schedule

    async def advance_session(self, session_id: str, history_epoch: int) -> None:
        """Advance accepted work without retaining any particular input receipt."""
        if self._session_scheduler is not None:
            await self._session_scheduler(session_id, history_epoch)
            return
        if session_id in self._advancing_sessions:
            return
        self._advancing_sessions.add(session_id)
        try:
            while (
                await self.run_pending_inputs(session_id, history_epoch=history_epoch) is not None
            ):
                pass
        except Exception as exc:  # noqa: BLE001 - successor execution isolation boundary.
            await self.event_recorder.publish(
                session_id=session_id,
                event_type="diagnostic",
                run_id=None,
                payload={
                    "code": "session_dispatch_failed",
                    "severity": "error",
                    "component": "runtime",
                    "error_type": type(exc).__name__,
                    "recoverable": True,
                },
            )
        finally:
            self._advancing_sessions.remove(session_id)

    async def run_pending_inputs(
        self,
        session_id: str,
        *,
        history_epoch: int,
        on_provider_stream_start: Callable[[], None] | None = None,
    ) -> RunResult | None:
        """Execute one current input unit, preserving the native consumption boundary."""
        while True:
            snapshot = await run_blocking(self.store.load_runtime_session, session_id)
            if snapshot.session.history_epoch != history_epoch:
                return None
            if maybe_active_run(snapshot.runs, history_epoch=history_epoch) is not None:
                return None
            if snapshot.pending_continuation is not None:
                return None
            pending = await self._pending_queued_inputs(snapshot)
            if not pending:
                return None
            try:
                return await self._run_queued_inputs(
                    session_id,
                    pending,
                    on_provider_stream_start=on_provider_stream_start,
                )
            except QueuedInputSelectionChanged:
                continue

    async def _run_queued_inputs(
        self,
        session_id: str,
        inputs: list[QueuedInput],
        *,
        direct_first: bool = False,
        continuation_request: SessionContinuationRequest | None = None,
        on_provider_stream_start: Callable[[], None] | None = None,
    ) -> RunResult:
        if any(item.resource_refs or item.attachments for item in inputs):
            await self.refresh_configuration()
        await self._synchronize_extensions()
        snapshot = await self._input_target(session_id)
        accepted = tuple(inputs)
        return await self._start_inputs(
            snapshot,
            queued_run(snapshot.session, accepted, direct_first=direct_first),
            accepted,
            continuation_request=continuation_request,
            on_provider_stream_start=on_provider_stream_start,
        )

    async def _queued_input_units(
        self, session: SessionRecord, *, mode: QueuedInputMode | None = None
    ) -> list[tuple[QueuedInput, ...]]:
        """Bind the consumption policy once for a nonempty queue selection."""
        if not session.pending_input_ids:
            return []
        await self.refresh_configuration()
        return await run_blocking(
            self.queued_inputs.eligible_units_session,
            session,
            mode=mode,
            limit=1 if self.queued_input_config.consumption_mode == "one_by_one" else None,
        )

    async def _pending_queued_inputs(self, snapshot: RuntimeSessionSnapshot) -> list[QueuedInput]:
        pending_units = await self._queued_input_units(snapshot.session)
        if not pending_units:
            return []
        if len(pending_units) == 1:
            return list(pending_units[0])
        first = pending_units[0][0]
        compatible_units: list[tuple[QueuedInput, ...]] = []
        for unit in pending_units:
            if any(
                member.driver != first.driver
                or member.prompt_policy_checked != first.prompt_policy_checked
                for member in unit
            ):
                break
            compatible_units.append(unit)
        return [item for unit in compatible_units for item in unit]

    def _select_input_driver(self, driver_id: str | None) -> RunDriverBinding | None:
        if driver_id is None:
            return None
        if driver_id == DEFAULT_DRIVER_ID:
            return RunDriverBinding()
        if self._extension_catalog is None:
            raise ValueError("plugin Run drivers require an extension catalog")
        return self._extension_catalog.select_driver(driver_id)

    async def _consume_driver_inputs(self, run: RunRecord, turn_id: str) -> list[MessageRecord]:
        session = await run_blocking(self.store.load_session_record, run.session_id)
        units = await self._queued_input_units(session, mode="steer")
        eligible = [item for unit in units for item in unit]
        if not eligible:
            return []

        current = self._extension_catalog.snapshot() if self._extension_catalog else None
        try:
            await self.input_admission.evaluate_policies(
                session=session,
                run=run,
                inputs=eligible,
                catalog=current or self._extension_snapshot_for_run(run.run_id),
            )
        finally:
            if current is not None:
                current.close()

        appended: list[MessageRecord] = []
        for unit in units:
            consumed, events = await run_blocking(
                self._consume_queued_turn_inputs,
                run=run,
                inputs=unit,
                turn_id=turn_id,
            )
            if not consumed:
                continue
            appended.extend(consumed)
            await self.event_recorder.publish_committed(events)
        return appended

    async def _evaluate_turn_completion(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        assistant_text: str,
        assistant_reasoning: str,
        turn_id: str,
    ) -> TurnPolicyEvaluation:
        policies = self._turn_policies_for_run(run.run_id)
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
        await self.event_recorder.record_turn_policy_failures(evaluation.failures)
        if evaluation.failures or not evaluation.should_continue:
            return evaluation

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
            role="harness",
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
        return evaluation

    def _prepare_input_messages(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        inputs: Sequence[QueuedInput],
        is_queued: bool,
        turn_id: str | None = None,
    ) -> tuple[list[MessageRecord], tuple[EventRecord, ...]]:
        """Project complete inputs in order, independently of their delivery boundary."""
        units = []
        sequence = session.message_count + 1
        for item in inputs:
            unit = self._prepare_input(
                session=session,
                run=run,
                item=item,
                sequence=sequence,
                is_queued=is_queued,
            )
            units.append(unit)
            sequence += len(unit)
        remaining = (
            self._remaining_input_budget(session, run, [m for unit in units for m in unit])
            if any(item.source_history is not None for item in inputs)
            else 0
        )
        messages: list[MessageRecord] = []
        diagnostics: list[EventRecord] = []
        for item, unit in zip(inputs, units, strict=True):
            if item.source_history is not None:
                remaining, diagnostic = prepend_source_history(
                    self.store,
                    session,
                    run.run_id,
                    item.source_history,
                    messages,
                    unit,
                    remaining,
                    self.context_builder.token_estimator,
                )
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
            for member in unit:
                member.sequence = session.message_count + len(messages) + 1
                messages.append(member)
        events = input_message_events(run, inputs, messages, queued=is_queued, turn_id=turn_id)
        return messages, (*events, *diagnostics)

    def _remaining_input_budget(
        self, session: SessionRecord, run: RunRecord, inputs: list[MessageRecord]
    ) -> int:
        """Reserve the current history, tools and all accepted questions before adding evidence."""
        _, active, history = self.context_preparation.load_history(session.session_id)
        view = self.context_preparation.project(
            session=session,
            messages=history + inputs,
            active=active,
            tools=self._tools_for_run(run.run_id).definitions(),
            adapter=self.session_models.adapter_for_session_id(session.session_id),
            skill_instructions=self._instructions_for_run(run),
            prompt_context=self.context_preparation.local_prompt_context(),
        )
        previous = self.sessions.effective_context_metrics(session.session_id)
        extra_fixed = (
            max(0, previous.estimated_fixed_tokens - view.metrics.estimated_fixed_tokens)
            if previous
            else 0
        )
        return (view.metrics.estimated_remaining_tokens or 0) - extra_fixed

    def _prepare_input(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        item: QueuedInput,
        sequence: int,
        is_queued: bool,
    ) -> list[MessageRecord]:
        """Materialize one complete input as an indivisible message unit."""
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
            skill_activations=item.skill_activations,
            sequence=sequence,
            resource_refs=item.resource_refs,
            metadata=metadata,
            semantic_tag=item.semantic_tag,
            semantic_attributes=item.semantic_attributes,
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
            messages[0] = self.resources.prepare_input(
                messages[0],
                input_modalities=self.session_models.adapter_for_session_id(
                    session.session_id
                ).model.input_modalities,
                workspace=session.workspace,
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
            if not self.queued_inputs.matches_pending(latest_session, inputs):
                raise QueuedInputSelectionChanged
            messages, input_events = self._prepare_input_messages(
                session=latest_session, run=run, inputs=inputs, is_queued=True
            )
            started_session, started_run, events = self.run_lifecycle.start(
                session=latest_session,
                run=run,
                snapshot=snapshot,
                input_messages=tuple(messages),
                consumed_inputs=tuple(item.queue_id for item in inputs),
                events=input_events,
                continuation_request=continuation_request,
            )
        return started_session, started_run, messages, events

    def _consume_queued_turn_inputs(
        self,
        *,
        run: RunRecord,
        inputs: tuple[QueuedInput, ...],
        turn_id: str,
    ) -> tuple[list[MessageRecord], tuple[EventRecord, ...]]:
        """Consume one complete unit and its queue identities in one durable decision."""

        with self.store.session_mutation(run.session_id):
            latest = self.store.load_session_record(run.session_id)
            if not self.queued_inputs.matches_pending(latest, inputs):
                return [], ()
            messages, input_events = self._prepare_input_messages(
                session=latest, run=run, inputs=inputs, is_queued=True, turn_id=turn_id
            )
            events = self.run_lifecycle.append_messages(
                session=latest,
                run=run,
                messages=messages,
                consumed_inputs=tuple(item.queue_id for item in inputs),
                events=input_events,
            )
            return messages, events

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
        return await self.parked_runs.approve(
            session_id,
            run_id,
            approval_id,
            approved=approved,
            reason=reason,
            arguments_override=arguments_override,
        )

    async def answer_user(
        self,
        *,
        session_id: str,
        run_id: str,
        answer: JsonValue,
        question: ToolQuestion | None = None,
    ) -> RunResult:
        return await self.parked_runs.answer(session_id, run_id, answer, question)

    async def resume_agent_wait(
        self,
        *,
        session_id: str,
        identity: ToolCallIdentity,
        resolution: AgentWaitResolution,
    ) -> RunResult:
        """Resolve one durable child join and continue the original parent Run."""

        return await self.parked_runs.resume_child(session_id, identity, resolution)

    async def _continue_resolved_batch(
        self,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        signal: AbortSignal,
    ) -> RunResult:
        try:
            if run.status not in TOOL_WAIT_RUN_STATUSES:
                await self._ensure_extension_snapshot(run)
            handling = await self._tool_calls_for_run(run.run_id).execute_batch(
                session=session,
                run=run,
                batch=batch,
                abort_signal=signal,
            )
        except SessionBusyError:
            raise
        except Exception as exc:  # noqa: BLE001 - coordinator is a runtime boundary.
            latest_session = await run_blocking(
                self.store.load_session_record,
                session.session_id,
            )
            latest_run = await run_blocking(self.store.load_run, session.session_id, run.run_id)
            failed = await self.run_lifecycle.fail(
                session=latest_session,
                run=latest_run,
                error=f"tool batch handling failed: {exc}",
                final_text="",
                had_tool_error=False,
            )
            return await self.run_lifecycle.finalize_requested_abort(failed)
        session, run = handling.session, handling.run
        abort_result = self.run_ownership.aborted_result(
            session_id=session.session_id,
            run_id=run.run_id,
            final_text="",
            had_tool_error=handling.had_tool_error,
        )
        if abort_result is not None:
            return await self.run_lifecycle.finalize_requested_abort(abort_result)
        if handling.status != "completed":
            return RunResult(
                session=session,
                run=run,
                had_tool_error=handling.had_tool_error,
            )
        session, active_adapter = await run_blocking(
            self.session_models.bind_session, session.session_id
        )
        return await self.driver_execution.execute(
            extensions=self._extension_catalog,
            session=session,
            run=run,
            abort_signal=signal,
            adapter=active_adapter,
            resuming_driver=True,
            resume_turn_id=batch.turn_id,
        )

    async def _finalize_run_boundary(
        self,
        result: RunResult,
    ) -> RunResult:
        run = result.run
        usage = await run_blocking(self.run_lifecycle.usage_for_run, run)
        if run.status not in ACTIVE_RUN_STATUSES:
            try:
                if self._agent_controls is not None:
                    await self._agent_controls.settle_terminal_run(run)
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
        if run.status not in ACTIVE_RUN_STATUSES and not run.has_pending_continuation:
            await self.advance_session(run.session_id, run.history_epoch)
        return updated

    async def _settle_run_continuation(
        self,
        run: RunRecord,
        *,
        final_text: str = "",
    ) -> RunContinuationSettlement:
        if not run.has_pending_continuation:
            return RunContinuationSettlement(run=run)

        withdrawn = await run_blocking(
            RunContinuationService(
                store=self.store,
                registry=self.registries.continuations,
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

    def _finish_run_observers(self, run: RunRecord) -> None:
        if run.status not in ACTIVE_RUN_STATUSES:
            self.event_recorder.finish_run_observers(run.run_id)
        if run.status != "running":
            self._release_run_resources(run.run_id)

    def _release_run_resources(self, run_id: str) -> None:
        snapshot = self._detach_extension_snapshot(run_id)
        if snapshot is not None:
            snapshot.close()

    def _detach_extension_snapshot(self, run_id: str) -> RuntimeCapabilities | None:
        resources = self._run_resources.pop(run_id, None)
        return resources.take_snapshot() if resources is not None else None

    def _extension_snapshot_for_run(self, run_id: str | None) -> RuntimeCapabilities | None:
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
            require_available_controls(
                session.controls, snapshot.session_controllers if snapshot is not None else None
            )
            metadata["session_control_snapshot"] = session.controls.run_snapshot(model_round)
            resources.controller_ids = frozenset(
                item.controller_id for item in session.controls.activations
            )
            update: dict[str, object] = {"metadata": metadata}
            if latest.kind == "agent" and snapshot is not None:
                update["continuation"] = self._continuation_checkpoint(
                    latest, snapshot, session.controls
                )
                metadata["extension_catalog_id"] = snapshot.catalog_id
                metadata["extension_generations"] = snapshot.generation_metadata()
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
                    spawn_policy=(
                        self._agent_controls.spawn_policy
                        if self._agent_controls is not None
                        else None
                    ),
                )
                update["agent_catalog"] = catalog
                if self._agent_controls is not None:
                    resources.bind_agent_catalog(catalog)
            updated = latest.model_copy(update=update)
            self.store.save_run(updated)
            return updated

    def _bind_control_state(self, run_id: str, controls: SessionControlState) -> str | None:
        resources = self._run_resources[run_id]
        snapshot = resources.snapshot
        unavailable = (
            snapshot.session_controllers.unavailable_controls(controls)
            if snapshot is not None
            else tuple(activation.controller_id for activation in controls.activations)
        )
        resources.controller_ids = frozenset(
            activation.controller_id for activation in controls.activations
        ).difference(unavailable)
        return (
            "Required Session controls are unavailable: " + ", ".join(unavailable)
            if unavailable
            else None
        )

    async def _attach_extension_snapshot(self, run: RunRecord) -> RunRecord:
        await self.refresh_configuration()
        await self._synchronize_extensions()
        snapshot = (
            self._extension_catalog.snapshot() if self._extension_catalog is not None else None
        )
        return await self._bind_extension_snapshot(run, snapshot)

    async def _bind_extension_snapshot(
        self,
        run: RunRecord,
        snapshot: RuntimeCapabilities | None,
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
                require_available_controls(session.controls, snapshot.session_controllers)
                metadata["session_control_snapshot"] = session.controls.run_snapshot(0)
                resources.controller_ids = frozenset(
                    activation.controller_id for activation in session.controls.activations
                )
                metadata["extension_catalog_id"] = snapshot.catalog_id
                metadata["extension_generations"] = snapshot.generation_metadata()
                if run.kind == "agent":
                    update["continuation"] = self._continuation_checkpoint(
                        run, snapshot, session.controls
                    )
            return run.model_copy(update=update)
        except BaseException:
            self._release_run_resources(run.run_id)
            raise

    def _continuation_checkpoint(
        self,
        run: RunRecord,
        snapshot: RuntimeCapabilities,
        controls: SessionControlState,
    ) -> RunContinuationCheckpoint | None:
        controllers = snapshot.continuation_controllers(controls)
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
        if run.agent_catalog is not None and self._agent_controls is not None:
            resources.bind_agent_catalog(run.agent_catalog)
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
