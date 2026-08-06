from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from pydantic import JsonValue
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.messages import Prune
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import TextArea

from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.queued_inputs import QueuedInputMode
from ouro.application.session_models import SessionCatalogEntry
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.approvals import SessionApprovalMode
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import REASONING_EFFORTS, ReasoningRequest
from ouro.core.results import RunResult
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication
from ouro.tui.commands import (
    TUI_COMMANDS,
    TuiCommand,
    TuiCommandArgument,
    argument_suggestion,
    matching_arguments,
    matching_commands,
    resolve_command,
)
from ouro.tui.configuration_workflows import ConfigurationWorkflowsApp
from ouro.tui.controller import (
    TUI_APPROVAL_PRESETS,
    LoadedSession,
    TuiApprovalPreset,
    TuiBootstrap,
    TuiController,
    TuiLifecycleStatus,
    TuiSystemStatus,
)
from ouro.tui.question_forms import project_question_form
from ouro.tui.screens import Choice, ChoiceScreen, HelpScreen, ModelConfigurationScreen
from ouro.tui.state import PendingInteraction, TuiSessionProjection, project_agent_interaction
from ouro.tui.widgets import (
    AskUserPanel,
    BrandHeader,
    CommandPopup,
    Composer,
    ConversationView,
    InteractionBar,
    QueuedInputPanel,
    StatusLine,
)


class RuntimeEventsReady(Message):
    """Signal that the UI-owned Runtime event buffer has a prefix to render."""


class LifecycleStatusReceived(Message):
    def __init__(self, status: TuiLifecycleStatus) -> None:
        super().__init__()
        self.status = status


_RUNTIME_RENDER_INTERVAL_SECONDS = 0.05
_RUNTIME_RENDER_BOUNDARY_EVENTS = {
    "run_completed",
    "run_aborted",
    "run_failed",
    "run_paused",
}
_AGENT_INTERACTION_EVENTS = {
    "agent_spawned",
    "agent_wait_requested",
    "agent_wait_resolved",
    "agent_result_ready",
    "agent_result_delivered",
    "agent_closed",
}
_CONFIGURE_MODEL = "__ouro_configure_model__"
_APPROVAL_DESCRIPTIONS: dict[TuiApprovalPreset, str] = {
    "inherit": "Use each tool's configured permission policy",
    "auto": "Allow host-controlled tools; keep user input interactive",
    "approval": "Ask before every host-controlled tool call",
    "deny": "Block every host-controlled tool call",
    "afk": "Auto-approve tools and resolve user input without waiting",
}

_REASONING_REQUESTS: dict[str, ReasoningRequest] = {
    "default": ReasoningRequest(),
    "enabled": ReasoningRequest(mode="enabled"),
    "disabled": ReasoningRequest(mode="disabled"),
    "minimal": ReasoningRequest(mode="enabled", effort="minimal"),
    "low": ReasoningRequest(mode="enabled", effort="low"),
    "medium": ReasoningRequest(mode="enabled", effort="medium"),
    "high": ReasoningRequest(mode="enabled", effort="high"),
    "xhigh": ReasoningRequest(mode="enabled", effort="xhigh"),
    "max": ReasoningRequest(mode="enabled", effort="max"),
}


class _OuroHostScreen(Screen):
    """Mark the Host as closing before Textual detaches its widget tree."""

    def __init__(self, on_prune: Callable[[], None]) -> None:
        super().__init__(id="_default")
        self._on_host_prune = on_prune

    async def on_prune(self, event: Prune) -> None:
        self._on_host_prune()
        await super().on_prune(event)


