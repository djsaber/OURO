from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from ouro.application.dto import ApplicationStartupReport
from ouro.application.models import AvailableModel
from ouro.application.runtime import RuntimeService
from ouro.application.session_models import SessionCatalogEntry, SessionDetail
from ouro.core.approvals import SessionApprovalMode
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.results import RunResult
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication

EventPublisher = Callable[[EventRecord], None]
LifecyclePublisher = Callable[["TuiLifecycleStatus"], None]


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
        close_application: bool = True,
    ) -> None:
        self._publish_event = publish_event
        self._publish_lifecycle = publish_lifecycle
        self._home = home
        self._system_prompt = system_prompt
        self._session_backend = session_backend
        self._approval_mode = approval_mode
        self._interaction_mode = interaction_mode
        self._application = application
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
        models = tuple(runtime.available_models())
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
            ),
        )

    async def close(self) -> None:
        application = self._application
        self._started = False
        if application is not None and self._close_application:
            await application.close()

    async def submit(self, prompt: str) -> RunResult:
        runtime = self._runtime()
        is_new_session = self._session_id is None
        if is_new_session:
            self._creating_session = True
        try:
            result = await runtime.run(
                prompt,
                session_id=self._session_id,
                initial_model=self._initial_model if is_new_session else None,
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
        run_id: str,
        approval_id: str,
        approved: bool,
        reason: str = "User rejected",
    ) -> RunResult:
        return await self._runtime().approve_tool(
            session_id=self._require_session(),
            run_id=run_id,
            approval_id=approval_id,
            approved=approved,
            reason=reason,
        )

    async def answer(self, *, run_id: str, answer: JsonValue) -> RunResult:
        return await self._runtime().answer_user(
            session_id=self._require_session(),
            run_id=run_id,
            answer=answer,
        )

    def new_session(self) -> None:
        self._session_id = None

    def sessions(self) -> tuple[SessionCatalogEntry, ...]:
        return self._runtime().sessions.catalog().entries

    def models(self) -> tuple[AvailableModel, ...]:
        return tuple(self._runtime().available_models())

    def select_model(self, selector: str) -> AvailableModel:
        runtime = self._runtime()
        model = runtime.resolve_model(selector)
        if self._session_id is None:
            self._initial_model = model.key
        else:
            runtime.switch_session_model(self._session_id, model.key)
        return model

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
        return LoadedSession(detail=detail, messages=tuple(messages), events=tuple(events))

    def _runtime(self) -> RuntimeService:
        if not self._started or self._application is None:
            raise RuntimeError("TUI controller is not started")
        return self._application.runtime

    def _require_session(self) -> str:
        if self._session_id is None:
            raise RuntimeError("no active session")
        return self._session_id

    async def _receive_event(self, event: EventRecord) -> None:
        """Cross one persisted Runtime event into Textual's message pump."""

        if self._creating_session and self._session_id is None:
            self._session_id = event.session_id
        self._publish_event(event)
        await asyncio.sleep(0)
