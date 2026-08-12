from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast, get_args

from pydantic import JsonValue

from ouro.application.agent_models import AgentInteractionView
from ouro.application.agents import AgentService
from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import (
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
    SkillCatalogView,
    SkillInstallScopeInput,
    SkillManagementView,
    SkillValidationView,
    ToolExecutionConfigView,
)
from ouro.application.dto import ApplicationStartupReport
from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.plugin_models import PluginCommandExecutionView, PluginCommandView
from ouro.application.queued_inputs import QueuedInputMode, QueuedUserInputView
from ouro.application.runtime import RuntimeService
from ouro.application.session_models import (
    SessionBranchResult,
    SessionCatalogEntry,
    SessionContextStatus,
    SessionDetail,
    SessionView,
)
from ouro.application.skill_activations import SkillInvocationRequest
from ouro.application.usage_models import UsageTotals
from ouro.config import ProviderType
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.compaction import CompactionCommandResult, CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.errors import ConfigError
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


def project_mcp_system_status(
    status: TuiSystemStatus,
    servers: tuple[McpServerStatus, ...],
) -> TuiSystemStatus:
    """Project one live MCP snapshot into the shared TUI system summary."""

    connected = tuple(server for server in servers if server.status == "connected")
    return replace(
        status,
        mcp_connected_count=len(connected),
        mcp_total_count=len(servers),
        mcp_tool_count=sum(server.tool_count for server in connected),
        mcp_connecting_count=sum(
            server.status in {"starting", "reconnecting"} for server in servers
        ),
        mcp_failed_count=sum(server.status == "failed" for server in servers),
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


@dataclass(frozen=True)
class TuiToolConfiguration:
    name: str
    display_name: str
    source: ToolSource | Literal["unavailable"]
    effect: ToolEffect
    default_permission: PermissionMode | None
    permission_override: PermissionMode | None
    permission_control: str
    available: bool = True

    @property
    def effective_permission(self) -> PermissionMode | None:
        if self.permission_control == "tool":
            return self.default_permission
        return self.permission_override or self.default_permission


@dataclass(frozen=True)
class TuiToolExecutionConfiguration:
    mode: ToolExecutionMode
    max_concurrency: int
    workspace_only: bool
    max_output_chars: int


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
class TuiPluginCommandExecution:
    execution: PluginCommandExecutionView
    run_results: tuple[RunResult, ...] = ()


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
        self._creating_session = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

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
        self._application.runtime.add_event_sink(self._receive_event)
        await self._application.start()
        self._started = True
        runtime = self._application.runtime
        if self._configuration is None and runtime.environment.home is not None:
            self._configuration = ConfigurationFacade(runtime.environment.home)
        models = tuple(runtime.available_models())
        usage = await self.usage_totals()
        plugin_summaries = (
            tuple(self._application.plugins.list()) if self._application.plugins is not None else ()
        )
        skills = runtime.available_skills()
        active_plugins = tuple(
            item for item in plugin_summaries if item.status in {"active", "ready"}
        )
        plugin_issue_statuses = {
            "invalid",
            "incompatible",
            "needs_configuration",
            "needs_code_trust",
            "needs_authorization",
            "degraded",
            "blocked",
            "failed",
        }
        mcp_servers = self.mcp_servers()
        return TuiBootstrap(
            models=models,
            current_model=runtime.current_model(),
            sessions=await self.sessions(),
            startup_report=self._application.startup_report,
            mcp_servers=mcp_servers,
            system_status=project_mcp_system_status(
                TuiSystemStatus(
                    workspace=runtime.default_workspace,
                    provider_ids=tuple(sorted({model.provider_id for model in models})),
                    model_count=len(models),
                    skill_count=len(skills),
                    skill_names=tuple(sorted(item.name for item in skills)),
                    plugin_active_count=len(active_plugins),
                    plugin_total_count=len(plugin_summaries),
                    plugin_issue_count=sum(
                        item.status in plugin_issue_statuses for item in plugin_summaries
                    ),
                    plugin_active_names=tuple(sorted(item.name for item in active_plugins)),
                    usage_request_count=usage.request_count,
                    usage_input_tokens=usage.input_tokens,
                    usage_output_tokens=usage.output_tokens,
                    usage_estimated_cost=usage.estimated_cost,
                    usage_priced_request_count=usage.priced_request_count,
                ),
                mcp_servers,
            ),
        )

    async def close(self) -> None:
        application = self._application
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
        return self._application.agents.list_agent_interactions(session.root_session_id)

    def new_session(self) -> None:
        self._session_id = None
        self._session_archived = False
        self._initial_model = None
        self._approval_mode = self._default_approval_mode
        self._interaction_mode = self._default_interaction_mode
        self._reasoning_request = ReasoningRequest()

    async def sessions(self) -> tuple[SessionCatalogEntry, ...]:
        runtime = self._runtime()
        return await asyncio.to_thread(lambda: runtime.sessions.catalog().entries)

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
        return await asyncio.to_thread(self._delete_archived_sessions, session_ids)

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
                message for message in self._session_messages(session_id) if message.role == "user"
            )
        )

    async def branch_session(self, message_id: str) -> SessionBranchResult:
        session_id = self._require_session()
        return await asyncio.to_thread(
            self._branch_at_user_message,
            session_id,
            message_id,
        )

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
                if message.message_id == message_id and message.role == "user"
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
            enabled=draft.enabled,
            make_default=draft.make_default,
        )
        return next(
            provider for provider in catalog.providers if provider.provider_id == draft.provider_id
        )

    async def update_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        return await asyncio.to_thread(self._update_provider, draft)

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
        definitions = {item.name: item for item in self._runtime().tools.definitions()}
        overrides = self._require_configuration().permission_config().tools
        names = sorted(set(definitions).union(overrides))
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
                        permission_override=overrides.get(name),
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
                    permission_override=overrides.get(name),
                    permission_control=definition.permission_control,
                )
            )
        return tuple(items)

    async def set_tool_permission(
        self,
        tool_name: str,
        mode: PermissionMode | None,
    ) -> TuiToolConfiguration | None:
        return await asyncio.to_thread(self._set_tool_permission, tool_name, mode)

    def _set_tool_permission(
        self,
        tool_name: str,
        mode: PermissionMode | None,
    ) -> TuiToolConfiguration | None:
        current = next(
            (item for item in self._tool_configurations() if item.name == tool_name),
            None,
        )
        if current is None:
            raise ConfigError(f"tool not found: {tool_name}")
        if mode is not None and current.permission_control == "tool":
            raise ConfigError(f"tool permission is controlled by its contract: {tool_name}")
        configuration = self._require_configuration()
        if mode is None:
            configuration.clear_tool_permission(tool_name)
        else:
            configuration.set_tool_permission(tool_name, mode)
        return next(
            (item for item in self._tool_configurations() if item.name == tool_name),
            None,
        )

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
        return await asyncio.to_thread(configuration.skill_catalog, include_disabled=True)

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
        if self._session_id is None and selected.requires_session:
            created = runtime.create_session(
                title=_plugin_session_title(selected.name, argument),
                initial_model=self._initial_model,
                reasoning_request=self._reasoning_request,
                approval_mode=self._approval_mode,
                interaction_mode=self._interaction_mode,
            )
            self._session_id = created.session_id
            self._initial_model = None
        execution = await application.plugins.execute_command(
            selected.command_id,
            argument,
            session_id=self._session_id,
            workspace=runtime.default_workspace,
        )
        if self._session_id is None and execution.session_inputs:
            first = execution.session_inputs[0]
            created = runtime.create_session(
                title=first.title or _plugin_session_title(selected.name, argument),
                initial_model=self._initial_model,
                reasoning_request=self._reasoning_request,
                approval_mode=self._approval_mode,
                interaction_mode=self._interaction_mode,
            )
            self._session_id = created.session_id
            self._initial_model = None
        results: list[RunResult] = []
        for session_input in execution.session_inputs:
            results.append(
                await runtime.submit_plugin_input(session_input, session_id=self._session_id)
            )
        return TuiPluginCommandExecution(execution=execution, run_results=tuple(results))

    async def usage_totals(self) -> UsageTotals:
        return await asyncio.to_thread(self._usage_totals)

    def _usage_totals(self) -> UsageTotals:
        usage = self._runtime().usage
        return usage.summarize().totals if usage is not None else UsageTotals()

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
        loaded = await asyncio.to_thread(self._read_session, session_id)
        self._session_id = session_id
        self._session_archived = loaded.detail.session.archived
        self._initial_model = None
        return loaded

    async def _unarchive_current_session_after_update(self) -> None:
        session_id = self._session_id
        if session_id is None or not self._session_archived:
            return
        sessions = self._runtime().sessions
        await asyncio.to_thread(sessions.set_archived, session_id, False)
        self._session_archived = False

    def _read_session(self, session_id: str) -> LoadedSession:
        sessions = self._runtime().sessions
        detail = sessions.open(session_id)
        messages = self._session_messages(session_id)

        events: list[EventRecord] = []
        # Messages are the canonical presentation history for terminal Runs.
        # Event replay exists only to reconstruct mutable state for the active
        # Run (pending interaction, live tools, and activity). Replaying the
        # latest completed Run makes an idle Session open scale with an
        # unrelated historical event journal.
        run_id = detail.session.active_run_id
        if run_id is not None:
            cursor = 0
            while True:
                event_page = sessions.event_page(
                    session_id,
                    cursor=cursor,
                    limit=500,
                    run_id=run_id,
                )
                events.extend(event_page.events)
                if event_page.next_cursor is None:
                    break
                cursor = event_page.next_cursor
        return LoadedSession(
            detail=detail,
            messages=tuple(messages),
            events=tuple(events),
            runs=sessions.run_history(session_id),
            context_status=sessions.context_status(session_id),
            effective_context_metrics=sessions.effective_context_metrics(session_id),
            compactions=sessions.compactions(session_id),
            queued_inputs=self._queued_user_inputs(session_id),
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
            self._session_id = event.session_id
        self._publish_event(event)
        await asyncio.sleep(0)


def _plugin_session_title(name: str, argument: str) -> str:
    normalized = " ".join(argument.split())
    return normalized[:60] if normalized else f"/{name}"
