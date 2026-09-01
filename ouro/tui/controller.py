from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast, get_args

from pydantic import JsonValue

from ouro.application.agent_models import (
    AgentDefinitionCatalogView,
    AgentDefinitionDetail,
    AgentInteractionView,
    AgentToolOptionView,
    AgentTreeNode,
    AgentTreeSnapshot,
)
from ouro.application.agent_tree import AgentTreeSubscription
from ouro.application.agents import AgentService
from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import (
    AgentRuntimeConfigView,
    CompactionConfigInput,
    CompactionConfigView,
    MCPConnectionCheckView,
    MCPRuntimeServerView,
    MCPServerConfigInput,
    MCPServerConfigView,
    MCPTransportInput,
    ProviderCatalogView,
    ProviderEndpointCatalogView,
    ProviderEndpointView,
    ProviderTemplateView,
    ProviderView,
    QueuedInputConfigView,
    SkillCatalogView,
    SkillInstallScopeInput,
    SkillManagementView,
    SkillPolicyView,
    SkillValidationView,
    SkillView,
    ToolExecutionConfigView,
)
from ouro.application.dto import ApplicationStartupReport
from ouro.application.events import RuntimeEventSinkRegistration
from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.plugin_models import (
    PluginActionResult,
    PluginCommandExecutionView,
    PluginCommandView,
    PluginDetail,
    PluginSummary,
)
from ouro.application.queued_inputs import QueuedInputMode, QueuedUserInputView
from ouro.application.runtime import RuntimeService
from ouro.application.session_models import (
    SessionBranchResult,
    SessionCatalogEntry,
    SessionContextStatus,
    SessionDetail,
    SessionObservationSnapshot,
    SessionView,
)
from ouro.application.skill_activations import SkillInvocationRequest
from ouro.application.supervisor import RuntimeSubscription
from ouro.application.usage_models import UsageSummary, UsageTotals
from ouro.config import EndpointReasoningConfig, ProviderType, QueuedInputConsumptionMode
from ouro.core.agents import ExecutionMode, ToolSelection
from ouro.core.approvals import (
    SESSION_APPROVAL_MODES,
    SessionApprovalMode,
    ToolPermissionControl,
)
from ouro.core.base import Metadata
from ouro.core.billing import UsageFilter
from ouro.core.compaction import CompactionCommandResult, CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.errors import ConfigError, PersistenceError, SessionNotFoundError
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning
from ouro.core.results import RunResult
from ouro.core.state import RunRecord
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode, ToolEffect, ToolSource
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication
from ouro.mcp import MCPServerConfig
from ouro.skills import SkillMetadata
from ouro.tui.state import PendingInteraction

EventPublisher = Callable[[EventRecord], None]
LifecyclePublisher = Callable[["TuiLifecycleStatus"], None]

TuiApprovalPreset = Literal["inherit", "auto", "approval", "deny", "afk"]
TUI_APPROVAL_PRESETS = cast(tuple[TuiApprovalPreset, ...], get_args(TuiApprovalPreset))


@dataclass(frozen=True)
class TuiLifecycleStatus:
    activity: str
    detail: str


@dataclass(frozen=True)
class McpServerStatus:
    server_id: str
    name: str
    transport: str
    status: str
    tool_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class TuiSystemStatus:
    workspace: str | None = None
    provider_ids: tuple[str, ...] = ()
    model_count: int = 0
    skill_count: int = 0
    skill_names: tuple[str, ...] = ()
    plugin_active_count: int = 0
    plugin_total_count: int = 0
    plugin_issue_count: int = 0
    plugin_active_names: tuple[str, ...] = ()
    mcp_connected_count: int = 0
    mcp_total_count: int = 0
    mcp_tool_count: int = 0
    mcp_connecting_count: int = 0
    mcp_failed_count: int = 0
    usage_request_count: int = 0
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_estimated_cost: float = 0.0
    usage_priced_request_count: int = 0
    usage_daily_tokens: tuple[int, ...] = ()


def project_mcp_system_status(
    status: TuiSystemStatus,
    servers: tuple[McpServerStatus, ...],
) -> TuiSystemStatus:
    """Project one live MCP snapshot into the shared TUI system summary."""

    connected_count = 0
    tool_count = 0
    connecting_count = 0
    failed_count = 0
    for server in servers:
        if server.status == "connected":
            connected_count += 1
            tool_count += server.tool_count
        elif server.status in {"starting", "reconnecting"}:
            connecting_count += 1
        elif server.status == "failed":
            failed_count += 1
    return replace(
        status,
        mcp_connected_count=connected_count,
        mcp_total_count=len(servers),
        mcp_tool_count=tool_count,
        mcp_connecting_count=connecting_count,
        mcp_failed_count=failed_count,
    )


_PLUGIN_ISSUE_STATUSES = frozenset(
    {
        "invalid",
        "incompatible",
        "needs_configuration",
        "degraded",
        "blocked",
        "failed",
    }
)
_CHILD_CONTROL_EVENT_TYPES = frozenset(
    {
        "approval_requested",
        "approval_resolved",
        "ask_user_requested",
        "ask_user_resolved",
        "run_completed",
        "run_aborted",
        "run_failed",
        "run_paused",
    }
)


def project_plugin_system_status(
    status: TuiSystemStatus,
    plugins: tuple[PluginSummary, ...],
) -> TuiSystemStatus:
    """Project the canonical plugin catalog into the shared TUI summary."""

    active_names: list[str] = []
    issue_count = 0
    for plugin in plugins:
        if plugin.status in {"active", "ready"}:
            active_names.append(plugin.name)
        if plugin.status in _PLUGIN_ISSUE_STATUSES:
            issue_count += 1
    return replace(
        status,
        plugin_active_count=len(active_names),
        plugin_total_count=len(plugins),
        plugin_issue_count=issue_count,
        plugin_active_names=tuple(sorted(active_names)),
    )


@dataclass(frozen=True)
class TuiProviderConfiguration:
    """Supplier-level configuration intent collected by the TUI."""

    provider_id: str
    provider_type: ProviderType
    base_url: str | None
    api_path: str | None
    api_key: str | None
    api_key_env: str | None
    timeout: float
    provider_options: dict[str, object] | None = None
    enabled: bool = True
    make_default: bool = False


@dataclass(frozen=True)
class TuiEndpointConfiguration:
    """One provider-owned callable endpoint collected by the TUI."""

    provider_id: str
    endpoint_id: str
    model_id: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = False
    reasoning: EndpointReasoningConfig | None = None
    request_options: dict[str, object] | None = None


@dataclass(frozen=True)
class TuiToolConfiguration:
    name: str
    display_name: str
    source: ToolSource | Literal["unavailable"]
    effect: ToolEffect
    default_permission: PermissionMode | None
    plugin_id: str | None
    plugin_name: str | None
    plugin_permission_override: PermissionMode | None
    permission_override: PermissionMode | None
    permission_control: ToolPermissionControl
    available: bool = True

    @property
    def inherited_permission(self) -> PermissionMode | None:
        if self.permission_control == "tool":
            return self.default_permission
        return self.plugin_permission_override or self.default_permission

    @property
    def effective_permission(self) -> PermissionMode | None:
        if self.permission_control == "tool":
            return self.default_permission
        return self.permission_override or self.inherited_permission


@dataclass(frozen=True)
class TuiToolExecutionConfiguration:
    mode: ToolExecutionMode
    max_concurrency: int
    workspace_only: bool
    max_output_chars: int


@dataclass(frozen=True)
class TuiAgentRuntimeConfiguration:
    expected_revision: int
    allowed_agent_id_patterns: tuple[str, ...]
    allowed_model_patterns: tuple[str, ...]
    allowed_execution_modes: tuple[ExecutionMode, ...]
    allow_write: bool
    allow_network: bool
    allow_mcp: bool
    allow_external_paths: bool


@dataclass(frozen=True)
class TuiAgentDefinitionConfiguration:
    agent_id: str
    name: str
    description: str
    system_prompt: str
    tool_selection: ToolSelection
    model: str | None


@dataclass(frozen=True)
class TuiMCPServerConfiguration:
    server_id: str
    name: str
    transport: MCPTransportInput
    target: str | None
    args: list[str] | None
    env: dict[str, str] | None
    inherit_env: list[str] | None
    cwd: str | None
    headers: dict[str, str] | None
    credential_refs: dict[str, str] | None
    enabled: bool
    auto_start: bool
    auto_reconnect: bool
    max_reconnect_attempts: int
    reconnect_backoff_seconds: float
    timeout_seconds: float
    health_check_interval_seconds: float
    permission_mode: PermissionMode
    tool_effect_overrides: dict[str, ToolEffect] | None


