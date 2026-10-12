from __future__ import annotations

import asyncio
import json
import logging
import sys
from calendar import month_name
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Literal
from weakref import ReferenceType, ref

from pydantic import JsonValue
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.driver import Driver
from textual.geometry import Size
from textual.message import Message
from textual.messages import Prune
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import TextArea
from textual.worker import Worker

from ouro.application.agent_models import AgentTreeNode
from ouro.application.agent_tree import AgentTreeSubscription
from ouro.application.dto import Diagnostic
from ouro.application.event_models import RuntimeEventView
from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.queued_inputs import QueuedInputMode, QueuedUserInputView
from ouro.application.runtime_activity import overlay_run_activity
from ouro.application.session_models import SessionCatalogEntry, SessionSummary
from ouro.application.skill_activations import SkillInvocationRequest
from ouro.application.supervisor import RuntimeSubscription
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError, PersistenceError, UnsupportedDataVersionError
from ouro.core.events import EventRecord
from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord, SkillActivationContent
from ouro.core.reasoning import REASONING_EFFORTS, ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.state import DEFAULT_SESSION_TITLE
from ouro.deployment import SessionBackend, TaskBackend
from ouro.local_application import LocalApplication
from ouro.tui.attachment_editor import file_count_label
from ouro.tui.attachment_paths import pasted_files
from ouro.tui.attachment_picker import AttachmentPicker
from ouro.tui.clipboard import ClipboardWriteError, write_system_clipboard
from ouro.tui.commands import (
    REFERENCE_COMMANDS,
    TUI_COMMANDS,
    TuiCommand,
    TuiCommandArgument,
    argument_suggestion,
    matching_arguments,
    matching_commands,
    resolve_command,
)
from ouro.tui.configuration_workflows import (
    SYSTEM_SETTING_ACTIONS,
    ConfigurationWorkflowsApp,
)
from ouro.tui.controller import (
    TUI_APPROVAL_PRESETS,
    LoadedSession,
    TuiBootstrap,
    TuiController,
    TuiLifecycleStatus,
    TuiSystemStatus,
    project_mcp_system_status,
    project_usage_system_status,
)
from ouro.tui.conversation_history import (
    ConversationHistory,
    ConversationHistoryState,
    ToolNavigationRequested,
    UserMessageCopyRequested,
)
from ouro.tui.input_drafts import TuiInputDrafts, TuiInputDraftStore
from ouro.tui.menu_labels import MODEL_THINKING_LABEL, THINKING_LABELS, TOOL_APPROVAL_LABEL
from ouro.tui.menu_sorting import sorted_models
from ouro.tui.menu_styles import MENU_CSS
from ouro.tui.model_menus import (
    REASONING_REQUESTS,
    UNAVAILABLE_MODEL_LABEL,
    approval_choices,
    model_display_label,
    reasoning_choices,
    reasoning_values,
)
from ouro.tui.plan_confirmation import project_plan_confirmation
from ouro.tui.preferences import (
    MAX_RECENT_WORKSPACES,
    SessionGrouping,
    TuiPreferences,
    TuiPreferencesStore,
)
from ouro.tui.question_forms import project_question_form
from ouro.tui.screens import (
    Choice,
    ChoiceScreen,
    EditableChoiceScreen,
    HelpScreen,
    MenuDestination,
    MultiChoiceScreen,
    help_choices,
)
from ouro.tui.skill_picker import SkillPicker
from ouro.tui.state import (
    PendingInteraction,
    RunPresentation,
    TuiSessionProjection,
    canonical_event_order,
    describe_run_pause,
    project_agent_interaction,
    project_run_presentation,
    project_run_presentations,
)
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.theme import (
    DEFAULT_TUI_THEME,
    THEME_SATURATION_LEVELS,
    TUI_THEMES,
    TuiTheme,
)
from ouro.tui.welcome import WelcomeStyle, welcome_catalog
from ouro.tui.widgets import (
    COMMAND_POPUP_MAX_HEIGHT,
    COMMAND_POPUP_MIN_HEIGHT,
    COMPOSER_MAX_HEIGHT,
    COMPOSER_MIN_HEIGHT,
    INTERACTION_PANEL_MAX_HEIGHT,
    INTERACTION_PANEL_MIN_HEIGHT,
    AskUserPanel,
    BrandHeader,
    CommandPopup,
    Composer,
    ConversationView,
    InteractionBar,
    PlanConfirmationPanel,
    QueuedInputPanel,
    StatusLine,
    WelcomePanel,
    user_message_text,
)

_LOG = logging.getLogger(__name__)


class RuntimeEventsReady(Message):
    """Signal that the UI-owned Runtime event buffer has a prefix to render."""


class LifecycleStatusReceived(Message):
    def __init__(self, status: TuiLifecycleStatus) -> None:
        super().__init__()
        self.status = status


class SessionObservationLagged(Message):
    def __init__(self, *, session_id: str, diagnostic: Diagnostic) -> None:
        super().__init__()
        self.session_id = session_id
        self.diagnostic = diagnostic


_MCP_STATUS_SYNC_INTERVAL_SECONDS = 0.25
_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS = 0.3
_SESSION_TITLE_MAX_CELLS = 56
_RUNTIME_EVENT_BUFFER_MAX = 1000
_RUNTIME_EVENT_APPLY_MAX = 64
_SESSION_HISTORY_CACHE_SIZE = 3
_RUNTIME_RENDER_BOUNDARY_EVENTS = {
    "compaction_completed",
    "run_completed",
    "run_aborted",
    "run_failed",
    "run_paused",
}
_SESSION_CONTROL_REFRESH_EVENTS = {
    "tool_call_completed",
    *_RUNTIME_RENDER_BOUNDARY_EVENTS,
}
_SESSION_INPUT_MESSAGE_EVENTS = {
    "user_message_added",
    "host_message_added",
    "plugin_message_added",
}
_TOOL_PRESENTATION_BOUNDARY_EVENTS = {
    "run_started",
    "model_request_started",
    "model_reasoning_delta",
    "model_text_delta",
    "tool_batch_planned",
    "tool_batch_completed",
    "compaction_completed",
    "queued_input_consumed",
    *_SESSION_INPUT_MESSAGE_EVENTS,
}
_AGENT_INTERACTION_EVENTS = {
    "agent_spawned",
    "agent_wait_requested",
    "agent_wait_resolved",
    "agent_result_ready",
    "agent_result_delivered",
    "agent_closed",
}
_SESSION_GROUPING_ACTION = "__ouro_session_grouping__"
_THEME_ACTION = "__ouro_theme__"
_THOUGHT_DISPLAY_ACTION = "__ouro_thought_display__"
_WELCOME_STYLE_PREFIX = "welcome:"
_WELCOME_MOTION_ACTION = "__ouro_welcome_motion__"
_INPUT_DRAFT_MEMORY_ACTION = "__ouro_input_draft_memory__"
_RENAME_SESSION_ACTION = "__ouro_rename_session__"
_ARCHIVE_SESSION_ACTION = "__ouro_archive_session__"
_DELETE_SESSION_ACTION = "__ouro_delete_session__"
_SYSTEM_SETTING_ACTION_PREFIX = "__ouro_system_setting__:"
_RECENT_ACTION_PREFIX = "__ouro_recent_action__:"
_MENU_ACTIONS: MenuDestination = "actions"
_MENU_PLUGINS: MenuDestination = "plugins"
_MENU_SESSIONS: MenuDestination = "sessions"
_MENU_TASKS: MenuDestination = "tasks"
_SESSION_DOMAIN_LABELS = {
    "default": "USER",
    "task": "TASK",
    "subagent": "SUBAGENT",
    "archived": "ARCHIVED",
}
_SESSION_GROUPING_LABELS: dict[SessionGrouping, str] = {
    "date": "By date",
    "workspace": "By workspace",
}
_ACTION_GROUP_RANK = {
    group: rank
    for rank, group in enumerate(
        (
            "Settings",
            "Sessions",
            "Tasks",
            "Providers",
            "Plugins",
            "Tools",
            "Subagents",
            "MCPs",
            "Skills",
            "Support",
        )
    )
}
_THOUGHT_DISPLAY_OPTIONS: dict[str, bool] = {"expand": True, "collapse": False}
_THOUGHT_DISPLAY_LABELS = {
    "expand": "Show thinking",
    "collapse": "Hide thinking",
}
_INPUT_DRAFT_MEMORY_OPTIONS: dict[str, bool] = {"enable": True, "disable": False}
_INPUT_DRAFT_MEMORY_LABELS = {
    "enable": "Remember drafts",
    "disable": "Do not save drafts",
}
_CLEAR_WORKSPACE_HISTORY_ARGUMENT = "clear"


class _OuroHostScreen(Screen):
    """Own Host-level lifecycle and page-selection interactions."""

    def __init__(
        self,
        on_prune: Callable[[], None],
        on_resize: Callable[[Size], None] | None = None,
        *,
        clipboard_writer: Callable[[str], None] = write_system_clipboard,
    ) -> None:
        super().__init__(id="_default")
        self._on_host_prune = on_prune
        self._on_host_resize = on_resize
        self._clipboard_writer = clipboard_writer

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or not (selected_text := self.get_selected_text()):
            return
        event.prevent_default()
        event.stop()
        if await self._copy_to_clipboard(selected_text):
            self.clear_selection()

    async def on_user_message_copy_requested(self, message: UserMessageCopyRequested) -> None:
        message.stop()
        await self._copy_to_clipboard(message.text)

    async def _copy_to_clipboard(self, text: str) -> bool:
        try:
            await asyncio.to_thread(self._clipboard_writer, text)
        except ClipboardWriteError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return False
        self.notify("Copied to clipboard", markup=False)
        return True

    async def on_prune(self, event: Prune) -> None:
        self._on_host_prune()
        await super().on_prune(event)

    def on_resize(self, event: events.Resize) -> None:
        if self._on_host_resize is not None:
            self._on_host_resize(event.size)


_OURO_TUI_CSS = """
    /* Geometry remains fixed here; the owning app injects every visual token
       from its complete TuiTheme so CSS and Rich rendering share one source. */
    $conversation-left-gutter: 2;
    $conversation-right-gutter: 1;

    Screen {
        width: 100%;
        height: 100%;
        margin: 0;
        padding: 0;
        background: $ouro-background;
        color: $ouro-text-primary;
        layout: vertical;
    }

    /* Textual mounts notifications as screen-level system widgets. Keep that
       framework lifecycle, but project the chrome through the same frontend
       tokens as OURO's composer and menus. Severity changes only the rail
       color; geometry and surfaces remain stable. */
    ToastRack {
        dock: top;
        align: right top;
        margin-top: 1;
        margin-bottom: 0;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    Toast {
        width: auto;
        min-width: 28;
        max-width: 50%;
        height: auto;
        margin: 0 1 1 0;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-theme;
    }

    Toast.-information {
        border-left: solid $ouro-theme;
    }

    Toast.-warning {
        border-left: solid $ouro-warning-bright;
    }

    Toast.-error {
        border-left: solid $ouro-danger;
    }

    VerticalScroll, TextArea, OptionList {
        scrollbar-background: $ouro-background;
        scrollbar-color: $ouro-border;
        scrollbar-color-hover: $ouro-scrollbar-hover;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
    }

    #brand-header {
        width: 100%;
        height: 1;
        dock: top;
        background: $ouro-background;
        color: $ouro-text-primary;
    }

    #main {
        width: 100%;
        height: 1fr;
        background: $ouro-background;
    }

    #conversation {
        width: 100%;
        height: 1fr;
        padding: 1 $conversation-right-gutter 0 $conversation-left-gutter;
        background: $ouro-background;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #conversation-transcript {
        width: 100%;
        height: auto;
        min-height: 100%;
        background: transparent;
        /* Conversation rows are full-width, auto-height chronological blocks,
           exactly the contract of Textual's incremental stream layout. It
           reuses placements before the visible window instead of measuring the
           complete transcript whenever adjacent composer chrome changes. */
        layout: stream;
    }

    #conversation-history {
        width: 100%;
        height: auto;
        min-height: 0;
        background: transparent;
        overflow: hidden;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size: 0 0;
        scrollbar-gutter: auto;
    }

    #input-region {
        width: 100%;
        height: auto;
        margin: 0;
        padding: 0;
        color: $ouro-text-secondary;
        background: $ouro-background;
    }

    #main.welcome-page #conversation {
        display: none;
    }

    #input-region.welcome-page {
        height: 100%;
    }

    #input-region.welcome-page #composer,
    #input-region.welcome-page #command-popup,
    #input-region.welcome-page #subagent-navigation-panel,
    #input-region.welcome-page StatusLine {
        width: 100%;
        margin-left: 0;
        margin-right: 0;
    }

    #input-region.welcome-page StatusLine {
        padding-left: 0;
        padding-right: 0;
    }

    .turn {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }

    .assistant-turn {
        /* Match ConversationHistory's two-cell assistant Padding so the live
           tail and its frozen replacement wrap at the same content width. */
        padding: 0 2;
    }

    .assistant-turn.after-tool {
        /* Textual stores padding as one Spacing value: overriding only the
           top side clears the inline padding declared above. Restate all four
           sides so post-tool live turns keep the history content width. */
        padding: 1 2 0 2;
    }

    .assistant-markdown {
        width: 100%;
        height: auto;
        background: transparent;
        padding: 0;
    }

    .reasoning-view {
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
        color: $ouro-text-lowlight;
        background: transparent;
    }

    .reasoning-label {
        height: 1;
        color: $ouro-text-lowlight;
        text-style: none;
    }

    /* The reasoning body carries its │ border inside its strips, matching
       the immutable history's frozen rendering cell for cell. */
    .reasoning-lines {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1 0 0;
        color: $ouro-text-lowlight;
        background: transparent;
    }

    .reasoning-lines.collapsed {
        height: 0;
        margin-top: 0;
    }

    .tool-row {
        height: auto;
        margin: 0 2;
        color: $ouro-text-lowlight;
        background: transparent;
    }

    #subagent-navigation-panel {
        width: 100%;
        height: 1;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 0;
        color: $ouro-text-lowlight;
        background: $ouro-background;
        text-align: right;
        overflow: hidden;
    }

    .approval-panel {
        height: auto;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-separator;
    }

    .approval-panel:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    .approval-title {
        height: 1;
    }

    .approval-controls {
        height: auto;
    }

    #interaction-bar {
        min-height: $interaction-panel-min-height;
        max-height: $interaction-panel-max-height;
        overflow-y: auto;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #interaction-summary {
        height: auto;
        margin-bottom: 1;
    }

    #ask-user-panel {
        height: auto;
        max-height: 18;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-separator;
    }

    #ask-user-panel:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    #plan-confirmation-panel {
        max-height: 24;
        overflow: hidden;
    }

    #plan-confirmation-title {
        height: 1;
        margin-bottom: 1;
    }

    #plan-confirmation-document {
        height: auto;
        max-height: 12;
        overflow-y: auto;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #plan-confirmation-body {
        height: auto;
    }

    #plan-confirmation-controls {
        margin-top: 1;
    }

    #command-popup {
        height: auto;
        min-height: $command-popup-min-height;
        max-height: $command-popup-max-height;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-separator;
        overflow-y: hidden;
        scrollbar-gutter: auto;
    }

    #command-popup-heading {
        dock: top;
        width: 100%;
        height: 1;
    }

    #command-popup-content {
        width: 100%;
        height: auto;
    }

    /* The composer grows with its wrapped document. `height: auto` on a
       TextArea resolves through its virtual size, so Textual measures the
       text itself; the bounds below (border-box, so inclusive of the blank
       frame row above and below) cap it at three text rows and keep one row
       when empty. Past the cap the widget scrolls instead of growing. */
    #composer {
        height: auto;
        min-height: $composer-min-height;
        max-height: $composer-max-height;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2;
        color: $ouro-text-highlight;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-separator;
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    #composer:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    #composer.read-only-session:disabled {
        opacity: 1;
    }

    #composer .text-area--suggestion {
        color: $ouro-text-disabled;
    }

    #composer .text-area--cursor-line {
        background: transparent;
    }

    #queued-input-panel {
        height: auto;
        max-height: 8;
        margin: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        padding: 1 2 0 2;
        color: $ouro-text-primary;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: solid $ouro-separator;
    }

    #queued-input-content {
        width: 100%;
        height: auto;
        background: transparent;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }

    StatusLine {
        height: 1;
        margin: 0;
        padding: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        color: $ouro-text-lowlight;
        background: $ouro-background;
    }

    #activity-indicator { width: 1fr; }
    #context-detail { width: auto; text-align: right; }


"""


