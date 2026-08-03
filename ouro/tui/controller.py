from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

from pydantic import JsonValue

from ouro.application.agent_models import AgentInteractionView
from ouro.application.agents import AgentService
from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import ProviderView
from ouro.application.dto import ApplicationStartupReport
from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.queued_inputs import QueuedInputMode, QueuedUserInputView
from ouro.application.runtime import RuntimeService
from ouro.application.session_models import SessionCatalogEntry, SessionDetail
from ouro.application.usage_models import UsageTotals
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.errors import ConfigError
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication
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
    mcp_failed_count: int = 0
    startup_diagnostic_count: int = 0
    usage_request_count: int = 0
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_estimated_cost: float = 0.0
    usage_priced_request_count: int = 0


@dataclass(frozen=True)
class TuiModelConfiguration:
    """Validated model-endpoint intent collected by the TUI."""

    provider_id: str
    endpoint_id: str
    model_id: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_vision: bool = False


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
        self._initial_model: str | None = None
        self._creating_session = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self) -> TuiBootstrap:
        if self._started:
            raise RuntimeError("TUI controller is already started")
        if self._application is None:
            self._application = LocalApplication.build(
                self._home,
                system_prompt=self._system_prompt,
                session_backend=self._session_backend,
            )
        startup_configs = self._application.mcp.startup_configs
        if startup_configs:
            self._publish_lifecycle(
                TuiLifecycleStatus(
                    activity="mcp_connecting",
                    detail=f"MCP · connecting {len(startup_configs)} server(s)",
                )
            )
        self._application.runtime.add_event_sink(self._receive_event)
        await self._application.start()
        self._started = True
        runtime = self._application.runtime
        if self._configuration is None and runtime.environment.home is not None:
            self._configuration = ConfigurationFacade(runtime.environment.home)
        models = tuple(runtime.available_models())
        usage = self.usage_totals()
        plugin_summaries = (
            tuple(self._application.plugins.list())
            if self._application.plugins is not None
            else ()
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
        mcp_servers = tuple(
            McpServerStatus(
                server_id=record.config.server_id,
                name=record.config.name,
                transport=record.config.transport,
                status=record.status,
                tool_count=len(record.tool_names),
                error=record.error,
            )
            for record in self._application.mcp.startup_records
        )
        connected_mcp = tuple(item for item in mcp_servers if item.status == "connected")
        return TuiBootstrap(
            models=models,
            current_model=runtime.current_model(),
            sessions=runtime.sessions.catalog().entries,
            startup_report=self._application.startup_report,
            mcp_servers=mcp_servers,
            system_status=TuiSystemStatus(
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
                mcp_connected_count=len(connected_mcp),
                mcp_total_count=len(mcp_servers),
                mcp_tool_count=sum(item.tool_count for item in connected_mcp),
                mcp_failed_count=sum(item.status == "failed" for item in mcp_servers),
                startup_diagnostic_count=len(self._application.startup_report.diagnostics),
                usage_request_count=usage.request_count,
                usage_input_tokens=usage.input_tokens,
                usage_output_tokens=usage.output_tokens,
                usage_estimated_cost=usage.estimated_cost,
                usage_priced_request_count=usage.priced_request_count,
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
    ) -> RunResult:
        runtime = self._runtime()
        is_new_session = self._session_id is None
        if is_new_session:
            self._creating_session = True
        try:
            result = await runtime.run(
                prompt,
                session_id=self._session_id,
                queue_mode=queue_mode,
                initial_model=self._initial_model if is_new_session else None,
                initial_reasoning_request=(
                    self._reasoning_request if is_new_session else None
                ),
                approval_mode=self._approval_mode if is_new_session else None,
                interaction_mode=self._interaction_mode if is_new_session else None,
            )
            self._session_id = result.session.session_id
            self._initial_model = None
            return result
        finally:
            if is_new_session:
                self._creating_session = False

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

    def agent_interactions(self) -> tuple[AgentInteractionView, ...]:
        if self._session_id is None or self._application is None:
            return ()
        if self._application.agents is None:
            return ()
        session = self._runtime().sessions.open(self._session_id, recover=False).session
        return self._application.agents.list_agent_interactions(session.root_session_id)

    def new_session(self) -> None:
        self._session_id = None
        self._initial_model = None
        self._approval_mode = self._default_approval_mode
        self._interaction_mode = self._default_interaction_mode
        self._reasoning_request = ReasoningRequest()

    def sessions(self) -> tuple[SessionCatalogEntry, ...]:
        return self._runtime().sessions.catalog().entries

    def queued_user_inputs(self, session_id: str | None = None) -> tuple[QueuedUserInputView, ...]:
        target_session_id = session_id or self._require_session()
        return self._runtime().pending_user_inputs(target_session_id)

    def message(self, session_id: str, message_id: str) -> MessageRecord:
        return self._runtime().sessions.message(session_id, message_id)

    def models(self) -> tuple[AvailableModel, ...]:
        return tuple(self._runtime().available_models())

    def model_providers(self) -> tuple[ProviderView, ...]:
        configuration = self._require_configuration()
        return tuple(
            provider for provider in configuration.provider_catalog().providers if provider.enabled
        )

    def configure_model(self, model: TuiModelConfiguration) -> AvailableModel:
        """Create one endpoint through the canonical configuration application service."""

        configuration = self._require_configuration()
        endpoints = configuration.endpoint_catalog(model.provider_id).endpoints
        if any(endpoint.endpoint_id == model.endpoint_id for endpoint in endpoints):
            raise ConfigError(
                f"model endpoint already exists: {model.provider_id}/{model.endpoint_id}"
            )
        configuration.save_endpoint(
            model.provider_id,
            model.endpoint_id,
            model=model.model_id,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            supports_tools=model.supports_tools,
            supports_vision=model.supports_vision,
        )
        return self._runtime().resolve_model(f"{model.provider_id}/{model.endpoint_id}")

    def usage_totals(self) -> UsageTotals:
        usage = self._runtime().usage
        return usage.summarize().totals if usage is not None else UsageTotals()

    def select_model(self, selector: str) -> AvailableModel:
        runtime = self._runtime()
        model = runtime.resolve_model(selector)
        if self._session_id is None:
            runtime.model_reasoning(model.key, self._reasoning_request)
            self._initial_model = model.key
        else:
            runtime.switch_session_model(self._session_id, model.key)
        return model

    def session_approval_mode(self) -> SessionApprovalMode:
        """Return the draft or persisted approval policy for the visible Session."""

        if self._session_id is None:
            return self._approval_mode
        return self._runtime().sessions.open(
            self._session_id,
            recover=False,
        ).session.approval_mode

    def set_session_approval_mode(
        self,
        approval_mode: SessionApprovalMode,
    ) -> SessionApprovalMode:
        """Set the next-Session draft or persist an idle Session's approval policy."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if self._session_id is None:
            self._approval_mode = approval_mode
        else:
            self._runtime().set_session_approval_mode(self._session_id, approval_mode)
        return approval_mode

    def session_approval_preset(self) -> TuiApprovalPreset:
        """Project the Session policy pair as one mutually exclusive TUI preset."""

        if self._session_id is None:
            approval_mode = self._approval_mode
            interaction_mode = self._interaction_mode
        else:
            session = self._runtime().sessions.open(
                self._session_id,
                recover=False,
            ).session
            approval_mode = session.approval_mode
            interaction_mode = session.interaction_mode
        if approval_mode == "auto" and interaction_mode == "unattended":
            return "afk"
        return approval_mode

    def set_session_approval_preset(
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
        if self._session_id is None:
            self._approval_mode = approval_mode
            self._interaction_mode = interaction_mode
        else:
            self._runtime().set_session_policies(
                self._session_id,
                approval_mode=approval_mode,
                interaction_mode=interaction_mode,
            )
        return preset

    def session_reasoning(self) -> ModelReasoningState:
        """Return endpoint-resolved reasoning for the draft or persisted Session."""

        runtime = self._runtime()
        if self._session_id is not None:
            return runtime.session_reasoning(self._session_id)
        model = self._initial_model or runtime.current_model().key
        return runtime.model_reasoning(model, self._reasoning_request)

    def set_session_reasoning(
        self,
        reasoning_request: ReasoningRequest,
    ) -> ModelReasoningState:
        """Validate and set draft or persisted reasoning through RuntimeService."""

        runtime = self._runtime()
        if self._session_id is None:
            model = self._initial_model or runtime.current_model().key
            state = runtime.model_reasoning(model, reasoning_request)
            self._reasoning_request = state.requested
            return state
        runtime.set_session_reasoning(self._session_id, reasoning_request)
        return runtime.session_reasoning(self._session_id)

    def load_session(self, session_id: str) -> LoadedSession:
        sessions = self._runtime().sessions
        detail = sessions.open(session_id)
        messages: list[MessageRecord] = []
        cursor = 0
        while True:
            message_page = sessions.message_page(session_id, cursor=cursor, limit=200)
            messages.extend(message_page.messages)
            if message_page.next_cursor is None:
                break
            cursor = message_page.next_cursor

        events: list[EventRecord] = []
        run_id = detail.recent_run.run_id if detail.recent_run is not None else None
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
        self._session_id = session_id
        self._initial_model = None
        return LoadedSession(
            detail=detail,
            messages=tuple(messages),
            events=tuple(events),
            queued_inputs=self.queued_user_inputs(session_id),
        )

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
            raise RuntimeError("model configuration is unavailable for this Runtime")
        return self._configuration

    async def _receive_event(self, event: EventRecord) -> None:
        """Cross one persisted Runtime event into Textual's message pump."""

        if self._creating_session and self._session_id is None:
            self._session_id = event.session_id
        self._publish_event(event)
        await asyncio.sleep(0)