@dataclass(frozen=True)
class TuiSkillInstallConfiguration:
    source: str
    scope: SkillInstallScopeInput
    selector: str | None = None
    install_all: bool = False
    force: bool = False
    ref: str | None = None


@dataclass(frozen=True)
class TuiPluginInstallConfiguration:
    source: str
    ref: str | None = None
    subdirectory: str | None = None


@dataclass(frozen=True)
class TuiSkillInvocationOption:
    name: str
    description: str
    argument_hint: str | None = None


@dataclass(frozen=True)
class TuiCompactionConfiguration:
    strategy_id: str
    fallback_strategy_id: str | None
    auto_enabled: bool
    trigger_ratio: float
    target_ratio: float
    model_key: str | None
    temperature: float | None
    max_output_tokens: int | None
    reasoning: ReasoningRequest | None


@dataclass(frozen=True)
class TuiSessionControlStatus:
    controller_id: str
    label: str


@dataclass(frozen=True)
class TuiPluginCommandExecution:
    execution: PluginCommandExecutionView
    run_results: tuple[RunResult, ...] = ()
    session_controls: tuple[TuiSessionControlStatus, ...] = ()


@dataclass(frozen=True)
class TuiBootstrap:
    models: tuple[AvailableModel, ...]
    current_model: AvailableModel
    sessions: tuple[SessionCatalogEntry, ...]
    startup_report: ApplicationStartupReport
    mcp_servers: tuple[McpServerStatus, ...] = ()
    system_status: TuiSystemStatus = TuiSystemStatus()


@dataclass(frozen=True)
class LoadedSession:
    detail: SessionDetail
    messages: tuple[MessageRecord, ...]
    events: tuple[EventRecord, ...]
    runs: tuple[RunRecord, ...] = ()
    context_status: SessionContextStatus | None = None
    effective_context_metrics: ContextMetrics | None = None
    compactions: tuple[CompactionRecord, ...] = ()
    queued_inputs: tuple[QueuedUserInputView, ...] = ()
    session_controls: tuple[TuiSessionControlStatus, ...] = ()
    event_high_water: int = 0


@dataclass(frozen=True)
class TuiSessionObservation:
    loaded: LoadedSession
    subscription: RuntimeSubscription