class OuroTuiApp(ConfigurationWorkflowsApp):
    TITLE = "OURO"
    SUB_TITLE = "Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    $ouro-background: #0c0c0c;
    $ouro-panel: #171717;
    $ouro-interaction: #5b8fd9;

    Screen {
        width: 100%;
        height: 100%;
        margin: 0;
        padding: 0;
        background: $ouro-background;
        color: #d6d9df;
        layout: vertical;
    }

    VerticalScroll, TextArea, OptionList {
        scrollbar-background: $ouro-background;
        scrollbar-color: #505967;
        scrollbar-color-hover: #6a7482;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
    }

    #brand-header {
        width: 100%;
        height: 1;
        dock: top;
        background: $ouro-background;
        color: #d6d9df;
    }

    #main {
        width: 100%;
        height: 1fr;
        background: $ouro-background;
    }

    #conversation {
        width: 100%;
        height: 1fr;
        padding: 1 1 0 2;
        background: $ouro-background;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    .welcome {
        width: 86;
        max-width: 100%;
        margin: 2 0;
        padding: 1 2;
        color: #aeb5bf;
        background: $ouro-background;
    }

    .turn {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }

    .user-turn {
        margin: 1 1 2 1;
        padding: 1 2;
        color: #e5e7eb;
        background: $ouro-panel;
        border-left: solid $ouro-interaction;
    }

    .assistant-turn {
        padding: 0 2;
    }

    .assistant-markdown {
        height: auto;
        background: transparent;
        padding: 0;
    }

    .reasoning-view {
        height: auto;
        margin: 0;
        padding: 0;
        color: #8f9aa8;
        background: transparent;
    }

    .reasoning-label {
        height: 1;
        color: #788492;
        text-style: bold;
    }

    .reasoning-content {
        height: auto;
        margin: 0 0 1 1;
        padding: 0 1;
        color: #8f9aa8;
        text-style: dim italic;
        background: transparent;
        border-left: solid #505967;
    }

    Markdown, MarkdownBlock, MarkdownFence, MarkdownTable {
        background: transparent;
    }

    .tool-row {
        height: auto;
        margin: 0 0 1 2;
        color: #aeb5bf;
        background: transparent;
    }

    .notice {
        height: auto;
        margin: 0 1 1 1;
        padding: 0 1;
        color: #d9a441;
        background: transparent;
    }

    .error-notice {
        color: #e08a8a;
    }

    #interaction-bar {
        height: auto;
        max-height: 10;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid #34373d;
        border-bottom: none;
        border-left: solid #4d5560;
    }

    #interaction-bar:focus {
        border-left: solid $ouro-interaction;
        border-right: solid #3f4650;
    }

    #ask-user-panel {
        height: auto;
        max-height: 18;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid #34373d;
        border-bottom: none;
        border-left: solid #4d5560;
    }

    #ask-user-panel:focus {
        border-left: solid $ouro-interaction;
        border-right: solid #3f4650;
    }

    #command-popup {
        height: auto;
        max-height: 14;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid #34373d;
        border-bottom: none;
        border-left: solid #4d5560;
        overflow-y: hidden;
        scrollbar-gutter: auto;
    }

    #command-popup-content {
        width: auto;
        height: auto;
    }

    #composer {
        height: 3;
        margin: 0 2;
        padding: 1 2;
        color: #e5e7eb;
        background: $ouro-panel;
        border-top: none;
        border-right: solid #34373d;
        border-bottom: none;
        border-left: solid #4d5560;
    }

    #composer:focus {
        border-left: solid $ouro-interaction;
        border-right: solid #3f4650;
    }

    #composer .text-area--suggestion {
        color: #626b78;
    }

    #queued-input-panel {
        height: auto;
        max-height: 8;
        margin: 0 1;
        padding: 0 0 1 1;
        color: #d6d9df;
        background: $ouro-background;
    }

    #queued-input-content {
        height: auto;
        background: $ouro-background;
    }

    StatusLine {
        height: 1;
        padding: 0 2;
        color: #6f7a87;
        background: $ouro-background;
    }

    #activity-indicator { width: 1fr; }
    #context-detail { width: auto; text-align: right; }
    #status-detail { width: auto; margin-left: 2; text-align: right; }

    ChoiceScreen, ModelConfigurationScreen, ToolExecutionConfigurationScreen,
    CompactionConfigurationScreen,
    MCPConfigurationScreen, SkillInstallScreen, ConfirmScreen, HelpScreen {
        align: center middle;
        background: $ouro-background 45%;
    }

    #choice-title {
        width: 72;
        max-width: 90%;
        height: 3;
        padding: 1 2 0 2;
        color: #d9a441;
        text-style: bold;
        background: #17191d;
        border-top: tall #7c6536;
    }

    #choice-filter {
        width: 72;
        max-width: 90%;
        background: #17191d;
        border: none;
        padding: 0 2;
    }

    #choice-list {
        width: 72;
        max-width: 90%;
        height: 16;
        background: #17191d;
        border-bottom: tall #7c6536;
        padding: 0 0 1 1;
    }

    #model-config-panel {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 95%;
        padding: 1 0 1 2;
        color: #d6d9df;
        background: #17191d;
        border: tall #7c6536;
    }

    #model-config-title {
        height: 2;
        color: #d9a441;
        text-style: bold;
    }

    .model-config-label {
        height: 1;
        margin-top: 1;
        color: #8f9aa8;
    }

    #model-config-panel Input, #model-config-panel Select {
        background: #202329;
        border: none;
    }

    .model-config-fields {
        height: 4;
        margin-top: 1;
    }

    .model-config-number-field {
        width: 1fr;
        height: 4;
        margin-right: 1;
    }

    .model-config-number-label {
        height: 1;
        color: #8f9aa8;
    }

    .model-config-capabilities {
        height: 3;
    }

    .model-config-capabilities Checkbox {
        width: 1fr;
    }

    #model-config-error {
        height: auto;
        min-height: 1;
        color: #e08a8a;
    }

    #model-config-hint {
        height: 1;
        color: #6f7a87;
    }

    .model-config-actions {
        height: 3;
        align-horizontal: right;
    }

    .model-config-actions Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }

    .configuration-panel {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 95%;
        padding: 1 0 1 2;
        color: #d6d9df;
        background: #17191d;
        border: tall #7c6536;
    }

    .mcp-configuration-panel { height: 95%; }

    .configuration-title {
        height: 2;
        color: #d9a441;
        text-style: bold;
    }

    .configuration-label {
        height: 1;
        margin-top: 1;
        color: #8f9aa8;
    }

    .configuration-panel Input, .configuration-panel Select {
        background: #202329;
        border: none;
    }

    .configuration-note {
        height: auto;
        margin-top: 1;
        color: #8f9aa8;
    }

    .configuration-error {
        height: auto;
        min-height: 1;
        color: #e08a8a;
    }

    .configuration-hint {
        height: 1;
        color: #6f7a87;
    }

    .configuration-actions {
        height: 3;
        align-horizontal: right;
    }

    .configuration-actions Button {
        width: auto;
        min-width: 12;
        margin-left: 1;
    }

    .configuration-checks { height: 3; margin-top: 1; }
    .configuration-checks Checkbox { width: 1fr; }
    .configuration-number-row { height: 4; margin-top: 1; }
    .configuration-number-field { width: 1fr; height: 4; margin-right: 1; }

    .confirm-panel {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 2 3;
        background: #17191d;
        border: tall #7c6536;
    }

    #help-panel {
        width: 72;
        max-width: 90%;
        height: auto;
        padding: 2 3;
        background: #17191d;
        border: tall #7c6536;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_session", "New session", show=False),
        Binding("ctrl+s", "sessions", "Sessions", show=False),
        Binding("ctrl+m", "models", "Model", show=False),
        Binding("ctrl+p", "actions", "Actions", show=False),
        Binding("ctrl+c", "abort", "Abort", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        system_prompt: str | None = None,
        session_backend: SessionBackend | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
        resume_session_id: str | None = None,
        application: LocalApplication | None = None,
        close_application: bool = True,
    ) -> None:
        super().__init__()
        self._controller = TuiController(
            self._publish_runtime_event,
            self._publish_lifecycle_status,
            home=home,
            system_prompt=system_prompt,
            session_backend=session_backend,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            application=application,
            close_application=close_application,
        )
        self._resume_session_id = resume_session_id
        self._projection: TuiSessionProjection | None = None
        self._interaction: PendingInteraction | None = None
        self._models: tuple[AvailableModel, ...] = ()
        self._sessions: tuple[SessionCatalogEntry, ...] = ()
        self._active_model = "starting"
        self._active_title = "New session"
        self._system_status = TuiSystemStatus()
        self._host_closing = False
        self._waiting_for_session_event = False
        self._dismissed_command_text: str | None = None
        self._pending_runtime_events: list[EventRecord] = []
        self._runtime_events_ready_posted = False
        self._runtime_events_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield BrandHeader()
        with Vertical(id="main"):
            yield ConversationView()
            yield CommandPopup()
            yield AskUserPanel()
            yield InteractionBar()
            yield Composer()
            yield StatusLine()
            yield QueuedInputPanel()

    def get_default_screen(self) -> Screen:
        return _OuroHostScreen(self._begin_host_close)

    def on_mount(self) -> None:
        self.query_one(StatusLine).set_activity("starting")
        self.query_one(Composer).disabled = True
        self.run_worker(self._start(), name="tui-start", exclusive=True)

    async def on_unmount(self) -> None:
        self._begin_host_close()
        await self._controller.close()

    def _begin_host_close(self) -> None:
        if self._host_closing:
            return
        self._host_closing = True
        if self._runtime_events_timer is not None:
            self._runtime_events_timer.stop()
            self._runtime_events_timer = None
        self.workers.cancel_all()

    async def _start(self) -> None:
        conversation = self.query_one(ConversationView)
        try:
            bootstrap = await self._controller.start()
        except Exception as exc:  # noqa: BLE001 - surface boundary failures in the Host.
            self.query_one(StatusLine).set_activity("failed")
            await conversation.add_notice(f"OURO could not start: {exc}", error=True)
            self.query_one(StatusLine).set_detail("startup failed")
            return
        self._apply_bootstrap(bootstrap)
        await self._refresh_reasoning_indicator()
        if self._resume_session_id is not None:
            try:
                await self._open_session(self._resume_session_id)
            except Exception as exc:  # noqa: BLE001 - invalid persisted selection is recoverable.
                await conversation.add_notice(f"Could not open session: {exc}", error=True)
                await conversation.show_welcome()
        else:
            await conversation.show_welcome()
        self._apply_system_status(bootstrap)
        composer = self.query_one(Composer)
        composer.disabled = False
        composer.focus()

    def _apply_bootstrap(self, bootstrap: TuiBootstrap) -> None:
        self._models = bootstrap.models
        self._sessions = bootstrap.sessions
        self._active_model = (
            bootstrap.current_model.display_name or bootstrap.current_model.model_id
        )
        header = self.query_one(BrandHeader)
        header.set_context(session_title=self._active_title)
        status_line = self.query_one(StatusLine)
        status_line.set_model(self._active_model)
        status_line.set_activity("idle")
        diagnostics = bootstrap.startup_report.diagnostics
        if diagnostics:
            self.query_one(StatusLine).set_detail(f"{len(diagnostics)} startup diagnostic(s)")
        else:
            self.query_one(StatusLine).set_detail("")

    def _apply_reasoning_state(self, state: ModelReasoningState) -> None:
        """Project the effective thinking level onto the status line."""
        if state.capability.activation == "unsupported":
            value, peak = None, False
        else:
            value = _reasoning_value(state.effective)
            peak = _reasoning_peak(state)
        self.query_one(StatusLine).set_reasoning(value, peak=peak)

    async def _refresh_reasoning_indicator(self) -> None:
        """Refresh the status line thinking level after a model or session change."""
        try:
            state = await self._controller.session_reasoning()
        except Exception:  # noqa: BLE001 - reasoning is optional status, not a blocker.
            return
        self._apply_reasoning_state(state)

    def _publish_runtime_event(self, event: EventRecord) -> None:
        if self._host_closing:
            return
        self._pending_runtime_events.append(event)
        if event.event_type in _RUNTIME_RENDER_BOUNDARY_EVENTS:
            if self._runtime_events_timer is not None:
                self._runtime_events_timer.stop()
                self._runtime_events_timer = None
            self._post_runtime_events_ready()
            return
        if not self._runtime_events_ready_posted and self._runtime_events_timer is None:
            self._runtime_events_timer = self.set_timer(
                _RUNTIME_RENDER_INTERVAL_SECONDS,
                self._post_runtime_events_ready,
            )

    def _post_runtime_events_ready(self) -> None:
        self._runtime_events_timer = None
        if self._host_closing or not self._pending_runtime_events:
            return
        if not self._runtime_events_ready_posted:
            self._runtime_events_ready_posted = self.post_message(RuntimeEventsReady())

    def _publish_lifecycle_status(self, status: TuiLifecycleStatus) -> None:
        if not self._host_closing:
            self.post_message(LifecycleStatusReceived(status))

    def on_lifecycle_status_received(self, message: LifecycleStatusReceived) -> None:
        self.query_one(StatusLine).set_activity(message.status.activity)
        self.query_one(StatusLine).set_detail(message.status.detail)

    def _apply_system_status(self, bootstrap: TuiBootstrap) -> None:
        self._system_status = bootstrap.system_status
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def on_runtime_events_ready(self, _message: RuntimeEventsReady) -> None:
        self._runtime_events_ready_posted = False
        if self._host_closing or not self.query(ConversationView):
            self._pending_runtime_events.clear()
            return
        await self._flush_pending_runtime_events()

    async def _flush_pending_runtime_events(self) -> None:
        if self._runtime_events_timer is not None:
            self._runtime_events_timer.stop()
            self._runtime_events_timer = None
        if not self._pending_runtime_events:
            return
        events = tuple(self._pending_runtime_events)
        self._pending_runtime_events.clear()
        await self._apply_runtime_events(events)

    async def _apply_runtime_events(
        self,
        events: Sequence[EventRecord],
        *,
        allow_new_projection: bool = False,
    ) -> None:
        if not events:
            return
        projection = self._projection
        if projection is None:
            if not self._waiting_for_session_event and not allow_new_projection:
                return
            projection = TuiSessionProjection(events[0].session_id)
            self._projection = projection
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()

        diagnostics: list[str] = []
        latest_update = None
        queue_changed = False
        refresh_agent_interactions = any(
            event.session_id != projection.session_id
            or event.event_type in _AGENT_INTERACTION_EVENTS
            for event in events
        )
        assistant_visible = bool(projection.assistant_text)
        conversation = self.query_one(ConversationView)
        stream_kind: str | None = None
        stream_parts: list[str] = []

        async def flush_stream() -> None:
            nonlocal stream_kind
            if not stream_parts:
                return
            text = "".join(stream_parts)
            stream_parts.clear()
            if stream_kind == "reasoning":
                await conversation.append_reasoning(text)
            else:
                await conversation.append_assistant(text)
            stream_kind = None

        async def append_stream(kind: str, text: str) -> None:
            nonlocal stream_kind
            if stream_kind is not None and stream_kind != kind:
                await flush_stream()
            stream_kind = kind
            stream_parts.append(text)

        for event in events:
            if event.event_type == "run_started":
                assistant_visible = False
            previous_tools = projection.activity.tool_calls
            update = projection.apply(event)
            if update is None:
                continue
            latest_update = update
            if event.event_type in {"user_message_added", "queued_input_consumed"}:
                await flush_stream()
                message_id = event.payload.get("message_id")
                if isinstance(message_id, str):
                    user_message = await self._controller.message(event.session_id, message_id)
                    if user_message.role == "user" and user_message.text_content:
                        await conversation.add_user(user_message.text_content)
                        assistant_visible = False
            if event.event_type in {
                "follow_up_input_queued",
                "steering_input_queued",
                "user_message_added",
                "queued_input_consumed",
            }:
                queue_changed = True
            if update.reasoning_delta:
                await append_stream("reasoning", update.reasoning_delta)
            if update.assistant_delta:
                await append_stream("assistant", update.assistant_delta)
                assistant_visible = True
            if update.round_break and assistant_visible:
                await append_stream("assistant", "\n\n")
            if update.activity.tool_calls != previous_tools:
                await flush_stream()
                await conversation.sync_tools(update.activity.tool_calls)
            if update.diagnostic:
                diagnostics.append(update.diagnostic)

        await flush_stream()

        if self._host_closing or not conversation.is_attached:
            return

        if latest_update is None:
            await self._refresh_interaction(refresh_agents=refresh_agent_interactions)
            return
        if latest_update.activity.activity != "streaming_reasoning":
            conversation.finish_reasoning()
        if latest_update.activity.status in {
            "completed",
            "aborted",
            "failed",
            "paused",
            "recoverable",
        }:
            await conversation.finish_assistant()
        if queue_changed:
            await self._sync_queued_inputs(latest_update.event.session_id)
        self._sync_activity(latest_update.activity)
        await self._refresh_interaction(refresh_agents=refresh_agent_interactions)
        for diagnostic in diagnostics:
            await conversation.add_notice(diagnostic, error=True)

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        interaction = self._interaction
        if interaction is not None and interaction.kind == "approval":
            self.query_one(InteractionBar).focus()
            return
        if interaction is not None and interaction.kind == "question":
            panel = self.query_one(AskUserPanel)
            if panel.display and panel.awaiting_freeform:
                panel.accept_freeform(message.text)
            return
        active_run = self._has_active_run()
        if not active_run:
            self.query_one(StatusLine).set_activity("queued")
        mode_label = "guide" if message.mode == "steer" else "follow-up"
        self.query_one(StatusLine).set_detail(
            "Submitting…" if self._projection is None else f"Queueing {mode_label}…"
        )
        if self._projection is None:
            self.query_one(Composer).disabled = True
            self._waiting_for_session_event = True
        self._run_prompt(message.text, message.mode)

    @work(group="run")
    async def _run_prompt(self, prompt: str, queue_mode: QueuedInputMode) -> None:
        try:
            result = await self._controller.submit(prompt, queue_mode=queue_mode)
            await self._apply_run_result(result)
            await self._sync_queued_inputs(result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - providers and tools fail across this boundary.
            self.query_one(StatusLine).set_activity("failed")
            self.query_one(StatusLine).set_detail("Run failed")
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
        finally:
            self._waiting_for_session_event = False
            if not self._host_closing and self.query(Composer):
                composer = self.query_one(Composer)
                interaction = self._interaction
                composer.disabled = interaction is not None and interaction.kind == "approval"
                if interaction is not None and interaction.kind == "approval":
                    self.query_one(InteractionBar).focus()
                elif interaction is not None and interaction.kind == "question":
                    panel = self.query_one(AskUserPanel)
                    if panel.awaiting_freeform:
                        composer.focus()
                    else:
                        panel.focus()
                else:
                    composer.focus()

    async def _apply_run_result(self, result: RunResult) -> None:
        await self._flush_pending_runtime_events()
        await self._apply_runtime_events(result.events, allow_new_projection=True)
        if self._projection is None or self._projection.session_id != result.session.session_id:
            self._projection = TuiSessionProjection(result.session.session_id)
        if result.final_text and not self._projection.assistant_text:
            self._projection.assistant_text = result.final_text
            conversation = self.query_one(ConversationView)
            await conversation.append_assistant(result.final_text)
            await conversation.finish_assistant()
        self._active_title = result.session.title
        self.query_one(BrandHeader).set_context(session_title=self._active_title)
        await self._refresh_usage_status()
        await self._refresh_interaction()

    async def _refresh_usage_status(self) -> None:
        usage = await self._controller.usage_totals()
        self._system_status = replace(
            self._system_status,
            usage_request_count=usage.request_count,
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
            usage_estimated_cost=usage.estimated_cost,
            usage_priced_request_count=usage.priced_request_count,
        )
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def _sync_queued_inputs(self, session_id: str | None = None) -> None:
        panel = self.query_one(QueuedInputPanel)
        if session_id is None and self._controller.session_id is None:
            panel.set_inputs(())
            return
        panel.set_inputs(await self._controller.queued_user_inputs(session_id))

    def on_text_area_changed(self, message: TextArea.Changed) -> None:
        if message.text_area is not self.query_one(Composer):
            return
        value = message.text_area.text
        message.text_area.suggestion = ""
        popup = self.query_one(CommandPopup)
        ask_user = self.query_one(AskUserPanel)
        if ask_user.display and ask_user.awaiting_freeform:
            popup.hide()
            return
        if value == self._dismissed_command_text:
            return
        self._dismissed_command_text = None
        if not value.startswith("/") or "\n" in value:
            popup.hide()
            return
        _command_text, separator, argument = value.partition(" ")
        if not separator:
            commands = matching_commands(value, self._command_catalog())
            popup.show_commands(tuple(sorted(commands, key=lambda item: item.name)))
            return
        command, _normalized_argument = resolve_command(value, self._command_catalog())
        if command is None or not command.takes_arguments:
            popup.hide()
            return
        arguments = matching_arguments(command, argument)
        popup.show_arguments(command, arguments)
        message.text_area.suggestion = argument_suggestion(
            command,
            argument,
            popup.selected_argument,
        )

    async def on_composer_command_submitted(self, message: Composer.CommandSubmitted) -> None:
        popup = self.query_one(CommandPopup)
        composer = self.query_one(Composer)
        interaction = self._interaction
        ask_user = self.query_one(AskUserPanel)
        if (
            interaction is not None
            and interaction.kind == "question"
            and ask_user.display
            and ask_user.awaiting_freeform
        ):
            composer.clear()
            popup.hide()
            self._dismissed_command_text = None
            ask_user.accept_freeform(message.text)
            return
        selected = popup.selected_command
        if selected is not None and selected.takes_arguments:
            self._edit_command(f"{selected.name} ")
            return
        argument_command = popup.argument_command
        selected_argument = popup.selected_argument
        if argument_command is not None and selected_argument is not None:
            _command_text, _separator, argument = message.text.partition(" ")
            head, head_separator, remainder = argument.strip().partition(" ")
            option_is_complete = head.casefold() == selected_argument.value.casefold()
            if not option_is_complete or (
                selected_argument.requires_value and (not head_separator or not remainder.strip())
            ):
                suffix = " " if selected_argument.requires_value else ""
                self._edit_command(f"{argument_command.name} {selected_argument.value}{suffix}")
                return
        value = selected.name if selected is not None else message.text
        composer.clear()
        popup.hide()
        self._dismissed_command_text = None
        await self._dispatch_command(value)
        if self.is_running:
            composer.focus()

    def on_composer_command_navigated(self, message: Composer.CommandNavigated) -> None:
        popup = self.query_one(CommandPopup)
        popup.navigate(message.offset)
        command = popup.argument_command
        if command is None:
            return
        composer = self.query_one(Composer)
        _command_text, _separator, argument = composer.text.partition(" ")
        composer.suggestion = argument_suggestion(
            command,
            argument,
            popup.selected_argument,
        )

    def on_composer_command_dismissed(self, _message: Composer.CommandDismissed) -> None:
        composer = self.query_one(Composer)
        self._dismissed_command_text = composer.text
        composer.suggestion = ""
        self.query_one(CommandPopup).hide()

    @work(exclusive=True, group="interaction")
    async def _resolve_approval(
        self,
        interaction: PendingInteraction,
        approved: bool,
    ) -> None:
        approval_id = interaction.request_id
        if approval_id is None:
            await self.query_one(ConversationView).add_notice(
                "Approval request has no identifier",
                error=True,
            )
            self.query_one(InteractionBar).resume()
            return
        try:
            result = await self._controller.resolve_approval(
                interaction=interaction,
                approved=approved,
            )
            if result is not None:
                await self._apply_run_result(result)
            await self._refresh_interaction()
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            self.query_one(InteractionBar).resume()

    @work(exclusive=True, group="interaction")
    async def _answer_question(self, interaction: PendingInteraction, answer: JsonValue) -> None:
        self.query_one(Composer).disabled = True
        try:
            result = await self._controller.answer(interaction=interaction, answer=answer)
            if result is not None:
                await self._apply_run_result(result)
            await self._refresh_interaction()
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            self.query_one(AskUserPanel).resume()
        finally:
            composer = self.query_one(Composer)
            composer.disabled = False
            current = self._interaction
            panel = self.query_one(AskUserPanel)
            if current is not None and current.kind == "question" and panel.display:
                if panel.awaiting_freeform:
                    composer.focus()
                else:
                    panel.focus()
            else:
                composer.focus()

    def on_interaction_bar_decision(self, message: InteractionBar.Decision) -> None:
        self._resolve_approval(message.interaction, message.approved)

    def on_ask_user_panel_completed(self, message: AskUserPanel.Completed) -> None:
        interaction = self._interaction
        if interaction is not None and interaction.kind == "question":
            self._answer_question(interaction, message.answer)

    def on_ask_user_panel_freeform_requested(
        self,
        _message: AskUserPanel.FreeformRequested,
    ) -> None:
        composer = self.query_one(Composer)
        composer.placeholder = "Type another answer…"
        composer.focus()

    def _sync_activity(self, activity: RuntimeActivitySnapshot) -> None:
        self.query_one(StatusLine).set_activity(activity.activity)
        detail = f"{len(activity.tool_calls)} tool(s)" if activity.tool_calls else ""
        status_line = self.query_one(StatusLine)
        status_line.set_context(activity.context_status)
        status_line.set_detail(detail)

    async def _refresh_interaction(
        self,
        *,
        refresh_agents: bool = False,
    ) -> PendingInteraction | None:
        primary = self._projection.pending_interaction if self._projection else None
        current_is_agent = self._interaction is not None and self._interaction.is_subagent
        agent_interactions = (
            await self._controller.agent_interactions()
            if primary is None and (refresh_agents or current_is_agent)
            else ()
        )
        interaction = primary or (
            project_agent_interaction(agent_interactions[0]) if agent_interactions else None
        )
        if interaction != self._interaction:
            self._interaction = interaction
            self._sync_interaction(interaction)
        return interaction

    def _sync_interaction(self, interaction: PendingInteraction | None) -> None:
        interaction_bar = self.query_one(InteractionBar)
        ask_user = self.query_one(AskUserPanel)
        composer = self.query_one(Composer)
        if interaction is not None and interaction.kind == "question":
            interaction_bar.show_interaction(None)
            ask_user.show_request(project_question_form(interaction.tool_name, interaction.request))
            composer.disabled = False
            composer.placeholder = "Choose an option above…"
            return
        ask_user.hide_panel()
        interaction_bar.show_interaction(interaction)
        composer.placeholder = "Ask anything…"
        if interaction is not None and interaction.kind == "approval":
            composer.disabled = True
            interaction_bar.focus()
        else:
            composer.disabled = False
            composer.focus()

    def _edit_command(self, value: str) -> None:
        composer = self.query_one(Composer)
        composer.load_text(value)
        composer.move_cursor((0, len(value)))
        composer.focus()

    async def _dispatch_command(self, value: str) -> None:
        command, argument = resolve_command(value)
        if command is None:
            self._run_plugin_command(value)
            return
        if command.name == "/quit":
            await self.action_quit()
        elif command.name == "/new":
            await self._new_session()
        elif command.name == "/sessions":
            self.action_sessions()
        elif command.name == "/fork":
            self._fork_session(argument or None)
        elif command.name == "/model":
            if argument.casefold() in {"add", "configure", "new"}:
                await self._configure_model()
            elif argument:
                await self._select_model(argument)
            else:
                self.action_models()
        elif command.name == "/approval":
            if argument:
                await self._set_approval_mode(argument.casefold())
            else:
                self.action_approval_settings()
        elif command.name == "/thinking":
            if argument:
                await self._set_reasoning(argument.casefold())
            else:
                self.action_thinking_settings()
        elif command.name == "/compact":
            if argument:
                await self.query_one(ConversationView).add_notice(
                    "Usage: /compact",
                    error=True,
                )
            else:
                self._compact_session()
        elif command.name == "/config":
            self._choose_configuration(argument or None)
        elif command.name == "/tools":
            self._choose_tools(argument or None)
        elif command.name == "/mcp":
            self._choose_mcp(argument or None)
        elif command.name == "/skills":
            self._choose_skills(argument or None)
        elif command.name == "/clear":
            await self._refresh_usage_status()
            await self.query_one(ConversationView).show_welcome()
        elif command.name == "/navigate":
            self._navigate_to(argument)
        elif command.name == "/abort":
            self.action_abort()
        elif command.name == "/help":
            self.action_help()

    def _command_catalog(self) -> tuple[TuiCommand, ...]:
        catalog = list(TUI_COMMANDS)
        navigate_options = self._navigate_options()
        for index, command in enumerate(catalog):
            if command.name == "/navigate":
                catalog[index] = TuiCommand(
                    name=command.name,
                    title=command.title,
                    description=command.description,
                    argument_hint=command.argument_hint,
                    aliases=command.aliases,
                    argument_options=navigate_options,
                )
                break
        return (*catalog, *self._plugin_commands())

    def _navigate_options(self) -> tuple[TuiCommandArgument, ...]:
        """Build /navigate argument options from the live dialogue state."""
        options = [
            TuiCommandArgument("top", "Jump to the first message"),
            TuiCommandArgument("bottom", "Jump to the latest message"),
        ]
        try:
            conversation = self.query_one(ConversationView)
        except Exception:  # noqa: BLE001 — app may not have mounted the view yet.
            return tuple(options)
        for index, widget in enumerate(conversation.query(".user-turn"), start=1):
            options.append(TuiCommandArgument(f"#{index}", _summarize(str(widget.render()))))
        return tuple(options)

    def _plugin_commands(self) -> tuple[TuiCommand, ...]:
        try:
            commands = self._controller.plugin_commands()
        except Exception:  # noqa: BLE001 - startup may not have completed yet.
            return ()
        name_counts = {
            item.name: sum(candidate.name == item.name for candidate in commands)
            for item in commands
        }
        matches: list[TuiCommand] = []
        for item in commands:
            short_name = f"/{item.name}"
            name = (
                short_name
                if name_counts[item.name] == 1 and resolve_command(short_name)[0] is None
                else f"/{item.command_id}"
            )
            command = TuiCommand(
                name=name,
                title=item.description or f"Plugin command · {item.plugin_id}",
                description=f"Plugin: {item.plugin_id}",
                argument_hint=item.usage or None,
                argument_options=tuple(
                    TuiCommandArgument(
                        value=option.value,
                        description=option.description,
                        argument_hint=option.usage or None,
                    )
                    for option in item.argument_options
                ),
            )
            matches.append(command)
        return tuple(matches)

    @work(exclusive=True, group="plugin-command")
    async def _run_plugin_command(self, value: str) -> None:
        command_text, _, argument = value.strip().partition(" ")
        selector = command_text.removeprefix("/")
        creating_projection = self._projection is None
        if creating_projection:
            self._waiting_for_session_event = True
            self.query_one(Composer).disabled = True
        try:
            result = await self._controller.execute_plugin_command(selector, argument.strip())
            if result is None:
                await self.query_one(ConversationView).add_notice(
                    f"Unknown command: {command_text}. Use /help to see available commands."
                )
                return
            execution = result.execution
            if execution.output:
                await self.query_one(ConversationView).add_notice(execution.output)
            elif execution.data:
                await self.query_one(ConversationView).add_notice(
                    json.dumps(execution.data, ensure_ascii=False, indent=2)
                )
            for run_result in result.run_results:
                await self._apply_run_result(run_result)
                await self._sync_queued_inputs(run_result.session.session_id)
            if (
                not result.run_results
                and self._projection is None
                and self._controller.session_id is not None
            ):
                await self._open_session(self._controller.session_id)
        except Exception as exc:  # noqa: BLE001 - plugin failures are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
        finally:
            if creating_projection:
                self._waiting_for_session_event = False
                composer = self.query_one(Composer)
                composer.disabled = False
                composer.focus()

    @work(exclusive=True, group="compaction")
    async def _compact_session(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before compacting context", severity="warning")
            return
        if self._controller.session_id is None:
            self.notify("No active session to compact")
            return
        composer = self.query_one(Composer)
        composer.disabled = True
        self.query_one(StatusLine).set_activity("compacting")
        self.query_one(StatusLine).set_detail("Compacting context…")
        try:
            result = await self._controller.compact()
            await self._flush_pending_runtime_events()
            await self._apply_runtime_events(result.events)
            await self._refresh_usage_status()
            if result.skipped or result.compaction is None:
                self.notify("No complete older turn is eligible for compaction")
            else:
                self.notify(
                    f"Context compacted: {len(result.compaction.covered_message_ids)} message(s)"
                )
        except Exception as exc:  # noqa: BLE001 - compaction errors are user-visible.
            self.query_one(StatusLine).set_activity("failed")
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
        finally:
            composer.disabled = False
            composer.focus()

    async def _new_session(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before starting a new session", severity="warning")
            return
        self._controller.new_session()
        self._projection = None
        self._interaction = None
        self._active_title = "New session"
        self.query_one(BrandHeader).set_context(session_title=self._active_title)
        self.query_one(StatusLine).set_activity("idle")
        self.query_one(StatusLine).set_context(None)
        self._sync_interaction(None)
        self.query_one(QueuedInputPanel).set_inputs(())
        await self.query_one(ConversationView).show_welcome()

    def action_new_session(self) -> None:
        self.run_worker(self._new_session(), exclusive=True, group="navigation")

    def action_sessions(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before switching sessions", severity="warning")
            return
        self._choose_session()

    def _navigate_to(self, target: str) -> None:
        """Scroll the conversation to the target identified by the /navigate argument."""
        conversation = self.query_one(ConversationView)
        if target == "top":
            conversation.scroll_home(animate=False)
        elif target == "bottom":
            conversation.anchor()
        elif target.startswith("#") and target[1:].isdigit():
            user_turns = list(conversation.query(".user-turn"))
            index = int(target[1:]) - 1
            if 0 <= index < len(user_turns):
                conversation.scroll_to_widget(user_turns[index], top=True, animate=False)

    @work(exclusive=True, group="navigation")
    async def _choose_session(self) -> None:
        try:
            self._sessions = await self._controller.sessions()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if not self._sessions:
            self.notify("No saved sessions yet")
            return
        choices = tuple(
            Choice(
                value=item.session_id,
                label=("★ " if item.pinned else "") + item.title,
                description=(
                    f"{item.status} · {item.current_model or 'default model'} · {item.session_id}"
                ),
            )
            for item in self._sessions
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(title="SESSIONS", placeholder="Filter sessions…", choices=choices)
        )
        if selected is not None:
            await self._open_session(selected)

    async def _open_session(self, session_id: str) -> None:
        loaded = await self._controller.load_session(session_id)
        await self.query_one(ConversationView).load_messages(loaded.messages)
        self.query_one(QueuedInputPanel).set_inputs(loaded.queued_inputs)
        await self._restore_projection(loaded)
        session = loaded.detail.session
        self._active_title = session.title
        self._active_model = session.current_model or self._active_model
        self.query_one(BrandHeader).set_context(session_title=self._active_title)
        self.query_one(StatusLine).set_model(self._active_model)
        await self._refresh_reasoning_indicator()

    @work(exclusive=True, group="navigation")
    async def _fork_session(self, message_id: str | None = None) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before forking the session", severity="warning")
            return
        if self._controller.session_id is None:
            self.notify("No active session to fork")
            return
        selected = message_id
        if selected is None:
            try:
                points = await self._controller.branch_points()
            except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return
            if not points:
                self.notify("No dialogue messages are available as fork points")
                return
            selected = await self.push_screen_wait(
                ChoiceScreen(
                    title="FORK SESSION",
                    placeholder="Filter canonical messages…",
                    choices=tuple(
                        Choice(
                            message.message_id,
                            _branch_point_label(message),
                            (
                                f"message {message.sequence} · {message.message_id} · "
                                "history through this boundary"
                            ),
                        )
                        for message in reversed(points)
                    ),
                )
            )
        if selected is None:
            return
        source_session_id = self._controller.session_id
        try:
            result = await self._controller.branch_session(selected)
            await self._open_session(result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - branch validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Session forked: {source_session_id} → {result.session.session_id}")

    async def _restore_projection(self, loaded: LoadedSession) -> None:
        projection = TuiSessionProjection(loaded.detail.session.session_id)
        projection.restore(loaded.events)
        self._projection = projection
        self._sync_activity(projection.activity)
        self._interaction = None
        await self._refresh_interaction(refresh_agents=True)
        self.run_worker(
            self.query_one(ConversationView).sync_tools(projection.activity.tool_calls),
            group="restore-tools",
            exclusive=True,
        )

    def action_models(self) -> None:
        self._choose_model()

    @work(exclusive=True, group="navigation")
    async def _choose_model(self) -> None:
        self._models = await self._controller.models()
        choices = (
            Choice(
                value=_CONFIGURE_MODEL,
                label="＋ Configure a new model",
                description="Add an endpoint to an existing provider",
            ),
            *tuple(
                Choice(
                    value=model.key,
                    label=model.display_name or model.model_id,
                    description=f"{model.provider_id} · {model.endpoint_id} · {model.key}",
                )
                for model in self._models
            ),
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(title="MODELS", placeholder="Filter models…", choices=choices)
        )
        if selected == _CONFIGURE_MODEL:
            await self._configure_model()
        elif selected is not None:
            await self._select_model(selected)

    async def _configure_model(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before configuring models", severity="warning")
            return
        try:
            providers = await self._controller.model_providers()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if not providers:
            await self.query_one(ConversationView).add_notice(
                "No enabled provider is configured. Configure a provider before adding models.",
                error=True,
            )
            return
        provider_choices = tuple(
            Choice(
                value=provider.provider_id,
                label=provider.provider_id,
                description=f"{provider.provider_type} · {provider.endpoint_count} endpoint(s)",
            )
            for provider in providers
        )
        configured = await self.push_screen_wait(ModelConfigurationScreen(provider_choices))
        if configured is None:
            return
        try:
            model = await self._controller.configure_model(configured)
        except Exception as exc:  # noqa: BLE001 - configuration validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._models = await self._controller.models()
        await self._select_model(model.key)

    async def _select_model(self, selector: str) -> None:
        try:
            model = await self._controller.select_model(selector)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._active_model = model.display_name or model.model_id
        self.query_one(StatusLine).set_model(self._active_model)
        await self._refresh_reasoning_indicator()
        self.query_one(StatusLine).set_context(None)
        self.notify(f"Model: {self._active_model}")

    def action_approval_settings(self) -> None:
        self._choose_approval_mode()

    @work(exclusive=True, group="navigation")
    async def _choose_approval_mode(self) -> None:
        if self._has_active_run():
            self.notify(
                "Abort the active run before changing approval settings",
                severity="warning",
            )
            return
        try:
            current = await self._controller.session_approval_preset()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        choices = tuple(
            Choice(
                value=mode,
                label=("● " if mode == current else "  ") + mode,
                description=_APPROVAL_DESCRIPTIONS[mode],
            )
            for mode in TUI_APPROVAL_PRESETS
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title="SESSION APPROVAL",
                placeholder="Filter approval modes…",
                choices=choices,
            )
        )
        if selected is not None:
            await self._set_approval_mode(selected)

    async def _set_approval_mode(self, value: str) -> None:
        if value not in TUI_APPROVAL_PRESETS:
            available = "|".join(TUI_APPROVAL_PRESETS)
            await self.query_one(ConversationView).add_notice(
                f"Usage: /approval <{available}>",
                error=True,
            )
            return
        try:
            mode = await self._controller.set_session_approval_preset(value)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Approval: {mode}")

    def action_thinking_settings(self) -> None:
        self._choose_reasoning()

    @work(exclusive=True, group="navigation")
    async def _choose_reasoning(self) -> None:
        if self._has_active_run():
            self.notify(
                "Abort the active run before changing thinking settings",
                severity="warning",
            )
            return
        try:
            state = await self._controller.session_reasoning()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        current = _reasoning_value(state.requested)
        choices = tuple(
            Choice(
                value=value,
                label=("● " if value == current else "  ") + value,
                description=_reasoning_description(value, state),
            )
            for value in _available_reasoning_values(state)
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title="MODEL THINKING",
                placeholder="Filter thinking modes…",
                choices=choices,
            )
        )
        if selected is not None:
            await self._set_reasoning(selected)

    async def _set_reasoning(self, value: str) -> None:
        request = _REASONING_REQUESTS.get(value)
        if request is None:
            available = "|".join(_REASONING_REQUESTS)
            await self.query_one(ConversationView).add_notice(
                f"Usage: /thinking <{available}>",
                error=True,
            )
            return
        try:
            state = await self._controller.set_session_reasoning(request)
        except Exception as exc:  # noqa: BLE001 - Runtime owns capability validation.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(_format_reasoning_state(state))
        self._apply_reasoning_state(state)

    def action_actions(self) -> None:
        self._choose_action()

    @work(exclusive=True, group="navigation")
    async def _choose_action(self) -> None:
        action_commands = tuple(
            command
            for name in (
                "/new",
                "/sessions",
                "/fork",
                "/model",
                "/approval",
                "/thinking",
                "/compact",
                "/config",
                "/abort",
                "/help",
            )
            if (command := resolve_command(name)[0]) is not None
        )
        choices = tuple(
            Choice(command.name, command.title, command.description) for command in action_commands
        )
        selected = await self.push_screen_wait(
            ChoiceScreen(title="ACTIONS", placeholder="Type an action…", choices=choices)
        )
        if selected is not None:
            await self._dispatch_command(selected)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_abort(self) -> None:
        if not self._has_active_run():
            self.notify("No active run to abort")
            return
        self._abort_run()

    async def action_quit(self) -> None:
        self.exit(0)

    def _has_active_run(self) -> bool:
        return self._projection is not None and self._projection.activity.status in {
            "queued",
            "running",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
        }

    @work(exclusive=True, group="abort")
    async def _abort_run(self) -> None:
        try:
            await self._controller.abort()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)


def _reasoning_value(request: ReasoningRequest) -> str:
    return request.effort or request.mode


_EFFORT_RANK = {effort: rank for rank, effort in enumerate(REASONING_EFFORTS)}


def _reasoning_peak(state: ModelReasoningState) -> bool:
    """Whether the effective effort is the highest the endpoint supports."""
    efforts = state.capability.supported_efforts
    effort = state.effective.effort
    if not efforts or effort is None:
        return False
    return effort is max(efforts, key=lambda candidate: _EFFORT_RANK[candidate])


def _available_reasoning_values(state: ModelReasoningState) -> tuple[str, ...]:
    capability = state.capability
    if capability.activation == "unsupported":
        return ("default",)
    activation = (
        ("default", "enabled", "disabled")
        if capability.activation == "toggle"
        else ("default", "enabled")
    )
    return (*activation, *capability.supported_efforts)


def _reasoning_description(value: str, state: ModelReasoningState) -> str:
    if value == "default":
        return f"Use endpoint default (effective: {_reasoning_value(state.effective)})"
    if value == "enabled":
        return "Enable thinking without overriding the endpoint effort"
    if value == "disabled":
        return "Disable thinking for future model turns"
    return f"Enable thinking with {value} effort"


def _format_reasoning_state(state: ModelReasoningState) -> str:
    requested = _reasoning_value(state.requested)
    effective = _reasoning_value(state.effective)
    return f"Thinking: {requested} (effective: {effective})"


def _branch_point_label(message: MessageRecord) -> str:
    speaker = "YOU" if message.role == "user" else "OURO"
    preview = " ".join(message.text_content.split())
    if not preview and message.tool_calls:
        preview = "tool request · " + ", ".join(call.name for call in message.tool_calls)
    if not preview:
        preview = "empty message"
    if len(preview) > 72:
        preview = preview[:69].rstrip() + "…"
    return f"{speaker} · {preview}"


def visible_transcript(messages: tuple[MessageRecord, ...]) -> tuple[MessageRecord, ...]:
    """Expose the transcript rule for focused unit tests."""

    return tuple(
        message
        for message in messages
        if message.role in {"user", "assistant"} and bool(message.text_content)
    )


def _summarize(text: str, limit: int = 60) -> str:
    """Collapse whitespace and truncate so user messages fit a single menu line."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"