class OuroTuiApp(ConfigurationWorkflowsApp):
    _session_domain = "default"
    TITLE = "OURO"
    SUB_TITLE = "Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = _OURO_TUI_CSS + MENU_CSS

    BINDINGS = [
        Binding(
            "ctrl+n",
            "new_session",
            "New session",
            show=False,
            key_display="Ctrl+N",
            priority=True,
        ),
        Binding(
            "ctrl+s",
            "sessions",
            "Sessions",
            show=False,
            key_display="Ctrl+S",
            priority=True,
        ),
        Binding(
            "ctrl+t",
            "tasks",
            "Tasks",
            show=False,
            key_display="Ctrl+T",
            priority=True,
        ),
        Binding(
            "alt+p",
            "plugins",
            "Plugins",
            show=False,
            key_display="Alt+P",
            priority=True,
        ),
        Binding(
            "alt+t",
            "cycle_thinking",
            "Cycle model thinking",
            show=False,
            key_display="Alt+T",
            priority=True,
        ),
        Binding(
            "alt+a",
            "cycle_approval",
            "Cycle session approval",
            show=False,
            key_display="Alt+A",
            priority=True,
        ),
        Binding(
            "ctrl+p",
            "actions",
            "Actions",
            show=False,
            key_display="Ctrl+P",
            priority=True,
        ),
        Binding(
            "ctrl+up",
            "conversation_top",
            "Conversation top",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+down",
            "conversation_bottom",
            "Conversation bottom",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+left",
            "previous_user_message",
            "Previous user message",
            show=False,
            priority=True,
        ),
        Binding(
            "ctrl+right",
            "next_user_message",
            "Next user message",
            show=False,
            priority=True,
        ),
        Binding("alt+left", "agent_node_left", "Previous subagent", show=False, priority=True),
        Binding("alt+right", "agent_node_right", "Next subagent", show=False, priority=True),
        Binding("alt+up", "agent_node_root", "Main session", show=False, priority=True),
        Binding("alt+down", "agent_node_latest", "Latest subagent", show=False, priority=True),
        Binding(
            "ctrl+c",
            "abort",
            "Stop task",
            show=False,
            key_display="Ctrl+C",
            priority=True,
        ),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+h", "help", "Help", show=False, key_display="Ctrl+H", priority=True),
    ]

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        system_prompt: str | None = None,
        session_backend: SessionBackend | None = None,
        task_backend: TaskBackend | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
        resume_session_id: str | None = None,
        theme: TuiTheme | None = None,
        welcome_styles: tuple[WelcomeStyle, ...] = (),
        application: LocalApplication | None = None,
        close_application: bool = True,
    ) -> None:
        preference_home = (
            application.runtime.environment.home
            if home is None and application is not None
            else home
        )
        self._welcome_styles = welcome_catalog(welcome_styles)
        self._preferences_store = TuiPreferencesStore(
            preference_home, welcome_styles=self._welcome_styles
        )
        self._preferences_error: str | None = None
        try:
            preferences = self._preferences_store.load()
        except OuroError as exc:
            preferences = TuiPreferences()
            self._preferences_error = str(exc)
        self._preferences = preferences
        self._input_draft_store = TuiInputDraftStore(preference_home)
        self._input_drafts_error: str | None = None
        try:
            input_drafts = (
                self._input_draft_store.load()
                if preferences.input_draft_memory_enabled
                else TuiInputDrafts()
            )
        except OuroError as exc:
            input_drafts = TuiInputDrafts()
            self._input_drafts_error = str(exc)
        self._input_drafts = input_drafts
        theme_source = theme if theme is not None else TUI_THEMES[preferences.theme]
        self._tui_theme_saturation_level = (
            theme_source.saturation_level()
            if theme is not None
            else preferences.theme_saturation_level
        )
        self._tui_theme = (
            theme_source.at_saturation(theme_source.saturation)
            if theme is not None
            else theme_source.at_saturation_level(self._tui_theme_saturation_level)
        )
        super().__init__()
        self.console.push_theme(self._tui_theme.rich_theme())
        self._controller = TuiController(
            self._publish_runtime_event,
            self._publish_lifecycle_status,
            home=home,
            system_prompt=system_prompt,
            session_backend=session_backend,
            task_backend=task_backend,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            application=application,
            close_application=close_application,
        )
        self.startup_data_error: PersistenceError | None = None
        self._resume_session_id = resume_session_id
        self._projection: TuiSessionProjection | None = None
        self._activity_override: str | None = None
        self._history_edit_in_progress = False
        self._visible_read_only = False
        self._visible_agent: AgentTreeNode | None = None
        self._observation_subscription: RuntimeSubscription | None = None
        self._agent_tree_subscription: AgentTreeSubscription | None = None
        self._agent_tree_revision = 0
        self._agent_nodes: dict[str, AgentTreeNode] = {}
        self._agent_root_status = "idle"
        self._agent_switch_in_progress = False
        self._pending_agent_switch: tuple[str, AgentTreeNode | None] | None = None
        self._controlled_composer_draft: str | None = None
        self._held_session_scroll_y: dict[str, float] = {}
        self._session_history_states: OrderedDict[str, ConversationHistoryState] = OrderedDict()
        self._interaction: PendingInteraction | None = None
        self._models: tuple[AvailableModel, ...] = ()
        self._sessions: tuple[SessionCatalogEntry, ...] = ()
        self._active_model = "starting"
        self._active_title = DEFAULT_SESSION_TITLE
        self._active_workspace: str | None = None
        self._reasoning_state: ModelReasoningState | None = None
        self._system_status = TuiSystemStatus()
        self._welcome_refresh_lock = asyncio.Lock()
        self._welcome_refresh_requested: Literal["usage", "system"] | None = None
        self._host_closing = False
        self._waiting_for_session_event = False
        self._dismissed_command_text: str | None = None
        self._attachment_picker: AttachmentPicker | None = None
        self._skill_picker: SkillPicker | None = None
        self._pending_runtime_events: list[EventRecord] = []
        self._runtime_event_buffer_lagged: set[str] = set()
        self._runtime_events_ready_posted = False
        self._runtime_events_frame_scheduled = False
        self._runtime_events_worker: Worker[None] | None = None
        self._runtime_event_flush_lock = asyncio.Lock()
        self._runtime_event_apply_lock = asyncio.Lock()
        self._session_controls_refresh_requested = False
        self._session_controls_refresh_task: asyncio.Task[None] | None = None
        self._mcp_status_timer: Timer | None = None
        self._input_surface: WelcomePanel | None = None
        self._input_draft_revision = 0
        self._input_draft_save_task: asyncio.Task[None] | None = None
        self._input_draft_error_notified = False
        self._preferences_save_lock = asyncio.Lock()
        self._pending_tui_theme_id: str | None = None
        self._pending_tui_theme_saturation_level: int | None = None
        self._theme_switch_revision = 0
        self._theme_switch_active = False
        self._theme_switch_changed = asyncio.Event()
        self._theme_picker_ref: ReferenceType[ChoiceScreen] | None = None
        self._theme_picker_revision = 0

    @property
    def tui_theme(self) -> TuiTheme:
        return self._tui_theme

    def get_css_variables(self) -> dict[str, str]:
        return {
            **super().get_css_variables(),
            **self._tui_theme.css_variables(),
            "command-popup-min-height": str(COMMAND_POPUP_MIN_HEIGHT),
            "command-popup-max-height": str(COMMAND_POPUP_MAX_HEIGHT),
            "interaction-panel-min-height": str(INTERACTION_PANEL_MIN_HEIGHT),
            "interaction-panel-max-height": str(INTERACTION_PANEL_MAX_HEIGHT),
            "composer-min-height": str(COMPOSER_MIN_HEIGHT),
            "composer-max-height": str(COMPOSER_MAX_HEIGHT),
        }

    def compose(self) -> ComposeResult:
        header = BrandHeader(
            initial_context=self._active_title,
            initial_workspace=self._active_workspace,
        )
        header.display = False
        yield header
        with Vertical(id="main"):
            yield ConversationView(
                reasoning_expanded=self._preferences.reasoning_expanded,
                welcome_state_changed=self._set_welcome_mode,
                system_status_changed=self._set_welcome_system_status,
            )
            self._input_surface = WelcomePanel(
                welcome_style=self._welcome_styles[self._preferences.welcome_style],
                welcome_motion=self._preferences.welcome_motion,
            )
            yield self._input_surface

    def get_driver_class(self) -> type[Driver]:
        driver_class = super().get_driver_class()
        if sys.platform == "win32":
            from textual.drivers.windows_driver import WindowsDriver

            if driver_class is WindowsDriver:
                from ouro.tui.windows_driver import OuroWindowsDriver

                return OuroWindowsDriver
        return driver_class

    def get_default_screen(self) -> Screen:
        return _OuroHostScreen(self._begin_host_close, self._resize_input_surface)

    def _resize_input_surface(self, size: Size) -> None:
        if self._input_surface is not None:
            self._input_surface.set_viewport_size(size)

    async def on_event(self, event: events.Event) -> None:
        # A terminal drop is a Paste even when AppBlur has cleared widget focus.
        # Recognize files before Textual routes the event to the focused widget.
        if (
            isinstance(event, events.Paste)
            and not event.is_forwarded
            and self._surface_available()
            and len(self.screen_stack) == 1
        ):
            composer = self.query_one(Composer)
            if (
                composer.attachments_enabled
                and not composer.disabled
                and not composer.read_only
                and not composer.import_pending
            ):
                revision = self._controller.selection_revision
                paths = await asyncio.to_thread(pasted_files, event.text)
                if paths:
                    if not composer.consume_paste(event):
                        return
                    if (
                        revision != self._controller.selection_revision
                        or not self._surface_available()
                        or len(self.screen_stack) != 1
                        or self._visible_read_only
                        or self._interaction is not None
                        or composer.import_pending
                    ):
                        self.notify("Input changed while checking files; drop the files again")
                        return
                    self.app_focus = True
                    self._close_attachment_menu()
                    composer.import_pending = True
                    self._attach_files(paths)
                    return
        await super().on_event(event)

    def on_mount(self) -> None:
        self.query_one(StatusLine).set_activity("starting")
        composer = self.query_one(Composer)
        popup = self.query_one(CommandPopup)
        composer.set_suggestion_resolver(lambda: self._command_suggestion(composer, popup))
        composer.set_command_matcher(lambda: self._matches_command(composer))
        composer.disabled = True
        self._mcp_status_timer = self.set_interval(
            _MCP_STATUS_SYNC_INTERVAL_SECONDS,
            self._sync_mcp_status,
            pause=True,
        )
        _worker: Worker[None] = self.run_worker(self._start, name="tui-start", exclusive=True)
        if self._preferences_error is not None:
            self.notify(self._preferences_error, severity="warning")
        if self._input_drafts_error is not None:
            self._input_draft_error_notified = True
            self.notify(self._input_drafts_error, severity="warning", markup=False)

    async def on_unmount(self) -> None:
        self._begin_host_close()
        if self._session_controls_refresh_task is not None:
            await asyncio.gather(
                self._session_controls_refresh_task,
                return_exceptions=True,
            )
        if self._input_draft_save_task is not None:
            await self._input_draft_save_task
        await self._controller.close()

    def _surface_available(self) -> bool:
        return (
            not self._host_closing
            and bool(self.query(ConversationView))
            and bool(self.query(StatusLine))
        )

    def _begin_host_close(self) -> None:
        if self._host_closing:
            return
        self._host_closing = True
        if self._observation_subscription is not None:
            self._observation_subscription.close()
            self._observation_subscription = None
        if self._agent_tree_subscription is not None:
            self._agent_tree_subscription.close()
            self._agent_tree_subscription = None
        self._agent_nodes.clear()
        self._runtime_events_frame_scheduled = False
        self._session_controls_refresh_requested = False
        if self._session_controls_refresh_task is not None:
            self._session_controls_refresh_task.cancel()
        if self._mcp_status_timer is not None:
            self._mcp_status_timer.stop()
            self._mcp_status_timer = None
        self.workers.cancel_all()

    async def _start(self) -> None:
        try:
            await self._start_application()
        except PersistenceError as exc:
            self.startup_data_error = exc
            self.exit(1)

    async def _start_application(self) -> None:
        session_load_error: tuple[str, Exception] | None = None
        try:
            bootstrap = await self._controller.start()
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface boundary failures in the Host.
            if self._surface_available():
                self.query_one(StatusLine).set_activity("failed")
                self.notify(f"OURO could not start: {exc}", severity="error", markup=False)
            return
        if not self._surface_available():
            return
        self._apply_bootstrap(bootstrap)
        self._apply_system_status(bootstrap)
        self.set_interval(2.0, self._sync_task_status)
        if bootstrap.system_status.workspace is not None:
            await self._remember_workspace(bootstrap.system_status.workspace)
        await self._refresh_reasoning_indicator()
        if self._resume_session_id is not None:
            try:
                await self._open_session(self._resume_session_id)
            except UnsupportedDataVersionError:
                raise
            except Exception as exc:  # noqa: BLE001 - invalid persisted selection is recoverable.
                await self._show_welcome(refresh_status=False)
                session_load_error = (self._resume_session_id, exc)
        else:
            await self._show_welcome(refresh_status=False)
        await self._refresh_approval_indicator()
        if bootstrap.mcp_servers and self._mcp_status_timer is not None:
            self._mcp_status_timer.resume()
        composer = self.query_one(Composer)
        with self.batch_update():
            composer.display = True
            composer.disabled = self._visible_read_only
            if not self._visible_read_only:
                composer.focus()
        if session_load_error is not None:
            self._show_session_load_error(*session_load_error)

    def _apply_bootstrap(self, bootstrap: TuiBootstrap) -> None:
        self._models = bootstrap.models
        self._sessions = bootstrap.sessions
        self._active_model = _status_model_label(bootstrap.current_model)
        status_line = self.query_one(StatusLine)
        status_line.set_model(self._active_model)
        status_line.set_activity("idle")

    def _apply_reasoning_state(self, state: ModelReasoningState) -> None:
        """Project the effective thinking level onto the status line.

        The label appears only when reasoning is effectively on. An enabled
        intent without an effort is shown as ``thinking``; ``disabled`` and
        unresolved ``default`` intents hide the indicator so the status line
        falls back to the model endpoint alone.
        """
        if not self._surface_available():
            return
        self._reasoning_state = state
        if state.capability.activation == "unsupported" or state.effective.mode != "enabled":
            value, peak = None, False
        else:
            value = state.effective.effort or "thinking"
            peak = _reasoning_peak(state)
        self.query_one(StatusLine).set_reasoning(value, peak=peak)

    async def _refresh_reasoning_indicator(self) -> None:
        """Refresh the status line thinking level after a model or session change."""
        self._reasoning_state = None
        try:
            state = await self._controller.session_reasoning()
        except Exception:  # noqa: BLE001 - reasoning is optional status, not a blocker.
            if self._surface_available():
                self.query_one(StatusLine).set_reasoning(None)
            return
        if not self._surface_available():
            return
        self._apply_reasoning_state(state)

    async def _refresh_session_controls(self) -> None:
        """Project the current durable harness state onto the status line."""

        if self._host_closing or not self.query(StatusLine):
            return
        if self._visible_read_only:
            status_line = self.query_one(StatusLine)
            status_line.set_subagent(self._visible_agent)
            status_line.set_session_controls(())
            return
        session_id = self._controller.session_id
        try:
            statuses = await self._controller.session_control_statuses()
        except Exception:  # noqa: BLE001 - status projection must not interrupt the Session.
            return
        if (
            not self._surface_available()
            or self._visible_read_only
            or self._controller.session_id != session_id
        ):
            return
        status_line = self.query_one(StatusLine)
        status_line.set_subagent(None)
        status_line.set_session_controls(tuple(status.label for status in statuses))

    def _schedule_session_controls_refresh(self) -> None:
        if self._host_closing or self._visible_read_only:
            return
        self._session_controls_refresh_requested = True
        task = self._session_controls_refresh_task
        if task is None or task.done():
            self._session_controls_refresh_task = asyncio.create_task(
                self._drain_session_controls_refresh(),
                name="tui-session-controls-refresh",
            )

    async def _drain_session_controls_refresh(self) -> None:
        try:
            while self._session_controls_refresh_requested and not self._host_closing:
                self._session_controls_refresh_requested = False
                await self._refresh_session_controls()
        finally:
            self._session_controls_refresh_task = None

    def _publish_runtime_event(self, event: EventRecord) -> None:
        """Accept fallback sink events not already owned by the visible subscription."""

        if self._host_closing:
            return
        subscription = self._observation_subscription
        if (
            subscription is not None
            and not subscription.closed
            and subscription.session_id == event.session_id
        ):
            return
        if not self._queue_runtime_event(event):
            self._signal_runtime_event_buffer_lag(event.session_id)

    def _queue_runtime_event(self, event: EventRecord) -> bool:
        if len(self._pending_runtime_events) >= _RUNTIME_EVENT_BUFFER_MAX:
            return False
        self._pending_runtime_events.append(event)
        if event.event_type in _RUNTIME_RENDER_BOUNDARY_EVENTS:
            self._post_runtime_events_ready()
            return True
        if not self._runtime_events_ready_posted and not self._runtime_events_frame_scheduled:
            # The compositor already owns the display cadence. Schedule one
            # prefix per native frame instead of undersampling it with a
            # second fixed-period timer.
            self._runtime_events_frame_scheduled = self.call_after_refresh(
                self._post_runtime_events_ready,
            )
        return True

    def _signal_runtime_event_buffer_lag(self, session_id: str) -> None:
        if session_id in self._runtime_event_buffer_lagged or self._host_closing:
            return
        self._runtime_event_buffer_lagged.add(session_id)
        self.post_message(
            SessionObservationLagged(
                session_id=session_id,
                diagnostic=Diagnostic(
                    code="tui_runtime_event_buffer_lagged",
                    severity="warning",
                    component="tui.session_observation",
                    message="TUI event buffer overflowed; reload the durable Session snapshot",
                    session_id=session_id,
                    recoverable=True,
                    suggested_action="Reload the visible Session observation.",
                ),
            )
        )

    def _replace_observation(self, subscription: RuntimeSubscription | None) -> None:
        if self._host_closing:
            if subscription is not None:
                subscription.close()
            return
        previous = self._observation_subscription
        if previous is not None and previous is not subscription:
            previous.close()
        self._observation_subscription = subscription
        if subscription is None:
            return

        async def consume() -> None:
            await self._consume_observation(subscription)

        _worker: Worker[None] = self.run_worker(
            consume,
            name=f"observe-{subscription.session_id}",
            group="session-observation",
            exclusive=True,
        )

    async def _consume_observation(self, subscription: RuntimeSubscription) -> None:
        try:
            async for notification in subscription:
                if self._host_closing or self._observation_subscription is not subscription:
                    return
                if notification.kind == "event" and notification.event is not None:
                    if not self._queue_runtime_event(_event_record(notification.event)):
                        self._signal_runtime_event_buffer_lag(subscription.session_id)
                        return
                elif notification.kind == "diagnostic" and notification.diagnostic is not None:
                    self.post_message(
                        SessionObservationLagged(
                            session_id=subscription.session_id,
                            diagnostic=notification.diagnostic,
                        )
                    )
                    return
        finally:
            subscription.close()

    async def _replace_agent_tree(self, root_session_id: str | None) -> tuple[AgentTreeNode, ...]:
        self._agent_tree_revision += 1
        revision = self._agent_tree_revision
        previous = self._agent_tree_subscription
        if previous is not None and previous.root_session_id == root_session_id:
            self._sync_subagent_navigation_panel(root_session_id=root_session_id)
            return tuple(self._agent_nodes.values())
        if previous is not None:
            previous.close()
        self._agent_tree_subscription = None
        self._agent_nodes.clear()
        self._agent_root_status = "idle"
        if root_session_id is None:
            self._sync_subagent_navigation_panel(root_session_id=None)
            return ()
        subscription = await self._controller.subscribe_agent_tree(root_session_id)
        if subscription is None:
            self._sync_subagent_navigation_panel(root_session_id=root_session_id)
            return ()
        if self._host_closing or revision != self._agent_tree_revision:
            subscription.close()
            return ()
        self._agent_tree_subscription = subscription
        snapshot = subscription.initial_snapshot
        self._agent_nodes = {node.delegation_id: node for node in snapshot.nodes}
        if snapshot.root_activity is not None:
            self._agent_root_status = snapshot.root_activity.status
        self._sync_subagent_navigation_panel(root_session_id=root_session_id)

        _worker: Worker[None] = self.run_worker(
            partial(self._consume_agent_tree, subscription),
            name=f"agent-tree-{root_session_id}",
            group="agent-tree",
            exclusive=True,
        )
        return tuple(self._agent_nodes.values())

    def _sync_subagent_navigation_panel(
        self,
        *,
        root_session_id: str | None,
        current_session_id: str | None = None,
    ) -> None:
        if self._host_closing or not self.query(SubagentNavigationPanel):
            return
        panel = self.query_one(SubagentNavigationPanel)
        panel.set_tree(
            root_session_id=root_session_id,
            agents=self._agent_nodes.values(),
            root_status=self._agent_root_status,
        )
        visible_session_id = current_session_id or self._visible_session_id()
        if visible_session_id is not None:
            panel.set_current_session(visible_session_id)

    async def _consume_agent_tree(self, subscription: AgentTreeSubscription) -> None:
        try:
            async for notification in subscription:
                if self._host_closing or self._agent_tree_subscription is not subscription:
                    return
                if notification.kind == "diagnostic" and notification.diagnostic is not None:
                    self.notify(
                        notification.diagnostic.message,
                        severity="warning",
                        markup=False,
                    )
                    snapshot = await self._controller.agent_tree_snapshot(
                        subscription.root_session_id
                    )
                    if snapshot is None:
                        continue
                    nodes = snapshot.nodes
                    root_activity = snapshot.root_activity
                    self._agent_nodes = {node.delegation_id: node for node in nodes}
                elif notification.kind == "snapshot" and notification.snapshot is not None:
                    snapshot = notification.snapshot
                    nodes = snapshot.nodes
                    root_activity = snapshot.root_activity
                    self._agent_nodes = {node.delegation_id: node for node in nodes}
                else:
                    continue
                self._agent_root_status = (
                    root_activity.status if root_activity is not None else "idle"
                )
                async with self._runtime_event_apply_lock:
                    if self._agent_tree_subscription is subscription and self._surface_available():
                        self.query_one(ConversationView).sync_agents(nodes)
                        visible_agent = self._visible_agent
                        if visible_agent is not None:
                            updated_agent = self._agent_nodes.get(visible_agent.delegation_id)
                            if updated_agent is not None:
                                self._visible_agent = updated_agent
                                self.query_one(StatusLine).set_subagent(updated_agent)
                        self._sync_subagent_navigation_panel(
                            root_session_id=subscription.root_session_id
                        )
        except StopAsyncIteration, asyncio.CancelledError:
            return
        finally:
            subscription.close()

    async def on_session_observation_lagged(
        self,
        message: SessionObservationLagged,
    ) -> None:
        self._runtime_event_buffer_lagged.discard(message.session_id)
        if message.session_id != self._visible_session_id() or self._host_closing:
            return
        if message.diagnostic.code == "runtime_subscription_read_failed":
            self.notify(message.diagnostic.message, severity="error", markup=False)
            return
        self.notify("Live view fell behind; reloading durable session history…", severity="warning")
        await self._reload_visible_observation()

    def _post_runtime_events_ready(self) -> None:
        self._runtime_events_frame_scheduled = False
        if self._host_closing or not self._pending_runtime_events:
            return
        if not self._runtime_events_ready_posted:
            self._runtime_events_ready_posted = self.post_message(RuntimeEventsReady())

    def _publish_lifecycle_status(self, status: TuiLifecycleStatus) -> None:
        if not self._host_closing:
            self.post_message(LifecycleStatusReceived(status))

    def on_lifecycle_status_received(self, message: LifecycleStatusReceived) -> None:
        if self._activity_override is None:
            self.query_one(StatusLine).set_activity(message.status.activity)

    def _apply_system_status(self, bootstrap: TuiBootstrap) -> None:
        self._system_status = bootstrap.system_status
        if self._active_workspace is None and bootstrap.system_status.workspace is not None:
            self._active_workspace = bootstrap.system_status.workspace
            self.query_one(BrandHeader).set_context(workspace=self._active_workspace)
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def _show_welcome(self, *, refresh_status: bool = True) -> None:
        if not self._surface_available():
            return
        await self.query_one(ConversationView).show_welcome()
        if not self._surface_available():
            return
        self._restore_input_draft(self._controller.session_id)
        self._set_welcome_mode(True)
        if refresh_status:
            self.call_after_refresh(self._start_welcome_status_refresh)

    def _start_welcome_status_refresh(self) -> None:
        if self._welcome_status_visible():
            _worker: Worker[None] = self.run_worker(
                partial(self._refresh_welcome_status, full=True), group="welcome-status"
            )

    def _set_welcome_mode(self, enabled: bool) -> None:
        self.query_one(BrandHeader).display = not enabled
        self.query_one("#main", Vertical).set_class(enabled, "welcome-page")
        self.query_one(WelcomePanel).set_welcome_mode(enabled)

    def _set_welcome_system_status(self, status: TuiSystemStatus) -> None:
        self.query_one(WelcomePanel).set_system_status(status)

    def _sync_mcp_status(self) -> None:
        """Refresh the visible summary from the process-owned live MCP records."""

        if self._host_closing:
            return
        status = project_mcp_system_status(
            self._system_status,
            self._controller.mcp_servers(),
        )
        if status == self._system_status:
            return
        self._system_status = status
        self.query_one(ConversationView).set_system_status(status)

    def on_runtime_events_ready(self, _message: RuntimeEventsReady) -> None:
        self._runtime_events_ready_posted = False
        if self._host_closing or not self.query(ConversationView):
            self._pending_runtime_events.clear()
            return
        # Awaiting rendering here stalls the App message pump, including key
        # dispatch, even when the renderer itself runs off-loop. A single worker
        # preserves frame cadence while leaving the input dispatcher available.
        if self._runtime_events_worker is None or self._runtime_events_worker.is_finished:
            # Create the coroutine only when the worker starts; it may be cancelled first.
            self._runtime_events_worker = self.run_worker(
                self._flush_pending_runtime_events,
                name="runtime-event-frame",
                group="runtime-events",
            )

    async def _flush_pending_runtime_events(self) -> None:
        # Explicit completion drains and frame workers share the same ordering
        # boundary, including the asynchronous reads preceding projection.
        async with self._runtime_event_flush_lock:
            if not self._pending_runtime_events or self._host_closing:
                return
            events = _runtime_event_frame_prefix(self._pending_runtime_events)
            del self._pending_runtime_events[: len(events)]
            await self._apply_runtime_events(events)
        if (
            self._pending_runtime_events
            and not self._runtime_events_ready_posted
            and not self._runtime_events_frame_scheduled
        ):
            self._runtime_events_frame_scheduled = self.call_after_refresh(
                self._post_runtime_events_ready,
            )

    async def _apply_runtime_events(
        self,
        events: Sequence[EventRecord],
    ) -> None:
        projection = self._projection
        if projection is not None and any(
            event.session_id == projection.session_id
            and (
                event.history_epoch > projection.history_epoch
                or (
                    event.sequence > projection.last_sequence
                    and event.history_epoch == projection.history_epoch
                    and event.event_type == "run_status_changed"
                    and event.payload.get("resumed") is True
                )
            )
            for event in events
        ):
            # A new history or resumed checkpoint uses the same snapshot/live handoff.
            await self._reload_visible_observation()
        projection = self._projection
        if projection is None:
            events = tuple(
                event for event in events if event.session_id == self._controller.session_id
            )
        input_messages: dict[str, MessageRecord] = {}
        if events:
            session_id = projection.session_id if projection is not None else events[0].session_id
            epoch = projection.history_epoch if projection is not None else events[0].history_epoch
            message_ids = tuple(
                dict.fromkeys(
                    identity
                    for event in events
                    if event.session_id == session_id
                    and event.history_epoch == epoch
                    and (projection is None or event.sequence > projection.last_sequence)
                    and event.event_type
                    in {*_SESSION_INPUT_MESSAGE_EVENTS, "queued_input_consumed"}
                    if isinstance((identity := event.payload.get("message_id")), str)
                )
            )
            if message_ids:
                page = await self._controller.messages_at_history(
                    session_id, message_ids, history_epoch=epoch
                )
                if page.history_epoch != epoch:
                    await self._reload_visible_observation()
                    return
                input_messages = {message.message_id: message for message in page.messages}
        async with self._runtime_event_apply_lock:
            current = self._projection
            if current is not projection and (
                current is None
                or projection is None
                or current.session_id != projection.session_id
                or current.history_epoch != projection.history_epoch
            ):
                return
            await self._apply_runtime_event_batch(
                events,
                input_messages=input_messages,
            )

    async def _apply_runtime_event_batch(
        self,
        events: Sequence[EventRecord],
        *,
        input_messages: dict[str, MessageRecord],
    ) -> None:
        if self._host_closing or not events or not self.query(ConversationView):
            return
        projection = self._projection
        if projection is None:
            if not self._waiting_for_session_event:
                return
            events = tuple(
                event for event in events if event.session_id == self._controller.session_id
            )
            if not events:
                return
            projection = TuiSessionProjection(
                events[0].session_id, history_epoch=events[0].history_epoch
            )
            self._projection = projection
            # Only the selected draft may become a durable Session on this surface.
            await self._replace_agent_tree(projection.session_id)
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
        conversation = self.query_one(ConversationView)
        stream_kind: str | None = None
        stream_parts: list[str] = []
        stream_started_at: datetime | None = None
        tools_dirty = False
        refresh_session_controls = any(
            event.event_type in _SESSION_CONTROL_REFRESH_EVENTS for event in events
        )

        async def flush_stream() -> None:
            nonlocal stream_kind, stream_started_at
            if not stream_parts:
                return
            text = "".join(stream_parts)
            stream_parts.clear()
            if stream_kind == "reasoning":
                await conversation.append_reasoning(text, started_at=stream_started_at)
            else:
                await conversation.append_assistant(text)
            stream_kind = None
            stream_started_at = None

        async def append_stream(
            kind: str,
            text: str,
            *,
            started_at: datetime | None = None,
        ) -> None:
            nonlocal stream_kind, stream_started_at
            if stream_kind is not None and stream_kind != kind:
                await flush_stream()
            stream_kind = kind
            if started_at is not None and stream_started_at is None:
                stream_started_at = started_at
            stream_parts.append(text)

        async def flush_tools() -> None:
            nonlocal tools_dirty
            if not tools_dirty:
                return
            await conversation.sync_tools(projection.activity.tool_calls)
            tools_dirty = False

        for event in events:
            if tools_dirty and event.event_type in _TOOL_PRESENTATION_BOUNDARY_EVENTS:
                await flush_tools()
            previous_tools = projection.activity.tool_calls
            update = projection.apply(event)
            if update is None:
                continue
            latest_update = update
            if update.activity.tool_calls != previous_tools:
                if not tools_dirty:
                    await flush_stream()
                tools_dirty = True
            if event.event_type == "model_request_started":
                await flush_stream()
                await conversation.finish_assistant()
                conversation.begin_model_request(run_id=event.run_id)
            if event.event_type in {*_SESSION_INPUT_MESSAGE_EVENTS, "queued_input_consumed"}:
                await flush_stream()
                message_id = event.payload.get("message_id")
                if isinstance(message_id, str):
                    input_message = input_messages[message_id]
                    if input_message.role == "user":
                        await conversation.add_user_message(input_message)
            if event.event_type == "compaction_completed":
                await flush_stream()
                covered = event.payload.get("covered_message_count")
                if isinstance(covered, int) and covered >= 0:
                    tokens_before = event.payload.get("tokens_before")
                    tokens_after = event.payload.get("tokens_after")
                    duration_seconds = event.payload.get("duration_seconds")
                    await conversation.add_compaction(
                        covered_message_count=covered,
                        tokens_before=(
                            tokens_before
                            if isinstance(tokens_before, int) and tokens_before >= 0
                            else None
                        ),
                        tokens_after=(
                            tokens_after
                            if isinstance(tokens_after, int) and tokens_after >= 0
                            else None
                        ),
                        duration_seconds=(
                            float(duration_seconds)
                            if isinstance(duration_seconds, (int, float))
                            and not isinstance(duration_seconds, bool)
                            and duration_seconds >= 0
                            else None
                        ),
                    )
            if event.event_type in {
                "follow_up_input_queued",
                "steering_input_queued",
                "queued_input_cancelled",
                *_SESSION_INPUT_MESSAGE_EVENTS,
                "queued_input_consumed",
            }:
                queue_changed = True
            if update.reasoning_delta:
                await append_stream(
                    "reasoning",
                    update.reasoning_delta,
                    started_at=update.reasoning_started_at,
                )
            if update.reasoning_duration_seconds is not None:
                await flush_stream()
                conversation.finish_reasoning(update.reasoning_duration_seconds)
            if update.assistant_delta:
                await append_stream("assistant", update.assistant_delta)
            if update.diagnostic:
                diagnostics.append(update.diagnostic)
            if event.event_type in {"run_completed", "run_aborted", "run_failed", "run_paused"}:
                await flush_stream()
                await flush_tools()
                await self._append_run_summary(event, model=projection.model)

        await flush_stream()
        await flush_tools()

        if self._host_closing or not conversation.is_attached:
            return
        if refresh_session_controls and not self._visible_read_only:
            self._schedule_session_controls_refresh()

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
        if queue_changed and not self._visible_read_only:
            await self._sync_queued_inputs(latest_update.event.session_id)
        self._sync_activity(latest_update.activity)
        await self._refresh_interaction(refresh_agents=refresh_agent_interactions)
        if self._host_closing or not conversation.is_attached:
            return
        for diagnostic in diagnostics:
            self.notify(diagnostic, severity="error", markup=False)
        if (
            not self._visible_read_only
            and self._controller.session_id == projection.session_id
            and (
                self._observation_subscription is None
                or self._observation_subscription.session_id != projection.session_id
            )
        ):
            self._replace_observation(
                await self._controller.subscribe_session(
                    projection.session_id,
                    after_sequence=projection.last_sequence,
                )
            )

    async def _append_run_summary(self, event: EventRecord, *, model: str | None) -> None:
        if event.run_id is None:
            return
        try:
            run = await self._controller.run_record(event.session_id, event.run_id)
        except OuroError as exc:
            self.notify(f"Cannot load run summary: {exc}", severity="error", markup=False)
            return
        if not self._surface_available() or run.kind != "agent":
            return
        # Internal endpoint identity remains durable; only its user-facing
        # display projection crosses into the transcript.
        presentation = project_run_presentation(run, models=self._models)
        if presentation.model == "unknown model" and model is not None:
            presentation = replace(presentation, model=model)
        await self.query_one(ConversationView).add_run_summary(presentation)

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        self._close_attachment_menu()
        if self._visible_read_only:
            self.notify("Subagent sessions are read-only", severity="warning")
            return
        interaction = self._interaction
        if interaction is not None and interaction.kind == "approval":
            self.query_one(InteractionBar).focus()
            return
        if interaction is not None and interaction.kind == "question":
            plan_panel = self.query_one(PlanConfirmationPanel)
            if plan_panel.display and plan_panel.awaiting_feedback:
                plan_panel.accept_feedback(message.text)
                return
            panel = self.query_one(AskUserPanel)
            if panel.display and panel.awaiting_freeform:
                panel.accept_freeform(message.text)
            return
        revision = self._controller.selection_revision
        origin_session_id = self._controller.session_id
        try:
            models = await self._controller.models()
        except Exception as exc:  # noqa: BLE001 - keep the unsent draft on catalog failures.
            self._restore_unsent_input(message.content, revision, origin_session_id)
            self.notify(str(exc), severity="error", markup=False)
            return
        if revision != self._controller.selection_revision:
            self._restore_unsent_input(message.content, revision, origin_session_id)
            self.notify("Session changed while preparing input; the original draft was restored")
            return
        if not models:
            self._restore_unsent_input(message.content, revision, origin_session_id)
            self.notify("Add a provider and endpoint to send a message")
            self._prompt_provider_setup()
            return
        if not self._active_model and any(model.provider_id != "faux" for model in models):
            self._restore_unsent_input(message.content, revision, origin_session_id)
            self.notify("Select an available model with /model before sending")
            return
        self._submit_run(
            message.text,
            message.mode,
            attachments=message.content.attachments,
            skills=message.content.skills,
        )

    def on_composer_attachment_requested(self, message: Composer.AttachmentRequested) -> None:
        if self._visible_read_only or self._interaction is not None:
            self.query_one(Composer).import_pending = False
            self._close_attachment_menu()
            return
        if message.paste is None:
            composer = self.query_one(Composer)
            composer.begin_attachment_menu()
            self._show_attachment_menu()
        else:
            self._attach_files(message.paste)

    @work(exclusive=True, group="attachment-directory")
    async def _load_attachment_directory(self, directory: Path) -> None:
        picker = self._attachment_picker
        if picker is None:
            return
        revision = self._controller.selection_revision
        composer = self.query_one(Composer)
        query = composer.text
        picker.loading = True
        self._show_attachment_menu()
        try:
            entries = await asyncio.to_thread(picker.read_directory, directory)
        except OSError as exc:
            if (
                picker is self._attachment_picker
                and revision == self._controller.selection_revision
            ):
                picker.loading = False
                self._show_attachment_menu()
                self.notify(str(exc), severity="error", markup=False)
            return
        if picker is self._attachment_picker and revision == self._controller.selection_revision:
            with self.batch_update():
                # Publish a complete directory view without collapsing the existing menu.
                # A filter typed during the read belongs to the newly opened directory.
                if picker.loaded and composer.text == query:
                    composer.set_attachment_query("")
                picker.directory, picker.entries = directory, entries
                picker.loading, picker.loaded = False, True
                self._show_attachment_menu()

    def _show_attachment_menu(self) -> None:
        if not self._surface_available():
            return
        composer = self.query_one(Composer)
        context = composer.command_context()
        if not composer.attachment_menu_open:
            return
        if context is None or not context[0].startswith("@"):
            self._close_attachment_menu()
            return
        name, separator, query = context[0].partition(" ")
        popup = self.query_one(CommandPopup)
        composer.suggestion = ""
        if not separator:
            self._attachment_picker = None
            self._skill_picker = None
            popup.show_commands(matching_commands(name, REFERENCE_COMMANDS))
            return
        if name not in {command.name for command in REFERENCE_COMMANDS}:
            popup.hide()
            return
        composer.reference_command = name
        if name == "@skill":
            self._attachment_picker = None
            if self._skill_picker is None:
                existing = {item.name for item in composer.draft_content.skills}
                self._skill_picker = SkillPicker(
                    tuple(item for item in self._skill_arguments() if item.value not in existing)
                )
            arguments = self._skill_picker.arguments(query, self._tui_theme)
            selection_count = len(self._skill_picker.selected)
            title, heading = "No matching skills", "Skills"
        else:
            self._skill_picker = None
            if self._attachment_picker is None:
                directory = Path(
                    self._active_workspace or self._controller.draft_workspace or Path.cwd()
                )
                self._attachment_picker = AttachmentPicker(
                    directory, limit=max(0, 16 - len(composer.draft_content.attachments))
                )
                self._load_attachment_directory(directory)
            picker = self._attachment_picker
            if picker.loading and picker.loaded:
                return
            arguments = () if picker.loading else picker.arguments(query, self._tui_theme)
            title = "Loading files…" if picker.loading else "No matching files"
            heading = str(picker.directory)
            selection_count = len(picker.selected)
        popup.show_arguments(
            TuiCommand(name, title, "Reference files or skills"),
            arguments,
            query=query,
            heading=heading,
            selection_count=selection_count,
            hint=self._tui_theme.inline_separator.join(
                ("↑/↓ move", "Space select", "Enter open / reference", "Esc close")
            ),
        )

    def on_composer_attachment_toggled(self, _message: Composer.AttachmentToggled) -> None:
        if self._skill_picker is not None:
            argument = self.query_one(CommandPopup).selected_argument
            if argument is not None:
                self._skill_picker.toggle(argument.value)
                self._show_attachment_menu()
            return
        picker = self._attachment_picker
        argument = self.query_one(CommandPopup).selected_argument
        if picker is not None and not picker.loading and argument is not None:
            try:
                picker.toggle(argument.value)
            except ValueError as exc:
                self.notify(str(exc), severity="warning", markup=False)
            self._show_attachment_menu()

    def _choose_attachment(self) -> None:
        popup = self.query_one(CommandPopup)
        composer = self.query_one(Composer)
        if popup.selected_command is not None:
            composer.select_reference_command(popup.selected_command.name)
            self._show_attachment_menu()
            return
        if self._skill_picker is not None:
            argument = popup.selected_argument
            names = self._skill_picker.skills_for(argument.value if argument is not None else "")
            if names:
                self._close_attachment_menu()
                composer.insert_skills(tuple(InputSkillReference(name=name) for name in names))
            return
        picker = self._attachment_picker
        argument = self.query_one(CommandPopup).selected_argument
        if picker is None or picker.loading:
            return
        value = argument.value if argument is not None else ""
        directory = picker.directory_for(value)
        if directory is not None:
            self._load_attachment_directory(directory)
            return
        paths = picker.files_for(value)
        if paths:
            self._close_attachment_menu()
            self.query_one(Composer).import_pending = True
            self._reference_files(paths)

    @work(exclusive=True, group="attachment-reference")
    async def _reference_files(self, paths: tuple[Path, ...]) -> None:
        composer = self.query_one(Composer)
        revision = self._controller.selection_revision
        composer.import_pending = True
        composer.disabled = True
        try:
            items = await self._controller.reference_files(paths)
            if revision != self._controller.selection_revision:
                return
            composer.insert_attachments(items)
            content = composer.input_content
            self._remember_input_draft(
                self._controller.session_id,
                content.text,
                attachments=content.attachments,
                skills=content.skills,
            )
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error", markup=False)
        finally:
            composer.import_pending = False
            if self._surface_available():
                self._sync_interaction_focus(self._interaction)

    def _close_attachment_menu(self) -> None:
        self._attachment_picker = None
        self._skill_picker = None
        composer = self.query_one(Composer)
        if composer.attachment_menu_open:
            composer.finish_attachment_menu()
            self.query_one(CommandPopup).hide()

    @work(exclusive=True, group="attachment-import")
    async def _attach_files(self, paste: str | tuple[Path, ...]) -> None:
        composer = self.query_one(Composer)
        revision = self._controller.selection_revision
        original_session = self._controller.session_id
        original = composer.input_content
        selection = composer.selection
        insert_into_original = composer.prepare_attachment_insertion()
        composer.import_pending = True
        composer.disabled = True
        try:
            if isinstance(paste, str):
                paths = await asyncio.to_thread(pasted_files, paste)
                if not paths:
                    if revision == self._controller.selection_revision:
                        composer.insert_plain_paste(paste)
                    return
            else:
                paths = paste
            if revision != self._controller.selection_revision:
                return
            if len(original.attachments) + len(paths) > 16:
                self.notify("A message can contain at most 16 attachments", severity="warning")
                return
            session = await self._controller.attachment_session()
            imported: list[InputFile] = []
            for path in paths:
                try:
                    imported.append(await self._controller.import_attachment(session, path))
                except Exception as exc:  # noqa: BLE001 - report each failed file and retain successes.
                    self.notify(f"{path.name}: {exc}", severity="error", markup=False)
            await self._controller.validate_attachment_session(session)
            if revision != self._controller.selection_revision:
                # The import belongs to its captured Session, never to a later navigation.
                restored = insert_into_original(tuple(imported)) if imported else original
                self._remember_input_draft(
                    session.session_id,
                    restored.text,
                    attachments=restored.attachments,
                    skills=restored.skills,
                )
                return
            if original_session is None:
                self._remember_input_draft(None, "")
                self._remember_input_draft(
                    session.session_id,
                    original.text,
                    attachments=original.attachments,
                    skills=original.skills,
                )
            composer.load_input(original)
            composer.selection = selection
            composer.insert_attachments(tuple(imported))
            content = composer.input_content
            self._remember_input_draft(
                session.session_id,
                content.text,
                attachments=content.attachments,
                skills=content.skills,
            )
        except Exception as exc:  # noqa: BLE001 - keep the unsent draft on import failures.
            self.notify(str(exc), severity="error", markup=False)
        finally:
            composer.import_pending = False
            if self._surface_available():
                self._sync_interaction_focus(self._interaction)

    @work(exclusive=True, group="provider-onboarding")
    async def _prompt_provider_setup(self) -> None:
        await self._add_provider()
        models = await self._refresh_models()
        if models and not self._active_model:
            self.notify("Select a model with /model before sending your draft")

    def _submit_run(
        self,
        prompt: str,
        queue_mode: QueuedInputMode,
        *,
        skills: tuple[InputSkillReference, ...] = (),
        attachments: tuple[InputFile, ...] | None = None,
    ) -> None:
        if self._visible_read_only:
            return
        self._set_welcome_mode(False)
        active_run = self._has_active_run()
        if not active_run:
            self.query_one(StatusLine).set_activity("queued")
        if self._projection is None:
            self.query_one(Composer).disabled = True
            self._waiting_for_session_event = True
        attachments = (
            attachments
            if attachments is not None
            else self._input_drafts.content_for(self._controller.session_id).attachments
        )
        self._remember_input_draft(self._controller.session_id, "")
        self._run_prompt(prompt, queue_mode, skills=skills, attachments=attachments)

    @work(group="run")
    async def _run_prompt(
        self,
        prompt: str,
        queue_mode: QueuedInputMode,
        *,
        skills: tuple[InputSkillReference, ...] = (),
        attachments: tuple[InputFile, ...] = (),
    ) -> None:
        revision = self._controller.selection_revision
        origin_session_id = self._controller.session_id
        accepted = False

        def input_accepted() -> None:
            nonlocal accepted
            accepted = True

        try:
            result = await self._controller.submit(
                prompt,
                queue_mode=queue_mode,
                skill_invocations=tuple(
                    SkillInvocationRequest(
                        name=item.name,
                        arguments=prompt,
                        text_offset=item.text_offset,
                        order=item.order,
                    )
                    for item in skills
                ),
                resource_refs=InputContent(text=prompt, attachments=attachments).resource_refs,
                attachments=attachments,
                on_input_accepted=input_accepted,
            )
            await self._apply_run_result(result)
            await self._sync_queued_inputs(result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - providers and tools fail across this boundary.
            same_selection = revision == self._controller.selection_revision
            if not accepted:
                content = InputContent(text=prompt, attachments=attachments, skills=skills)
                self._restore_unsent_input(content, revision, origin_session_id)
            if same_selection:
                self.query_one(StatusLine).set_activity("failed")
            self.notify(str(exc), severity="error", markup=False)
        finally:
            if revision == self._controller.selection_revision:
                self._waiting_for_session_event = False
                if not self._host_closing and self.query(Composer):
                    self._sync_interaction_focus(self._interaction)

    def _restore_unsent_input(
        self, content: InputContent, revision: int, origin_session_id: str | None
    ) -> None:
        same_selection = revision == self._controller.selection_revision
        target_session = self._controller.session_id if same_selection else origin_session_id
        current = (
            self.query_one(Composer).input_content
            if same_selection
            else self._input_drafts.content_for(target_session)
        )
        restored = current.followed_by(content)
        self._remember_input_draft(
            target_session, restored.text, attachments=restored.attachments, skills=restored.skills
        )
        if same_selection:
            self._edit_draft(restored)

    async def _apply_run_result(self, result: RunResult) -> None:
        if not self._owns_session_surface(result.session.session_id):
            await self._refresh_welcome_status()
            return
        await self._flush_pending_runtime_events()
        await self._apply_run_result_events(result.events)
        if not self._surface_available():
            return
        async with self._runtime_event_apply_lock:
            if not self._owns_session_surface(result.session.session_id):
                return
            if (
                self._projection is not None
                and result.run.history_epoch < self._projection.history_epoch
            ):
                return
            if self._projection is None:
                self._projection = TuiSessionProjection(
                    result.session.session_id, history_epoch=result.session.history_epoch
                )
            if result.final_text and not self._projection.assistant_text:
                self._projection.assistant_text = result.final_text
                conversation = self.query_one(ConversationView)
                await conversation.append_assistant(result.final_text)
                await conversation.finish_assistant()
            self._active_title = result.session.title
            self._active_workspace = result.session.workspace or self._system_status.workspace
            self.query_one(BrandHeader).set_context(
                session_title=self._active_title,
                workspace=self._active_workspace,
            )
        await self._refresh_welcome_status()
        await self._refresh_interaction()
        await self._refresh_session_controls()

    def _owns_session_surface(self, session_id: str) -> bool:
        if self._visible_read_only:
            return False
        if self._projection is not None:
            return self._projection.session_id == session_id
        return self._waiting_for_session_event and self._controller.session_id == session_id

    async def _apply_run_result_events(self, events: Sequence[EventRecord]) -> None:
        if not events or self._host_closing:
            return
        session_id = events[0].session_id
        projection = self._projection
        high_water = (
            projection.last_sequence
            if projection is not None and projection.session_id == session_id
            else 0
        )
        high_water = max(
            (
                high_water,
                *(
                    event.sequence
                    for event in self._pending_runtime_events
                    if event.session_id == session_id
                ),
            )
        )
        target_sequence = high_water
        for event in events:
            if event.session_id != session_id or event.sequence <= high_water:
                continue
            if not self._queue_runtime_event(event):
                self._signal_runtime_event_buffer_lag(event.session_id)
                break
            high_water = event.sequence
            target_sequence = event.sequence
        if target_sequence == 0:
            return

        await self._flush_pending_runtime_events()
        while not self._host_closing:
            projection = self._projection
            if (
                projection is not None
                and projection.session_id == session_id
                and projection.last_sequence >= target_sequence
            ):
                return
            if not any(
                event.session_id == session_id and event.sequence <= target_sequence
                for event in self._pending_runtime_events
            ):
                return
            await self.wait_for_refresh()

    async def _refresh_welcome_status(self, *, full: bool = False) -> None:
        # Entry and completion notifications share one reader. A full snapshot
        # includes usage; notifications during a read request one follow-up read.
        if not self._welcome_status_visible():
            return
        if full or self._welcome_refresh_requested != "system":
            self._welcome_refresh_requested = "system" if full else "usage"
        if self._welcome_refresh_lock.locked():
            return
        async with self._welcome_refresh_lock:
            while self._welcome_refresh_requested and self._welcome_status_visible():
                requested = self._welcome_refresh_requested
                self._welcome_refresh_requested = None
                revision = self._controller.selection_revision
                previous = self._system_status
                try:
                    if requested == "system":
                        status = await self._controller.system_status()
                    else:
                        usage = await self._controller.usage_summary()
                        status = project_usage_system_status(self._system_status, usage)
                except Exception as exc:  # noqa: BLE001 - retain the last valid Host projection.
                    if (
                        self._welcome_status_visible()
                        and revision == self._controller.selection_revision
                    ):
                        self.notify(
                            f"Could not refresh {requested} status: {exc}",
                            severity="warning",
                            markup=False,
                        )
                    continue
                if not self._welcome_status_visible():
                    return
                if revision != self._controller.selection_revision:
                    continue
                if requested == "system" and previous != self._system_status:
                    # Live workspace, model and service updates take precedence
                    # over a snapshot captured before they were applied.
                    self._welcome_refresh_requested = "system"
                    continue
                if status != self._system_status:
                    self._system_status = status
                    self.query_one(ConversationView).set_system_status(status)

    def _welcome_status_visible(self) -> bool:
        return self._surface_available() and self.query_one(WelcomePanel).has_class("welcome-page")

    async def _refresh_models(self) -> tuple[AvailableModel, ...]:
        """Synchronize the Host's model projection with the live Runtime catalog."""

        models = await self._controller.models()
        self._models = models
        self._system_status = replace(
            self._system_status,
            model_names=tuple(
                sorted(
                    (model_display_label(model) for model in models),
                    key=str.casefold,
                )
            ),
            model_count=len(models),
        )
        if self._surface_available():
            self.query_one(ConversationView).set_system_status(self._system_status)
        return models

    async def _sync_queued_inputs(self, session_id: str | None = None) -> None:
        if not self._surface_available() or not self.query(QueuedInputPanel):
            return
        session_id = session_id or self._controller.session_id
        if session_id is None:
            self.query_one(QueuedInputPanel).set_inputs(())
            return
        if not self._owns_session_surface(session_id):
            return
        inputs = await self._controller.queued_user_inputs(session_id)
        if not self._surface_available() or not self._owns_session_surface(session_id):
            return
        self.query_one(QueuedInputPanel).set_inputs(inputs)

    async def on_text_area_changed(self, message: TextArea.Changed) -> None:
        if not self._surface_available():
            return
        if message.text_area is not self.query_one(Composer):
            return
        composer = message.text_area
        value = composer.text
        command_context = composer.observe_command_change()
        if not self._visible_read_only:
            content = composer.draft_content
            self._remember_input_draft(
                self._controller.session_id,
                content.text,
                attachments=content.attachments,
                skills=content.skills,
            )
        popup = self.query_one(CommandPopup)
        ask_user = self.query_one(AskUserPanel)
        plan_panel = self.query_one(PlanConfirmationPanel)
        if composer.attachment_menu_open:
            self._show_attachment_menu()
            return
        if (ask_user.display and ask_user.awaiting_freeform) or (
            plan_panel.display and plan_panel.awaiting_feedback
        ):
            popup.hide()
            self._sync_command_suggestion(composer, popup)
            return
        if value == self._dismissed_command_text:
            return
        self._dismissed_command_text = None
        if command_context is None:
            popup.hide()
            self._sync_command_suggestion(composer, popup)
            return
        command_value, _draft = command_context
        _command_text, separator, argument = command_value.partition(" ")
        if not separator:
            command_catalog = self._command_catalog()
            commands = matching_commands(command_value, command_catalog)
            popup.show_commands(tuple(sorted(commands, key=lambda item: item.name)))
            self._sync_command_suggestion(composer, popup, command_catalog=command_catalog)
            return
        static_command, _normalized_argument = resolve_command(_command_text, TUI_COMMANDS)
        if (
            static_command is not None
            and static_command.name == "/model"
            and (
                not argument.strip()
                or popup.argument_command is None
                or popup.argument_command.name != "/model"
            )
        ):
            try:
                await self._refresh_models()
            except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
                popup.hide()
                self._sync_command_suggestion(composer, popup)
                self.notify(
                    f"Could not refresh models: {exc}",
                    severity="error",
                    markup=False,
                )
                return
        command_catalog = self._command_catalog()
        command, _normalized_argument = resolve_command(command_value, command_catalog)
        if command is None or not command.takes_arguments:
            popup.hide()
            self._sync_command_suggestion(composer, popup, command_catalog=command_catalog)
            return
        arguments = matching_arguments(command, argument)
        popup.show_arguments(command, arguments, query=argument)
        self._sync_command_suggestion(composer, popup, command_catalog=command_catalog)

    def on_text_area_selection_changed(self, message: TextArea.SelectionChanged) -> None:
        if not self._surface_available():
            return
        composer = self.query_one(Composer)
        if message.text_area is composer:
            self._sync_command_suggestion(composer, self.query_one(CommandPopup))

    def _command_suggestion(
        self,
        composer: Composer,
        popup: CommandPopup,
        *,
        command_catalog: tuple[TuiCommand, ...] | None = None,
    ) -> str:
        ask_user = self.query_one(AskUserPanel)
        plan_panel = self.query_one(PlanConfirmationPanel)
        if (ask_user.display and ask_user.awaiting_freeform) or (
            plan_panel.display and plan_panel.awaiting_feedback
        ):
            return ""
        context = composer.command_context()
        if context is None or not composer.command_cursor_at_end():
            return ""
        command_text, _draft = context
        if command_text == self._dismissed_command_text:
            return ""
        _command_name, separator, argument = command_text.partition(" ")
        if not separator:
            return ""
        catalog = command_catalog if command_catalog is not None else self._command_catalog()
        command, _normalized_argument = resolve_command(command_text, catalog)
        if command is None or not command.takes_arguments:
            return ""
        arguments = matching_arguments(command, argument)
        selected_value = (
            popup.selected_argument.value if popup.selected_argument is not None else None
        )
        selected = next(
            (item for item in arguments if item.value == selected_value),
            arguments[0] if arguments else None,
        )
        return argument_suggestion(command, argument, selected)

    def _matches_command(self, composer: Composer) -> bool:
        context = composer.command_context()
        if context is None:
            return False
        value, _draft = context
        if composer.attachment_menu_open:
            name, separator, query = value.partition(" ")
            if not separator:
                return bool(matching_commands(name, REFERENCE_COMMANDS))
            if name == "@skill":
                return self._skill_picker is not None and bool(
                    self._skill_picker.selected
                    or self._skill_picker.arguments(query, self._tui_theme)
                )
            picker = self._attachment_picker
            return name == "@file" and (
                picker is None
                or picker.loading
                or bool(picker.selected or picker.arguments(query, self._tui_theme))
            )
        catalog = self._command_catalog()
        _name, separator, argument = value.partition(" ")
        if not separator:
            return bool(matching_commands(value, catalog))
        command, _argument = resolve_command(value, catalog)
        if command is None:
            return False
        if not argument.strip():
            return True
        return command.takes_arguments and (
            command.freeform_arguments
            or any(option.is_suggestion for option in command.argument_options)
            or bool(matching_arguments(command, argument))
        )

    def _sync_command_suggestion(
        self,
        composer: Composer,
        popup: CommandPopup,
        *,
        command_catalog: tuple[TuiCommand, ...] | None = None,
    ) -> None:
        composer.suggestion = self._command_suggestion(
            composer,
            popup,
            command_catalog=command_catalog,
        )

    async def on_composer_command_submitted(self, message: Composer.CommandSubmitted) -> None:
        if self._visible_read_only:
            self.notify("Subagent sessions are read-only", severity="warning")
            return
        if self.query_one(Composer).attachment_menu_open:
            self._choose_attachment()
            return
        popup = self.query_one(CommandPopup)
        composer = self.query_one(Composer)
        interaction = self._interaction
        ask_user = self.query_one(AskUserPanel)
        plan_panel = self.query_one(PlanConfirmationPanel)
        if (
            interaction is not None
            and interaction.kind == "question"
            and plan_panel.display
            and plan_panel.awaiting_feedback
        ):
            composer.clear()
            popup.hide()
            self._dismissed_command_text = None
            plan_panel.accept_feedback(message.text)
            return
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
        value = message.command_text
        selected = popup.selected_command
        if selected is not None and selected.takes_arguments:
            self._edit_command(f"{selected.name} ", draft=message.draft)
            return
        argument_command = popup.argument_command
        selected_argument = popup.selected_argument
        if argument_command is not None and selected_argument is not None:
            _command_text, _separator, argument = message.command_text.partition(" ")
            head, head_separator, remainder = argument.strip().partition(" ")
            option_is_complete = head.casefold() == selected_argument.value.casefold()
            if not option_is_complete or (
                selected_argument.requires_value and (not head_separator or not remainder.strip())
            ):
                selected_value = f"{argument_command.name} {selected_argument.value}"
                if selected_argument.requires_value:
                    self._edit_command(f"{selected_value} ", draft=message.draft)
                    return
                value = selected_value
        value = selected.name if selected is not None else value
        composer.restore_command_draft(message.draft)
        content = composer.input_content
        self._remember_input_draft(
            self._controller.session_id,
            content.text,
            attachments=content.attachments,
            skills=content.skills,
        )
        popup.hide()
        self._dismissed_command_text = None
        await self._dispatch_command(value)
        if self.is_running:
            composer.focus()

    def on_composer_command_navigated(self, message: Composer.CommandNavigated) -> None:
        popup = self.query_one(CommandPopup)
        popup.navigate(message.offset)
        composer = self.query_one(Composer)
        self._sync_command_suggestion(composer, popup)

    def on_composer_command_dismissed(self, _message: Composer.CommandDismissed) -> None:
        composer = self.query_one(Composer)
        if composer.attachment_menu_open:
            self._close_attachment_menu()
            return
        context = composer.command_context()
        if context is not None and context[1]:
            composer.restore_command_draft(context[1])
            self._dismissed_command_text = None
            composer.suggestion = ""
            self.query_one(CommandPopup).hide()
            return
        self._dismissed_command_text = composer.text
        composer.suggestion = ""
        self.query_one(CommandPopup).hide()

    @work(group="interaction")
    async def _resolve_approval(
        self,
        interaction: PendingInteraction,
        approved: bool,
    ) -> None:
        if self._visible_read_only:
            return
        revision = self._controller.selection_revision
        approval_id = interaction.request_id
        if approval_id is None:
            self.notify("Approval request has no identifier", severity="error", markup=False)
            self.query_one(InteractionBar).resume()
            return
        try:
            result = await self._controller.resolve_approval(
                interaction=interaction,
                approved=approved,
            )
            if result is not None:
                await self._apply_run_result(result)
            if revision == self._controller.selection_revision:
                await self._refresh_interaction()
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            if self._interaction == interaction:
                self.query_one(InteractionBar).resume()

    @work(group="interaction")
    async def _answer_input(self, interaction: PendingInteraction, answer: JsonValue) -> None:
        if self._visible_read_only:
            return
        revision = self._controller.selection_revision
        self.query_one(Composer).disabled = True
        try:
            result = await self._controller.answer(interaction=interaction, answer=answer)
            if result is not None:
                await self._apply_run_result(result)
            if revision == self._controller.selection_revision:
                await self._refresh_interaction()
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            if self._interaction != interaction:
                return
            plan_panel = self.query_one(PlanConfirmationPanel)
            if plan_panel.display:
                plan_panel.resume()
            else:
                self.query_one(AskUserPanel).resume()
        finally:
            if revision == self._controller.selection_revision:
                self._sync_interaction_focus(self._interaction)

    def on_interaction_bar_decision(self, message: InteractionBar.Decision) -> None:
        if self._visible_read_only:
            return
        self._resolve_approval(message.interaction, message.approved)

    def on_ask_user_panel_completed(self, message: AskUserPanel.Completed) -> None:
        interaction = self._interaction
        if interaction is not None and interaction.kind == "question":
            self._answer_input(interaction, message.answer)

    def on_ask_user_panel_freeform_requested(
        self,
        _message: AskUserPanel.FreeformRequested,
    ) -> None:
        self._sync_composer_placeholder()
        self._sync_interaction_focus(self._interaction)

    def on_plan_confirmation_panel_completed(
        self,
        message: PlanConfirmationPanel.Completed,
    ) -> None:
        interaction = self._interaction
        if interaction is not None and interaction.kind == "question":
            self._answer_input(interaction, message.answer)

    def on_plan_confirmation_panel_feedback_requested(
        self,
        _message: PlanConfirmationPanel.FeedbackRequested,
    ) -> None:
        self._sync_composer_placeholder()
        self._sync_interaction_focus(self._interaction)

    def _sync_activity(self, activity: RuntimeActivitySnapshot) -> None:
        status_line = self.query_one(StatusLine)
        if self._activity_override is None:
            status_line.set_activity(
                activity.activity,
                model_attempt=activity.model_attempt,
                model_max_attempts=activity.model_max_attempts,
            )
        status_line.set_context_metrics(activity.effective_context_metrics)
        if activity.session_id == self._controller.session_id:
            root_status_changed = activity.status != self._agent_root_status
            self._agent_root_status = activity.status
            root_session_id = self._controller.session_id
            if root_status_changed and root_session_id is not None:
                self._sync_subagent_navigation_panel(root_session_id=root_session_id)
        self._sync_composer_placeholder()

    async def _refresh_interaction(
        self,
        *,
        refresh_agents: bool = False,
    ) -> PendingInteraction | None:
        if self._host_closing or not self.query(Composer):
            return None
        if self._visible_read_only:
            if self._interaction is not None:
                self._interaction = None
                self._sync_interaction(None)
            else:
                self._sync_interaction_focus(None)
            return None
        projection = self._projection
        primary = projection.pending_interaction if projection else None
        current_is_agent = self._interaction is not None and self._interaction.is_subagent
        agent_interactions = (
            await self._controller.agent_interactions()
            if primary is None and (refresh_agents or current_is_agent)
            else ()
        )
        if self._host_closing or not self.query(Composer) or self._projection is not projection:
            return None
        interaction = primary or (
            project_agent_interaction(agent_interactions[0]) if agent_interactions else None
        )
        if interaction != self._interaction:
            self._interaction = interaction
            self._sync_interaction(interaction)
        else:
            if interaction is not None and interaction.kind == "approval":
                self.query_one(InteractionBar).update_pending_count(
                    self._pending_approval_count(interaction)
                )
            self._sync_interaction_focus(interaction)
        return interaction

    def _pending_approval_count(self, interaction: PendingInteraction) -> int:
        projection = self._projection
        if projection is None:
            return 1
        pending = tuple(item for item in projection.pending_interactions if item.kind == "approval")
        return len(pending) if interaction in pending else 1

    def _sync_interaction(self, interaction: PendingInteraction | None) -> None:
        if self._host_closing or not self.query(InteractionBar):
            return
        with self.batch_update():
            interaction_bar = self.query_one(InteractionBar)
            ask_user = self.query_one(AskUserPanel)
            plan_panel = self.query_one(PlanConfirmationPanel)
            if interaction is not None and interaction.kind == "question":
                interaction_bar.show_interaction(None)
                plan_request = project_plan_confirmation(interaction)
                if plan_request is not None:
                    ask_user.hide_panel()
                    plan_panel.show_request(plan_request)
                else:
                    plan_panel.hide_panel()
                    ask_user.show_request(
                        project_question_form(interaction.tool_name, interaction.request)
                    )
                self._sync_composer_placeholder()
                self._sync_interaction_focus(interaction)
                return
            ask_user.hide_panel()
            plan_panel.hide_panel()
            interaction_bar.show_interaction(
                interaction,
                pending_count=(
                    self._pending_approval_count(interaction)
                    if interaction is not None and interaction.kind == "approval"
                    else 1
                ),
            )
            self._sync_composer_placeholder()
            self._sync_interaction_focus(interaction)

    def _sync_interaction_focus(self, interaction: PendingInteraction | None) -> None:
        """Give the active interaction exclusive ownership of keyboard input."""

        if self._host_closing or not self.query(Composer):
            return
        composer = self.query_one(Composer)
        composer.attachments_enabled = not self._visible_read_only and interaction is None
        if not composer.attachments_enabled:
            self._close_attachment_menu()
        if composer.import_pending:
            composer.disabled = True
            return
        if self._visible_read_only:
            composer.disabled = True
            return
        if interaction is not None and interaction.kind == "approval":
            composer.disabled = True
            self.query_one(InteractionBar).focus()
            return
        if interaction is not None and interaction.kind == "question":
            plan_panel = self.query_one(PlanConfirmationPanel)
            ask_user = self.query_one(AskUserPanel)
            if plan_panel.display:
                composer.disabled = not plan_panel.awaiting_feedback
                (composer if plan_panel.awaiting_feedback else plan_panel).focus()
                return
            if ask_user.display:
                composer.disabled = not ask_user.awaiting_freeform
                (composer if ask_user.awaiting_freeform else ask_user).focus()
                return
        composer.disabled = False
        composer.focus()

    def _sync_composer_placeholder(self) -> None:
        """Project run and interaction state onto the composer hint."""

        if self._host_closing or not self.query(Composer):
            return
        if self._visible_read_only:
            composer = self.query_one(Composer)
            composer.set_subagent_placeholder()
            return
        interaction = self._interaction
        if interaction is not None and interaction.kind == "question":
            plan_panel = self.query_one(PlanConfirmationPanel)
            if plan_panel.display and plan_panel.awaiting_feedback:
                placeholder = Composer.PLAN_FEEDBACK_PLACEHOLDER
            else:
                placeholder = (
                    Composer.FREEFORM_PLACEHOLDER
                    if self.query_one(AskUserPanel).awaiting_freeform
                    else Composer.QUESTION_PLACEHOLDER
                )
        elif self._has_active_run():
            composer = self.query_one(Composer)
            composer.set_active_run_placeholder()
            return
        else:
            placeholder = Composer.DEFAULT_PLACEHOLDER
        self.query_one(Composer).set_plain_placeholder(placeholder)

    def _edit_command(self, value: str, *, draft: str = "") -> None:
        self._close_attachment_menu()
        composer = self.query_one(Composer)
        composer.load_command(value, draft=draft)
        composer.focus()

    def _edit_draft(self, value: str | InputContent) -> None:
        self._close_attachment_menu()
        composer = self.query_one(Composer)
        composer.load_input(value if isinstance(value, InputContent) else InputContent(text=value))
        composer.move_cursor((composer.document.line_count - 1, len(composer.document.lines[-1])))
        composer.focus()

    def _restore_input_draft(self, session_id: str | None) -> None:
        self._close_attachment_menu()
        self.query_one(Composer).bind_history(session_id)
        draft = (
            self._input_drafts.content_for(session_id)
            if self._preferences.input_draft_memory_enabled
            else InputContent()
        )
        if not self._preferences.input_draft_memory_enabled:
            self._remember_input_draft(session_id, "")
        self.query_one(Composer).restore_command_draft("")
        self.query_one(Composer).load_input(draft)

    def _remember_input_draft(
        self,
        session_id: str | None,
        text: str,
        *,
        attachments: tuple[InputFile, ...] = (),
        skills: tuple[InputSkillReference, ...] = (),
    ) -> None:
        self._set_input_drafts(
            self._input_drafts.remember(session_id, text, attachments=attachments, skills=skills)
        )

    def _forget_input_drafts(self, session_ids: Sequence[str]) -> None:
        if not self._preferences.input_draft_memory_enabled:
            return
        self._set_input_drafts(self._input_drafts.forget_sessions(session_ids))

    def _set_input_drafts(
        self,
        drafts: TuiInputDrafts,
        *,
        force_persist: bool = False,
    ) -> None:
        if drafts == self._input_drafts and not force_persist:
            return
        self._input_drafts = drafts
        # A rejected on-disk artifact must survive until explicit data maintenance.
        if self._input_drafts_error is not None:
            return
        if not self._preferences.input_draft_memory_enabled and not force_persist:
            return
        self._input_draft_revision += 1
        task = self._input_draft_save_task
        if task is None or task.done():
            self._input_draft_save_task = asyncio.create_task(
                self._persist_input_drafts(),
                name="tui-input-drafts",
            )

    async def _persist_input_drafts(self) -> None:
        try:
            while True:
                revision = self._input_draft_revision
                await asyncio.sleep(_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS)
                if revision != self._input_draft_revision:
                    continue
                drafts = (
                    self._input_drafts
                    if self._preferences.input_draft_memory_enabled
                    else TuiInputDrafts()
                )
                try:
                    await asyncio.to_thread(self._input_draft_store.save, drafts)
                except OuroError as exc:
                    _LOG.warning("Could not persist TUI input drafts: %s", exc)
                    if not self._input_draft_error_notified and not self._host_closing:
                        self._input_draft_error_notified = True
                        self.notify(str(exc), severity="warning", markup=False)
                    if revision != self._input_draft_revision:
                        continue
                    return
                if revision == self._input_draft_revision:
                    return
        finally:
            self._input_draft_save_task = None

    async def _dispatch_command(self, value: str) -> None:
        command, argument = resolve_command(value)
        if command is None:
            self._run_plugin_command(value)
            return
        if command.name == "/quit":
            await self.action_quit()
        elif command.name == "/new":
            await self._new_session()
        elif command.name == "/workspace":
            if argument.casefold() == _CLEAR_WORKSPACE_HISTORY_ARGUMENT:
                await self._clear_recent_workspaces()
            elif argument:
                await self._select_workspace(argument)
        elif command.name == "/sessions":
            self.action_sessions()
        elif command.name == "/actions":
            self.action_actions()
        elif command.name in {"/continue", "/undo"}:
            if argument:
                self.notify(f"{command.name} does not accept arguments", severity="warning")
            else:
                self._edit_session_history(command.name)
        elif command.name == "/fork":
            if argument:
                self._fork_session(argument.casefold())
        elif command.name == "/model":
            if argument:
                await self._select_model(argument)
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
        elif command.name == "/thoughts":
            if argument:
                await self._set_reasoning_expanded(argument.casefold())
        elif command.name == "/compact":
            if not argument:
                self._compact_session()
        elif command.name == "/summary":
            if argument:
                self.notify("Usage: /summary", severity="warning")
            else:
                self._show_session_summaries()
        elif command.name == "/export":
            if argument:
                self.notify("Usage: /export", severity="warning")
            else:
                self._export_current_session(self._visible_session_id())
        elif command.name == "/unqueue":
            if argument:
                await self._unqueue_message(argument)
        elif command.name == "/clear":
            await self._show_welcome()
        elif command.name == "/navigate":
            self._navigate_to(argument)
        elif command.name == "/abort":
            self.action_abort()
        elif command.name == "/help":
            self.action_help()

    def _command_catalog(self) -> tuple[TuiCommand, ...]:
        catalog = list(TUI_COMMANDS)
        argument_owner = self._active_builtin_argument_command()
        user_message_options = (
            self._user_message_arguments() if argument_owner in {"/fork", "/navigate"} else ()
        )
        for index, command in enumerate(catalog):
            if command.name == "/workspace" and argument_owner == command.name:
                catalog[index] = replace(
                    command,
                    argument_hint=(
                        command.argument_hint if self._controller.session_id is None else None
                    ),
                    argument_options=self._workspace_arguments(),
                    freeform_arguments=self._controller.session_id is None,
                )
            elif command.name == "/navigate" and argument_owner == command.name:
                catalog[index] = replace(
                    command,
                    argument_options=self._navigate_options(
                        user_message_options=user_message_options
                    ),
                )
            elif command.name == "/fork" and argument_owner == command.name:
                catalog[index] = replace(
                    command,
                    argument_options=self._fork_arguments(
                        user_message_options=user_message_options
                    ),
                )
            elif (
                command.name == "/thinking"
                and argument_owner == command.name
                and self._reasoning_state is not None
            ):
                options = _reasoning_arguments(self._reasoning_state)
                catalog[index] = replace(
                    command,
                    argument_hint=(
                        "[" + "|".join(option.value for option in options) + "]"
                        if options
                        else None
                    ),
                    argument_options=options,
                )
            elif command.name == "/model" and argument_owner == command.name:
                options = tuple(
                    TuiCommandArgument(
                        value=model.key,
                        description=model.provider_display_name or "Other models",
                        label=model_display_label(model),
                    )
                    for model in sorted_models(self._models)
                )
                catalog[index] = replace(command, argument_options=options)
            elif command.name == "/unqueue" and argument_owner == command.name:
                catalog[index] = replace(
                    command,
                    argument_options=self._queued_input_arguments(),
                )
        return (*catalog, *self._plugin_commands())

    def _active_builtin_argument_command(self) -> str | None:
        """Return the builtin whose argument menu is currently being edited."""

        if not self.query(Composer):
            return None
        context = self.query_one(Composer).command_context()
        if context is None:
            return None
        command_text, _draft = context
        name, separator, _argument = command_text.partition(" ")
        if not separator:
            return None
        command, _normalized_argument = resolve_command(name, TUI_COMMANDS)
        return command.name if command is not None else None

    def _workspace_arguments(self) -> tuple[TuiCommandArgument, ...]:
        recent_workspaces = (
            self._preferences.recent_workspaces if self._controller.session_id is None else ()
        )
        return (
            *(
                TuiCommandArgument(workspace, "", is_suggestion=True)
                for workspace in recent_workspaces
            ),
            TuiCommandArgument(
                _CLEAR_WORKSPACE_HISTORY_ARGUMENT,
                "",
                label="clear",
                accent=self._tui_theme.danger,
            ),
        )

    async def _select_workspace(self, value: str) -> None:
        try:
            workspace = await self._controller.set_workspace(value)
        except Exception as exc:  # noqa: BLE001 - validation errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        self._active_workspace = workspace
        self._system_status = replace(self._system_status, workspace=workspace)
        self.query_one(BrandHeader).set_context(workspace=workspace)
        self.query_one(ConversationView).set_system_status(self._system_status)
        await self._remember_workspace(workspace)

    def _queued_input_arguments(self) -> tuple[TuiCommandArgument, ...]:
        inputs = self.query_one(QueuedInputPanel).inputs
        return tuple(
            TuiCommandArgument(
                value=item.queue_id,
                description=(
                    "Follow-up message" if item.mode == "follow_up" else "Active-run guidance"
                ),
                label=f"{index}. {_queued_input_preview(item)}",
                trailing_badge=file_count_label(item.attachments, item.resource_refs),
            )
            for index, item in enumerate(inputs, start=1)
        )

    def _skill_arguments(self) -> tuple[TuiCommandArgument, ...]:
        try:
            skills = self._controller.skill_invocation_options()
        except Exception:  # noqa: BLE001 - startup may not have completed yet.
            return ()
        return tuple(
            TuiCommandArgument(
                skill.name,
                skill.description,
            )
            for skill in skills
        )

    async def _unqueue_message(self, queue_id: str) -> None:
        session_id = self._controller.session_id
        revision = self._controller.selection_revision
        try:
            cancelled = await self._controller.cancel_queued_user_input(queue_id)
        except Exception as exc:  # noqa: BLE001 - surface application boundary failures.
            self.notify(str(exc), severity="error", markup=False)
            return
        if cancelled is None:
            self.notify(
                "That message is no longer queued",
                severity="warning",
                markup=False,
            )
            return
        attachments = cancelled.attachments or await self._controller.input_attachments(
            session_id or "", cancelled.resource_refs
        )
        content = InputContent(
            text=cancelled.prompt, attachments=attachments, skills=cancelled.skills
        )
        current = (
            self.query_one(Composer).draft_content
            if revision == self._controller.selection_revision
            else self._input_drafts.content_for(session_id)
        )
        restored = current.followed_by(content)
        self._remember_input_draft(
            session_id, restored.text, attachments=restored.attachments, skills=restored.skills
        )
        if revision == self._controller.selection_revision:
            self._edit_draft(restored)
        if not current.is_empty:
            self.notify("Restored queued input after the current draft")
        try:
            await self._sync_queued_inputs()
        except Exception as exc:  # noqa: BLE001 - cancellation and restoration already succeeded.
            self.notify(f"Could not refresh queued inputs: {exc}", severity="warning", markup=False)

    def _navigate_options(
        self,
        *,
        user_message_options: tuple[TuiCommandArgument, ...] | None = None,
    ) -> tuple[TuiCommandArgument, ...]:
        """Build /navigate argument options from the live dialogue state."""
        return (
            TuiCommandArgument("top", "Jump to the first message"),
            TuiCommandArgument("bottom", "Jump to the latest message"),
            *(
                user_message_options
                if user_message_options is not None
                else self._user_message_arguments()
            ),
        )

    def _fork_arguments(
        self,
        *,
        user_message_options: tuple[TuiCommandArgument, ...] | None = None,
    ) -> tuple[TuiCommandArgument, ...]:
        """Build /fork targets through the same numbered user-message projection."""

        if self._controller.session_id is None:
            return ()
        return (
            TuiCommandArgument(
                "full",
                "Fork the complete current session",
                label="Full",
            ),
            *(
                user_message_options
                if user_message_options is not None
                else self._user_message_arguments()
            ),
        )

    def _user_message_arguments(self) -> tuple[TuiCommandArgument, ...]:
        """Project visible user turns as stable numbered command options."""

        try:
            conversation = self.query_one(ConversationView)
        except Exception:  # noqa: BLE001 — app may not have mounted the view yet.
            return ()
        return tuple(
            TuiCommandArgument(f"#{index}", _summarize(text))
            for index, text in enumerate(conversation.user_turn_summaries(), start=1)
        )

    def _plugin_commands(self) -> tuple[TuiCommand, ...]:
        try:
            commands = self._controller.plugin_commands()
        except Exception:  # noqa: BLE001 - startup may not have completed yet.
            return ()
        name_counts = Counter(item.name for item in commands)
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
                title=(
                    item.description
                    or f"Plugin command{self._tui_theme.inline_separator}{item.plugin_id}"
                ),
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
            matches.append(replace(command, freeform_arguments=bool(command.input_hint)))
        return tuple(matches)

    @work(group="plugin-command")
    async def _run_plugin_command(self, value: str) -> None:
        revision = self._controller.selection_revision
        command_text, _, argument = value.strip().partition(" ")
        selector = command_text.removeprefix("/")
        creating_projection = self._projection is None
        if creating_projection:
            self._waiting_for_session_event = True
            self.query_one(Composer).disabled = True
        try:
            result = await self._controller.execute_plugin_command(selector, argument.strip())
            if revision != self._controller.selection_revision:
                await self._refresh_welcome_status()
                return
            if result is None:
                self.notify(
                    f"Unknown command: {command_text}. Use /help to see available commands.",
                    severity="warning",
                    markup=False,
                )
                return
            execution = result.execution
            self.query_one(StatusLine).set_session_controls(
                tuple(status.label for status in result.session_controls)
            )
            command_output = (
                execution.output if execution.command_output is None else execution.command_output
            )
            command_data = (
                execution.data if execution.command_data is None else execution.command_data
            )
            has_command_presentation = bool(command_output or command_data)
            if (
                has_command_presentation
                and not result.run_results
                and self._projection is None
                and self._controller.session_id is not None
            ):
                await self._open_session(self._controller.session_id)
            if command_output:
                await self.query_one(ConversationView).add_command_output(command_output)
            elif command_data:
                await self.query_one(ConversationView).add_command_output(
                    json.dumps(command_data, ensure_ascii=False, indent=2)
                )
            for run_result in result.run_results:
                await self._apply_run_result(run_result)
                await self._sync_queued_inputs(run_result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - plugin failures are user-visible.
            self.notify(str(exc), severity="error", markup=False)
        finally:
            if creating_projection and revision == self._controller.selection_revision:
                self._waiting_for_session_event = False
                self._sync_interaction_focus(self._interaction)

    @work(exclusive=True, group="session-summary")
    async def _show_session_summaries(self) -> None:
        session_id = self._visible_session_id()
        if session_id is None:
            self.notify("Open a session to view summaries", severity="warning")
            return
        try:
            summaries = await self._controller.session_summaries(session_id)
        except Exception as exc:  # noqa: BLE001 - query errors are user-visible.
            _LOG.warning("Could not load session summaries: %s", exc)
            self.notify("Could not load summaries for the current session", severity="error")
            return
        async with self._runtime_event_apply_lock:
            if not self._surface_available() or self._visible_session_id() != session_id:
                return
            await self.query_one(ConversationView).add_command_output(
                _format_session_summaries(summaries)
            )

    @work(exclusive=True, group="compaction")
    async def _compact_session(self) -> None:
        if self._has_active_run():
            self.notify("Stop the current task before compacting context", severity="warning")
            return
        if self._controller.session_id is None:
            return
        composer = self.query_one(Composer)
        composer.disabled = True
        self.query_one(StatusLine).set_activity("compacting")
        try:
            result = await self._controller.compact()
            await self._flush_pending_runtime_events()
            await self._apply_runtime_events(result.events)
            await self._refresh_welcome_status()
        except Exception as exc:  # noqa: BLE001 - compaction errors are user-visible.
            self.query_one(StatusLine).set_activity("failed")
            self.notify(str(exc), severity="error", markup=False)
        finally:
            self._sync_interaction_focus(self._interaction)

    async def _new_session(self) -> None:
        try:
            model = await self._controller.new_session()
        except Exception as exc:  # noqa: BLE001 - keep the current view on failure.
            self.notify(str(exc), severity="error", markup=False)
            return
        async with self._runtime_event_apply_lock:
            with self.batch_update():
                self._remember_visible_session_history()
                self._replace_observation(None)
                self._projection = None
                self._waiting_for_session_event = False
                self._pending_runtime_events.clear()
                self._visible_read_only = False
                self._visible_agent = None
                self._controlled_composer_draft = None
                self.query_one(Composer).set_class(False, "read-only-session")
                self._interaction = None
                await self._replace_agent_tree(None)
                self._active_title = DEFAULT_SESSION_TITLE
                self._active_workspace = (
                    self._controller.draft_workspace or self._system_status.workspace
                )
                self._system_status = replace(self._system_status, workspace=self._active_workspace)
                self.query_one(ConversationView).set_system_status(self._system_status)
                self.query_one(BrandHeader).set_context(
                    session_title=self._active_title,
                    workspace=self._active_workspace,
                )
                status_line = self.query_one(StatusLine)
                self._active_model = _status_model_label(model)
                status_line.set_model(self._active_model)
                status_line.set_activity("idle")
                status_line.set_context(None)
                status_line.set_subagent(None)
                status_line.set_session_controls(())
                self._sync_interaction(None)
                await self._refresh_approval_indicator()
                await self._refresh_reasoning_indicator()
                self.query_one(QueuedInputPanel).set_inputs(())
                await self._show_welcome()

    def action_new_session(self) -> None:
        menu = self._active_navigation_menu()
        if menu is None and len(self.screen_stack) > 1:
            return
        if menu is not None:
            menu.dismiss(None)
        _worker: Worker[None] = self.run_worker(
            self._new_session, exclusive=True, group="navigation"
        )

    def action_sessions(self) -> None:
        self._request_menu_navigation(_MENU_SESSIONS)

    def action_main_session(self) -> None:
        if not self._visible_read_only and not self._agent_switch_in_progress:
            return
        self._navigate_agent_node("root")

    def action_agent_node_left(self) -> None:
        self._navigate_agent_node("left")

    def action_agent_node_right(self) -> None:
        self._navigate_agent_node("right")

    def action_agent_node_root(self) -> None:
        self._navigate_agent_node("root")

    def action_agent_node_latest(self) -> None:
        self._navigate_agent_node("latest")

    def on_subagent_navigation_panel_navigation_requested(
        self,
        message: SubagentNavigationPanel.NavigationRequested,
    ) -> None:
        message.stop()
        if (
            self._host_closing
            or len(self.screen_stack) > 1
            or not self.query(SubagentNavigationPanel)
        ):
            return
        panel = self.query_one(SubagentNavigationPanel)
        if not panel.display:
            return
        panel.begin_navigation()
        self._request_agent_switch(panel.select_session(message.session_id))

    def _navigate_agent_node(
        self,
        direction: Literal["left", "right", "root", "latest"],
    ) -> None:
        if (
            self._host_closing
            or len(self.screen_stack) > 1
            or not self.query(SubagentNavigationPanel)
        ):
            return
        panel = self.query_one(SubagentNavigationPanel)
        if not panel.display:
            return
        panel.begin_navigation()
        if direction == "left":
            target_session_id = panel.select_relative(-1)
        elif direction == "right":
            target_session_id = panel.select_relative(1)
        elif direction == "root":
            target_session_id = panel.select_root()
        else:
            target_session_id = panel.select_latest_child()
        self._request_agent_switch(target_session_id)

    def _request_agent_switch(self, target_session_id: str | None) -> None:
        panel = self.query_one(SubagentNavigationPanel)
        if target_session_id is None:
            if not self._agent_switch_in_progress:
                panel.end_navigation()
            return
        target_agent = panel.selected_agent
        if target_session_id != self._controller.session_id and target_agent is None:
            if not self._agent_switch_in_progress:
                panel.end_navigation()
            return
        if not self._agent_switch_in_progress and target_session_id == self._visible_session_id():
            panel.end_navigation()
            return
        self._pending_agent_switch = (target_session_id, target_agent)
        if self._agent_switch_in_progress:
            return
        self._agent_switch_in_progress = True
        _worker: Worker[None] = self.run_worker(
            self._switch_agent_nodes,
            exclusive=True,
            group="navigation",
        )

    async def _switch_agent_nodes(self) -> None:
        try:
            while self._pending_agent_switch is not None:
                target_session_id, target_agent = self._pending_agent_switch
                self._pending_agent_switch = None
                if target_session_id == self._visible_session_id():
                    continue
                try:
                    self.query_one(StatusLine).set_activity("loading_session")
                    if target_session_id == self._controller.session_id:
                        await self._show_main_session()
                    elif target_agent is not None:
                        await self._open_child_session(target_agent)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - navigation failures are visible.
                    if self._surface_available():
                        self.notify(str(exc), severity="error", markup=False)
        except asyncio.CancelledError:
            raise
        finally:
            self._pending_agent_switch = None
            self._agent_switch_in_progress = False
            if self.query(SubagentNavigationPanel):
                panel = self.query_one(SubagentNavigationPanel)
                panel.set_current_session(self._visible_session_id())
                panel.end_navigation()

    def action_conversation_top(self) -> None:
        self._navigate_to("top")

    def action_conversation_bottom(self) -> None:
        self._navigate_to("bottom")

    def action_previous_user_message(self) -> None:
        self.query_one(ConversationView).scroll_to_previous_user_turn()

    def action_next_user_message(self) -> None:
        self.query_one(ConversationView).scroll_to_next_user_turn()

    def _visible_session_id(self) -> str | None:
        """Return the Session that currently owns the conversation surface."""

        projection = self._projection
        if projection is None or self.query_one(ConversationView).showing_welcome:
            return None
        return projection.session_id

    def _remember_session_history(
        self,
        session_id: str,
        state: ConversationHistoryState | None,
    ) -> None:
        """Retain recent inactive history without duplicating the active buffer."""

        self._session_history_states.pop(session_id, None)
        if state is None:
            return
        self._session_history_states[session_id] = state
        while len(self._session_history_states) > _SESSION_HISTORY_CACHE_SIZE:
            self._session_history_states.popitem(last=False)

    def _remember_visible_session_history(self) -> None:
        session_id = self._visible_session_id()
        if session_id is None:
            return
        conversation = self.query_one(ConversationView)
        held_scroll_y = conversation.held_scroll_y()
        if held_scroll_y is None:
            self._held_session_scroll_y.pop(session_id, None)
        else:
            self._held_session_scroll_y[session_id] = held_scroll_y
        self._remember_session_history(session_id, conversation.take_history_state())

    async def on_tool_navigation_requested(
        self,
        message: ToolNavigationRequested,
    ) -> None:
        message.stop()
        if self._visible_read_only:
            return
        parent_session_id = self._visible_session_id()
        if parent_session_id is None:
            return
        try:
            agent = await self._controller.resolve_agent_navigation(
                parent_session_id=parent_session_id,
                parent_run_id=message.parent_run_id,
                parent_tool_call_id=message.parent_tool_call_id,
            )
            if agent is None:
                self.notify(
                    "This subagent session is not available yet",
                    severity="warning",
                )
                return
            panel = self.query_one(SubagentNavigationPanel)
            panel.begin_navigation()
            self._request_agent_switch(panel.select_session(agent.session_id))
        except Exception as exc:  # noqa: BLE001 - navigation failures are user-visible.
            self.notify(str(exc), severity="error", markup=False)

    def _navigate_to(self, target: str) -> None:
        """Scroll the conversation to the target identified by the /navigate argument."""
        conversation = self.query_one(ConversationView)
        if target == "top":
            conversation.scroll_home(animate=False)
        elif target == "bottom":
            conversation.anchor()
        elif target.startswith("#") and target[1:].isdigit():
            index = int(target[1:]) - 1
            conversation.scroll_to_user_turn(index)

    async def _sync_task_status(self) -> None:
        if not self._surface_available():
            return
        counts, error = await self._controller.task_status()
        if counts != self._system_status.task_counts or error != self._system_status.task_error:
            self._system_status = replace(self._system_status, task_counts=counts, task_error=error)
            self.query_one(ConversationView).set_system_status(self._system_status)

    async def _choose_session(
        self,
        *,
        menu: ChoiceScreen | None = None,
        switch: bool = False,
        operation: Literal["open", "rename", "archive", "delete"] = "open",
    ) -> None:
        if not await self._refresh_sessions(include_subagents=True):
            return
        domain = "archived" if operation == "delete" else self._session_domain
        session_menu: ChoiceScreen

        def choices() -> tuple[Choice, ...]:
            return _session_choices(
                _sessions_in_domain(self._sessions, domain),
                selected_session_id=self._visible_session_id(),
                grouping=self._preferences.session_grouping,
                theme=self._tui_theme,
            )

        def title() -> str:
            label = {
                "open": "SESSION",
                "rename": "RENAME SESSION",
                "archive": "UNARCHIVE SESSIONS" if domain == "archived" else "ARCHIVE SESSIONS",
                "delete": "DELETE SESSIONS",
            }[operation]
            return self._tui_theme.section_separator.join((label, _SESSION_DOMAIN_LABELS[domain]))

        def hint() -> str:
            action = {
                "open": "select",
                "rename": "edit",
                "archive": "unarchive" if domain == "archived" else "archive",
                "delete": "confirm",
            }[operation]
            return self._tui_theme.inline_separator.join(
                (
                    *(("Tab switch sessions",) if operation != "delete" else ()),
                    "↑/↓ move",
                    "←/→ group",
                    *(("Space toggle",) if operation == "delete" else ()),
                    f"Enter {action}",
                    "Esc back",
                )
            )

        async def refresh_choices() -> tuple[Choice, ...] | None:
            if not await self._refresh_sessions(include_subagents=True):
                return None
            return choices()

        async def switch_domain() -> None:
            nonlocal domain
            if not await self._refresh_sessions(include_subagents=True):
                return
            domain = _next_cycle_value(tuple(_SESSION_DOMAIN_LABELS), domain)
            self._session_domain = domain
            session_menu.replace_page(
                navigation_id=_MENU_SESSIONS if operation == "open" else None,
                title=title(),
                placeholder="Filter sessions…",
                choices=choices(),
                on_select=select_session,
                on_tab=switch_domain,
                interaction_hint=hint(),
                retain_focus_position_on_select=operation == "archive",
            )

        async def select_session(session_id: str) -> tuple[Choice, ...] | None:
            if operation == "archive":
                try:
                    await self._controller.set_session_archived(
                        session_id, archived=domain != "archived"
                    )
                except Exception as exc:  # noqa: BLE001
                    self.notify(str(exc), severity="error", markup=False)
                    return None
                return await refresh_choices()
            # History rendering needs the destination screen to be visible.
            session_menu.dismiss(None)
            try:
                await self._open_session(session_id)
            except Exception as exc:  # noqa: BLE001 - one invalid Session must not exit TUI.
                self._show_session_load_error(session_id, exc)
            return None

        async def rename_session(session_id: str, new_title: str) -> tuple[Choice, ...] | None:
            try:
                renamed = await self._controller.rename_session(session_id, new_title)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error", markup=False)
                return None
            if session_id == self._controller.session_id:
                self._active_title = renamed.title
                self.query_one(BrandHeader).set_context(session_title=renamed.title)
            return await refresh_choices()

        async def delete_selected(session_ids: tuple[str, ...]) -> tuple[Choice, ...] | None:
            if not await self._refresh_sessions(include_subagents=True):
                return None
            archived_ids = {item.session_id for item in _sessions_in_domain(self._sessions, domain)}
            if not set(session_ids).issubset(archived_ids):
                self.notify("Only archived sessions can be deleted", severity="warning")
                return choices()
            current_session_id = self._controller.session_id
            try:
                removed = await self._controller.delete_sessions(session_ids)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error", markup=False)
                return None
            self._forget_input_drafts(removed)
            if current_session_id in removed:
                await self._new_session()
            return await refresh_choices()

        if operation == "delete":
            session_menu = MultiChoiceScreen(
                title=title(),
                placeholder="Filter archived sessions…",
                choices=choices(),
                on_confirm=delete_selected,
                selected_marker=self._tui_theme.selected_choice_marker,
                unselected_marker=self._tui_theme.unselected_choice_marker,
                confirm_label="Delete",
                destructive=True,
                interaction_hint=hint(),
            )
        elif operation == "rename":
            session_menu = EditableChoiceScreen(
                title=title(),
                placeholder="Filter sessions…",
                choices=choices(),
                on_save=rename_session,
                on_tab=switch_domain,
                interaction_hint=hint(),
            )
        else:
            if menu is not None:
                session_menu = menu
                if operation == "archive":
                    menu.push_page(
                        title=title(),
                        placeholder="Filter sessions…",
                        choices=choices(),
                        on_select=select_session,
                        on_tab=switch_domain,
                        interaction_hint=hint(),
                        retain_focus_position_on_select=True,
                    )
                    return
                navigate = (
                    menu.replace_page
                    if menu.navigation_id == _MENU_SESSIONS
                    else menu.switch_page
                    if switch
                    else menu.push_page
                )
                navigate(
                    navigation_id=_MENU_SESSIONS,
                    title=title(),
                    placeholder="Filter sessions…",
                    choices=choices(),
                    on_select=select_session,
                    on_tab=switch_domain,
                    interaction_hint=hint(),
                    retain_focus_position_on_select=operation == "archive",
                )
                return
            session_menu = ChoiceScreen(
                navigation_id=_MENU_SESSIONS,
                title=title(),
                placeholder="Filter sessions…",
                choices=choices(),
                on_select=select_session,
                on_tab=switch_domain,
                interaction_hint=hint(),
            )
        if menu is not None:
            await menu.open_child(session_menu)
        else:
            self.push_screen(session_menu)

    async def _refresh_sessions(self, *, include_subagents: bool = False) -> bool:
        try:
            self._sessions = await self._controller.sessions(include_subagents=include_subagents)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error", markup=False)
            return False
        return True

    async def _open_session(self, session_id: str) -> None:
        previous_projection = self._projection
        status_line = self.query_one(StatusLine)
        status_line.set_activity("loading_session")
        subscription: RuntimeSubscription | None = None
        try:
            loaded = await self._controller.load_session(session_id)
            subscription = await self._controller.subscribe_session(
                session_id,
                after_sequence=loaded.event_high_water,
            )
            await self._install_session_surface(
                loaded,
                subscription=subscription,
            )
        except Exception, asyncio.CancelledError:
            if subscription is not None:
                subscription.close()
            if previous_projection is None:
                status_line.set_activity("idle")
            else:
                self._sync_activity(previous_projection.activity)
            raise

    async def _install_session_surface(
        self,
        loaded: LoadedSession,
        *,
        subscription: RuntimeSubscription,
    ) -> None:
        session = loaded.detail.session
        projection, run_presentations, active_stream_tail = await asyncio.to_thread(
            self._prepare_loaded_session,
            loaded,
            self._models,
        )
        agents = await self._replace_agent_tree(session.agent_root_session_id)
        read_only = session.session_kind == "subagent"
        agent = next(
            (
                candidate
                for candidate in agents
                if candidate.session_id == session.session_id
                and candidate.delegation_id == session.delegation_id
            ),
            None,
        )
        if read_only and agent is None:
            raise PersistenceError(
                f"subagent session {session.session_id} is absent from Agent tree "
                f"{session.agent_root_session_id}"
            )
        async with self._runtime_event_apply_lock:
            # Session history still renders in bounded chunks, but switching the
            # shared surface is one visual commit. Repainting an incomplete
            # destination would expose its growing height and stale scroll state.
            with self.batch_update():
                conversation = self.query_one(ConversationView)
                visible_session_id = self._visible_session_id()
                self._remember_visible_session_history()
                reusable_history = self._session_history_states.pop(
                    session.session_id,
                    None,
                )
                composer = self.query_one(Composer)
                if read_only and not self._visible_read_only:
                    self._controlled_composer_draft = (
                        composer.text
                        if self._projection is not None
                        else self._input_drafts.draft_for(session.agent_root_session_id)
                    )
                    composer.restore_command_draft("")
                elif not read_only and self._visible_read_only:
                    if (
                        self._visible_agent is not None
                        and self._visible_agent.root_session_id == session.session_id
                    ):
                        composer.restore_command_draft(self._controlled_composer_draft or "")
                    else:
                        self._restore_input_draft(session.session_id)
                    self._controlled_composer_draft = None
                elif not read_only and visible_session_id != session.session_id:
                    self._restore_input_draft(session.session_id)
                # The selected Session now owns the surface. Clear every interaction
                # projected by the previous Session before yielding while its history
                # is mounted; the new projection will restore its own pending request.
                self._visible_read_only = read_only
                self._visible_agent = agent
                composer.set_class(read_only, "read-only-session")
                self.query_one(SubagentNavigationPanel).set_current_session(session.session_id)
                self._projection = None
                self._waiting_for_session_event = False
                self._interaction = None
                self._sync_interaction(None)
                await conversation.load_messages(
                    loaded.messages,
                    loaded.compactions,
                    run_presentations,
                    mutable_tool_calls=(
                        (projection.activity.run_id, tool.tool_call_id)
                        for tool in projection.activity.tool_calls
                    ),
                    visible_scaffold_delegation_id=(
                        session.delegation_id if session.session_kind == "subagent" else None
                    ),
                    held_scroll_y=self._held_session_scroll_y.get(session.session_id),
                    reusable_history=reusable_history,
                )
                self._set_welcome_mode(conversation.showing_welcome)
                self.query_one(QueuedInputPanel).set_inputs(
                    () if read_only else loaded.queued_inputs
                )
                await self._restore_projection(
                    projection,
                    active_stream_tail=active_stream_tail,
                )
                conversation.sync_agents(agents)
                agent_name = None if agent is None else agent.nickname or agent.agent_name
                self._active_title = agent_name if agent_name is not None else session.title
                self._active_workspace = session.workspace or self._system_status.workspace
                if session.current_model is not None:
                    self._active_model = _model_display_name(
                        session.current_model,
                        self._models,
                    )
                self.query_one(BrandHeader).set_context(
                    session_title=self._active_title,
                    session_detail=agent.task_description if agent is not None else None,
                    workspace=self._active_workspace,
                )
                status_line = self.query_one(StatusLine)
                status_line.set_model(self._active_model)
                status_line.set_subagent(agent if read_only else None)
                status_line.set_session_controls(
                    () if read_only else tuple(status.label for status in loaded.session_controls)
                )
                if read_only:
                    composer.disabled = True
                    composer.set_subagent_placeholder()
                else:
                    await self._refresh_approval_indicator()
                    await self._refresh_reasoning_indicator()
                    self._sync_composer_placeholder()
                    self._sync_interaction_focus(self._interaction)
        self._replace_observation(subscription)

    async def _open_child_session(self, agent: AgentTreeNode) -> None:
        observation = await self._controller.observe_session(agent.session_id)
        try:
            await self._install_session_surface(
                observation.loaded,
                subscription=observation.subscription,
            )
        except Exception, asyncio.CancelledError:
            observation.subscription.close()
            raise

    async def _show_main_session(self) -> None:
        session_id = self._controller.session_id
        if session_id is None:
            return
        observation = await self._controller.observe_session(session_id)
        try:
            await self._install_session_surface(
                observation.loaded,
                subscription=observation.subscription,
            )
        except Exception, asyncio.CancelledError:
            observation.subscription.close()
            raise

    async def _reload_visible_observation(self) -> None:
        agent = self._visible_agent
        try:
            if self._visible_read_only and agent is not None:
                await self._open_child_session(agent)
            else:
                await self._show_main_session()
        except Exception as exc:  # noqa: BLE001 - observation recovery is user-visible.
            self.notify(str(exc), severity="error", markup=False)

    def _show_session_load_error(
        self,
        session_id: str,
        error: Exception,
    ) -> None:
        self.push_screen(self._session_load_error_screen(session_id, str(error)))

    def _session_load_error_screen(
        self,
        session_id: str,
        error: str,
    ) -> ChoiceScreen:
        return ChoiceScreen(
            title="SESSION LOAD FAILED",
            placeholder="Press Enter to close…",
            choices=(
                Choice(
                    "close",
                    "Close",
                    description=f"Could not open {session_id}:\n{error}",
                    description_accent=self._tui_theme.failure,
                ),
            ),
        )

    @work(group="session-history")
    async def _edit_session_history(self, command: str) -> None:
        if (
            self._visible_read_only
            or (command != "/undo" and self._has_active_run())
            or self._activity_override is not None
            or self._history_edit_in_progress
        ):
            self.notify("This operation requires an idle main session", severity="warning")
            return
        if self._controller.session_id is None:
            self.notify("There is no session history to edit", severity="warning")
            return
        self._history_edit_in_progress = True
        session_id = self._controller.session_id
        revision = self._controller.selection_revision
        if command == "/undo":
            self._activity_override = "undoing"
            self.query_one(Composer).disabled = True
        self.query_one(StatusLine).set_activity(
            "preparing_model" if command == "/continue" else "undoing"
        )
        try:
            if command == "/undo":
                selected = await self._controller.undo()
                text = selected.input.text_content
                attachments = selected.attachments or await self._controller.input_attachments(
                    selected.session.session_id, selected.resource_refs
                )
                skills = tuple(
                    InputSkillReference(
                        name=block.name, text_offset=block.text_offset, order=block.order
                    )
                    for block in selected.input.content
                    if isinstance(block, SkillActivationContent)
                )
                self._remember_input_draft(
                    selected.session.session_id, text, attachments=attachments, skills=skills
                )
                if not self._owns_session_surface(session_id):
                    return
                await self._show_main_session()
                self._edit_draft(InputContent(text=text, attachments=attachments, skills=skills))
                if selected.resource_refs:
                    self.notify(
                        f"Restored {len(selected.resource_refs)} attachment(s) to this input"
                    )
            else:
                result = await self._controller.continue_session()
                await self._apply_run_result(result)
                if not self._owns_session_surface(session_id):
                    return
                await self._show_main_session()
                if result.run.status == "paused":
                    self.notify(describe_run_pause(result.run), severity="warning", markup=False)
                elif result.run.status == "failed":
                    self.notify(
                        f"Continue failed: {result.run.error}", severity="warning", markup=False
                    )
        except Exception as exc:  # noqa: BLE001 - command failures are user-visible.
            self.notify(str(exc), severity="warning", markup=False)
        finally:
            self._history_edit_in_progress = False
            self._activity_override = None
            if revision == self._controller.selection_revision:
                self._restore_projection_activity(self._projection)
                self._sync_interaction_focus(self._interaction)

    @work(exclusive=True, group="session-fork")
    async def _fork_session(
        self,
        target: str,
    ) -> None:
        if self._has_active_run():
            self.notify("Stop the current task before copying the conversation", severity="warning")
            return
        if self._controller.session_id is None:
            return
        selected_message: MessageRecord | None = None
        previous_projection = self._projection
        self._activity_override = "forking"
        self.query_one(StatusLine).set_activity(self._activity_override)
        try:
            if target == "full":
                result = await self._controller.branch_session()
            else:
                points = await self._controller.branch_points()
                if not target.startswith("#") or not target[1:].isdigit():
                    raise ValueError(f"unknown fork target: {target}")
                index = int(target[1:]) - 1
                if not 0 <= index < len(points):
                    raise ValueError(f"user message is not a fork point: {target}")
                selected_message = points[index]
                result = await self._controller.branch_session(selected_message.message_id)
            menu = self._active_navigation_menu()
            if menu is not None:
                menu.dismiss(None)
            self._activity_override = None
            await self._open_session(result.session.session_id)
        except asyncio.CancelledError:
            self._activity_override = None
            self._restore_projection_activity(previous_projection)
            raise
        except Exception as exc:  # noqa: BLE001 - branch validation is user-visible.
            self._activity_override = None
            self._restore_projection_activity(previous_projection)
            self.notify(str(exc), severity="error", markup=False)
            return
        if selected_message is not None:
            self._edit_draft(user_message_text(selected_message))

    def _restore_projection_activity(
        self,
        projection: TuiSessionProjection | None,
    ) -> None:
        if projection is None:
            self.query_one(StatusLine).set_activity("idle")
        else:
            self._sync_activity(projection.activity)

    @staticmethod
    def _prepare_loaded_session(
        loaded: LoadedSession,
        models: tuple[AvailableModel, ...],
    ) -> tuple[
        TuiSessionProjection,
        tuple[RunPresentation, ...],
        tuple[str, str],
    ]:
        """Build persisted projections without occupying Textual's event loop."""

        ordered_events = canonical_event_order(loaded.events)
        projection = TuiSessionProjection(
            loaded.detail.session.session_id, history_epoch=loaded.detail.session.history_epoch
        )
        projection.restore(
            ordered_events,
            event_high_water=loaded.event_high_water,
        )
        if loaded.detail.recent_run is not None:
            projection.activity = overlay_run_activity(
                projection.activity, loaded.detail.recent_run
            )
        projection.activity = projection.activity.model_copy(
            update={
                "context_status": loaded.context_status,
                "effective_context_metrics": loaded.effective_context_metrics,
            }
        )
        return (
            projection,
            project_run_presentations(
                (run for run in loaded.runs if run.kind == "agent"),
                loaded.messages,
                models=models,
            ),
            _active_stream_tail(ordered_events, loaded.messages),
        )

    async def _restore_projection(
        self,
        projection: TuiSessionProjection,
        *,
        active_stream_tail: tuple[str, str],
    ) -> None:
        self._projection = projection
        self._sync_activity(projection.activity)
        if self._visible_read_only:
            self._interaction = None
            self._sync_interaction(None)
        else:
            await self._refresh_interaction(refresh_agents=True)
        conversation = self.query_one(ConversationView)
        reasoning, answer = active_stream_tail
        if projection.activity.run_id is not None:
            conversation.begin_model_request(run_id=projection.activity.run_id)
        if reasoning:
            await conversation.append_reasoning(
                reasoning,
                started_at=projection.reasoning_started_at,
            )
            if projection.reasoning_duration_seconds is not None:
                conversation.finish_reasoning(projection.reasoning_duration_seconds)
        if answer:
            await conversation.append_assistant(answer)
        await conversation.sync_tools(projection.activity.tool_calls)

    async def _select_model(self, selector: str) -> None:
        applies_to_next_request = self._has_active_run()
        try:
            model = await self._controller.select_model(selector)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error", markup=False)
            return
        self._active_model = _status_model_label(model)
        status_line = self.query_one(StatusLine)
        status_line.set_model(self._active_model)
        await self._refresh_reasoning_indicator()
        projection = self._projection
        metrics = None
        if projection is not None:
            metrics = projection.activity.effective_context_metrics
            if metrics is not None:
                metrics = metrics.with_model_limits(
                    context_window_tokens=model.context_window,
                    reserved_output_tokens=model.max_output_tokens,
                )
                projection.activity = projection.activity.model_copy(
                    update={"effective_context_metrics": metrics}
                )
        status_line.set_context_metrics(metrics)
        if applies_to_next_request:
            reasoning = (
                f"{self._tui_theme.inline_separator}{_format_reasoning_state(self._reasoning_state)}"
                if self._reasoning_state is not None
                else ""
            )
            self.notify(f"Model: {self._active_model}{reasoning} (next request)")

    def action_approval_settings(self) -> None:
        if self._visible_read_only:
            return
        self._choose_approval_mode()

    async def action_cycle_approval(self) -> None:
        if self._visible_read_only:
            return
        try:
            current = await self._controller.session_approval_preset()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        await self._set_approval_mode(_next_cycle_value(TUI_APPROVAL_PRESETS, current))

    @work(exclusive=True, group="navigation")
    async def _choose_approval_mode(self) -> None:
        try:
            current = await self._controller.session_approval_preset()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        choices = approval_choices(TUI_APPROVAL_PRESETS, selected=current, theme=self._tui_theme)
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=TOOL_APPROVAL_LABEL.upper(),
                placeholder="Choose when to ask for approval…",
                choices=choices,
            )
        )
        if selected is not None:
            await self._set_approval_mode(selected)

    async def _set_approval_mode(self, value: str) -> None:
        if value not in TUI_APPROVAL_PRESETS:
            return
        applies_to_next_approval = self._has_active_run()
        try:
            mode = await self._controller.set_session_approval_preset(value)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        self.query_one(StatusLine).set_approval_mode(mode)
        if applies_to_next_approval:
            self.notify(f"Approval: {mode} (next approval)")

    async def _refresh_approval_indicator(self) -> None:
        try:
            mode = await self._controller.session_approval_preset()
        except Exception:  # noqa: BLE001 - status projection must not block the Host.
            return
        self.query_one(StatusLine).set_approval_mode(mode)

    def action_thinking_settings(self) -> None:
        if self._visible_read_only:
            return
        self._choose_reasoning()

    async def action_cycle_thinking(self) -> None:
        if self._visible_read_only:
            return
        try:
            state = await self._controller.session_reasoning()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        values = _available_reasoning_values(state)
        if len(values) < 2:
            return
        await self._set_reasoning(_next_cycle_value(values, _reasoning_value(state.effective)))

    @work(exclusive=True, group="navigation")
    async def _choose_reasoning(self) -> None:
        await self._edit_reasoning()

    async def _edit_reasoning(self) -> None:
        try:
            state = await self._controller.session_reasoning()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        choices = reasoning_choices(
            state.capability, selected=_reasoning_value(state.effective), theme=self._tui_theme
        )
        if not choices:
            self.notify(
                "This endpoint does not expose configurable thinking settings",
                markup=False,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=MODEL_THINKING_LABEL.upper(),
                placeholder="Choose a thinking level…",
                choices=choices,
            )
        )
        if selected is not None:
            await self._set_reasoning(selected)

    async def _set_reasoning(self, value: str) -> None:
        request = REASONING_REQUESTS.get(value)
        if request is None:
            return
        try:
            state = await self._controller.set_session_reasoning(request)
        except Exception as exc:  # noqa: BLE001 - Runtime owns capability validation.
            self.notify(str(exc), severity="error", markup=False)
            return
        self._apply_reasoning_state(state)

    def action_actions(self) -> None:
        if self._visible_read_only:
            return
        self._request_menu_navigation(_MENU_ACTIONS)

    def action_plugins(self) -> None:
        if self._visible_read_only:
            return
        self._request_menu_navigation(_MENU_PLUGINS)

    def action_tasks(self) -> None:
        if self._visible_read_only:
            return
        self._request_menu_navigation(_MENU_TASKS)

    def _request_menu_navigation(self, destination: MenuDestination) -> None:
        """Route global menu shortcuts through the current menu surface."""

        menu = self._active_navigation_menu()
        if menu is None and len(self.screen_stack) > 1:
            return
        if menu is not None and menu.navigation_id == destination:
            return

        async def navigate() -> None:
            active_menu = self._active_navigation_menu()
            if destination == _MENU_ACTIONS:
                await self._choose_action(
                    menu=active_menu,
                    switch=active_menu is not None,
                )
            elif destination == _MENU_PLUGINS:
                await self._choose_plugins(
                    menu=active_menu,
                    switch=active_menu is not None,
                )
            elif destination == _MENU_SESSIONS:
                await self._choose_session(
                    menu=active_menu,
                    switch=active_menu is not None,
                )
            elif destination == _MENU_TASKS:
                await self._choose_tasks(menu=active_menu, switch=active_menu is not None)

        _worker: Worker[None] = self.run_worker(navigate, exclusive=True, group="navigation")

    def _active_navigation_menu(self) -> ChoiceScreen | None:
        """Return the active reusable menu surface, excluding transient dialogs."""

        screen = self.screen
        return (
            screen
            if isinstance(screen, ChoiceScreen) and screen.navigation_root_id is not None
            else None
        )

    async def _choose_action(
        self,
        *,
        menu: ChoiceScreen | None = None,
        switch: bool = False,
    ) -> None:
        choices = self._action_choices()
        action_screen: ChoiceScreen

        async def refresh_actions() -> tuple[Choice, ...]:
            return self._action_choices()

        async def select_action(selected: str) -> tuple[Choice, ...] | None:
            action_id = selected.removeprefix(_RECENT_ACTION_PREFIX)
            await self._remember_action(action_id)
            if action_id.startswith(_SYSTEM_SETTING_ACTION_PREFIX):
                self._run_system_setting(
                    action_id.removeprefix(_SYSTEM_SETTING_ACTION_PREFIX),
                    menu=action_screen,
                )
                return None
            if action_id == _SESSION_GROUPING_ACTION:
                await self._choose_session_grouping(menu=action_screen)
                return None
            if action_id == _THEME_ACTION:
                await self._choose_theme(menu=action_screen)
                return None
            if action_id == _THOUGHT_DISPLAY_ACTION:
                await self._choose_thought_display(menu=action_screen)
                return None
            if action_id == _INPUT_DRAFT_MEMORY_ACTION:
                await self._choose_input_draft_memory(menu=action_screen)
                return None
            if action_id == "/sessions":
                await self._choose_session(menu=action_screen)
                return None
            if action_id == _RENAME_SESSION_ACTION:
                await self._choose_session(menu=action_screen, operation="rename")
                return None
            if action_id == _ARCHIVE_SESSION_ACTION:
                await self._choose_session(menu=action_screen, operation="archive")
                return None
            if action_id == _DELETE_SESSION_ACTION:
                await self._choose_session(menu=action_screen, operation="delete")
                return None
            if action_id == "/help":
                self._show_help_page(action_screen)
                return None
            action_screen.dismiss(None)
            await self._dispatch_command(action_id)
            return None

        if menu is not None:
            action_screen = menu
            navigate = menu.switch_page if switch else menu.push_page
            navigate(
                navigation_id=_MENU_ACTIONS,
                retain_for_switching=True,
                title="ACTIONS",
                placeholder="Type an action…",
                choices=choices,
                on_select=select_action,
                on_refresh=refresh_actions,
            )
            return
        action_screen = ChoiceScreen(
            navigation_id=_MENU_ACTIONS,
            retain_for_switching=True,
            title="ACTIONS",
            placeholder="Type an action…",
            choices=choices,
            on_select=select_action,
            on_refresh=refresh_actions,
        )
        self.push_screen(action_screen)

    def _action_choices(self) -> tuple[Choice, ...]:
        """Project Actions from the current preferences and visual theme."""

        system_actions = tuple(
            Choice(
                f"{_SYSTEM_SETTING_ACTION_PREFIX}{action.action_id}",
                action.label,
                trailing=(
                    self._shortcut_for_action("plugins")
                    if action.action_id == "plugins.list"
                    else self._shortcut_for_action("tasks")
                    if action.action_id == "tasks.list"
                    else ""
                ),
                group=action.group,
            )
            for action in SYSTEM_SETTING_ACTIONS
        )
        actions = tuple(
            sorted(
                (
                    Choice(
                        _THEME_ACTION,
                        "Theme",
                        accent=self._tui_theme.accent,
                        trailing=self._tui_theme.label,
                        trailing_swatch=self._tui_theme.accent,
                        group="Settings",
                    ),
                    Choice(
                        _THOUGHT_DISPLAY_ACTION,
                        "Thinking display",
                        group="Settings",
                    ),
                    Choice(
                        _INPUT_DRAFT_MEMORY_ACTION,
                        "Remember drafts",
                        group="Settings",
                    ),
                    Choice(
                        _SESSION_GROUPING_ACTION,
                        "Session grouping",
                        group="Settings",
                    ),
                    self._command_action_choice("/new", "Sessions", "new_session"),
                    self._command_action_choice("/sessions", "Sessions", "sessions"),
                    Choice(_RENAME_SESSION_ACTION, "Rename session", group="Sessions"),
                    Choice(_ARCHIVE_SESSION_ACTION, "Archive session", group="Sessions"),
                    Choice(_DELETE_SESSION_ACTION, "Delete session", group="Sessions"),
                    *system_actions,
                    self._command_action_choice("/help", "Support", "help"),
                ),
                key=lambda choice: _ACTION_GROUP_RANK[choice.group],
            )
        )
        return (*self._recent_action_choices(actions), *actions)

    def _command_action_choice(
        self,
        command_name: str,
        group: str,
        shortcut_action: str | None = None,
    ) -> Choice:
        command = resolve_command(command_name)[0]
        if command is None:
            raise RuntimeError(f"missing builtin TUI command: {command_name}")
        return Choice(
            command.name,
            command.title,
            trailing=(
                self._shortcut_for_action(shortcut_action) if shortcut_action is not None else ""
            ),
            group=group,
        )

    def _recent_action_choices(self, actions: tuple[Choice, ...]) -> tuple[Choice, ...]:
        actions_by_id = {action.value: action for action in actions}
        recent = tuple(
            actions_by_id[action_id]
            for action_id in self._preferences.recent_actions
            if action_id in actions_by_id
        )
        if len(recent) < 3:
            return ()
        return tuple(
            replace(
                action,
                value=f"{_RECENT_ACTION_PREFIX}{action.value}",
                group="Recent",
            )
            for action in recent
        )

    async def _remember_action(self, action_id: str) -> None:
        """Persist a distinct three-entry MRU using canonical action IDs."""

        async with self._preferences_save_lock:
            recent_actions = (
                action_id,
                *(item for item in self._preferences.recent_actions if item != action_id),
            )[:3]
            if recent_actions == self._preferences.recent_actions:
                return
            preferences = self._preferences.model_copy(update={"recent_actions": recent_actions})
            try:
                await asyncio.to_thread(self._preferences_store.save, preferences)
            except OuroError as exc:
                _LOG.warning("Could not persist recent TUI actions: %s", exc)
            self._preferences = preferences

    async def _remember_workspace(self, workspace: str) -> None:
        """Persist a distinct twenty-entry MRU using resolved workspace paths."""

        async with self._preferences_save_lock:
            recent_workspaces = (
                workspace,
                *(item for item in self._preferences.recent_workspaces if item != workspace),
            )[:MAX_RECENT_WORKSPACES]
            if recent_workspaces == self._preferences.recent_workspaces:
                return
            preferences = self._preferences.model_copy(
                update={"recent_workspaces": recent_workspaces}
            )
            try:
                await asyncio.to_thread(self._preferences_store.save, preferences)
            except OuroError as exc:
                _LOG.warning("Could not persist recent TUI workspaces: %s", exc)
                self.notify(str(exc), severity="warning", markup=False)
                return
            self._preferences = preferences

    async def _clear_recent_workspaces(self) -> None:
        """Clear the TUI-owned workspace MRU without changing the active workspace."""

        async with self._preferences_save_lock:
            if not self._preferences.recent_workspaces:
                self.notify("Recent workspaces are already clear", markup=False)
                return
            preferences = self._preferences.model_copy(update={"recent_workspaces": ()})
            try:
                await asyncio.to_thread(self._preferences_store.save, preferences)
            except OuroError as exc:
                _LOG.warning("Could not clear recent TUI workspaces: %s", exc)
                self.notify(str(exc), severity="warning", markup=False)
                return
            self._preferences = preferences
        self.notify("Recent workspaces cleared", markup=False)

    def _shortcut_for_action(self, action: str) -> str:
        for binding in Binding.make_bindings(self.BINDINGS):
            if binding.action == action:
                return self.get_key_display(binding)
        return ""

    async def _choose_theme(self, *, menu: ChoiceScreen | None = None) -> None:
        selected_theme_id = self._pending_tui_theme_id or self._tui_theme.theme_id
        selected_level = self._theme_choice_saturation_level(selected_theme_id)

        def theme_choices() -> tuple[Choice, ...]:
            return self._theme_choices(
                selected_theme_id=selected_theme_id,
                selected_level=selected_level,
            )

        async def refresh_themes() -> tuple[Choice, ...]:
            return theme_choices()

        async def select_choice(value: str) -> tuple[Choice, ...] | None:
            nonlocal selected_theme_id, selected_level
            if value.startswith(_WELCOME_STYLE_PREFIX) or value == _WELCOME_MOTION_ACTION:
                # Choice workers may be cancelled by another selection. The host
                # owns the complete save/mount transaction, serialized with prefs.
                worker: Worker[bool] = self.run_worker(
                    partial(self._set_welcome_style, value.removeprefix(_WELCOME_STYLE_PREFIX)),
                    group="welcome-style",
                )
                saved = await asyncio.shield(worker.wait())
                return theme_choices() if saved else None
            theme_id = value
            if theme_id != selected_theme_id:
                selected_theme_id = theme_id
            self._request_tui_theme(
                theme_id,
                saturation_level=selected_level,
            )
            return theme_choices()

        def preview_theme(screen: ChoiceScreen, theme_id: str) -> None:
            nonlocal selected_theme_id, selected_level
            theme = TUI_THEMES.get(theme_id)
            if theme is not None:
                if theme_id != selected_theme_id:
                    selected_theme_id = theme_id
                    screen.update_choices(
                        theme_choices(),
                        preferred_value=theme_id,
                    )
                screen.preview_accent(theme.accent_saturation_scale()[selected_level])
            else:
                screen.clear_accent_preview()

        def adjust_theme(
            screen: ChoiceScreen,
            theme_id: str,
            direction: int,
        ) -> tuple[Choice, ...] | None:
            nonlocal selected_theme_id, selected_level
            theme = TUI_THEMES.get(theme_id)
            if theme is None:
                return None
            if theme_id != selected_theme_id:
                selected_theme_id = theme_id
            level = max(0, min(THEME_SATURATION_LEVELS - 1, selected_level + direction))
            if level == selected_level:
                return None
            selected_level = level
            screen.preview_accent(theme.accent_saturation_scale()[level])
            return theme_choices()

        if menu is not None:
            menu.push_page(
                title="THEME",
                placeholder="Filter styles and colors…",
                choices=theme_choices(),
                initial_value=f"{_WELCOME_STYLE_PREFIX}{self._preferences.welcome_style}",
                on_highlight=lambda theme_id: preview_theme(menu, theme_id),
                on_select=select_choice,
                on_refresh=refresh_themes,
                on_adjust=lambda theme_id, direction: adjust_theme(
                    menu,
                    theme_id,
                    direction,
                ),
                interaction_hint=DEFAULT_TUI_THEME.inline_separator.join(
                    ("↑/↓ move", "←/→ saturation", "Enter select", "Esc back")
                ),
            )
            self._register_theme_picker(menu)
            return
        theme_screen: ChoiceScreen

        def preview_standalone_theme(theme_id: str) -> None:
            preview_theme(theme_screen, theme_id)

        theme_screen = ChoiceScreen(
            title="THEME",
            placeholder="Filter styles and colors…",
            choices=theme_choices(),
            initial_value=f"{_WELCOME_STYLE_PREFIX}{self._preferences.welcome_style}",
            on_highlight=preview_standalone_theme,
            on_select=select_choice,
            on_refresh=refresh_themes,
            on_adjust=lambda theme_id, direction: adjust_theme(
                theme_screen,
                theme_id,
                direction,
            ),
            interaction_hint=DEFAULT_TUI_THEME.inline_separator.join(
                ("↑/↓ move", "←/→ saturation", "Enter select", "Esc close")
            ),
        )
        self._register_theme_picker(theme_screen)
        await self.push_screen_wait(theme_screen)

    async def _set_welcome_style(self, value: str) -> bool:
        async with self._preferences_save_lock:
            update = (
                {"welcome_motion": not self._preferences.welcome_motion}
                if value == _WELCOME_MOTION_ACTION
                else {"welcome_style": value}
            )
            preferences = self._preferences_store.validate(
                {**self._preferences.model_dump(), **update}
            )
            try:
                await asyncio.to_thread(self._preferences_store.save, preferences)
            except OuroError as exc:
                self.notify(str(exc), severity="error", markup=False)
                return False
            self._preferences = preferences
            await self.query_one(WelcomePanel).set_welcome_style(
                self._welcome_styles[preferences.welcome_style],
                motion=preferences.welcome_motion,
            )
        return True

    async def _choose_thought_display(self, *, menu: ChoiceScreen | None = None) -> None:
        def display_choices(expanded: bool) -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    value,
                    label,
                    marker=(
                        self._tui_theme.selected_choice_marker
                        if _THOUGHT_DISPLAY_OPTIONS[value] == expanded
                        else ""
                    ),
                )
                for value, label in _THOUGHT_DISPLAY_LABELS.items()
            )

        async def select_display(value: str) -> tuple[Choice, ...] | None:
            if not await self._set_reasoning_expanded(value):
                return None
            return display_choices(self._preferences.reasoning_expanded)

        if menu is not None:
            menu.push_page(
                title="THINKING DISPLAY",
                placeholder="Choose how thinking is displayed…",
                choices=display_choices(self._preferences.reasoning_expanded),
                initial_value=("expand" if self._preferences.reasoning_expanded else "collapse"),
                on_select=select_display,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title="THINKING DISPLAY",
                placeholder="Choose how thinking is displayed…",
                choices=display_choices(self._preferences.reasoning_expanded),
                initial_value=("expand" if self._preferences.reasoning_expanded else "collapse"),
                on_select=select_display,
            )
        )

    async def _choose_session_grouping(self, *, menu: ChoiceScreen) -> None:
        def grouping_choices() -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    value,
                    label,
                    marker=(
                        self._tui_theme.selected_choice_marker
                        if value == self._preferences.session_grouping
                        else ""
                    ),
                )
                for value, label in _SESSION_GROUPING_LABELS.items()
            )

        async def select_grouping(value: str) -> tuple[Choice, ...] | None:
            async with self._preferences_save_lock:
                preferences = self._preferences_store.validate(
                    {**self._preferences.model_dump(), "session_grouping": value}
                )
                try:
                    await asyncio.to_thread(self._preferences_store.save, preferences)
                except OuroError as exc:
                    self.notify(str(exc), severity="warning", markup=False)
                    return None
                self._preferences = preferences
            return grouping_choices()

        menu.push_page(
            title="SESSION GROUPING",
            placeholder="Choose session grouping…",
            choices=grouping_choices(),
            initial_value=self._preferences.session_grouping,
            on_select=select_grouping,
        )

    async def _choose_input_draft_memory(
        self,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        def memory_choices(enabled: bool) -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    value,
                    label,
                    marker=(
                        self._tui_theme.selected_choice_marker
                        if _INPUT_DRAFT_MEMORY_OPTIONS[value] == enabled
                        else ""
                    ),
                )
                for value, label in _INPUT_DRAFT_MEMORY_LABELS.items()
            )

        async def select_memory(value: str) -> tuple[Choice, ...] | None:
            if not await self._set_input_draft_memory(value):
                return None
            return memory_choices(self._preferences.input_draft_memory_enabled)

        enabled = self._preferences.input_draft_memory_enabled
        initial_value = "enable" if enabled else "disable"
        if menu is not None:
            menu.push_page(
                title="REMEMBER DRAFTS",
                placeholder="Choose whether to save drafts…",
                choices=memory_choices(enabled),
                initial_value=initial_value,
                on_select=select_memory,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title="REMEMBER DRAFTS",
                placeholder="Choose whether to save drafts…",
                choices=memory_choices(enabled),
                initial_value=initial_value,
                on_select=select_memory,
            )
        )

    def _theme_choices(
        self,
        *,
        selected_theme_id: str | None = None,
        selected_level: int | None = None,
    ) -> tuple[Choice, ...]:
        if selected_theme_id is None:
            selected_theme_id = self._pending_tui_theme_id or self._tui_theme.theme_id
        if selected_level is None:
            selected_level = (
                self._pending_tui_theme_saturation_level
                if self._pending_tui_theme_saturation_level is not None
                else self._tui_theme_saturation_level
            )

        def theme_choice(theme: TuiTheme) -> Choice:
            saturation_scale = theme.accent_saturation_scale()
            is_selected = theme.theme_id == selected_theme_id
            display_level = selected_level if is_selected else 0
            display_accent = saturation_scale[display_level]
            return Choice(
                theme.theme_id,
                theme.label,
                group="Theme color",
                leading_swatch=display_accent,
                swatches=saturation_scale,
                swatch_marker_index=selected_level if is_selected else None,
                trailing_accent=display_accent,
                loading=theme.theme_id == self._pending_tui_theme_id,
            )

        return (
            *(
                Choice(
                    f"{_WELCOME_STYLE_PREFIX}{style.style_id}",
                    style.label,
                    group="Welcome style",
                    marker=(
                        self._tui_theme.selected_choice_marker
                        if style.style_id == self._preferences.welcome_style
                        else ""
                    ),
                )
                for style in self._welcome_styles.values()
            ),
            Choice(
                _WELCOME_MOTION_ACTION,
                "Animation",
                group="Welcome style",
                trailing="Playing" if self._preferences.welcome_motion else "Paused",
            ),
            *(theme_choice(theme) for theme in TUI_THEMES.values()),
        )

    def _theme_choice_saturation_level(self, theme_id: str) -> int:
        """Return the persisted or pending level used to initialize the picker."""

        if self._pending_tui_theme_id is not None:
            if theme_id == self._pending_tui_theme_id:
                return self._pending_tui_theme_saturation_level or 0
            return 0
        if theme_id == self._tui_theme.theme_id:
            return self._tui_theme_saturation_level
        return 0

    def _register_theme_picker(self, screen: ChoiceScreen) -> None:
        self._theme_picker_ref = ref(screen)
        self._theme_picker_revision = screen.page_revision

    async def _refresh_theme_picker(self) -> None:
        screen = self._theme_picker_ref() if self._theme_picker_ref is not None else None
        if screen is None:
            return
        if screen.page_revision == self._theme_picker_revision and await screen.refresh_choices():
            return
        if screen.update_choices(
            self._theme_choices(),
            expected_revision=self._theme_picker_revision,
            preferred_value=self._pending_tui_theme_id or self._tui_theme.theme_id,
        ):
            return
        await screen.refresh_choices()

    def _request_tui_theme(
        self,
        theme_id: str,
        *,
        saturation_level: int | None = None,
    ) -> None:
        theme = TUI_THEMES.get(theme_id)
        if theme is None:
            self.notify(f"Unknown TUI theme: {theme_id}", severity="error", markup=False)
            return
        level = (
            self._preferences.theme_saturation_level
            if saturation_level is None
            else saturation_level
        )
        try:
            theme.saturation_for_level(level)
        except ValueError:
            self.notify("Invalid TUI theme saturation", severity="error", markup=False)
            return
        if (
            theme_id == self._pending_tui_theme_id
            and level == self._pending_tui_theme_saturation_level
        ):
            return
        if (
            self._pending_tui_theme_id is None
            and theme_id == self._tui_theme.theme_id
            and level == self._tui_theme_saturation_level
            and self._preferences.theme == theme_id
            and self._preferences.theme_saturation_level == level
        ):
            return

        self._theme_switch_revision += 1
        self._pending_tui_theme_id = theme_id
        self._pending_tui_theme_saturation_level = level
        self._theme_switch_changed.set()
        if not self._theme_switch_active:
            self._theme_switch_active = True
            _worker: Worker[None] = self.run_worker(
                self._switch_tui_themes,
                name="tui-theme-switch",
                group="theme-switch",
            )

    async def _switch_tui_themes(self) -> None:
        """Persist and render only the latest requested complete theme."""

        try:
            while self._pending_tui_theme_id is not None:
                revision = self._theme_switch_revision
                theme_id = self._pending_tui_theme_id
                saturation_level = self._pending_tui_theme_saturation_level
                if saturation_level is None:
                    break
                source_theme = TUI_THEMES[theme_id]
                theme = source_theme.at_saturation_level(saturation_level)
                self._theme_switch_changed.clear()
                save_error: OuroError | None = None
                async with self._preferences_save_lock:
                    if revision != self._theme_switch_revision:
                        continue
                    preferences = self._preferences.model_copy(
                        update={
                            "theme": theme_id,
                            "theme_saturation_level": saturation_level,
                        }
                    )
                    try:
                        await asyncio.to_thread(self._preferences_store.save, preferences)
                    except OuroError as exc:
                        save_error = exc
                    if save_error is None and revision == self._theme_switch_revision:
                        self._preferences = preferences
                if save_error is not None:
                    if revision != self._theme_switch_revision:
                        continue
                    self._pending_tui_theme_id = None
                    self._pending_tui_theme_saturation_level = None
                    self.notify(str(save_error), severity="error", markup=False)
                    await self._refresh_theme_picker()
                    return
                if revision != self._theme_switch_revision:
                    continue

                self._apply_tui_theme(
                    theme,
                    source=source_theme,
                    saturation_level=saturation_level,
                )
                await self._wait_for_theme_render_or_request()
                if revision != self._theme_switch_revision:
                    continue

                self._pending_tui_theme_id = None
                self._pending_tui_theme_saturation_level = None
                await self._refresh_theme_picker()
        finally:
            self._theme_switch_active = False

    async def _wait_for_theme_render_or_request(self) -> None:
        histories = tuple(self.query(ConversationHistory))
        if not histories:
            return

        async def wait_for_histories() -> None:
            await asyncio.gather(*(history.wait_for_theme_refresh() for history in histories))

        render_task = asyncio.create_task(wait_for_histories())
        request_task = asyncio.create_task(self._theme_switch_changed.wait())
        done, pending = await asyncio.wait(
            (render_task, request_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if render_task in done:
            await render_task

    async def _set_reasoning_expanded(self, value: str) -> bool:
        expanded = _THOUGHT_DISPLAY_OPTIONS.get(value)
        if expanded is None:
            return False
        async with self._preferences_save_lock:
            preferences = self._preferences.model_copy(update={"reasoning_expanded": expanded})
            try:
                await asyncio.to_thread(self._preferences_store.save, preferences)
            except OuroError as exc:
                self.notify(str(exc), severity="error", markup=False)
                return False
            self._preferences = preferences
        self._session_history_states.clear()
        self.query_one(ConversationView).set_reasoning_expanded(expanded)
        return True

    async def _set_input_draft_memory(self, value: str) -> bool:
        enabled = _INPUT_DRAFT_MEMORY_OPTIONS.get(value)
        if enabled is None:
            return False
        async with self._preferences_save_lock:
            if enabled == self._preferences.input_draft_memory_enabled:
                return True
            preferences = self._preferences.model_copy(
                update={"input_draft_memory_enabled": enabled}
            )
            try:
                await asyncio.to_thread(self._preferences_store.save, preferences)
            except OuroError as exc:
                _LOG.warning("Could not persist TUI input draft preference: %s", exc)
                self.notify(str(exc), severity="warning", markup=False)
                return False
            self._preferences = preferences
        drafts = TuiInputDrafts()
        if not self._visible_read_only and self.query(Composer):
            drafts = drafts.remember(
                self._controller.session_id,
                self.query_one(Composer).input_content.text,
                attachments=self.query_one(Composer).input_content.attachments,
                skills=self.query_one(Composer).input_content.skills,
            )
        self._set_input_drafts(drafts, force_persist=True)
        return True

    def _apply_tui_theme(
        self,
        theme: TuiTheme,
        *,
        source: TuiTheme | None = None,
        saturation_level: int | None = None,
    ) -> None:
        source_theme = theme if source is None else source
        self._tui_theme_saturation_level = (
            source_theme.saturation_level() if saturation_level is None else saturation_level
        )
        if theme == self._tui_theme:
            return
        self._session_history_states.clear()
        self._tui_theme = theme
        self.console.pop_theme()
        self.console.push_theme(theme.rich_theme())
        self.refresh_css(animate=False)
        self._refresh_theme_content()

    def _refresh_theme_content(self) -> None:
        for screen in self.screen_stack:
            for node in (screen, *screen.walk_children()):
                refresh_theme = getattr(node, "refresh_theme", None)
                if callable(refresh_theme):
                    refresh_theme()

    def action_help(self) -> None:
        screen = self.screen
        if isinstance(screen, HelpScreen):
            screen.dismiss(None)
            return
        menu = self._active_navigation_menu()
        if menu is None:
            if len(self.screen_stack) == 1:
                self.push_screen(HelpScreen(plugin_commands=self._plugin_commands()))
            return
        if menu.page_title == "KEYBOARD HELP":
            _worker: Worker[None] = self.run_worker(
                menu.action_cancel, exclusive=True, group="navigation"
            )
            return
        self._show_help_page(menu)

    def _show_help_page(self, menu: ChoiceScreen) -> None:
        choices = help_choices(plugin_commands=self._plugin_commands())

        async def keep_help_open(_selected: str) -> tuple[Choice, ...]:
            return choices

        menu.push_page(
            title="KEYBOARD HELP",
            placeholder="Filter shortcuts and commands…",
            choices=choices,
            on_select=keep_help_open,
            interaction_hint=HelpScreen.BACK_HINT,
        )

    def action_abort(self) -> None:
        if self._visible_read_only:
            return
        if not self._has_active_run():
            return
        self._abort_run()

    async def action_quit(self) -> None:
        self.exit(0)

    def _has_active_run(self) -> bool:
        return self._projection is not None and self._projection.activity.status in {
            "queued",
            "running",
            "cancelling",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
        }

    @work(exclusive=True, group="abort")
    async def _abort_run(self) -> None:
        try:
            await self._controller.abort()
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error", markup=False)


def _model_display_name(route: str, models: tuple[AvailableModel, ...]) -> str:
    if route.startswith("faux/"):
        return ""
    model = next((candidate for candidate in models if candidate.key == route), None)
    return model_display_label(model) if model is not None else UNAVAILABLE_MODEL_LABEL


def _status_model_label(model: AvailableModel) -> str:
    return "" if model.provider_id == "faux" else model_display_label(model)


def _runtime_event_frame_prefix(events: Sequence[EventRecord]) -> tuple[EventRecord, ...]:
    """Keep a streamed tool snapshot visible before its lifecycle advances."""

    prefix = events[:_RUNTIME_EVENT_APPLY_MAX]
    saw_tool_stream = False
    for index, event in enumerate(prefix):
        if event.event_type == "model_tool_call_delta":
            saw_tool_stream = True
        elif saw_tool_stream:
            prefix = prefix[:index]
            break
    return tuple(prefix)


def _event_record(event: RuntimeEventView) -> EventRecord:
    """Adapt the redacted Host event DTO to the existing TUI reducer input."""

    return EventRecord(
        history_epoch=event.history_epoch,
        event_id=event.event_id,
        session_id=event.session_id,
        run_id=event.run_id,
        turn_id=event.turn_id,
        event_type=event.event_type,
        created_at=event.created_at,
        sequence=event.sequence,
        payload=dict(event.data),
    )


def _active_stream_tail(
    events: Sequence[EventRecord],
    messages: Sequence[MessageRecord] = (),
) -> tuple[str, str]:
    """Return stream text not yet represented by a canonical assistant message.

    ``model_message_completed`` closes the provider stream, but the Runtime
    persists the resulting MessageRecord at a later durable boundary. A Host
    snapshot can legitimately land between those operations, so only an actual
    canonical assistant message proves that the corresponding deltas are
    already present in history.
    """

    ordered = canonical_event_order(events)
    run_id = next((event.run_id for event in ordered if event.run_id is not None), None)
    covered_turns = {
        message.turn_id
        for message in messages
        if message.role == "assistant" and message.run_id == run_id and message.turn_id is not None
    }
    start = 0
    for index, event in enumerate(ordered):
        if event.event_type == "run_status_changed" and event.payload.get("resumed") is True:
            start = index + 1
            continue
        if event.event_type != "model_message_completed":
            continue
        if event.turn_id is not None and event.turn_id in covered_turns:
            start = index + 1
    reasoning: list[str] = []
    answer: list[str] = []
    for event in ordered[start:]:
        value = (
            event.payload.get("reasoning_content")
            if event.event_type == "model_reasoning_delta"
            else event.payload.get("text")
            if event.event_type == "model_text_delta"
            else None
        )
        if not isinstance(value, str):
            continue
        (reasoning if event.event_type == "model_reasoning_delta" else answer).append(value)
    return "".join(reasoning), "".join(answer)


def _next_cycle_value(values: Sequence[str], current: str) -> str:
    """Return the next canonical option, wrapping back to the first."""

    return values[(values.index(current) + 1) % len(values)] if current in values else values[0]


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
    return reasoning_values(state.capability)


def _reasoning_arguments(state: ModelReasoningState) -> tuple[TuiCommandArgument, ...]:
    return tuple(
        TuiCommandArgument(value, _reasoning_description(value), label=THINKING_LABELS[value])
        for value in _available_reasoning_values(state)
    )


def _reasoning_description(value: str) -> str:
    if value == "default":
        return "Use the model's default thinking level"
    if value == "enabled":
        return "Enable model thinking"
    if value == "disabled":
        return "Turn off thinking for future replies"
    return f"Use {THINKING_LABELS[value].lower()} thinking effort"


def _format_session_summaries(summaries: tuple[SessionSummary, ...]) -> str:
    if not summaries:
        return "No summaries for the current session."
    latest = summaries[-1]
    state = "active" if latest.active else "inactive"
    return (
        f"Latest session summary\n\n"
        f"{latest.compaction_id} [{state}]\n"
        f"Created: {latest.created_at.isoformat()}\n"
        f"Trigger: {latest.trigger}\n\n{latest.summary}"
    )


def _format_reasoning_state(state: ModelReasoningState) -> str:
    requested = _reasoning_value(state.requested)
    effective = _reasoning_value(state.effective)
    return f"Thinking: {requested} (effective: {effective})"


def _truncate_session_title(title: str) -> str:
    rendered = Text(title)
    rendered.truncate(_SESSION_TITLE_MAX_CELLS, overflow="ellipsis")
    return rendered.plain


def _session_choice_label(item: SessionCatalogEntry, *, theme: TuiTheme) -> str:
    title = item.title
    if item.session_kind == "primary" and item.parent_session_id is not None:
        title = f"forked{theme.inline_separator}{title}"
    if item.pinned:
        title = f"★ {title}"
    return _truncate_session_title(title)


def _sessions_in_domain(
    sessions: tuple[SessionCatalogEntry, ...], domain: str
) -> tuple[SessionCatalogEntry, ...]:
    return tuple(
        item
        for item in sessions
        if (
            item.archived
            if domain == "archived"
            else not item.archived
            and (
                item.session_kind == "subagent"
                if domain == "subagent"
                else item.session_kind != "subagent" and item.namespace == domain
            )
        )
    )


def _session_choices(
    sessions: tuple[SessionCatalogEntry, ...],
    *,
    selected_session_id: str | None = None,
    today: date | None = None,
    grouping: SessionGrouping = "date",
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    local_today = today or datetime.now().astimezone().date()
    groups: dict[str, list[Choice]] = {}
    for item in sessions:
        group = (
            item.workspace or "No workspace"
            if grouping == "workspace"
            else _session_date_group(item.last_activity_at, today=local_today)
        )
        groups.setdefault(group, []).append(
            Choice(
                value=item.session_id,
                label=_session_choice_label(item, theme=theme),
                edit_value=item.title,
                trailing="index error" if item.catalog_error is not None else item.status,
                accent=(
                    theme.failure
                    if (item.catalog_error is not None or item.catalog_marks_unreadable)
                    else theme.warning
                    if item.health != "ok"
                    else None
                ),
                marker=(
                    theme.selected_choice_marker if item.session_id == selected_session_id else ""
                ),
                group=group,
            )
        )
    return tuple(choice for group_choices in groups.values() for choice in group_choices)


def _session_date_group(activity_at: datetime, *, today: date) -> str:
    activity_date = activity_at.astimezone().date()
    age = (today - activity_date).days
    if age <= 0:
        return "Today"
    if age == 1:
        return "Yesterday"
    if age < 7:
        return "Previous 7 days"
    if age < 30:
        return "Previous 30 days"
    return f"{month_name[activity_date.month]} {activity_date.year}"


def _queued_input_draft(item: QueuedUserInputView) -> str:
    return " ".join((*[skill.label for skill in item.skills], item.prompt)).strip()


def _queued_input_preview(item: QueuedUserInputView) -> str:
    return _summarize(_queued_input_draft(item), limit=64) or "(empty message)"


def _summarize(text: str, limit: int = 60) -> str:
    """Collapse whitespace and truncate so user messages fit a single menu line."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"