class TuiController:
    """Translate TUI intents to the public LocalApplication boundary."""

    def __init__(
        self,
        publish_event: EventPublisher,
        publish_lifecycle: LifecyclePublisher,
        *,
        home: str | Path | None = None,
        system_prompt: str | None = None,
        session_backend: SessionBackend | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
        application: LocalApplication | None = None,
        configuration: ConfigurationFacade | None = None,
        close_application: bool = True,
    ) -> None:
        self._publish_event = publish_event
        self._publish_lifecycle = publish_lifecycle
        self._home = home
        self._system_prompt = system_prompt
        self._session_backend = session_backend
        self._default_approval_mode = approval_mode
        self._default_interaction_mode = interaction_mode
        self._approval_mode = approval_mode
        self._reasoning_request = ReasoningRequest()
        self._interaction_mode = interaction_mode
        self._application = application
        self._configuration = configuration
        self._close_application = close_application
        self._started = False
        self._session_id: str | None = None
        self._session_archived = False
        self._initial_model: str | None = None
        self._draft_workspace: str | None = None
        self._creating_session = False
        self._event_root_sessions: dict[str, str] = {}
        self._event_sink_registration: RuntimeEventSinkRegistration | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def draft_workspace(self) -> str | None:
        """Return the workspace staged for the next Session."""

        return self._draft_workspace

    async def start(self) -> TuiBootstrap:
        if self._started:
            raise RuntimeError("TUI controller is already started")
        if self._application is None:
            self._application = await asyncio.to_thread(
                LocalApplication.build,
                self._home,
                system_prompt=self._system_prompt,
                session_backend=self._session_backend,
            )
        startup_configs = self._application.mcp.startup_configs
        if startup_configs:
            self._publish_lifecycle(
                TuiLifecycleStatus(
                    activity="mcp_connecting",
                    detail="",
                )
            )
        registration = self._application.runtime.add_event_sink(self._receive_event)
        try:
            await self._application.start()
        except Exception:
            await registration.close()
            raise
        self._event_sink_registration = registration
        self._started = True
        runtime = self._application.runtime
        self._draft_workspace = runtime.default_workspace
        if self._configuration is None and runtime.environment.home is not None:
            self._configuration = ConfigurationFacade(runtime.environment.home)
        models = tuple(runtime.available_models())
        usage = await self.usage_summary()
        mcp_servers = self.mcp_servers()
        return TuiBootstrap(
            models=models,
            current_model=runtime.current_model(),
            sessions=(),
            startup_report=self._application.startup_report,
            mcp_servers=mcp_servers,
            system_status=self._project_system_status(models, usage, mcp_servers),
        )

    async def system_status(self) -> TuiSystemStatus:
        """Project current process-owned resources for a newly opened Host view."""

        models, usage = await asyncio.gather(self.models(), self.usage_summary())
        return self._project_system_status(models, usage, self.mcp_servers())

    def _project_system_status(
        self,
        models: tuple[AvailableModel, ...],
        usage: UsageSummary,
        mcp_servers: tuple[McpServerStatus, ...],
    ) -> TuiSystemStatus:
        runtime = self._runtime()
        assert self._application is not None
        skills = self._application.available_skills()
        plugins = (
            tuple(self._application.plugins.list()) if self._application.plugins is not None else ()
        )
        totals = usage.totals
        return project_plugin_system_status(
            project_mcp_system_status(
                TuiSystemStatus(
                    workspace=self._draft_workspace or runtime.default_workspace,
                    provider_ids=tuple(sorted({model.provider_id for model in models})),
                    model_count=len(models),
                    skill_count=len(skills),
                    skill_names=tuple(sorted(item.name for item in skills)),
                    usage_request_count=totals.request_count,
                    usage_input_tokens=totals.input_tokens,
                    usage_output_tokens=totals.output_tokens,
                    usage_estimated_cost=totals.estimated_cost,
                    usage_priced_request_count=totals.priced_request_count,
                    usage_daily_tokens=tuple(
                        group.totals.total_tokens for group in usage.groups[-10:]
                    ),
                ),
                mcp_servers,
            ),
            plugins,
        )

    async def close(self) -> None:
        application = self._application
        registration = self._event_sink_registration
        self._event_sink_registration = None
        if registration is not None:
            await registration.close()
        self._started = False
        if application is not None and self._close_application:
            await application.close()

    async def submit(
        self,
        prompt: str,
        *,
        queue_mode: QueuedInputMode = "follow_up",
        skill_invocation: SkillInvocationRequest | None = None,
    ) -> RunResult:
        runtime = self._runtime()
        is_new_session = self._session_id is None
        if is_new_session:
            self._creating_session = True
        try:
            result = await runtime.run(
                prompt,
                skill_invocation=skill_invocation,
                session_id=self._session_id,
                queue_mode=queue_mode,
                initial_model=self._initial_model if is_new_session else None,
                initial_reasoning_request=(self._reasoning_request if is_new_session else None),
                approval_mode=self._approval_mode if is_new_session else None,
                interaction_mode=self._interaction_mode if is_new_session else None,
                workspace=self._draft_workspace if is_new_session else None,
            )
            self._session_id = result.session.session_id
            self._initial_model = None
            await self._unarchive_current_session_after_update()
            return result
        finally:
            if is_new_session:
                self._creating_session = False

    def skill_invocation_options(self) -> tuple[TuiSkillInvocationOption, ...]:
        """Project currently invocable Runtime skills for the inline command menu."""

        return tuple(
            TuiSkillInvocationOption(
                name=skill.name,
                description=skill.description,
                argument_hint=skill.argument_hint,
            )
            for skill in self._runtime().available_skills()
            if skill.enabled and skill.user_invocable
        )

    def runtime_skill_names(self) -> tuple[str, ...]:
        return tuple(sorted(skill.name for skill in self._runtime().available_skills()))

    async def abort(self) -> RunResult | None:
        if self._session_id is None:
            return None
        return await self._runtime().abort_run(session_id=self._session_id)

    async def resolve_approval(
        self,
        *,
        interaction: PendingInteraction,
        approved: bool,
        reason: str = "User rejected",
    ) -> RunResult | None:
        if interaction.request_id is None:
            raise ValueError("approval request is missing its canonical identity")
        if interaction.delegation_id is not None:
            agents = self._agents()
            if interaction.session_id is None:
                raise ValueError("subagent approval is missing its canonical identity")
            await agents.resolve_agent_approval(
                interaction.delegation_id,
                child_session_id=interaction.session_id,
                child_run_id=interaction.run_id,
                tool_call_id=interaction.tool_call_id,
                approval_id=interaction.request_id,
                approved=approved,
                reason=reason,
            )
            return None
        return await self._runtime().approve_tool(
            session_id=self._require_session(),
            run_id=interaction.run_id,
            approval_id=interaction.request_id,
            approved=approved,
            reason=reason,
        )

    async def answer(
        self,
        *,
        interaction: PendingInteraction,
        answer: JsonValue,
    ) -> RunResult | None:
        if interaction.delegation_id is not None:
            agents = self._agents()
            if interaction.session_id is None or interaction.request_id is None:
                raise ValueError("subagent input request is missing its canonical identity")
            await agents.resolve_agent_input(
                interaction.delegation_id,
                child_session_id=interaction.session_id,
                child_run_id=interaction.run_id,
                tool_call_id=interaction.tool_call_id,
                request_id=interaction.request_id,
                answer=answer,
            )
            return None
        return await self._runtime().answer_user(
            session_id=self._require_session(),
            run_id=interaction.run_id,
            answer=answer,
        )

    async def agent_interactions(self) -> tuple[AgentInteractionView, ...]:
        session_id = self._session_id
        if session_id is None or self._application is None:
            return ()
        if self._application.agents is None:
            return ()
        interactions = await asyncio.to_thread(self._read_agent_interactions, session_id)
        return interactions if self._session_id == session_id else ()

    def _read_agent_interactions(self, session_id: str) -> tuple[AgentInteractionView, ...]:
        assert self._application is not None and self._application.agents is not None
        session = self._runtime().sessions.open(session_id, recover=False).session
        return self._application.agents.list_agent_interactions(session.agent_root_session_id)

    async def new_session(self) -> AvailableModel:
        """Reset the visible Session and return the next Session's model.

        A new Session keeps the most recently used route so the welcome page
        label and the first request agree. Leaving no Session preserves an
        explicit welcome-page selection; a route that no longer resolves falls
        back to the Runtime default.
        """

        runtime = self._runtime()
        previous_route = await self._leaving_session_route()
        self._session_id = None
        self._session_archived = False
        self._draft_workspace = runtime.default_workspace
        if previous_route is not None:
            self._initial_model = previous_route
        self._approval_mode = self._default_approval_mode
        self._interaction_mode = self._default_interaction_mode
        self._reasoning_request = ReasoningRequest()

        initial_model = self._initial_model
        draft: AvailableModel | None = None
        if initial_model is not None:
            try:
                draft = await asyncio.to_thread(runtime.resolve_model, initial_model)
            except ConfigError:
                draft = None
        if draft is None:
            draft = await asyncio.to_thread(runtime.current_model)
            self._initial_model = None
        return draft

    async def set_workspace(self, value: str) -> str:
        """Stage one existing directory for the next Session only."""

        if self._session_id is not None or self._creating_session:
            raise ValueError("Workspace can only be changed before creating a session")
        normalized = value.strip()
        if not normalized:
            raise ValueError("Workspace path must not be empty")

        def resolve() -> str:
            candidate = Path(normalized).expanduser()
            try:
                workspace = candidate.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"Workspace does not exist: {candidate}") from exc
            if not workspace.is_dir():
                raise ValueError(f"Workspace is not a directory: {workspace}")
            return str(workspace)

        workspace = await asyncio.to_thread(resolve)
        self._draft_workspace = workspace
        return workspace

    async def _leaving_session_route(self) -> str | None:
        session_id = self._session_id
        if session_id is None:
            return None
        runtime = self._runtime()

        def read() -> str | None:
            try:
                return runtime.current_model(session_id).key
            except (ConfigError, PersistenceError, FileNotFoundError):
                # Inheriting a route is optional. An invalid route or unreadable
                # previous Session must not prevent navigation to a fresh draft.
                return None

        return await asyncio.to_thread(read)

    async def sessions(self) -> tuple[SessionCatalogEntry, ...]:
        runtime = self._runtime()

        def primary_catalog() -> tuple[SessionCatalogEntry, ...]:
            """Project the durable catalog onto user-managed primary Sessions."""

            entries = runtime.sessions.catalog().entries

            def is_primary(entry: SessionCatalogEntry) -> bool:
                if entry.session_kind is not None:
                    return entry.session_kind == "primary"
                try:
                    session = runtime.store.load_session_record(entry.session_id)
                except (FileNotFoundError, PersistenceError):
                    # Preserve unreadable legacy catalog entries as manageable.
                    return True
                return session.session_kind == "primary"

            return tuple(entry for entry in entries if is_primary(entry))

        return await asyncio.to_thread(primary_catalog)

    async def set_session_archived(self, session_id: str, *, archived: bool) -> None:
        sessions = self._runtime().sessions
        await asyncio.to_thread(sessions.set_archived, session_id, archived)
        if session_id == self._session_id:
            self._session_archived = archived

    async def rename_session(self, session_id: str, title: str) -> SessionView:
        sessions = self._runtime().sessions
        result = await asyncio.to_thread(sessions.rename, session_id, title)
        return result.session

    async def delete_archived_sessions(
        self,
        session_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        removed = await asyncio.to_thread(self._delete_archived_sessions, session_ids)
        if self._session_id in removed:
            self._session_id = None
            self._session_archived = False
        return removed

    def _delete_archived_sessions(self, session_ids: tuple[str, ...]) -> tuple[str, ...]:
        sessions = self._runtime().sessions
        catalog = {entry.session_id: entry for entry in sessions.catalog().entries}
        selected = tuple(dict.fromkeys(session_ids))
        not_archived = tuple(
            session_id
            for session_id in selected
            if (entry := catalog.get(session_id)) is not None and not entry.archived
        )
        if not_archived:
            raise ValueError("only archived sessions can be deleted: " + ", ".join(not_archived))
        remaining = set(catalog)
        removed: list[str] = []
        for session_id in selected:
            if session_id not in remaining:
                continue
            result = sessions.trash(session_id)
            removed.extend(result.session_ids)
            remaining.difference_update(result.session_ids)
        return tuple(dict.fromkeys(removed))

    async def branch_points(self) -> tuple[MessageRecord, ...]:
        """Return user requests that can become an editable branch draft."""

        session_id = self._require_session()
        return await asyncio.to_thread(
            lambda: tuple(
                message
                for message in self._session_messages(session_id)
                if message.role == "user" and not message.scaffold
            )
        )

    async def branch_session(self, message_id: str | None = None) -> SessionBranchResult:
        session_id = self._require_session()
        if message_id is None:
            return await asyncio.to_thread(self._branch_current_session, session_id)
        return await asyncio.to_thread(
            self._branch_at_user_message,
            session_id,
            message_id,
        )

    def _branch_current_session(self, session_id: str) -> SessionBranchResult:
        session = self._runtime().store.load_session_record(session_id)
        return self._runtime().sessions.branch(session_id, session.last_message_id)

    def _branch_at_user_message(
        self,
        session_id: str,
        message_id: str,
    ) -> SessionBranchResult:
        messages = self._session_messages(session_id)
        selected_index = next(
            (
                index
                for index, message in enumerate(messages)
                if (
                    message.message_id == message_id
                    and message.role == "user"
                    and not message.scaffold
                )
            ),
            None,
        )
        if selected_index is None:
            raise ValueError(f"user message is not a fork point in this session: {message_id}")
        boundary_message_id = (
            messages[selected_index - 1].message_id if selected_index > 0 else None
        )
        return self._runtime().sessions.branch(session_id, boundary_message_id)

    async def queued_user_inputs(
        self,
        session_id: str | None = None,
    ) -> tuple[QueuedUserInputView, ...]:
        target_session_id = session_id or self._require_session()
        return await asyncio.to_thread(self._queued_user_inputs, target_session_id)

    async def cancel_queued_user_input(
        self,
        queue_id: str,
    ) -> QueuedUserInputView | None:
        session_id = self._require_session()
        return await self._runtime().cancel_pending_user_input(session_id, queue_id)

    def _queued_user_inputs(self, session_id: str) -> tuple[QueuedUserInputView, ...]:
        target_session_id = session_id
        return self._runtime().pending_user_inputs(target_session_id)

    async def message(self, session_id: str, message_id: str) -> MessageRecord:
        runtime = self._runtime()
        return await asyncio.to_thread(runtime.sessions.message, session_id, message_id)

    async def models(self) -> tuple[AvailableModel, ...]:
        runtime = self._runtime()
        return await asyncio.to_thread(lambda: tuple(runtime.available_models()))

    async def provider_catalog(self) -> ProviderCatalogView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.provider_catalog)

    async def provider_templates(self) -> tuple[ProviderTemplateView, ...]:
        configuration = self._require_configuration()
        catalog = await asyncio.to_thread(configuration.provider_template_catalog)
        return tuple(catalog.templates)

    async def add_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        return await asyncio.to_thread(self._add_provider, draft)

    def _add_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        configuration = self._require_configuration()
        catalog = configuration.add_provider(
            draft.provider_id,
            provider_type=draft.provider_type,
            base_url=draft.base_url,
            api_path=draft.api_path,
            api_key=draft.api_key,
            api_key_env=draft.api_key_env,
            timeout=draft.timeout,
            provider_options=draft.provider_options,
            enabled=draft.enabled,
            make_default=draft.make_default,
        )
        return next(
            provider for provider in catalog.providers if provider.provider_id == draft.provider_id
        )

    async def update_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        return await asyncio.to_thread(self._update_provider, draft)

    async def update_provider_template(self, provider_id: str) -> ProviderCatalogView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.update_provider, provider_id)

    def _update_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        configuration = self._require_configuration()
        current = configuration.provider(draft.provider_id)
        if current.provider_type != draft.provider_type:
            raise ConfigError(
                "provider type cannot be changed by editing settings: "
                f"{current.provider_type} != {draft.provider_type}"
            )
        catalog = configuration.update_provider_settings(
            draft.provider_id,
            base_url=draft.base_url,
            api_path=draft.api_path,
            api_key=draft.api_key,
            api_key_env=draft.api_key_env,
            timeout=draft.timeout,
            provider_options=draft.provider_options,
            enabled=draft.enabled,
            make_default=draft.make_default,
        )
        return next(
            provider for provider in catalog.providers if provider.provider_id == draft.provider_id
        )

    async def set_provider_enabled(self, provider_id: str, *, enabled: bool) -> ProviderView:
        configuration = self._require_configuration()

        def update() -> ProviderView:
            catalog = configuration.update_provider_settings(provider_id, enabled=enabled)
            return next(item for item in catalog.providers if item.provider_id == provider_id)

        return await asyncio.to_thread(update)

    async def use_provider(self, provider_id: str) -> ProviderView:
        configuration = self._require_configuration()

        def use() -> ProviderView:
            catalog = configuration.use_provider(provider_id)
            return next(item for item in catalog.providers if item.provider_id == provider_id)

        return await asyncio.to_thread(use)

    async def remove_provider(self, provider_id: str) -> None:
        configuration = self._require_configuration()
        await asyncio.to_thread(configuration.remove_provider, provider_id)

    async def provider_endpoints(self, provider_id: str) -> ProviderEndpointCatalogView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.endpoint_catalog, provider_id)

    async def add_endpoint(
        self,
        draft: TuiEndpointConfiguration,
    ) -> ProviderEndpointView:
        return await asyncio.to_thread(self._save_endpoint, draft, True)

    async def update_endpoint(
        self,
        draft: TuiEndpointConfiguration,
    ) -> ProviderEndpointView:
        return await asyncio.to_thread(self._save_endpoint, draft, False)

    def _save_endpoint(
        self,
        draft: TuiEndpointConfiguration,
        create: bool,
    ) -> ProviderEndpointView:
        configuration = self._require_configuration()
        endpoints = configuration.endpoint_catalog(draft.provider_id).endpoints
        exists = any(endpoint.endpoint_id == draft.endpoint_id for endpoint in endpoints)
        if create and exists:
            raise ConfigError(
                f"provider endpoint already exists: {draft.provider_id}/{draft.endpoint_id}"
            )
        if not create and not exists:
            raise ConfigError(
                f"provider endpoint not found: {draft.provider_id}/{draft.endpoint_id}"
            )
        catalog = configuration.save_endpoint(
            draft.provider_id,
            draft.endpoint_id,
            model=draft.model_id,
            context_window=draft.context_window,
            max_output_tokens=draft.max_output_tokens,
            supports_tools=draft.supports_tools,
            supports_vision=draft.supports_vision,
            supports_streaming=draft.supports_streaming,
            supports_structured_output=draft.supports_structured_output,
            reasoning=draft.reasoning,
            request_options=draft.request_options,
        )
        return next(
            endpoint for endpoint in catalog.endpoints if endpoint.endpoint_id == draft.endpoint_id
        )

    async def use_endpoint(self, provider_id: str, endpoint_id: str) -> ProviderEndpointView:
        configuration = self._require_configuration()

        def use() -> ProviderEndpointView:
            catalog = configuration.use_endpoint(provider_id, endpoint_id)
            return next(item for item in catalog.endpoints if item.endpoint_id == endpoint_id)

        return await asyncio.to_thread(use)

    async def remove_endpoint(self, provider_id: str, endpoint_id: str) -> None:
        configuration = self._require_configuration()
        await asyncio.to_thread(configuration.remove_endpoint, provider_id, endpoint_id)

    async def tool_configurations(self) -> tuple[TuiToolConfiguration, ...]:
        """Combine live tool contracts with durable permission overrides."""

        return await asyncio.to_thread(self._tool_configurations)

    def _tool_configurations(self) -> tuple[TuiToolConfiguration, ...]:
        definitions = {item.name: item for item in self._runtime().available_tool_definitions()}
        permissions = self._require_configuration().permission_config()
        configured_overrides = permissions.tools
        runtime_overrides = self._runtime().tool_permission_overrides()
        plugin_overrides = self._runtime().plugin_permission_overrides()
        plugin_names = {
            item.plugin_id: item.name
            for item in (
                self._application.plugins.list()
                if self._application is not None and self._application.plugins is not None
                else ()
            )
        }
        names = sorted(set(definitions).union(configured_overrides))
        items: list[TuiToolConfiguration] = []
        for name in names:
            definition = definitions.get(name)
            if definition is None:
                items.append(
                    TuiToolConfiguration(
                        name=name,
                        display_name=name,
                        source="unavailable",
                        effect="unknown",
                        default_permission=None,
                        plugin_id=None,
                        plugin_name=None,
                        plugin_permission_override=None,
                        permission_override=configured_overrides.get(name),
                        permission_control="host",
                        available=False,
                    )
                )
                continue
            items.append(
                TuiToolConfiguration(
                    name=name,
                    display_name=definition.display_name or name,
                    source=definition.source,
                    effect=definition.effect,
                    default_permission=definition.permission_mode,
                    plugin_id=(plugin_id := _tool_plugin_id(definition.metadata)),
                    plugin_name=plugin_names.get(plugin_id) if plugin_id is not None else None,
                    plugin_permission_override=(
                        plugin_overrides.get(plugin_id) if plugin_id is not None else None
                    ),
                    permission_override=runtime_overrides.get(name),
                    permission_control=definition.permission_control,
                )
            )
        return tuple(items)

    async def set_tool_permission(
        self,
        tool_name: str,
        mode: PermissionMode | None,
    ) -> TuiToolConfiguration | None:
        await asyncio.to_thread(self._persist_tool_permission, tool_name, mode)
        overrides = await asyncio.to_thread(self._require_configuration().permission_config)
        self._runtime().replace_tool_permission_overrides(overrides.tools, overrides.plugins)
        return next(
            (item for item in self._tool_configurations() if item.name == tool_name),
            None,
        )

    def _persist_tool_permission(
        self,
        tool_name: str,
        mode: PermissionMode | None,
    ) -> None:
        current = next(
            (item for item in self._tool_configurations() if item.name == tool_name),
            None,
        )
        if current is None:
            raise ConfigError(f"tool not found: {tool_name}")
        if current.permission_control == "tool":
            raise ConfigError(f"tool permission is controlled by its contract: {tool_name}")
        configuration = self._require_configuration()
        if mode is None:
            configuration.clear_tool_permission(tool_name)
        else:
            configuration.set_tool_permission(tool_name, mode)

    async def tool_execution_configuration(self) -> ToolExecutionConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.tool_execution_config)

    async def update_tool_execution(
        self,
        draft: TuiToolExecutionConfiguration,
    ) -> ToolExecutionConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(
            configuration.update_tool_execution,
            mode=draft.mode,
            max_concurrency=draft.max_concurrency,
            workspace_only=draft.workspace_only,
            max_output_chars=draft.max_output_chars,
        )

    async def agent_runtime_configuration(self) -> AgentRuntimeConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.agent_runtime_config)

    async def update_agent_runtime(
        self,
        draft: TuiAgentRuntimeConfiguration,
    ) -> AgentRuntimeConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(
            configuration.update_agent_runtime,
            expected_revision=draft.expected_revision,
            allowed_agent_id_patterns=draft.allowed_agent_id_patterns,
            allowed_model_patterns=draft.allowed_model_patterns,
            allowed_execution_modes=draft.allowed_execution_modes,
            allow_write=draft.allow_write,
            allow_network=draft.allow_network,
            allow_mcp=draft.allow_mcp,
            allow_external_paths=draft.allow_external_paths,
        )

    async def agent_definition_catalog(self) -> AgentDefinitionCatalogView:
        return await asyncio.to_thread(self._agent_definition_catalog)

    def _agent_definition_catalog(self) -> AgentDefinitionCatalogView:
        self._runtime()
        assert self._application is not None
        with self._application.agent_definition_services() as services:
            return services.definitions.list(tools=services.tools)

    async def agent_definition(
        self,
        agent_id: str,
    ) -> AgentDefinitionDetail:
        return await asyncio.to_thread(self._agent_definition, agent_id)

    def _agent_definition(self, agent_id: str) -> AgentDefinitionDetail:
        self._runtime()
        assert self._application is not None
        with self._application.agent_definition_services() as services:
            return services.definitions.get(agent_id, tools=services.tools)

    async def agent_definition_tools(self) -> tuple[AgentToolOptionView, ...]:
        return await asyncio.to_thread(self._agent_definition_tools)

    def _agent_definition_tools(self) -> tuple[AgentToolOptionView, ...]:
        self._runtime()
        assert self._application is not None
        with self._application.agent_definition_services() as services:
            return services.definitions.tool_options(services.tools)

    async def create_agent_definition(
        self,
        draft: TuiAgentDefinitionConfiguration,
        *,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        return await asyncio.to_thread(
            self._save_agent_definition,
            draft,
            expected_revision,
            True,
        )

    async def update_agent_definition(
        self,
        draft: TuiAgentDefinitionConfiguration,
        *,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        return await asyncio.to_thread(
            self._save_agent_definition,
            draft,
            expected_revision,
            False,
        )

    def _save_agent_definition(
        self,
        draft: TuiAgentDefinitionConfiguration,
        expected_revision: int,
        create: bool,
    ) -> AgentDefinitionCatalogView:
        self._runtime()
        assert self._application is not None
        with self._application.agent_definition_services() as services:
            operation = services.definitions.create if create else services.definitions.update
            return operation(
                agent_id=draft.agent_id,
                name=draft.name,
                description=draft.description,
                system_prompt=draft.system_prompt,
                tool_selection=draft.tool_selection,
                model=draft.model,
                tools=services.tools,
                expected_revision=expected_revision,
            )

    async def delete_agent_definition(
        self,
        agent_id: str,
        *,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        return await asyncio.to_thread(
            self._delete_agent_definition,
            agent_id,
            expected_revision,
        )

    def _delete_agent_definition(
        self,
        agent_id: str,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        self._runtime()
        assert self._application is not None
        with self._application.agent_definition_services() as services:
            return services.definitions.delete(
                agent_id,
                expected_revision=expected_revision,
                tools=services.tools,
            )

    async def mcp_configurations(self) -> tuple[MCPServerConfigView, ...]:
        configuration = self._require_configuration()
        return await asyncio.to_thread(lambda: tuple(configuration.mcp_catalog().servers))

    async def mcp_configuration(self, server_id: str) -> MCPServerConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.mcp_server, server_id)

    async def set_mcp_enabled(
        self,
        server_id: str,
        *,
        enabled: bool,
    ) -> MCPServerConfigView:
        configuration = self._require_configuration()
        previous = await asyncio.to_thread(configuration.mcp_server, server_id)
        if previous.enabled == enabled:
            return previous

        def update() -> tuple[MCPServerConfigView, MCPServerConfig]:
            catalog = configuration.save_mcp_server(
                server_id,
                MCPServerConfigInput(enabled=enabled),
            )
            saved = next(item for item in catalog.servers if item.server_id == server_id)
            config = configuration.mcp.get(server_id)
            assert config is not None
            return saved, config

        saved, config = await asyncio.to_thread(update)
        self._runtime()
        assert self._application is not None
        try:
            await self._application.reconcile_mcp_configuration(config)
        except Exception:
            await asyncio.to_thread(
                configuration.save_mcp_server,
                server_id,
                MCPServerConfigInput(enabled=previous.enabled),
            )
            restored = configuration.mcp.get(server_id)
            assert restored is not None
            await self._application.reconcile_mcp_configuration(restored)
            raise
        return saved

    async def save_mcp_configuration(
        self,
        draft: TuiMCPServerConfiguration,
    ) -> MCPServerConfigView:
        configuration = self._require_configuration()
        previous = await asyncio.to_thread(configuration.mcp_server, draft.server_id)
        saved, config = await asyncio.to_thread(self._save_mcp_configuration, draft)
        self._runtime()
        assert self._application is not None
        try:
            await self._application.reconcile_mcp_configuration(config)
        except Exception:
            await asyncio.to_thread(
                configuration.save_mcp_server,
                draft.server_id,
                MCPServerConfigInput.model_validate(previous.model_dump(mode="python")),
            )
            restored = configuration.mcp.get(draft.server_id)
            assert restored is not None
            await self._application.reconcile_mcp_configuration(restored)
            raise
        return saved

    def _save_mcp_configuration(
        self,
        draft: TuiMCPServerConfiguration,
    ) -> tuple[MCPServerConfigView, MCPServerConfig]:
        payload: dict[str, object] = {
            "name": draft.name,
            "transport": draft.transport,
            "enabled": draft.enabled,
            "auto_start": draft.auto_start,
            "auto_reconnect": draft.auto_reconnect,
            "max_reconnect_attempts": draft.max_reconnect_attempts,
            "reconnect_backoff_seconds": draft.reconnect_backoff_seconds,
            "timeout_seconds": draft.timeout_seconds,
            "health_check_interval_seconds": draft.health_check_interval_seconds,
            "permission_mode": draft.permission_mode,
        }
        if draft.target is not None:
            payload["command" if draft.transport == "stdio" else "url"] = draft.target
        for field_name in (
            "args",
            "env",
            "inherit_env",
            "cwd",
            "headers",
            "credential_refs",
            "tool_effect_overrides",
        ):
            value = getattr(draft, field_name)
            if value is not None:
                payload[field_name] = value
        update = MCPServerConfigInput.model_validate(payload)
        configuration = self._require_configuration()
        catalog = configuration.save_mcp_server(draft.server_id, update)
        saved = next(item for item in catalog.servers if item.server_id == draft.server_id)
        config = configuration.mcp.get(draft.server_id)
        assert config is not None
        return saved, config

    async def remove_mcp_configuration(self, server_id: str) -> None:
        configuration = self._require_configuration()
        await asyncio.to_thread(configuration.remove_mcp_server, server_id)

    async def check_mcp_server(self, server_id: str) -> MCPConnectionCheckView:
        return await self._require_configuration().check_mcp_server(server_id)

    def mcp_servers(self) -> tuple[McpServerStatus, ...]:
        self._runtime()
        assert self._application is not None
        return tuple(self._mcp_status(record) for record in self._application.mcp_runtime_servers())

    async def start_mcp_server(self, server_id: str) -> McpServerStatus:
        self._runtime()
        assert self._application is not None
        return self._mcp_status(await self._application.start_mcp_server(server_id))

    async def stop_mcp_server(self, server_id: str) -> McpServerStatus:
        self._runtime()
        assert self._application is not None
        return self._mcp_status(await self._application.stop_mcp_server(server_id))

    async def restart_mcp_server(self, server_id: str) -> McpServerStatus:
        self._runtime()
        assert self._application is not None
        return self._mcp_status(await self._application.restart_mcp_server(server_id))

    async def refresh_mcp_tools(self, server_id: str) -> McpServerStatus:
        self._runtime()
        assert self._application is not None
        record = await self._application.refresh_mcp_server_tools(server_id)
        return self._mcp_status(record)

    async def skill_catalog(self) -> SkillCatalogView:
        configuration = self._require_configuration()
        configured = await asyncio.to_thread(
            configuration.skill_catalog,
            include_disabled=True,
        )
        self._runtime()
        assert self._application is not None
        effective = self._application.available_skills()
        skills = {skill.name: skill for skill in configured.skills}
        for metadata in effective:
            configured_skill = skills.get(metadata.name)
            skills[metadata.name] = SkillView(
                **metadata.model_dump(mode="python"),
                managed=(configured_skill.managed if configured_skill is not None else False),
                install=(configured_skill.install if configured_skill is not None else None),
            )
        return configured.model_copy(update={"skills": [skills[name] for name in sorted(skills)]})

    async def skill_policy(self) -> SkillPolicyView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.skill_policy)

    async def update_skill_discovery(
        self,
        *,
        enabled: bool | None = None,
        source_updates: Mapping[str, bool] | None = None,
    ) -> SkillPolicyView:
        configuration = self._require_configuration()
        updates = dict(source_updates or {})
        previous = await asyncio.to_thread(configuration.skill_policy)
        if enabled is None and not updates:
            return previous
        saved = await asyncio.to_thread(
            configuration.update_skill_discovery,
            enabled=enabled,
            source_updates=updates,
        )
        try:
            await self._refresh_runtime_skills(configuration)
        except Exception:
            previous_sources = {source.source_id: source.enabled for source in previous.sources}
            await asyncio.to_thread(
                configuration.update_skill_discovery,
                enabled=previous.enabled if enabled is not None else None,
                source_updates={source_id: previous_sources[source_id] for source_id in updates},
            )
            raise
        return saved

    async def install_skills(
        self,
        draft: TuiSkillInstallConfiguration,
    ) -> SkillManagementView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(
            configuration.install_skills,
            draft.source,
            scope=draft.scope,
            selector=draft.selector,
            install_all=draft.install_all,
            force=draft.force,
            ref=draft.ref,
        )

    async def update_skill(self, name: str) -> SkillManagementView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.update_skill, name)

    async def remove_skill(self, name: str) -> SkillManagementView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.remove_skill, name)

    async def set_skill_enabled(
        self,
        name: str,
        *,
        enabled: bool,
    ) -> SkillManagementView:
        plugin_skill = next(
            (
                skill
                for skill in self._runtime().available_skills()
                if skill.name == name and _plugin_skill_owner(skill) is not None
            ),
            None,
        )
        if plugin_skill is not None:
            raise ConfigError(f"plugin skill {name!r} is managed through its owning plugin")
        configuration = self._require_configuration()
        result = await asyncio.to_thread(configuration.set_skill_enabled, name, enabled=enabled)
        try:
            await self._refresh_runtime_skills(configuration)
        except Exception:
            await asyncio.to_thread(
                configuration.set_skill_enabled,
                name,
                enabled=not enabled,
            )
            raise
        return result

    async def _refresh_runtime_skills(self, configuration: ConfigurationFacade) -> None:
        catalog = await asyncio.to_thread(configuration.skill_catalog)
        skills = tuple(
            SkillMetadata.model_validate(
                skill.model_dump(mode="python", exclude={"managed", "install"})
            )
            for skill in catalog.skills
        )
        self._runtime()
        assert self._application is not None
        await self._application.refresh_skills(skills)

    async def validate_skill(self, name: str) -> SkillValidationView:
        return await asyncio.to_thread(self._validate_skill, name)

    def _validate_skill(self, name: str) -> SkillValidationView:
        configuration = self._require_configuration()
        skill = configuration.skill(name).skill
        return configuration.validate_skills(skill.location)

    async def compaction_configuration(self) -> CompactionConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.compaction_config)

    async def queued_input_configuration(self) -> QueuedInputConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.queued_input_config)

    async def update_queued_input_consumption(
        self,
        consumption_mode: QueuedInputConsumptionMode,
    ) -> QueuedInputConfigView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(
            configuration.update_queued_inputs,
            consumption_mode=consumption_mode,
        )

    def compaction_strategies(self) -> tuple[str, ...]:
        return tuple(item.strategy_id for item in self._runtime().compaction_strategies.list())

    async def save_compaction_configuration(
        self,
        draft: TuiCompactionConfiguration,
    ) -> CompactionConfigView:
        return await asyncio.to_thread(self._save_compaction_configuration, draft)

    def _save_compaction_configuration(
        self,
        draft: TuiCompactionConfiguration,
    ) -> CompactionConfigView:
        provider_id: str | None = None
        endpoint_id: str | None = None
        if draft.model_key is not None:
            provider_id, separator, endpoint_id = draft.model_key.partition("/")
            if not separator or not provider_id or not endpoint_id:
                raise ConfigError(f"invalid compaction model route: {draft.model_key}")
            model = self._runtime().resolve_model(draft.model_key)
            if (
                draft.max_output_tokens is not None
                and draft.max_output_tokens > model.max_output_tokens
            ):
                raise ConfigError(
                    "compaction max_output_tokens exceed the selected endpoint output limit"
                )
            if draft.reasoning is not None:
                try:
                    resolve_reasoning(model.reasoning, draft.reasoning)
                except ValueError as exc:
                    raise ConfigError(f"invalid compaction reasoning override: {exc}") from exc
        elif any(
            value is not None
            for value in (draft.temperature, draft.max_output_tokens, draft.reasoning)
        ):
            raise ConfigError(
                "compaction model request overrides require a dedicated model endpoint"
            )
        return self._require_configuration().save_compaction(
            CompactionConfigInput(
                strategy_id=draft.strategy_id,
                fallback_strategy_id=draft.fallback_strategy_id,
                auto_enabled=draft.auto_enabled,
                trigger_ratio=draft.trigger_ratio,
                target_ratio=draft.target_ratio,
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                temperature=draft.temperature,
                max_output_tokens=draft.max_output_tokens,
                reasoning=draft.reasoning,
            )
        )

    async def compact(self) -> CompactionCommandResult:
        return await self._runtime().compact(session_id=self._require_session())

    def plugins(self) -> tuple[PluginSummary, ...]:
        application = self._application
        if application is None or application.plugins is None:
            return ()
        return tuple(application.plugins.list())

    def plugin(self, plugin_id: str) -> PluginDetail:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return application.plugins.show(plugin_id)

    async def install_plugin(
        self,
        draft: TuiPluginInstallConfiguration,
    ) -> PluginActionResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        result = await asyncio.to_thread(
            application.plugins.install,
            draft.source,
            ref=draft.ref,
            subdirectory=draft.subdirectory,
        )
        if not isinstance(result, PluginActionResult):
            raise RuntimeError("plugin install unexpectedly returned a validation result")
        return result

    async def configure_plugin(
        self,
        plugin_id: str,
        values: dict[str, object],
        credential_refs: dict[str, str],
    ) -> PluginActionResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await application.plugins.configure(
            plugin_id,
            values,
            credential_refs=credential_refs,
        )

    async def enable_plugin(self, plugin_id: str) -> PluginActionResult:
        return await self._plugin_operation("enable", plugin_id)

    async def disable_plugin(self, plugin_id: str) -> PluginActionResult:
        return await self._plugin_operation("disable", plugin_id)

    async def restart_plugin(self, plugin_id: str) -> PluginActionResult:
        return await self._plugin_operation("restart", plugin_id)

    async def check_plugin(self, plugin_id: str) -> PluginActionResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await asyncio.to_thread(application.plugins.check, plugin_id)

    async def select_plugin_install(
        self,
        plugin_id: str,
        package_digest: str,
    ) -> PluginActionResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await application.plugins.select(plugin_id, package_digest)

    async def remove_plugin(self, plugin_id: str) -> PluginActionResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await application.plugins.remove(plugin_id)

    async def _plugin_operation(self, action: str, plugin_id: str) -> PluginActionResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        operation = getattr(application.plugins, action)
        return await operation(plugin_id)

    def plugin_commands(self) -> tuple[PluginCommandView, ...]:
        application = self._application
        if application is None or application.plugins is None:
            return ()
        return tuple(application.plugins.list_commands())

    async def execute_plugin_command(
        self,
        selector: str,
        argument: str,
    ) -> TuiPluginCommandExecution | None:
        application = self._application
        if application is None or application.plugins is None:
            return None
        commands = self.plugin_commands()
        matches = (
            tuple(item for item in commands if item.command_id == selector)
            if ":" in selector
            else tuple(item for item in commands if item.name == selector)
        )
        if not matches:
            return None
        if len(matches) > 1:
            choices = ", ".join(f"/{item.command_id}" for item in matches)
            raise ValueError(f"Ambiguous plugin command /{selector}; use one of: {choices}")
        selected = matches[0]
        runtime = self._runtime()
        command_workspace = self._draft_workspace or runtime.default_workspace
        if self._session_id is None and selected.requires_session:
            created = runtime.create_session(
                title=_plugin_session_title(selected.name, argument),
                initial_model=self._initial_model,
                reasoning_request=self._reasoning_request,
                approval_mode=self._approval_mode,
                interaction_mode=self._interaction_mode,
                workspace=command_workspace,
            )
            self._session_id = created.session_id
            self._initial_model = None
        execution = await application.plugins.execute_command(
            selected.command_id,
            argument,
            session_id=self._session_id,
            workspace=command_workspace,
        )
        if self._session_id is None and (
            execution.session_inputs or execution.session_input_batches
        ):
            first = (
                execution.session_inputs[0]
                if execution.session_inputs
                else execution.session_input_batches[0].inputs[0]
            )
            created = runtime.create_session(
                title=first.title or _plugin_session_title(selected.name, argument),
                initial_model=self._initial_model,
                reasoning_request=self._reasoning_request,
                approval_mode=self._approval_mode,
                interaction_mode=self._interaction_mode,
                workspace=command_workspace,
            )
            self._session_id = created.session_id
            self._initial_model = None
        results: list[RunResult] = []
        if execution.session_input_batches:
            assert self._session_id is not None
            for batch in execution.session_input_batches:
                results.append(
                    await runtime.submit_plugin_input_batch(batch, session_id=self._session_id)
                )
        else:
            for session_input in execution.session_inputs:
                results.append(
                    await runtime.submit_plugin_input(session_input, session_id=self._session_id)
                )
        return TuiPluginCommandExecution(
            execution=execution,
            run_results=tuple(results),
            session_controls=await self.session_control_statuses(),
        )

    async def session_control_statuses(self) -> tuple[TuiSessionControlStatus, ...]:
        session_id = self._session_id
        if session_id is None:
            return ()
        return await asyncio.to_thread(self._session_control_statuses, session_id)

    def _session_control_statuses(
        self,
        session_id: str | None,
    ) -> tuple[TuiSessionControlStatus, ...]:
        if session_id is None:
            return ()
        application = self._application
        if application is None:
            return ()
        plugin_names: dict[str, str] = {}
        if application.plugins is not None:
            try:
                plugin_names = {item.plugin_id: item.name for item in application.plugins.list()}
            except Exception:  # noqa: BLE001 - labels have a stable identity fallback.
                # Session-control state remains present even when plugin catalog
                # presentation metadata is temporarily unavailable.
                pass
        statuses: list[TuiSessionControlStatus] = []
        seen_labels: set[str] = set()
        for activation in self._runtime().session_control_activations(session_id):
            if activation.phase != "active":
                continue
            label = plugin_names.get(activation.plugin_id) or _session_control_fallback_label(
                activation.plugin_id
            )
            if label in seen_labels:
                continue
            seen_labels.add(label)
            statuses.append(
                TuiSessionControlStatus(
                    controller_id=activation.controller_id,
                    label=label,
                )
            )
        return tuple(statuses)

    async def usage_totals(self) -> UsageTotals:
        return await asyncio.to_thread(self._usage_totals)

    def _usage_totals(self) -> UsageTotals:
        usage = self._runtime().usage
        return usage.summarize().totals if usage is not None else UsageTotals()

    async def usage_summary(self) -> UsageSummary:
        return await asyncio.to_thread(self._usage_summary)

    def _usage_summary(self) -> UsageSummary:
        usage = self._runtime().usage
        return (
            usage.summarize(group_by="date")
            if usage is not None
            else UsageSummary(
                usage_filter=UsageFilter(),
                totals=UsageTotals(),
                group_by="date",
            )
        )

    async def select_model(self, selector: str) -> AvailableModel:
        """Select a Session model and reset reasoning only when the route changes.

        Model capabilities define the valid reasoning controls, so a route change
        replaces the old route-bound intent with ``default`` in the same Runtime
        mutation. Re-selecting the current route preserves the user's intent.
        """

        runtime = self._runtime()
        session_id = self._session_id
        reasoning_request = self._reasoning_request
        initial_model = self._initial_model

        def select() -> tuple[AvailableModel, bool]:
            model = runtime.resolve_model(selector)
            if session_id is None:
                current_model = initial_model or runtime.current_model().key
                route_changed = model.key != current_model
                selected_reasoning = ReasoningRequest() if route_changed else reasoning_request
                runtime.model_reasoning(model.key, selected_reasoning)
            else:
                route_changed = model.key != runtime.current_model(session_id).key
                runtime.switch_session_model(
                    session_id,
                    model.key,
                    reasoning_request=ReasoningRequest() if route_changed else None,
                )
            return model, route_changed

        model, route_changed = await asyncio.to_thread(select)
        if session_id is None:
            self._initial_model = model.key
            if route_changed:
                self._reasoning_request = ReasoningRequest()
        else:
            await self._unarchive_current_session_after_update()
        return model

    async def session_approval_mode(self) -> SessionApprovalMode:
        """Return the draft or persisted approval policy for the visible Session."""

        session_id = self._session_id
        if session_id is None:
            return self._approval_mode
        sessions = self._runtime().sessions
        return await asyncio.to_thread(
            lambda: sessions.open(session_id, recover=False).session.approval_mode
        )

    async def set_session_approval_mode(
        self,
        approval_mode: SessionApprovalMode,
    ) -> SessionApprovalMode:
        """Set the next-Session draft or the visible Session's next approval policy."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        session_id = self._session_id
        if session_id is None:
            self._approval_mode = approval_mode
        else:
            runtime = self._runtime()
            await asyncio.to_thread(runtime.set_session_approval_mode, session_id, approval_mode)
            await self._unarchive_current_session_after_update()
        return approval_mode

    async def session_approval_preset(self) -> TuiApprovalPreset:
        """Project the Session policy pair as one mutually exclusive TUI preset."""

        session_id = self._session_id
        if session_id is None:
            approval_mode = self._approval_mode
            interaction_mode = self._interaction_mode
        else:
            sessions = self._runtime().sessions
            session = await asyncio.to_thread(
                lambda: sessions.open(session_id, recover=False).session
            )
            approval_mode = session.approval_mode
            interaction_mode = session.interaction_mode
        if approval_mode == "auto" and interaction_mode == "unattended":
            return "afk"
        return approval_mode

    async def set_session_approval_preset(
        self,
        preset: TuiApprovalPreset,
    ) -> TuiApprovalPreset:
        """Apply one approval preset without leaving stale unattended behavior."""

        if preset not in TUI_APPROVAL_PRESETS:
            raise ValueError(f"invalid TUI approval preset: {preset}")
        approval_mode: SessionApprovalMode = "auto" if preset == "afk" else preset
        interaction_mode: SessionInteractionMode = (
            "unattended" if preset == "afk" else "interactive"
        )
        session_id = self._session_id
        if session_id is None:
            self._approval_mode = approval_mode
            self._interaction_mode = interaction_mode
        else:
            runtime = self._runtime()
            await asyncio.to_thread(
                runtime.set_session_policies,
                session_id,
                approval_mode=approval_mode,
                interaction_mode=interaction_mode,
            )
            await self._unarchive_current_session_after_update()
        return preset

    async def session_reasoning(self) -> ModelReasoningState:
        """Return endpoint-resolved reasoning for the draft or persisted Session."""

        runtime = self._runtime()
        session_id = self._session_id
        if session_id is not None:
            return await asyncio.to_thread(runtime.session_reasoning, session_id)
        initial_model = self._initial_model
        reasoning_request = self._reasoning_request

        def resolve() -> ModelReasoningState:
            model = initial_model or runtime.current_model().key
            return runtime.model_reasoning(model, reasoning_request)

        return await asyncio.to_thread(resolve)

    async def set_session_reasoning(
        self,
        reasoning_request: ReasoningRequest,
    ) -> ModelReasoningState:
        """Validate and set draft or persisted reasoning through RuntimeService."""

        runtime = self._runtime()
        session_id = self._session_id
        if session_id is None:
            initial_model = self._initial_model

            def resolve() -> ModelReasoningState:
                model = initial_model or runtime.current_model().key
                return runtime.model_reasoning(model, reasoning_request)

            state = await asyncio.to_thread(resolve)
            self._reasoning_request = state.requested
            return state
        await asyncio.to_thread(runtime.set_session_reasoning, session_id, reasoning_request)
        await self._unarchive_current_session_after_update()
        return await asyncio.to_thread(runtime.session_reasoning, session_id)

    async def load_session(self, session_id: str) -> LoadedSession:
        loaded, control_archived = await asyncio.to_thread(
            self._read_session_selection,
            session_id,
        )
        self._session_id = loaded.detail.session.agent_root_session_id
        self._session_archived = control_archived
        self._initial_model = None
        return loaded

    def _read_session_selection(self, session_id: str) -> tuple[LoadedSession, bool]:
        """Read the selected surface and resolve its canonical control Session."""

        loaded = self._read_session(session_id)
        session = loaded.detail.session
        if session.session_kind == "primary":
            return loaded, session.archived
        root = self._runtime().sessions.open(
            session.agent_root_session_id,
            recover=False,
        ).session
        if root.session_kind != "primary":
            raise PersistenceError(
                f"subagent session {session.session_id} references non-primary root "
                f"{root.session_id}"
            )
        return loaded, root.archived

    async def observe_session(self, session_id: str) -> TuiSessionObservation:
        """Attach a read-only Host observation without changing the control Session."""

        loaded = await asyncio.to_thread(self._read_session, session_id)
        subscription = self.subscribe_session(
            session_id,
            after_sequence=loaded.event_high_water,
        )
        return TuiSessionObservation(loaded=loaded, subscription=subscription)

    def subscribe_session(
        self,
        session_id: str,
        *,
        after_sequence: int,
    ) -> RuntimeSubscription:
        application = self._application
        if not self._started or application is None:
            raise RuntimeError("TUI controller is not started")
        return application.supervisor.subscribe(
            session_id,
            after_sequence=after_sequence,
            max_buffer=1000,
        )

    def subscribe_agent_tree(self, root_session_id: str) -> AgentTreeSubscription | None:
        application = self._application
        if not self._started or application is None:
            raise RuntimeError("TUI controller is not started")
        if application.agents is None:
            return None
        return application.agents.subscribe_agent_tree(root_session_id)

    def agent_tree_snapshot(self, root_session_id: str) -> AgentTreeSnapshot | None:
        application = self._application
        if not self._started or application is None:
            raise RuntimeError("TUI controller is not started")
        if application.agents is None:
            return None
        return application.agents.agent_tree_snapshot(root_session_id)

    async def resolve_agent_navigation(
        self,
        *,
        parent_session_id: str,
        parent_run_id: str,
        parent_tool_call_id: str,
    ) -> AgentTreeNode | None:
        return await asyncio.to_thread(
            self._agents().resolve_agent_navigation,
            parent_session_id=parent_session_id,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
        )

    async def _unarchive_current_session_after_update(self) -> None:
        session_id = self._session_id
        if session_id is None or not self._session_archived:
            return
        sessions = self._runtime().sessions
        await asyncio.to_thread(sessions.set_archived, session_id, False)
        self._session_archived = False

    def _read_session(self, session_id: str) -> LoadedSession:
        runtime = self._runtime()
        sessions = runtime.sessions
        snapshot = sessions.observation_snapshot(session_id)
        detail = _observation_detail(snapshot)
        return LoadedSession(
            detail=detail,
            messages=snapshot.messages,
            events=snapshot.events,
            runs=snapshot.runs,
            context_status=sessions.context_status(session_id),
            effective_context_metrics=runtime.session_context_metrics(session_id),
            compactions=snapshot.compactions,
            queued_inputs=self._queued_user_inputs(session_id),
            session_controls=self._session_control_statuses(session_id),
            event_high_water=snapshot.event_high_water,
        )

    def _session_messages(self, session_id: str) -> list[MessageRecord]:
        sessions = self._runtime().sessions
        messages: list[MessageRecord] = []
        cursor = 0
        while True:
            page = sessions.message_page(session_id, cursor=cursor, limit=200)
            messages.extend(page.messages)
            if page.next_cursor is None:
                return messages
            cursor = page.next_cursor

    def _runtime(self) -> RuntimeService:
        if not self._started or self._application is None:
            raise RuntimeError("TUI controller is not started")
        return self._application.runtime

    def _require_session(self) -> str:
        if self._session_id is None:
            raise RuntimeError("no active session")
        return self._session_id

    def _agents(self) -> AgentService:
        if self._application is None or self._application.agents is None:
            raise RuntimeError("subagent interaction service is unavailable")
        return self._application.agents

    def _require_configuration(self) -> ConfigurationFacade:
        self._runtime()
        if self._configuration is None:
            raise RuntimeError("configuration is unavailable for this Runtime")
        return self._configuration

    @staticmethod
    def _mcp_status(record: MCPRuntimeServerView) -> McpServerStatus:
        return McpServerStatus(
            server_id=record.server_id,
            name=record.name,
            transport=record.transport,
            status=record.status,
            tool_count=record.tool_count,
            error=record.error,
        )

    async def _receive_event(self, event: EventRecord) -> None:
        """Cross one persisted Runtime event into Textual's message pump."""

        if self._creating_session and self._session_id is None:
            root_session_id = await asyncio.to_thread(
                self._event_root_session_id,
                event.session_id,
            )
            if root_session_id != event.session_id:
                return
            self._session_id = event.session_id
        controlled_session_id = self._session_id
        if controlled_session_id is None:
            return
        if event.session_id != controlled_session_id:
            if (
                event.event_type not in _CHILD_CONTROL_EVENT_TYPES
                or await asyncio.to_thread(
                    self._event_root_session_id,
                    event.session_id,
                )
                != controlled_session_id
            ):
                return
        self._publish_event(event)

    def _event_root_session_id(self, session_id: str) -> str | None:
        cached = self._event_root_sessions.get(session_id)
        if cached is not None:
            return cached
        try:
            session = self._runtime().sessions.open(session_id, recover=False).session
        except (FileNotFoundError, PersistenceError, SessionNotFoundError):
            return None
        root_session_id = session.agent_root_session_id
        self._event_root_sessions[session_id] = root_session_id
        return root_session_id


def _observation_detail(snapshot: SessionObservationSnapshot) -> SessionDetail:
    recent_run_id = snapshot.session.active_run_id or snapshot.session.last_run_id
    recent_run = next(
        (run for run in snapshot.runs if run.run_id == recent_run_id),
        None,
    )
    active_compaction = next(
        (
            item
            for item in snapshot.compactions
            if item.compaction_id == snapshot.session.active_compaction_id
        ),
        None,
    )
    return SessionDetail(
        session=snapshot.session,
        recent_run=recent_run,
        active_compaction=active_compaction,
    )


def _plugin_session_title(name: str, argument: str) -> str:
    normalized = " ".join(argument.split())
    return normalized[:60] if normalized else f"/{name}"


def _session_control_fallback_label(plugin_id: str) -> str:
    local_id = plugin_id.rsplit(".", 1)[-1]
    return local_id.replace("_", " ").replace("-", " ").title()


def _tool_plugin_id(metadata: Metadata) -> str | None:
    plugin_id = metadata.get("plugin_id")
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None


def _plugin_skill_owner(skill: SkillMetadata) -> str | None:
    ouro = skill.extensions.get("ouro")
    if not isinstance(ouro, dict):
        return None
    plugin_id = ouro.get("plugin_id")
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None
