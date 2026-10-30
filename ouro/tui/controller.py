"""Application operations consumed by the terminal Host."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from pydantic import JsonValue

from ouro.application.agents.service import AgentService
from ouro.application.agents.tree import AgentTreeSubscription
from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.contracts.agents import (
    AgentDefinitionCatalogView,
    AgentDefinitionDetail,
    AgentInteractionView,
    AgentToolOptionView,
    AgentTreeNode,
    AgentTreeSnapshot,
)
from ouro.application.contracts.configuration import (
    AgentRuntimeConfigView,
    CompactionConfigInput,
    CompactionConfigView,
    MCPConnectionCheckView,
    MCPRuntimeServerView,
    MCPServerConfigInput,
    MCPServerConfigView,
    ProviderCatalogView,
    ProviderEndpointCatalogView,
    ProviderEndpointView,
    ProviderTemplateView,
    ProviderView,
    QueuedInputConfigView,
    SkillCatalogView,
    SkillManagementView,
    SkillPolicyView,
    SkillValidationView,
    SkillView,
    SystemPromptConfigView,
    ToolExecutionConfigView,
)
from ouro.application.contracts.execution import RunHandle
from ouro.application.contracts.models import AvailableModel, ModelReasoningState
from ouro.application.contracts.plugin_configuration import (
    PluginConfigurationCandidate,
    PluginConfigurationPreview,
    PluginConfigurationResult,
    PluginConfigurationView,
)
from ouro.application.contracts.plugins import (
    PluginActionResult,
    PluginCommandView,
    PluginDetail,
    PluginSummary,
)
from ouro.application.contracts.sessions import (
    SessionBranchResult,
    SessionCatalogEntry,
    SessionDetail,
    SessionHistoryAnchor,
    SessionHistoryWindow,
    SessionObservationSnapshot,
    SessionSummary,
    SessionUndoResult,
    SessionView,
)
from ouro.application.events import RuntimeEventSinkRegistration
from ouro.application.inputs.queue import QueuedInputMode, QueuedUserInputView
from ouro.application.inputs.requests import UserInputRequest
from ouro.application.inputs.skills import SkillInvocationRequest
from ouro.application.plugins.commands import consume_plugin_command, submit_plugin_command
from ouro.application.provider_auth.service import ProviderAuthService
from ouro.application.runtime.service import RuntimeService
from ouro.application.tasks.service import TaskService
from ouro.application.transfer.service import (
    DataKind,
    DataTransferItem,
    DataTransferRequest,
    DataTransferResult,
)
from ouro.config import QueuedInputConsumptionMode
from ouro.config.endpoints import EndpointChanges
from ouro.config.prompts import PromptDraft, PromptKind
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.attachment_input import is_editable_user_input
from ouro.core.base import Metadata, new_id
from ouro.core.billing import UsageFilter
from ouro.core.callbacks import run_blocking
from ouro.core.compaction import CompactionCommandResult
from ouro.core.errors import ConfigError, PersistenceError, SessionNotFoundError
from ouro.core.events import EventRecord
from ouro.core.input_content import InputAttachment, InputFile, InputFileReference
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning
from ouro.core.results import RunResult
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tasks import TaskConfiguration, TaskCounts, TaskExecution, TaskRecord
from ouro.core.tools import PermissionMode
from ouro.core.usage_queries import UsageSummary, UsageTotals
from ouro.deployment import SessionBackend, TaskBackend
from ouro.local_application import LocalApplication
from ouro.mcp import MCPServerConfig
from ouro.skills import SkillMetadata
from ouro.tui.state import PendingInteraction
from ouro.tui.status import (
    project_mcp_system_status,
    project_plugin_system_status,
    project_usage_system_status,
)
from ouro.tui.view_models import (
    TUI_APPROVAL_PRESETS,
    EventPublisher,
    LifecyclePublisher,
    LoadedSession,
    McpServerStatus,
    TuiAgentDefinitionConfiguration,
    TuiAgentRuntimeConfiguration,
    TuiApprovalPreset,
    TuiBootstrap,
    TuiCompactionConfiguration,
    TuiEndpointConfiguration,
    TuiLifecycleStatus,
    TuiMCPServerConfiguration,
    TuiNewSession,
    TuiPluginCommandExecution,
    TuiPluginInstallConfiguration,
    TuiProviderConfiguration,
    TuiSessionControlStatus,
    TuiSessionObservation,
    TuiSkillInstallConfiguration,
    TuiSkillInvocationOption,
    TuiSystemStatus,
    TuiToolConfiguration,
    TuiToolExecutionConfiguration,
)

_CHILD_CONTROL_EVENT_TYPES = frozenset(
    {
        "approval_requested",
        "approval_resolved",
        "ask_user_requested",
        "ask_user_resolved",
        # Preflight interaction events can precede the persisted batch checkpoint.
        "run_status_changed",
        "run_completed",
        "run_aborted",
        "run_failed",
        "run_paused",
    }
)


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
        task_backend: TaskBackend | None = None,
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
        self._task_backend = task_backend
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
        self._selection_revision = 0
        self._session_archived = False
        self._initial_model: str | None = None
        self._draft_workspace: str | None = None
        self._creating_session = False
        self._event_root_sessions: dict[str, str] = {}
        self._event_sink_registration: RuntimeEventSinkRegistration | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def selection_revision(self) -> int:
        """Identify the current navigation, including an unbound welcome draft."""

        return self._selection_revision

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
                task_backend=self._task_backend,
            )
        startup_configs = self._application.mcp.startup_configs
        if startup_configs:
            self._publish_lifecycle(
                TuiLifecycleStatus(
                    activity="mcp_connecting",
                    detail="",
                )
            )
        registration = self._application.runtime.add_event_sink(
            self._receive_event, event_filter=self._accepts_event
        )
        try:
            await self._application.start()
        except BaseException:
            await registration.close()
            raise
        self._event_sink_registration = registration
        self._started = True
        runtime = self._application.runtime
        self._draft_workspace = runtime.default_workspace
        if self._configuration is None and runtime.environment.home is not None:
            self._configuration = ConfigurationFacade(runtime.environment.home)
        models, current_model, usage = await asyncio.gather(
            self.models(), self.default_model(), self.usage_summary()
        )
        mcp_servers = self.mcp_servers()
        skills = await self._runtime().available_skills()
        status = await asyncio.to_thread(
            self._project_system_status, models, usage, mcp_servers, skills
        )
        return TuiBootstrap(
            models=models,
            current_model=current_model,
            sessions=(),
            startup_report=self._application.startup_report,
            mcp_servers=mcp_servers,
            system_status=await self._with_task_status(status),
        )

    async def system_status(self) -> TuiSystemStatus:
        """Project current process-owned resources for a newly opened Host view."""

        models, usage, skills = await asyncio.gather(
            self.models(), self.usage_summary(), self._runtime().available_skills()
        )
        status = await asyncio.to_thread(
            self._project_system_status, models, usage, self.mcp_servers(), skills
        )
        return await self._with_task_status(status)

    def _project_system_status(
        self,
        models: tuple[AvailableModel, ...],
        usage: UsageSummary,
        mcp_servers: tuple[McpServerStatus, ...],
        skills: tuple[SkillMetadata, ...],
    ) -> TuiSystemStatus:
        runtime = self._runtime()
        assert self._application is not None
        plugins = (
            tuple(self._application.plugins.list()) if self._application.plugins is not None else ()
        )
        return project_plugin_system_status(
            project_mcp_system_status(
                project_usage_system_status(
                    TuiSystemStatus(
                        workspace=self._draft_workspace or runtime.default_workspace,
                        model_names=tuple(
                            sorted(
                                (model.display_name or model.model_id for model in models),
                                key=str.casefold,
                            )
                        ),
                        model_count=len(models),
                        skill_count=len(skills),
                        skill_names=tuple(sorted(item.name for item in skills)),
                    ),
                    usage,
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

    async def attachment_target(
        self, session_id: str | None, prepared_session_id: str
    ) -> SessionView | str:
        """Capture an existing Session epoch or reserve a new input's file namespace."""
        runtime = self._runtime()
        if session_id is not None:
            return await asyncio.to_thread(runtime.sessions.view, session_id)
        await self.prepare_input_resources(prepared_session_id)
        return prepared_session_id

    async def prepare_input_resources(self, identity: str) -> None:
        resources = self._runtime().resources
        if resources is not None:
            await asyncio.to_thread(resources.reserve, identity)

    async def release_input_resources(self, identity: str, retain: tuple[str, ...]) -> None:
        resources = self._runtime().resources
        if resources is not None:
            await asyncio.to_thread(resources.release_prepared, identity, retain=retain)

    async def input_accepted(self, session_id: str, input_id: str) -> bool:
        return await asyncio.to_thread(
            self._runtime().sessions.input_accepted, session_id, input_id
        )

    async def import_attachment(self, session: SessionView | str, path: Path) -> InputAttachment:
        resources = self._runtime().resources
        if resources is None:
            raise ConfigError("Resource storage is unavailable")
        if isinstance(session, str):
            result = await run_blocking(resources.import_prepared_resource, session, path)
        else:
            result = await run_blocking(
                resources.import_resource,
                session.session_id,
                path,
                history_epoch=session.history_epoch,
            )
        return InputAttachment(resource_id=result.resource.resource_id, name=path.name)

    async def reference_files(self, paths: tuple[Path, ...]) -> tuple[InputFile, ...]:
        """Capture selected paths and their workspace without creating a Session."""
        workspace = self._draft_workspace
        if self._session_id is not None:
            session = await asyncio.to_thread(self._runtime().sessions.view, self._session_id)
            workspace = session.workspace

        def capture() -> tuple[InputFile, ...]:
            items = []
            root = Path(workspace).resolve() if workspace is not None else None
            for path in paths:
                resolved = path.resolve(strict=True)
                if not resolved.is_file():
                    raise ValueError(f"Not a regular file: {path}")
                items.append(
                    InputFileReference(
                        name=path.name,
                        path=str(resolved),
                        workspace=str(root)
                        if root is not None and resolved.is_relative_to(root)
                        else None,
                    )
                )
            return tuple(items)

        return await asyncio.to_thread(capture)

    async def validate_attachment_session(self, session: SessionView | str) -> None:
        if isinstance(session, str):
            return
        latest = await asyncio.to_thread(self._runtime().sessions.view, session.session_id)
        if latest.history_epoch != session.history_epoch:
            raise SessionCommitConflict(
                "Session history changed while importing attachments; select the files again"
            )

    async def input_attachments(
        self, session_id: str, resource_refs: tuple[str, ...]
    ) -> tuple[InputFile, ...]:
        resources = self._runtime().resources
        if resources is None:
            return ()
        items = []
        for resource_id in resource_refs:
            resource = await asyncio.to_thread(resources.show_resource, session_id, resource_id)
            items.append(InputAttachment(resource_id=resource_id, name=resource.source.name))
        return tuple(items)

    async def submit(
        self,
        prompt: str,
        *,
        queue_mode: QueuedInputMode = "follow_up",
        skill_invocations: tuple[SkillInvocationRequest, ...] = (),
        resource_refs: tuple[str, ...] = (),
        attachments: tuple[InputFile, ...] = (),
        on_input_accepted: Callable[[], None] | None = None,
        new_session_id: str | None = None,
        input_id: str | None = None,
    ) -> RunResult:
        runtime = self._runtime()
        revision = self._selection_revision
        selection = (self._session_id, self._initial_model)
        available = await self.models()
        if not available:
            raise ConfigError("add a provider and endpoint before sending a message")
        selected = (
            await asyncio.to_thread(runtime.resolve_model, selection[1])
            if selection[0] is None and selection[1] is not None
            else await asyncio.to_thread(runtime.current_model, selection[0])
        )
        if (
            not self._started
            or revision != self._selection_revision
            or selection != (self._session_id, self._initial_model)
        ):
            raise ConfigError("session or model changed while preparing input; submit again")
        if selected.provider_id == "faux" and any(
            model.provider_id != "faux" for model in available
        ):
            raise ConfigError("select an available model before sending a message")
        is_new_session = self._session_id is None
        if is_new_session:
            self._creating_session = True

        def session_created(session: SessionRecord) -> None:
            if self._started and revision == self._selection_revision:
                self._session_id = session.session_id
                self._initial_model = None
                self._creating_session = False

        try:
            assert self._application is not None
            request = UserInputRequest(
                prompt=prompt,
                skill_invocations=skill_invocations,
                session_id=self._session_id,
                new_session_id=new_session_id if is_new_session else None,
                input_id=input_id or new_id("queued"),
                resource_refs=resource_refs,
                attachments=attachments,
                queue_mode=queue_mode,
                initial_model=self._initial_model if is_new_session else None,
                initial_reasoning_request=(self._reasoning_request if is_new_session else None),
                approval_mode=self._approval_mode if is_new_session else None,
                interaction_mode=self._interaction_mode if is_new_session else None,
                workspace=self._draft_workspace if is_new_session else None,
            )

            def input_accepted() -> None:
                if self._started and on_input_accepted is not None:
                    on_input_accepted()

            submission = await self._application.supervisor.submit_user_input(
                request, on_session_created=session_created, on_input_accepted=input_accepted
            )
            result = await submission.wait()
            if result.session.archived:
                await self.set_session_archived(result.session.session_id, archived=False)
            return result
        finally:
            if is_new_session and revision == self._selection_revision:
                self._creating_session = False

    async def skill_invocation_options(self) -> tuple[TuiSkillInvocationOption, ...]:
        """Project currently invocable Runtime skills for the inline command menu."""

        return tuple(
            TuiSkillInvocationOption(
                name=skill.name,
                description=skill.description,
                argument_hint=skill.argument_hint,
            )
            for skill in await self._runtime().available_skills()
            if skill.enabled and skill.user_invocable
        )

    async def runtime_skill_names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in await self._runtime().available_skills())

    async def continue_session(self) -> RunResult:
        assert self._application is not None
        handle = await self._application.supervisor.continue_run(session_id=self._require_session())
        result = await self._supervised_result(handle)
        if result.session.archived:
            await self.set_session_archived(result.session.session_id, archived=False)
        return result

    async def undo(self) -> SessionUndoResult:
        return await self._runtime().undo(self._require_session())

    async def abort(self) -> RunResult | None:
        if self._session_id is None:
            return None
        assert self._application is not None
        snapshot = await self._application.supervisor.abort(self._session_id)
        if snapshot.run_id is None:
            return None
        return await asyncio.to_thread(
            self._runtime().sessions.run_result,
            snapshot.session_id,
            snapshot.run_id,
        )

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
        assert self._application is not None
        session_id = self._require_session()
        # Preflight can show the request before its resumable batch is saved.
        # Keep the user's decision until the durable boundary permits admission.
        await self._application.supervisor.wait_run(
            session_id, interaction.run_id, until=frozenset({"waiting_approval"})
        )
        handle = await self._application.supervisor.resolve_tool_approval(
            session_id=session_id,
            run_id=interaction.run_id,
            approval_id=interaction.request_id,
            approved=approved,
            reason=reason,
        )
        return await self._supervised_result(handle)

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
        assert self._application is not None
        handle = await self._application.supervisor.resolve_tool_input(
            session_id=self._require_session(),
            run_id=interaction.run_id,
            answer=answer,
        )
        return await self._supervised_result(handle)

    async def _supervised_result(self, handle: RunHandle) -> RunResult:
        assert self._application is not None
        supervisor = self._application.supervisor
        try:
            snapshot = await supervisor.wait(handle)
            if snapshot.run_id is None:
                raise PersistenceError("Supervised interaction failed before a Run was identified")
            return await asyncio.to_thread(
                self._runtime().sessions.run_result,
                snapshot.session_id,
                snapshot.run_id,
            )
        finally:
            supervisor.detach_handle(handle)

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

        A new Session keeps the most recently used route and, when leaving an
        existing Session, its workspace so the welcome page and first request
        agree. Leaving no Session preserves an explicit welcome-page model
        selection; unreadable or unavailable settings fall back to the Runtime
        defaults.
        """

        prepared = await self.prepare_new_session()
        self.activate_new_session(prepared)
        return prepared.model

    async def prepare_new_session(self) -> TuiNewSession:
        """Resolve draft defaults without changing the selected Session."""
        runtime = self._runtime()
        initial_model = self._initial_model
        previous_route, previous_workspace = await self._leaving_session_defaults()
        if previous_route is not None:
            initial_model = previous_route
        draft: AvailableModel | None = None
        if initial_model is not None:
            try:
                draft = await asyncio.to_thread(runtime.resolve_model, initial_model)
            except ConfigError:
                draft = None
        if draft is None:
            draft = await asyncio.to_thread(runtime.current_model)
            initial_model = None
        return TuiNewSession(draft, initial_model, previous_workspace or runtime.default_workspace)

    def activate_new_session(self, prepared: TuiNewSession) -> None:
        self._selection_revision += 1
        self._creating_session = False
        self._session_id = None
        self._session_archived = False
        self._draft_workspace = prepared.workspace
        self._initial_model = prepared.initial_model
        self._approval_mode = self._default_approval_mode
        self._interaction_mode = self._default_interaction_mode
        self._reasoning_request = ReasoningRequest()

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

    async def _leaving_session_defaults(self) -> tuple[str | None, str | None]:
        session_id = self._session_id
        if session_id is None:
            return None, None
        runtime = self._runtime()

        def read() -> tuple[str | None, str | None]:
            try:
                session = runtime.sessions.open(session_id, recover=False).session
            except PersistenceError, SessionNotFoundError, FileNotFoundError:
                # Inheriting defaults is optional. An unreadable previous
                # Session must not prevent navigation to a fresh draft.
                return None, None
            return session.current_model, session.workspace

        return await asyncio.to_thread(read)

    async def sessions(self, *, include_subagents: bool = False) -> tuple[SessionCatalogEntry, ...]:
        runtime = self._runtime()
        return await asyncio.to_thread(
            lambda: tuple(
                entry
                for entry in runtime.sessions.catalog().entries
                if include_subagents or entry.session_kind != "subagent"
            )
        )

    def _tasks(self) -> TaskService:
        if self._application is None or self._application.tasks is None:
            raise RuntimeError("Task management is unavailable")
        return self._application.tasks

    async def tasks(self, *, offset: int = 0) -> tuple[TaskRecord, ...]:
        return await self._tasks().list(limit=100, offset=offset)

    async def task_counts(self) -> TaskCounts:
        if self._application is None or self._application.tasks is None:
            return TaskCounts()
        return await self._application.tasks.counts()

    async def task_status(self) -> tuple[TaskCounts, str | None]:
        try:
            counts = await self.task_counts()
        except (PersistenceError, ValueError) as exc:
            return TaskCounts(), str(exc)
        scheduler = self._application.task_scheduler if self._application else None
        error = scheduler.diagnostics[0].message if scheduler and scheduler.diagnostics else None
        return counts, error

    async def _with_task_status(self, status: TuiSystemStatus) -> TuiSystemStatus:
        counts, error = await self.task_status()
        return replace(status, task_counts=counts, task_error=error)

    async def task(self, task_id: str) -> TaskRecord:
        return await self._tasks().get(task_id)

    async def save_task(
        self,
        configuration: TaskConfiguration,
        *,
        existing: TaskRecord | None = None,
    ) -> TaskRecord:
        if existing is None:
            return await self._tasks().create(configuration)
        return await self._tasks().update(
            existing.task_id, configuration, expected_revision=existing.revision
        )

    async def set_task_enabled(self, task: TaskRecord, enabled: bool) -> TaskRecord:
        return await self._tasks().set_enabled(
            task.task_id, enabled, expected_revision=task.revision
        )

    async def delete_task(self, task: TaskRecord) -> None:
        await self._tasks().delete(task.task_id, expected_revision=task.revision)

    async def task_executions(self, task_id: str) -> tuple[TaskExecution, ...]:
        return await self._tasks().executions(task_id)

    async def interrupt_task(self, task: TaskRecord) -> None:
        if task.active_execution_id is None:
            return
        if self._application is None or self._application.task_scheduler is None:
            raise RuntimeError("Task execution controls are unavailable")
        await self._application.task_scheduler.execution.interrupt(task)

    async def set_session_archived(self, session_id: str, *, archived: bool) -> None:
        sessions = self._runtime().sessions
        await asyncio.to_thread(sessions.set_archived, session_id, archived)
        if session_id == self._session_id:
            self._session_archived = archived

    async def rename_session(self, session_id: str, title: str) -> SessionView:
        sessions = self._runtime().sessions
        result = await asyncio.to_thread(sessions.rename, session_id, title)
        return result.session

    async def delete_sessions(
        self,
        session_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        removed = await asyncio.to_thread(self._delete_sessions, session_ids)
        if self._session_id in removed:
            self._session_id = None
            self._session_archived = False
        return removed

    def _delete_sessions(self, session_ids: tuple[str, ...]) -> tuple[str, ...]:
        sessions = self._runtime().sessions
        catalog = {entry.session_id: entry for entry in sessions.catalog().entries}
        selected = tuple(dict.fromkeys(session_ids))
        remaining = set(catalog)
        removed: list[str] = []
        for session_id in selected:
            if session_id not in remaining:
                continue
            result = sessions.trash_from_catalog(session_id)
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
                if is_editable_user_input(message)
            )
        )

    async def branch_session(self, message_id: str | None = None) -> SessionBranchResult:
        session_id = self._require_session()
        if message_id is None:
            return await asyncio.to_thread(self._branch_current_session, session_id)
        return await asyncio.to_thread(
            self._runtime().sessions.branch,
            session_id,
            message_id,
            before_input=True,
        )

    def _branch_current_session(self, session_id: str) -> SessionBranchResult:
        session = self._runtime().sessions.view(session_id)
        return self._runtime().sessions.branch(session_id, session.last_message_id)

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

    async def run_record(self, session_id: str, run_id: str) -> RunRecord:
        return await asyncio.to_thread(self._runtime().sessions.run_record, session_id, run_id)

    async def models(self) -> tuple[AvailableModel, ...]:
        runtime = self._runtime()
        return await asyncio.to_thread(lambda: tuple(runtime.available_models()))

    async def default_model(self) -> AvailableModel:
        """Resolve the deployment default independently of the open Session or draft."""
        return await asyncio.to_thread(self._runtime().current_model)

    async def selected_model_key(self) -> str | None:
        """Resolve the current draft or controlled Session's endpoint identity."""

        runtime = self._runtime()
        session_id, initial_model = self._session_id, self._initial_model

        def resolve() -> str:
            if session_id is None and initial_model is not None:
                return runtime.resolve_model(initial_model).key
            return runtime.current_model(session_id).key

        try:
            return await asyncio.to_thread(resolve)
        except ConfigError:
            return None

    async def configured_default_model(self) -> AvailableModel | None:
        try:
            return await asyncio.to_thread(self._runtime().configured_default_model)
        except ConfigError:
            return None

    async def provider_catalog(self) -> ProviderCatalogView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.provider_catalog)

    def provider_auth(self) -> ProviderAuthService:
        """Borrow the application's authentication use cases without owning their lifetime."""
        self._runtime()
        assert self._application is not None
        service = self._application.provider_auth
        if service is None:
            raise ConfigError("authentication management is unavailable for this Runtime")
        return service

    async def provider_templates(self) -> tuple[ProviderTemplateView, ...]:
        configuration = self._require_configuration()
        catalog = await asyncio.to_thread(configuration.provider_template_catalog)
        return tuple(catalog.templates)

    async def add_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        return await asyncio.to_thread(self._add_provider, draft)

    def _add_provider(self, draft: TuiProviderConfiguration) -> ProviderView:
        configuration = self._require_configuration()
        catalog = configuration.add_provider(
            display_name=draft.display_name,
            provider_type=draft.provider_type,
            protocol=draft.protocol,
            auth_driver=draft.auth_driver,
            base_url=draft.base_url,
            api_path=draft.api_path,
            api_key=draft.api_key,
            api_key_env=draft.api_key_env,
            timeout=draft.timeout,
            headers=draft.headers,
            provider_options=draft.provider_options,
            enabled=draft.enabled,
            make_default=draft.make_default,
        )
        provider_id = catalog.changed_provider_id
        assert provider_id is not None
        return next(
            provider for provider in catalog.providers if provider.provider_id == provider_id
        )

    async def update_provider(
        self,
        provider_id: str,
        draft: TuiProviderConfiguration,
    ) -> ProviderView:
        return await asyncio.to_thread(self._update_provider, provider_id, draft)

    async def update_provider_template(self, provider_id: str) -> ProviderCatalogView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.update_provider, provider_id)

    def _update_provider(
        self,
        provider_id: str,
        draft: TuiProviderConfiguration,
    ) -> ProviderView:
        configuration = self._require_configuration()
        current = configuration.provider(provider_id)
        if current.provider_type != draft.provider_type:
            raise ConfigError(
                "provider type cannot be changed by editing settings: "
                f"{current.provider_type} != {draft.provider_type}"
            )
        if draft.protocol is not None and current.protocol != draft.protocol:
            raise ConfigError(
                "provider protocol cannot be changed by editing settings: "
                f"{current.protocol} != {draft.protocol}"
            )
        catalog = configuration.update_provider_settings(
            provider_id,
            display_name=draft.display_name,
            base_url=draft.base_url,
            api_path=draft.api_path,
            api_key=draft.api_key,
            api_key_env=draft.api_key_env,
            timeout=draft.timeout,
            headers=draft.headers,
            provider_options=draft.provider_options,
            enabled=draft.enabled,
            make_default=draft.make_default,
        )
        return next(
            provider for provider in catalog.providers if provider.provider_id == provider_id
        )

    async def set_provider_enabled(self, provider_id: str, *, enabled: bool) -> ProviderView:
        configuration = self._require_configuration()

        def update() -> ProviderView:
            catalog = configuration.update_provider_settings(provider_id, enabled=enabled)
            return next(item for item in catalog.providers if item.provider_id == provider_id)

        return await asyncio.to_thread(update)

    async def remove_provider(self, provider_id: str) -> None:
        configuration = self._require_configuration()
        await asyncio.to_thread(configuration.remove_provider, provider_id)

    async def provider_endpoints(self, provider_id: str) -> ProviderEndpointCatalogView:
        configuration = self._require_configuration()
        return await asyncio.to_thread(configuration.endpoint_catalog, provider_id)

    async def add_endpoint(
        self,
        provider_id: str,
        draft: TuiEndpointConfiguration,
    ) -> ProviderEndpointView:
        return await asyncio.to_thread(self._add_endpoint, provider_id, draft)

    async def update_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        draft: TuiEndpointConfiguration,
    ) -> ProviderEndpointView:
        return await asyncio.to_thread(self._update_endpoint, provider_id, endpoint_id, draft)

    def _add_endpoint(
        self,
        provider_id: str,
        draft: TuiEndpointConfiguration,
    ) -> ProviderEndpointView:
        configuration = self._require_configuration()
        catalog = configuration.add_endpoint(
            provider_id,
            display_name=draft.display_name,
            model=draft.model_id,
            context_window=draft.context_window,
            max_output_tokens=draft.max_output_tokens,
            supports_tools=draft.supports_tools,
            supports_image_input=draft.supports_image_input,
            supports_audio_input=draft.supports_audio_input,
            supports_video_input=draft.supports_video_input,
            supports_streaming=draft.supports_streaming,
            supports_structured_output=draft.supports_structured_output,
            reasoning=draft.reasoning,
            request_options=draft.request_options,
        )
        endpoint_id = catalog.changed_endpoint_id
        assert endpoint_id is not None
        return next(
            endpoint for endpoint in catalog.endpoints if endpoint.endpoint_id == endpoint_id
        )

    def _update_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        draft: TuiEndpointConfiguration,
    ) -> ProviderEndpointView:
        configuration = self._require_configuration()
        changes: EndpointChanges = {
            "display_name": draft.display_name,
            "model_id": draft.model_id,
            "context_window": draft.context_window,
            "max_output_tokens": draft.max_output_tokens,
            "supports_tools": draft.supports_tools,
            "supports_image_input": draft.supports_image_input,
            "supports_audio_input": draft.supports_audio_input,
            "supports_video_input": draft.supports_video_input,
            "supports_streaming": draft.supports_streaming,
            "supports_structured_output": draft.supports_structured_output,
        }
        if draft.reasoning is not None:
            changes["reasoning"] = draft.reasoning
        if draft.request_options is not None:
            changes["request_options"] = draft.request_options
        catalog = configuration.update_endpoint(provider_id, endpoint_id, **changes)
        return next(
            endpoint for endpoint in catalog.endpoints if endpoint.endpoint_id == endpoint_id
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

    async def prompt_configuration(self) -> SystemPromptConfigView:
        return await asyncio.to_thread(self._require_configuration().system_prompt)

    async def save_prompt(
        self,
        kind: PromptKind,
        draft: PromptDraft,
        *,
        entry_id: str | None = None,
        expected_revision: int,
    ) -> SystemPromptConfigView:
        return await asyncio.to_thread(
            self._require_configuration().save_prompt,
            kind,
            draft,
            entry_id=entry_id,
            expected_revision=expected_revision,
        )

    async def remove_prompt(
        self, kind: PromptKind, entry_id: str, *, expected_revision: int
    ) -> SystemPromptConfigView:
        return await asyncio.to_thread(
            self._require_configuration().remove_prompt,
            kind,
            entry_id,
            expected_revision=expected_revision,
        )

    async def set_prompt_enabled(
        self, kind: PromptKind, entry_id: str, enabled: bool, *, expected_revision: int
    ) -> SystemPromptConfigView:
        return await asyncio.to_thread(
            self._require_configuration().set_prompt_enabled,
            kind,
            entry_id,
            enabled,
            expected_revision=expected_revision,
        )

    async def move_prompt(
        self, kind: PromptKind, entry_id: str, position: int, *, expected_revision: int
    ) -> SystemPromptConfigView:
        return await asyncio.to_thread(
            self._require_configuration().move_prompt,
            kind,
            entry_id,
            position,
            expected_revision=expected_revision,
        )

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
            saved = configuration.save_mcp_server(
                server_id,
                MCPServerConfigInput(enabled=enabled),
            )
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
        previous = (
            await asyncio.to_thread(configuration.mcp.get, draft.server_id)
            if draft.server_id is not None
            else None
        )
        saved, config = await asyncio.to_thread(self._save_mcp_configuration, draft)
        self._runtime()
        assert self._application is not None
        try:
            await self._application.reconcile_mcp_configuration(config)
        except Exception:
            if previous is None:
                await asyncio.to_thread(configuration.remove_mcp_server, saved.server_id)
                raise
            await asyncio.to_thread(
                configuration.save_mcp_server,
                previous.server_id,
                MCPServerConfigInput.model_validate(
                    previous.model_dump(mode="python", exclude={"server_id", "metadata"})
                ),
            )
            restored = configuration.mcp.get(previous.server_id)
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
        saved = configuration.save_mcp_server(draft.server_id, update)
        config = configuration.mcp.get(saved.server_id)
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

    async def transfer_data(self, request: DataTransferRequest) -> DataTransferResult:
        self._runtime()
        assert self._application is not None
        return await asyncio.to_thread(self._application.transfer_data, request)

    async def data_archive_kind(self, path: Path) -> DataKind:
        assert self._application is not None
        return await asyncio.to_thread(self._application.data_archive_kind, path)

    async def data_transfer_items(
        self, request: DataTransferRequest
    ) -> tuple[DataTransferItem, ...]:
        assert self._application is not None
        return await asyncio.to_thread(self._application.data_transfer_items, request)

    async def skill_catalog(self) -> SkillCatalogView:
        configuration = self._require_configuration()
        configured = await asyncio.to_thread(
            configuration.skill_catalog,
            include_disabled=True,
        )
        self._runtime()
        assert self._application is not None
        effective = await self._application.available_skills()
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
                for skill in await self._runtime().available_skills()
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
            if draft.model_key == DEFAULT_MODEL:
                model = self._runtime().configured_default_model()
            else:
                provider_id, separator, endpoint_id = draft.model_key.partition("/")
                if not separator or not provider_id or not endpoint_id:
                    raise ConfigError(f"invalid compaction model route: {draft.model_key}")
                model = self._runtime().resolve_model(draft.model_key)
            if draft.reasoning is not None:
                try:
                    resolve_reasoning(model.reasoning, draft.reasoning)
                except ValueError as exc:
                    raise ConfigError(f"invalid compaction reasoning override: {exc}") from exc
        return self._require_configuration().save_compaction(
            CompactionConfigInput(
                strategy_id=draft.strategy_id,
                fallback_strategy_id=draft.fallback_strategy_id,
                auto_enabled=draft.auto_enabled,
                trigger_ratio=draft.trigger_ratio,
                target_ratio=draft.target_ratio,
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                use_default_model=draft.model_key == DEFAULT_MODEL,
                temperature=draft.temperature,
                max_output_tokens=draft.max_output_tokens,
                reasoning=draft.reasoning,
            )
        )

    async def compact(self) -> CompactionCommandResult:
        return await self._runtime().compact(session_id=self._require_session())

    async def plugins(self) -> tuple[PluginSummary, ...]:
        application = self._application
        if application is None or application.plugins is None:
            return ()
        return tuple(await asyncio.to_thread(application.plugins.list))

    async def plugin(self, plugin_id: str) -> PluginDetail:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await asyncio.to_thread(application.plugins.show, plugin_id)

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

    async def plugin_configuration(
        self, plugin_id: str, *, package_digest: str | None = None
    ) -> PluginConfigurationView:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await asyncio.to_thread(
            application.plugins.configuration, plugin_id, package_digest=package_digest
        )

    async def preview_plugin_configuration(
        self, candidate: PluginConfigurationCandidate
    ) -> PluginConfigurationPreview:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await asyncio.to_thread(application.plugins.preview_configuration, candidate)

    async def configure_plugin(
        self,
        candidate: PluginConfigurationCandidate,
    ) -> PluginConfigurationResult:
        application = self._application
        if application is None or application.plugins is None:
            raise RuntimeError("plugin management is unavailable")
        return await application.plugins.configure(candidate)

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
        *,
        on_submitted: Callable[[], None] | None = None,
    ) -> TuiPluginCommandExecution | None:
        application = self._application
        if application is None or application.plugins is None:
            return None
        runtime = self._runtime()

        revision = self._selection_revision

        def session_created(session_id: str) -> None:
            if revision == self._selection_revision:
                self._session_id = session_id
                self._initial_model = None

        submission = await submit_plugin_command(
            application.plugins,
            runtime,
            selector,
            argument,
            session_id=self._session_id,
            workspace=self._draft_workspace or runtime.default_workspace,
            initial_model=self._initial_model,
            reasoning_request=self._reasoning_request,
            approval_mode=self._approval_mode,
            interaction_mode=self._interaction_mode,
            on_session_created=session_created,
        )
        if submission is None:
            return None
        # Presentation may refresh the committed state while input consumption runs.
        if on_submitted is not None:
            on_submitted()
        results = [result async for result in consume_plugin_command(runtime, submission)]
        return TuiPluginCommandExecution(
            execution=submission.execution,
            run_results=tuple(results),
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

    async def usage_summary(self) -> UsageSummary:
        return await asyncio.to_thread(self._usage_summary)

    def _usage_summary(self) -> UsageSummary:
        usage = self._runtime().usage
        return (
            usage.summarize(group_by=("date",))
            if usage is not None
            else UsageSummary(
                usage_filter=UsageFilter(),
                totals=UsageTotals(),
                group_by=("date",),
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
                session = runtime.sessions.open(session_id, recover=False).session
                current_model = session.current_model or runtime.current_model().key
                route_changed = model.key != current_model
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
        return _approval_preset(approval_mode, interaction_mode)

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

    async def session_summaries(self, session_id: str) -> tuple[SessionSummary, ...]:
        """Query summary history without changing the controlled Session."""

        return await asyncio.to_thread(self._runtime().sessions.summaries, session_id)

    async def load_session(self, session_id: str) -> LoadedSession:
        observation = await self.prepare_session_selection(session_id)
        try:
            self.activate_observation(observation)
            return observation.loaded
        finally:
            observation.subscription.close()

    async def prepare_session_selection(
        self, session_id: str, *, history_anchor: SessionHistoryAnchor | None = None
    ) -> TuiSessionObservation:
        """Prepare an explicitly selected control Session, including execution recovery."""
        runtime = self._runtime()
        detail = await asyncio.to_thread(runtime.sessions.open, session_id, recover=False)
        if (
            detail.session.session_kind == "primary"
            and detail.recent_run is not None
            and detail.recent_run.status == "running"
        ):
            assert self._application is not None
            await self._application.supervisor.recover_session(session_id)
        return await self.observe_session(session_id, history_anchor=history_anchor)

    def activate_observation(self, observation: TuiSessionObservation) -> None:
        """Commit a prepared selection only when its view is ready to take the surface."""
        self._selection_revision += 1
        self._creating_session = False
        self._session_id = observation.control_session.session_id
        self._session_archived = observation.control_session.archived
        self._initial_model = None

    async def observe_session(
        self, session_id: str, *, history_anchor: SessionHistoryAnchor | None = None
    ) -> TuiSessionObservation:
        """Prepare display facts without changing selection or recovering execution."""
        application = self._application
        if not self._started or application is None:
            raise RuntimeError("TUI controller is not started")
        subscription = await application.supervisor.observe_session(
            session_id, history_limit=200, history_anchor=history_anchor
        )
        try:
            loaded = await self.load_observation_snapshot(subscription.initial_snapshot)
            session = loaded.detail.session
            control = (
                session
                if session.session_kind == "primary"
                else await asyncio.to_thread(
                    application.runtime.sessions.view, session.agent_root_session_id
                )
            )
            if control.session_kind != "primary":
                raise PersistenceError("Observed Session does not have a primary control Session")
            return TuiSessionObservation(loaded, subscription, control)
        except BaseException:
            subscription.close()
            raise

    async def load_observation_snapshot(
        self, snapshot: SessionObservationSnapshot
    ) -> LoadedSession:
        return await asyncio.to_thread(self._loaded_snapshot, snapshot)

    async def history_window(
        self, session_id: str, *, before: int, history_epoch: int
    ) -> SessionHistoryWindow:
        return await asyncio.to_thread(
            self._runtime().sessions.history_window,
            session_id,
            before=before,
            history_epoch=history_epoch,
        )

    async def subscribe_agent_tree(self, root_session_id: str) -> AgentTreeSubscription | None:
        application = self._application
        if not self._started or application is None:
            raise RuntimeError("TUI controller is not started")
        if application.agents is None:
            return None
        return await application.agents.subscribe_agent_tree(root_session_id)

    async def agent_tree_snapshot(self, root_session_id: str) -> AgentTreeSnapshot | None:
        application = self._application
        if not self._started or application is None:
            raise RuntimeError("TUI controller is not started")
        if application.agents is None:
            return None
        return await asyncio.to_thread(application.agents.agent_tree_snapshot, root_session_id)

    async def resolve_agent_navigation(
        self,
        *,
        parent_session_id: str,
        parent_run_id: str,
        parent_tool_call_id: str,
        parent_batch_id: str | None = None,
    ) -> AgentTreeNode | None:
        return await asyncio.to_thread(
            self._agents().resolve_agent_navigation,
            parent_session_id=parent_session_id,
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
            parent_batch_id=parent_batch_id,
        )

    async def _unarchive_current_session_after_update(self) -> None:
        session_id = self._session_id
        if session_id is None or not self._session_archived:
            return
        sessions = self._runtime().sessions
        await asyncio.to_thread(sessions.set_archived, session_id, False)
        self._session_archived = False

    def _loaded_snapshot(self, snapshot: SessionObservationSnapshot) -> LoadedSession:
        session_id = snapshot.session.session_id
        runtime = self._runtime()
        try:
            reasoning = runtime.reasoning_for_session(snapshot.session)
        except ConfigError:
            reasoning = None
        return LoadedSession(
            detail=_observation_detail(snapshot),
            messages=snapshot.messages,
            events=snapshot.events,
            runs=snapshot.runs,
            active_tool_calls=snapshot.active_tool_calls,
            pending_interactions=snapshot.pending_interactions,
            current_request=snapshot.current_request,
            context_status=snapshot.context_status,
            effective_context_metrics=snapshot.effective_context_metrics,
            compactions=snapshot.compactions,
            queued_inputs=snapshot.queued_inputs,
            session_controls=self._session_control_statuses(session_id),
            history_before_cursor=snapshot.history_before_cursor,
            approval_preset=_approval_preset(
                snapshot.session.approval_mode, snapshot.session.interaction_mode
            ),
            reasoning_state=reasoning,
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

    def _accepts_event(self, event: EventRecord) -> bool:
        """Select child control notifications without admitting Session body events."""

        return (
            self._session_id is not None
            and event.session_id != self._session_id
            and event.event_type in _CHILD_CONTROL_EVENT_TYPES
        )

    async def _receive_event(self, event: EventRecord) -> None:
        """Cross one persisted Runtime event into Textual's message pump."""

        controlled_session_id = self._session_id
        revision = self._selection_revision
        # Selection can change while an accepted event is waiting in the mailbox.
        if not self._accepts_event(event):
            return
        if event.session_id != controlled_session_id:
            if (
                await asyncio.to_thread(
                    self._event_root_session_id,
                    event.session_id,
                )
                != controlled_session_id
            ):
                return
        if revision != self._selection_revision:
            return
        self._publish_event(event)

    def _event_root_session_id(self, session_id: str) -> str | None:
        cached = self._event_root_sessions.get(session_id)
        if cached is not None:
            return cached
        try:
            session = self._runtime().sessions.open(session_id, recover=False).session
        except FileNotFoundError, PersistenceError, SessionNotFoundError:
            return None
        root_session_id = session.agent_root_session_id
        self._event_root_sessions[session_id] = root_session_id
        return root_session_id


def _approval_preset(
    approval_mode: SessionApprovalMode, interaction_mode: SessionInteractionMode
) -> TuiApprovalPreset:
    return "afk" if approval_mode == "auto" and interaction_mode == "unattended" else approval_mode


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
