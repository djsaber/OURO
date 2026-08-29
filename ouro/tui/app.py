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
from pathlib import Path
from typing import Literal

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

from ouro.application.agent_models import AgentTreeNode
from ouro.application.agent_tree import AgentTreeSubscription
from ouro.application.dto import Diagnostic
from ouro.application.event_models import RuntimeEventView
from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.queued_inputs import QueuedInputMode, QueuedUserInputView
from ouro.application.session_models import SessionCatalogEntry
from ouro.application.skill_activations import SkillInvocationRequest
from ouro.application.supervisor import RuntimeSubscription
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import REASONING_EFFORTS, ReasoningRequest
from ouro.core.results import RunResult
from ouro.deployment import SessionBackend
from ouro.local_application import LocalApplication
from ouro.tui.clipboard import ClipboardWriteError, write_system_clipboard
from ouro.tui.commands import (
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
)
from ouro.tui.conversation_history import ConversationHistoryState, ToolNavigationRequested
from ouro.tui.input_drafts import TuiInputDrafts, TuiInputDraftStore
from ouro.tui.plan_confirmation import project_plan_confirmation
from ouro.tui.preferences import (
    MAX_RECENT_WORKSPACES,
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
from ouro.tui.state import (
    PendingInteraction,
    RunPresentation,
    TuiSessionProjection,
    canonical_event_order,
    project_agent_interaction,
    project_run_presentation,
    project_run_presentations,
)
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.theme import TUI_DESIGN, TUI_THEMES, TuiTheme
from ouro.tui.widgets import (
    COMMAND_POPUP_MAX_HEIGHT,
    COMMAND_POPUP_MIN_HEIGHT,
    INTERACTION_PANEL_MAX_HEIGHT,
    INTERACTION_PANEL_MIN_HEIGHT,
    WELCOME_MAX_WIDTH,
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
_SESSION_TITLE_MAX_CELLS = 56
_RUNTIME_EVENT_BUFFER_MAX = 1000
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
_AGENT_INTERACTION_EVENTS = {
    "agent_spawned",
    "agent_wait_requested",
    "agent_wait_resolved",
    "agent_result_ready",
    "agent_result_delivered",
    "agent_closed",
}
_THEME_ACTION = "__ouro_theme__"
_THOUGHT_DISPLAY_ACTION = "__ouro_thought_display__"
_RENAME_SESSION_ACTION = "__ouro_rename_session__"
_ARCHIVE_SESSION_ACTION = "__ouro_archive_session__"
_DELETE_SESSION_ACTION = "__ouro_delete_session__"
_SYSTEM_SETTING_ACTION_PREFIX = "__ouro_system_setting__:"
_RECENT_ACTION_PREFIX = "__ouro_recent_action__:"
_MENU_ACTIONS: MenuDestination = "actions"
_MENU_PLUGINS: MenuDestination = "plugins"
_MENU_SESSIONS: MenuDestination = "sessions"
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
_THOUGHT_DISPLAY_OPTIONS: dict[str, bool] = {"expand": True, "collapse": False}
_THOUGHT_DISPLAY_LABELS = {
    "expand": "Expand thoughts",
    "collapse": "Collapse thoughts",
}


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
        try:
            await asyncio.to_thread(self._clipboard_writer, selected_text)
        except ClipboardWriteError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return
        self.clear_selection()
        self.notify("Copied to clipboard", markup=False)

    async def on_prune(self, event: Prune) -> None:
        self._on_host_prune()
        await super().on_prune(event)

    def on_resize(self, event: events.Resize) -> None:
        if self._on_host_resize is not None:
            self._on_host_resize(event.size)


_OURO_TUI_CSS = """
    /* Design tokens — one source of truth for the flat OURO chrome.
       $ouro-theme is the single accent color: borders, titles, the logo, and
       active highlights all derive from it, so the whole TUI recolors by
       changing one value. The owning app injects the persisted TUI palette so
       CSS and Rich-text styling share one source of truth (see theme.py). */
    $conversation-left-gutter: 2;
    $conversation-right-gutter: 1;
    $welcome-width: 72%;
    $welcome-min-width: 48;
    $welcome-inline-padding: 2;
    $welcome-logo-gap: 2;
    $welcome-section-gap: 2;
    $welcome-system-gap: 3;
    $welcome-label-width: 11;
    $welcome-tips-min-width: 20;

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

    .welcome-only {
        display: none;
    }

    #main.welcome-page {
        align: center middle;
    }

    #main.welcome-page #conversation {
        display: none;
    }

    #input-region.welcome-page {
        width: $welcome-width;
        min-width: $welcome-min-width;
        max-width: $welcome-max-width;
        height: 100%;
        padding: 0 $welcome-inline-padding;
    }

    #input-region.welcome-page .welcome-only {
        display: block;
    }

    .welcome-top {
        width: 100%;
        height: 1fr;
        align: center bottom;
    }

    .welcome-logo {
        width: auto;
        height: auto;
        margin-bottom: $welcome-logo-gap;
    }

    .welcome-bottom {
        width: 100%;
        height: 1fr;
        min-height: 0;
        overflow: hidden;
    }

    .welcome-section {
        height: auto;
        background: transparent;
    }

    .welcome-system {
        width: 100%;
        margin: $welcome-system-gap 0 0 0;
        overflow: hidden;
    }

    .welcome-system-heading {
        width: 100%;
        height: 1;
    }

    .welcome-title {
        width: auto;
        height: 1;
        color: $ouro-text-detail;
        text-style: bold;
    }

    .welcome-row {
        width: 100%;
        height: 1;
        overflow: hidden;
    }

    .welcome-label {
        width: $welcome-label-width;
        height: 1;
        color: $ouro-text-detail;
    }

    .welcome-value {
        width: 1fr;
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .welcome-lower {
        width: auto;
        height: auto;
        margin: $welcome-section-gap 0 0 0;
        overflow: hidden;
    }

    .welcome-usage {
        width: auto;
    }

    .welcome-spacer {
        width: 1fr;
        height: 1;
    }

    .welcome-tips {
        width: auto;
        min-width: $welcome-tips-min-width;
    }

    .welcome-tips .welcome-title {
        color: $ouro-theme;
    }

    .welcome-usage-line {
        width: auto;
        height: 1;
    }

    .welcome-tip-key {
        width: 8;
        height: 1;
        color: $ouro-text-detail;
    }

    .welcome-tip-description {
        width: auto;
        height: 1;
        color: $ouro-text-muted;
    }

    .welcome-version {
        width: 1fr;
        height: 1;
        color: $ouro-text-primary;
        text-style: bold;
        text-align: left;
    }

    #input-region.welcome-page #command-popup {
        overlay: screen;
        offset-y: -100%;
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
        color: $ouro-text-muted;
        background: transparent;
    }

    .reasoning-label {
        height: 1;
        color: $ouro-text-secondary;
        text-style: none;
    }

    /* The reasoning body carries its │ border inside its strips, matching
       the immutable history's frozen rendering cell for cell. */
    .reasoning-lines {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1 0 0;
        color: $ouro-text-muted;
        background: transparent;
    }

    .reasoning-lines.collapsed {
        height: 0;
        margin-top: 0;
    }

    .tool-row {
        height: auto;
        margin: 0 2;
        color: $ouro-text-secondary;
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

    #command-popup-content {
        width: 100%;
        height: auto;
    }

    #composer {
        height: 3;
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
        height: auto;
        background: transparent;
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

    ModalScreen {
        align: center middle;
        overflow: hidden;
        /* Exactly one visible menu owns the focus backdrop. Parent menu panels
           become fully transparent while a child page is active, preventing
           nested darkening without losing the menu's visual focus. */
        background: $ouro-background 45%;
    }

    /* All modal menus share one flat chrome with the composer and command
       menu: a low-contrast panel with a muted edge on the right. The left
       rail is segmented — the title carries the theme accent, the body
       carries the lowlight — so the header reads as emphasis and the body
       as structure, matching the "quiet frame + bright header" contrast of
       the input box. */
    #choice-panel {
        width: 1fr;
        max-width: 72;
        margin: 0 2;
        height: auto;
        min-height: 12;
        max-height: 27;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-progress-track;
        border-bottom: none;
        border-left: none;
    }

    #choice-title {
        width: 100%;
        height: 2;
        padding: 1 2 0 2;
        color: $ouro-theme;
        text-style: bold;
        background: transparent;
        border: none;
        border-left: solid $ouro-theme;
    }

    #choice-filter {
        width: 100%;
        height: 3;
        padding: 1 2;
        background: transparent;
        border: none;
        border-left: solid $ouro-theme;
    }

    #choice-list {
        width: 100%;
        height: auto;
        min-height: 5;
        max-height: 20;
        padding: 0 1 1 1;
    }

    /* Shared grouped-menu treatment. Choice and inline-edit screens use the
       same rows, group headers, focus color, and hidden chrome. */
    .grouped-menu-list {
        background: transparent;
        border: none;
        border-left: solid $ouro-separator;
        /* The menu is keyboard-first: arrow navigation keeps the highlighted
           option in view, so the scrollbar is pure chrome and is hidden to
           match the flat panels used elsewhere. */
        scrollbar-background: transparent;
        scrollbar-color: transparent;
        scrollbar-color-hover: transparent;
        scrollbar-size-vertical: 0;
        scrollbar-gutter: auto;
    }

    .grouped-menu-list .option-list--option-highlighted {
        color: $ouro-theme;
        background: $ouro-highlight;
        text-style: not bold;
    }

    .grouped-menu-list:focus .option-list--option-highlighted {
        color: $ouro-theme;
        background: $ouro-input-highlight;
        text-style: not bold;
    }

    #choice-hint {
        height: 2;
        padding: 0 2 1 2;
        color: $ouro-text-lowlight;
        background: transparent;
        border: none;
        border-left: solid $ouro-separator;
    }

"""


class OuroTuiApp(ConfigurationWorkflowsApp):
    TITLE = "OURO"
    SUB_TITLE = "Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = _OURO_TUI_CSS

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
            "Abort",
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
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
        resume_session_id: str | None = None,
        application: LocalApplication | None = None,
        close_application: bool = True,
    ) -> None:
        preference_home = (
            application.runtime.environment.home
            if home is None and application is not None
            else home
        )
        self._preferences_store = TuiPreferencesStore(preference_home)
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
            input_drafts = self._input_draft_store.load()
        except OuroError as exc:
            input_drafts = TuiInputDrafts()
            self._input_drafts_error = str(exc)
        self._input_drafts = input_drafts
        self._tui_theme = TUI_THEMES[preferences.theme]
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
        self._visible_read_only = False
        self._visible_agent: AgentTreeNode | None = None
        self._observation_subscription: RuntimeSubscription | None = None
        self._agent_tree_subscription: AgentTreeSubscription | None = None
        self._agent_nodes: dict[str, AgentTreeNode] = {}
        self._agent_root_status = "idle"
        self._agent_navigation_revision = 0
        self._controlled_composer_draft: str | None = None
        self._held_session_scroll_y: dict[str, float] = {}
        self._session_history_states: OrderedDict[str, ConversationHistoryState] = OrderedDict()
        self._interaction: PendingInteraction | None = None
        self._models: tuple[AvailableModel, ...] = ()
        self._sessions: tuple[SessionCatalogEntry, ...] = ()
        self._active_model = "starting"
        self._active_title = "New session"
        self._active_workspace: str | None = None
        self._reasoning_state: ModelReasoningState | None = None
        self._system_status = TuiSystemStatus()
        self._host_closing = False
        self._waiting_for_session_event = False
        self._dismissed_command_text: str | None = None
        self._pending_runtime_events: list[EventRecord] = []
        self._runtime_event_buffer_lagged: set[str] = set()
        self._runtime_events_ready_posted = False
        self._runtime_events_frame_scheduled = False
        self._runtime_event_apply_lock = asyncio.Lock()
        self._mcp_status_timer: Timer | None = None
        self._input_surface: WelcomePanel | None = None
        self._input_draft_revision = 0
        self._input_draft_save_task: asyncio.Task[None] | None = None
        self._input_draft_error_notified = False

    @property
    def tui_theme(self) -> TuiTheme:
        return self._tui_theme

    def get_css_variables(self) -> dict[str, str]:
        return {
            **super().get_css_variables(),
            **TUI_DESIGN.css_variables(),
            "ouro-theme": self._tui_theme.accent,
            "welcome-max-width": str(WELCOME_MAX_WIDTH),
            "command-popup-min-height": str(COMMAND_POPUP_MIN_HEIGHT),
            "command-popup-max-height": str(COMMAND_POPUP_MAX_HEIGHT),
            "interaction-panel-min-height": str(INTERACTION_PANEL_MIN_HEIGHT),
            "interaction-panel-max-height": str(INTERACTION_PANEL_MAX_HEIGHT),
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
            self._input_surface = WelcomePanel()
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

    def on_mount(self) -> None:
        self.query_one(StatusLine).set_activity("starting")
        composer = self.query_one(Composer)
        popup = self.query_one(CommandPopup)
        composer.set_suggestion_resolver(lambda: self._command_suggestion(composer, popup))
        composer.disabled = True
        self._mcp_status_timer = self.set_interval(
            _MCP_STATUS_SYNC_INTERVAL_SECONDS,
            self._sync_mcp_status,
            pause=True,
        )
        self.run_worker(self._start(), name="tui-start", exclusive=True)
        if self._preferences_error is not None:
            self.notify(self._preferences_error, severity="warning")
        if self._input_drafts_error is not None:
            self._input_draft_error_notified = True
            self.notify(self._input_drafts_error, severity="warning", markup=False)

    async def on_unmount(self) -> None:
        self._begin_host_close()
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
        if self._mcp_status_timer is not None:
            self._mcp_status_timer.stop()
            self._mcp_status_timer = None
        self.workers.cancel_all()

    async def _start(self) -> None:
        session_load_error: tuple[str, Exception] | None = None
        try:
            bootstrap = await self._controller.start()
        except Exception as exc:  # noqa: BLE001 - surface boundary failures in the Host.
            if self._surface_available():
                self.query_one(StatusLine).set_activity("failed")
                self.notify(f"OURO could not start: {exc}", severity="error", markup=False)
            return
        if not self._surface_available():
            return
        self._apply_bootstrap(bootstrap)
        self._apply_system_status(bootstrap)
        if bootstrap.system_status.workspace is not None:
            await self._remember_workspace(bootstrap.system_status.workspace)
        await self._refresh_reasoning_indicator()
        if self._resume_session_id is not None:
            try:
                await self._open_session(self._resume_session_id)
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
        self._active_model = bootstrap.current_model.endpoint_id
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
        try:
            statuses = await self._controller.session_control_statuses()
        except Exception:  # noqa: BLE001 - status projection must not interrupt the Session.
            return
        if not self._surface_available():
            return
        status_line = self.query_one(StatusLine)
        status_line.set_subagent(None)
        status_line.set_session_controls(tuple(status.label for status in statuses))

    def _publish_runtime_event(self, event: EventRecord) -> None:
        """Accept fallback sink events not already owned by the visible subscription."""

        if self._host_closing:
            return
        subscription = self._observation_subscription
        if subscription is not None and subscription.session_id == event.session_id:
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

    def _replace_observation(self, subscription: RuntimeSubscription) -> None:
        previous = self._observation_subscription
        if previous is not None and previous is not subscription:
            previous.close()
        self._observation_subscription = subscription
        self.run_worker(
            self._consume_observation(subscription),
            name=f"observe-{subscription.session_id}",
            group="session-observation",
            exclusive=True,
        )

    async def _consume_observation(self, subscription: RuntimeSubscription) -> None:
        try:
            async for notification in subscription:
                if self._host_closing:
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

    def _replace_agent_tree(self, root_session_id: str | None) -> tuple[AgentTreeNode, ...]:
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
        subscription = self._controller.subscribe_agent_tree(root_session_id)
        if subscription is None:
            self._sync_subagent_navigation_panel(root_session_id=root_session_id)
            return ()
        self._agent_tree_subscription = subscription
        snapshot = subscription.initial_snapshot
        self._agent_nodes = {
            node.delegation_id: node for node in snapshot.nodes
        }
        if snapshot.root_activity is not None:
            self._agent_root_status = snapshot.root_activity.status
        self._sync_subagent_navigation_panel(root_session_id=root_session_id)
        self.run_worker(
            self._consume_agent_tree(subscription),
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
                    snapshot = self._controller.agent_tree_snapshot(subscription.root_session_id)
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
        except (StopAsyncIteration, asyncio.CancelledError):
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
        self.notify(
            "Live view fell behind; reloading durable session history…",
            severity="warning",
        )
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
        self.query_one(StatusLine).set_activity(message.status.activity)

    def _apply_system_status(self, bootstrap: TuiBootstrap) -> None:
        self._system_status = bootstrap.system_status
        if self._active_workspace is None and bootstrap.system_status.workspace is not None:
            self._active_workspace = bootstrap.system_status.workspace
            self.query_one(BrandHeader).set_context(workspace=self._active_workspace)
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def _refresh_system_status(self) -> bool:
        try:
            status = await self._controller.system_status()
        except Exception as exc:  # noqa: BLE001 - retain the last valid Host projection.
            if self._surface_available():
                self.notify(
                    f"Could not refresh system status: {exc}",
                    severity="warning",
                    markup=False,
                )
            return False
        if not self._surface_available():
            return False
        self._system_status = status
        self.query_one(ConversationView).set_system_status(status)
        return True

    async def _show_welcome(self, *, refresh_status: bool = True) -> None:
        if refresh_status:
            await self._refresh_system_status()
        if not self._surface_available():
            return
        await self.query_one(ConversationView).show_welcome()
        if not self._surface_available():
            return
        self._restore_input_draft(self._controller.session_id)
        self._set_welcome_mode(True)

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

    async def on_runtime_events_ready(self, _message: RuntimeEventsReady) -> None:
        self._runtime_events_ready_posted = False
        if self._host_closing or not self.query(ConversationView):
            self._pending_runtime_events.clear()
            return
        await self._flush_pending_runtime_events()

    async def _flush_pending_runtime_events(self) -> None:
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
        async with self._runtime_event_apply_lock:
            await self._apply_runtime_event_batch(
                events,
                allow_new_projection=allow_new_projection,
            )

    async def _apply_runtime_event_batch(
        self,
        events: Sequence[EventRecord],
        *,
        allow_new_projection: bool = False,
    ) -> None:
        if self._host_closing or not events or not self.query(ConversationView):
            return
        projection = self._projection
        if projection is None:
            if not self._waiting_for_session_event and not allow_new_projection:
                return
            projection = TuiSessionProjection(events[0].session_id)
            self._projection = projection
            # A selected draft becomes a durable primary Session at this
            # boundary. Synthetic or foreign event projections do not own the
            # controller Session and therefore have no Agent tree to observe.
            if self._controller.session_id == projection.session_id:
                self._replace_agent_tree(projection.session_id)
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

        for event in events:
            previous_tools = projection.activity.tool_calls
            update = projection.apply(event)
            if update is None:
                continue
            latest_update = update
            if event.event_type == "model_request_started":
                await flush_stream()
                await conversation.finish_assistant()
                conversation.begin_model_request(run_id=event.run_id)
            if event.event_type in {*_SESSION_INPUT_MESSAGE_EVENTS, "queued_input_consumed"}:
                await flush_stream()
                message_id = event.payload.get("message_id")
                if isinstance(message_id, str):
                    input_message = await self._controller.message(event.session_id, message_id)
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
            if update.activity.tool_calls != previous_tools:
                await flush_stream()
                await conversation.sync_tools(update.activity.tool_calls)
            if update.diagnostic:
                diagnostics.append(update.diagnostic)

        await flush_stream()

        if self._host_closing or not conversation.is_attached:
            return
        if refresh_session_controls and not self._visible_read_only:
            await self._refresh_session_controls()

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
                self._controller.subscribe_session(
                    projection.session_id,
                    after_sequence=projection.last_sequence,
                )
            )

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        if self._visible_read_only:
            self.notify("Subagent sessions are read-only", severity="warning")
            return
        self._remember_input_draft(self._controller.session_id, "")
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
        self._submit_run(message.text, message.mode)

    def _submit_run(
        self,
        prompt: str,
        queue_mode: QueuedInputMode,
        *,
        skill_invocation: SkillInvocationRequest | None = None,
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
        self._run_prompt(prompt, queue_mode, skill_invocation=skill_invocation)

    @work(group="run")
    async def _run_prompt(
        self,
        prompt: str,
        queue_mode: QueuedInputMode,
        *,
        skill_invocation: SkillInvocationRequest | None = None,
    ) -> None:
        try:
            result = await self._controller.submit(
                prompt,
                queue_mode=queue_mode,
                skill_invocation=skill_invocation,
            )
            await self._apply_run_result(result)
            await self._sync_queued_inputs(result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - providers and tools fail across this boundary.
            self.query_one(StatusLine).set_activity("failed")
            self.notify(str(exc), severity="error", markup=False)
        finally:
            self._waiting_for_session_event = False
            if not self._host_closing and self.query(Composer):
                self._sync_interaction_focus(self._interaction)

    async def _apply_run_result(self, result: RunResult) -> None:
        await self._flush_pending_runtime_events()
        await self._apply_runtime_events(result.events, allow_new_projection=True)
        if not self._surface_available():
            return
        if self._visible_read_only:
            await self._refresh_usage_status()
            return
        if self._projection is None or self._projection.session_id != result.session.session_id:
            self._projection = TuiSessionProjection(result.session.session_id)
        if result.final_text and not self._projection.assistant_text:
            self._projection.assistant_text = result.final_text
            conversation = self.query_one(ConversationView)
            await conversation.append_assistant(result.final_text)
            await conversation.finish_assistant()
        if result.run.kind == "agent":
            presentation = project_run_presentation(result.run)
            if self._projection.model is not None:
                presentation = replace(presentation, model=self._projection.model)
            await self.query_one(ConversationView).add_run_summary(presentation)
        self._active_title = result.session.title
        self._active_workspace = result.session.workspace or self._system_status.workspace
        self.query_one(BrandHeader).set_context(
            session_title=self._active_title,
            workspace=self._active_workspace,
        )
        await self._refresh_usage_status()
        await self._refresh_interaction()
        await self._refresh_session_controls()

    async def _refresh_usage_status(self) -> None:
        usage = await self._controller.usage_totals()
        if not self._surface_available():
            return
        self._system_status = replace(
            self._system_status,
            usage_request_count=usage.request_count,
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
            usage_estimated_cost=usage.estimated_cost,
            usage_priced_request_count=usage.priced_request_count,
        )
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def _refresh_models(self) -> tuple[AvailableModel, ...]:
        """Synchronize the Host's model projection with the live Runtime catalog."""

        models = await self._controller.models()
        self._models = models
        self._system_status = replace(
            self._system_status,
            provider_ids=tuple(sorted({model.provider_id for model in models})),
            model_count=len(models),
        )
        if self._surface_available():
            self.query_one(ConversationView).set_system_status(self._system_status)
        return models

    async def _sync_queued_inputs(self, session_id: str | None = None) -> None:
        if not self._surface_available() or not self.query(QueuedInputPanel):
            return
        if session_id is None and self._controller.session_id is None:
            self.query_one(QueuedInputPanel).set_inputs(())
            return
        inputs = await self._controller.queued_user_inputs(session_id)
        if not self._surface_available() or not self.query(QueuedInputPanel):
            return
        self.query_one(QueuedInputPanel).set_inputs(inputs)

    async def on_text_area_changed(self, message: TextArea.Changed) -> None:
        if message.text_area is not self.query_one(Composer):
            return
        composer = message.text_area
        value = composer.text
        command_context = composer.observe_command_change()
        if not self._visible_read_only:
            persisted_value = (
                command_context[1] if command_context is not None and command_context[1] else value
            )
            self._remember_input_draft(self._controller.session_id, persisted_value)
        popup = self.query_one(CommandPopup)
        ask_user = self.query_one(AskUserPanel)
        plan_panel = self.query_one(PlanConfirmationPanel)
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
        popup.show_arguments(command, arguments)
        self._sync_command_suggestion(composer, popup, command_catalog=command_catalog)

    def on_text_area_selection_changed(self, message: TextArea.SelectionChanged) -> None:
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
        self._remember_input_draft(self._controller.session_id, message.draft)
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

    @work(exclusive=True, group="interaction")
    async def _resolve_approval(
        self,
        interaction: PendingInteraction,
        approved: bool,
    ) -> None:
        if self._visible_read_only:
            return
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
            await self._refresh_interaction()
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            self.query_one(InteractionBar).resume()

    @work(exclusive=True, group="interaction")
    async def _answer_input(self, interaction: PendingInteraction, answer: JsonValue) -> None:
        if self._visible_read_only:
            return
        self.query_one(Composer).disabled = True
        try:
            result = await self._controller.answer(interaction=interaction, answer=answer)
            if result is not None:
                await self._apply_run_result(result)
            await self._refresh_interaction()
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            plan_panel = self.query_one(PlanConfirmationPanel)
            if plan_panel.display:
                plan_panel.resume()
            else:
                self.query_one(AskUserPanel).resume()
        finally:
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
        primary = self._projection.pending_interaction if self._projection else None
        current_is_agent = self._interaction is not None and self._interaction.is_subagent
        agent_interactions = (
            await self._controller.agent_interactions()
            if primary is None and (refresh_agents or current_is_agent)
            else ()
        )
        if self._host_closing or not self.query(Composer):
            return None
        interaction = primary or (
            project_agent_interaction(agent_interactions[0]) if agent_interactions else None
        )
        if interaction != self._interaction:
            self._interaction = interaction
            self._sync_interaction(interaction)
        else:
            self._sync_interaction_focus(interaction)
        return interaction

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
            interaction_bar.show_interaction(interaction)
            self._sync_composer_placeholder()
            self._sync_interaction_focus(interaction)

    def _sync_interaction_focus(self, interaction: PendingInteraction | None) -> None:
        """Give the active interaction exclusive ownership of keyboard input."""

        if self._host_closing or not self.query(Composer):
            return
        composer = self.query_one(Composer)
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
        composer = self.query_one(Composer)
        composer.load_command(value, draft=draft)
        composer.focus()

    def _edit_draft(self, value: str) -> None:
        composer = self.query_one(Composer)
        composer.load_text(value)
        composer.move_cursor((composer.document.line_count - 1, len(composer.document.lines[-1])))
        composer.focus()

    def _restore_input_draft(self, session_id: str | None) -> None:
        self.query_one(Composer).restore_command_draft(self._input_drafts.draft_for(session_id))

    def _remember_input_draft(self, session_id: str | None, text: str) -> None:
        self._set_input_drafts(self._input_drafts.remember(session_id, text))

    def _forget_input_drafts(self, session_ids: Sequence[str]) -> None:
        self._set_input_drafts(self._input_drafts.forget_sessions(session_ids))

    def _set_input_drafts(self, drafts: TuiInputDrafts) -> None:
        if drafts == self._input_drafts:
            return
        self._input_drafts = drafts
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
                drafts = self._input_drafts
                try:
                    await asyncio.to_thread(self._input_draft_store.save, drafts)
                except OuroError as exc:
                    _LOG.warning("Could not persist TUI input drafts: %s", exc)
                    if not self._input_draft_error_notified and not self._host_closing:
                        self._input_draft_error_notified = True
                        self.notify(str(exc), severity="warning", markup=False)
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
            if argument:
                await self._select_workspace(argument)
        elif command.name == "/sessions":
            self.action_sessions()
        elif command.name == "/actions":
            self.action_actions()
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
        elif command.name == "/skill":
            await self._submit_skill(argument)
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
        catalog = [
            command
            for command in TUI_COMMANDS
            if command.name != "/workspace" or self._controller.session_id is None
        ]
        argument_owner = self._active_builtin_argument_command()
        user_message_options = (
            self._user_message_arguments()
            if argument_owner in {"/fork", "/navigate"}
            else ()
        )
        for index, command in enumerate(catalog):
            if command.name == "/workspace" and argument_owner == command.name:
                catalog[index] = replace(
                    command,
                    argument_options=self._workspace_arguments(),
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
                    argument_hint="[" + "|".join(option.value for option in options) + "]",
                    argument_options=options,
                )
            elif command.name == "/model" and argument_owner == command.name:
                options = tuple(
                    TuiCommandArgument(
                        value=model.key,
                        description=model.provider_id,
                        label=model.endpoint_id,
                    )
                    for model in self._models
                )
                catalog[index] = replace(command, argument_options=options)
            elif command.name == "/skill" and argument_owner == command.name:
                options = self._skill_arguments()
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
        return tuple(
            TuiCommandArgument(workspace, "")
            for workspace in self._preferences.recent_workspaces
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
                skill.argument_hint or "<instruction>",
            )
            for skill in skills
        )

    async def _submit_skill(self, argument: str) -> None:
        name, separator, task = argument.lstrip().partition(" ")
        if not name:
            return
        try:
            invocation = SkillInvocationRequest(
                name=name,
                arguments=task.lstrip() if separator else "",
            )
        except ValueError:
            self.notify(
                "Skill names must use lowercase letters, digits, and single hyphens.",
                severity="warning",
                markup=False,
            )
            return
        self._submit_run(
            invocation.arguments,
            "follow_up",
            skill_invocation=invocation,
        )

    async def _unqueue_message(self, queue_id: str) -> None:
        try:
            cancelled = await self._controller.cancel_queued_user_input(queue_id)
            await self._sync_queued_inputs()
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
        self._edit_draft(_queued_input_draft(cancelled))

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
                    or f"Plugin command{TUI_DESIGN.inline_separator}{item.plugin_id}"
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
                await self.query_one(ConversationView).add_notice(command_output)
            elif command_data:
                await self.query_one(ConversationView).add_notice(
                    json.dumps(command_data, ensure_ascii=False, indent=2)
                )
            for run_result in result.run_results:
                await self._apply_run_result(run_result)
                await self._sync_queued_inputs(run_result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - plugin failures are user-visible.
            self.notify(str(exc), severity="error", markup=False)
        finally:
            if creating_projection:
                self._waiting_for_session_event = False
                self._sync_interaction_focus(self._interaction)

    @work(exclusive=True, group="compaction")
    async def _compact_session(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before compacting context", severity="warning")
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
            await self._refresh_usage_status()
        except Exception as exc:  # noqa: BLE001 - compaction errors are user-visible.
            self.query_one(StatusLine).set_activity("failed")
            self.notify(str(exc), severity="error", markup=False)
        finally:
            self._sync_interaction_focus(self._interaction)

    async def _new_session(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before starting a new session", severity="warning")
            return
        try:
            model = await self._controller.new_session()
        except Exception as exc:  # noqa: BLE001 - keep the current view on failure.
            self.notify(str(exc), severity="error", markup=False)
            return
        self._projection = None
        self._replace_agent_tree(None)
        self._interaction = None
        self._active_title = "New session"
        self._active_workspace = self._controller.draft_workspace or self._system_status.workspace
        self._system_status = replace(self._system_status, workspace=self._active_workspace)
        self.query_one(ConversationView).set_system_status(self._system_status)
        self.query_one(BrandHeader).set_context(
            session_title=self._active_title,
            workspace=self._active_workspace,
        )
        status_line = self.query_one(StatusLine)
        self._active_model = model.endpoint_id
        status_line.set_model(self._active_model)
        status_line.set_activity("idle")
        status_line.set_context(None)
        status_line.set_session_controls(())
        self._sync_interaction(None)
        await self._refresh_approval_indicator()
        await self._refresh_reasoning_indicator()
        self.query_one(QueuedInputPanel).set_inputs(())
        await self._show_welcome()

    def action_new_session(self) -> None:
        if self._visible_read_only:
            return
        menu = self._active_navigation_menu()
        if menu is None and len(self.screen_stack) > 1:
            return
        if menu is not None:
            menu.dismiss(None)
        self.run_worker(self._new_session(), exclusive=True, group="navigation")

    def action_sessions(self) -> None:
        if self._visible_read_only:
            return
        if self._has_active_run():
            self.notify("Abort the active run before switching sessions", severity="warning")
            return
        self._request_menu_navigation(_MENU_SESSIONS)

    def action_main_session(self) -> None:
        if not self._visible_read_only:
            return
        self.run_worker(
            self._return_to_main_session(),
            exclusive=True,
            group="navigation",
        )

    def action_agent_node_left(self) -> None:
        self._navigate_agent_node("left")

    def action_agent_node_right(self) -> None:
        self._navigate_agent_node("right")

    def action_agent_node_root(self) -> None:
        self._navigate_agent_node("root")

    def action_agent_node_latest(self) -> None:
        self._navigate_agent_node("latest")

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
        if target_session_id is None or target_session_id == self._visible_session_id():
            panel.end_navigation()
            return
        target_agent = panel.selected_agent
        if target_session_id != self._controller.session_id and target_agent is None:
            panel.end_navigation()
            return
        self._agent_navigation_revision += 1
        revision = self._agent_navigation_revision
        self.run_worker(
            self._switch_agent_node(
                target_session_id=target_session_id,
                target_agent=target_agent,
                revision=revision,
            ),
            exclusive=True,
            group="navigation",
        )

    async def _switch_agent_node(
        self,
        *,
        target_session_id: str,
        target_agent: AgentTreeNode | None,
        revision: int,
    ) -> None:
        try:
            self.query_one(StatusLine).set_activity("loading_session")
            if target_session_id == self._controller.session_id:
                await self._show_main_session()
            elif target_agent is not None:
                await self._open_child_session(target_agent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - navigation failures are user-visible.
            if self._surface_available():
                self.notify(str(exc), severity="error", markup=False)
        finally:
            if revision == self._agent_navigation_revision and self.query(
                SubagentNavigationPanel
            ):
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
            self.query_one(StatusLine).set_activity("loading_session")
            await self._open_child_session(agent)
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

    async def _choose_session(
        self,
        *,
        menu: ChoiceScreen | None = None,
        switch: bool = False,
    ) -> None:
        if not await self._refresh_sessions():
            return
        if not self._sessions:
            return
        choices = _session_choices(
            self._sessions,
            selected_session_id=self._visible_session_id(),
        )
        session_menu: ChoiceScreen

        async def select_session(session_id: str) -> tuple[Choice, ...] | None:
            # History rendering waits for the main Screen to refresh. Close the
            # modal before loading so the destination view can participate in
            # that refresh instead of waiting behind the picker that initiated it.
            session_menu.dismiss(None)
            try:
                await self._open_session(session_id)
            except Exception as exc:  # noqa: BLE001 - one invalid Session must not exit TUI.
                self._show_session_load_error(session_id, exc)
                return None
            return None

        if menu is not None:
            session_menu = menu
            navigate = menu.switch_page if switch else menu.push_page
            navigate(
                navigation_id=_MENU_SESSIONS,
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=choices,
                on_select=select_session,
            )
            return
        session_menu = ChoiceScreen(
            navigation_id=_MENU_SESSIONS,
            title="SESSIONS",
            placeholder="Filter sessions…",
            choices=choices,
            on_select=select_session,
        )
        self.push_screen(session_menu)

    async def _choose_session_archive(self, *, menu: ChoiceScreen) -> None:
        if not await self._refresh_sessions():
            return
        if not self._sessions:
            return
        sessions = {session.session_id: session for session in self._sessions}

        async def toggle_archive(session_id: str) -> tuple[Choice, ...] | None:
            session = sessions[session_id]
            archived = not session.archived
            try:
                await self._controller.set_session_archived(
                    session_id,
                    archived=archived,
                )
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error", markup=False)
                return None
            if not await self._refresh_sessions():
                return None
            sessions.clear()
            sessions.update({session.session_id: session for session in self._sessions})
            return _session_choices(
                self._sessions,
                selected_session_id=self._visible_session_id(),
            )

        menu.push_page(
            title="ARCHIVE SESSIONS",
            placeholder="Filter sessions…",
            choices=_session_choices(
                self._sessions,
                selected_session_id=self._visible_session_id(),
            ),
            on_select=toggle_archive,
            retain_focus_position_on_select=True,
        )

    async def _choose_session_rename(self, *, menu: ChoiceScreen) -> None:
        if not await self._refresh_sessions():
            return
        active = tuple(session for session in self._sessions if not session.archived)
        if not active:
            return

        async def rename_session(
            session_id: str,
            title: str,
        ) -> tuple[Choice, ...] | None:
            try:
                renamed = await self._controller.rename_session(session_id, title)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error", markup=False)
                return None
            if session_id == self._controller.session_id:
                self._active_title = renamed.title
                self.query_one(BrandHeader).set_context(session_title=renamed.title)
            if not await self._refresh_sessions():
                return None
            active_sessions = tuple(session for session in self._sessions if not session.archived)
            return _rename_session_choices(
                active_sessions,
                selected_session_id=self._visible_session_id(),
            )

        screen = EditableChoiceScreen(
            title="RENAME SESSION",
            placeholder="Filter sessions…",
            choices=_rename_session_choices(
                active,
                selected_session_id=self._visible_session_id(),
            ),
            on_save=rename_session,
        )
        await menu.open_child(screen)

    async def _choose_session_delete(self, *, menu: ChoiceScreen) -> None:
        if not await self._refresh_sessions():
            return
        archived = tuple(session for session in self._sessions if session.archived)
        if not archived:
            return

        async def delete_selected(
            session_ids: tuple[str, ...],
        ) -> tuple[Choice, ...] | None:
            current_session_id = self._controller.session_id
            try:
                removed = await self._controller.delete_archived_sessions(session_ids)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error", markup=False)
                return None
            self._forget_input_drafts(removed)
            if current_session_id in removed:
                await self._new_session()
            if not await self._refresh_sessions():
                return None
            archived_sessions = tuple(session for session in self._sessions if session.archived)
            return _session_choices(
                archived_sessions,
                selected_session_id=self._visible_session_id(),
            )

        delete_screen = MultiChoiceScreen(
            title="DELETE SESSIONS",
            placeholder="Filter archived sessions…",
            choices=_session_choices(
                archived,
                selected_session_id=self._visible_session_id(),
            ),
            on_confirm=delete_selected,
            selected_marker=TUI_DESIGN.selected_choice_marker,
            unselected_marker=TUI_DESIGN.unselected_choice_marker,
            confirm_label="Delete",
        )
        await menu.open_child(delete_screen)

    async def _refresh_sessions(self) -> bool:
        try:
            self._sessions = await self._controller.sessions()
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
            subscription = self._controller.subscribe_session(
                session_id,
                after_sequence=loaded.event_high_water,
            )
            await self._install_session_surface(
                loaded,
                subscription=subscription,
            )
        except Exception:
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
        )
        agents = self._replace_agent_tree(session.agent_root_session_id)
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
                if visible_session_id is not None:
                    held_scroll_y = conversation.held_scroll_y()
                    if held_scroll_y is None:
                        self._held_session_scroll_y.pop(visible_session_id, None)
                    else:
                        self._held_session_scroll_y[visible_session_id] = held_scroll_y
                    self._remember_session_history(
                        visible_session_id,
                        conversation.take_history_state(),
                    )
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
                    composer.restore_command_draft(self._controlled_composer_draft or "")
                    self._controlled_composer_draft = None
                elif not read_only:
                    self._restore_input_draft(session.session_id)
                # The selected Session now owns the surface. Clear every interaction
                # projected by the previous Session before yielding while its history
                # is mounted; the new projection will restore its own pending request.
                self._visible_read_only = read_only
                self._visible_agent = agent
                composer.set_class(read_only, "read-only-session")
                self.query_one(SubagentNavigationPanel).set_current_session(session.session_id)
                self._projection = None
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
                    self._active_model = _model_endpoint_name(session.current_model)
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
        except (Exception, asyncio.CancelledError):
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
        except (Exception, asyncio.CancelledError):
            observation.subscription.close()
            raise

    async def _return_to_main_session(self) -> None:
        try:
            await self._show_main_session()
        except Exception as exc:  # noqa: BLE001 - navigation failures are user-visible.
            if self._surface_available():
                self.notify(str(exc), severity="error", markup=False)

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
        screen = ChoiceScreen(
            title="SESSION LOAD FAILED",
            placeholder="Press Enter to close…",
            choices=(
                Choice(
                    "close",
                    "Close",
                    description=f"Could not open {session_id}:\n{error}",
                ),
            ),
        )
        self.push_screen(screen)

    @work(exclusive=True, group="navigation")
    async def _fork_session(
        self,
        target: str,
    ) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before forking the session", severity="warning")
            return
        if self._controller.session_id is None:
            return
        selected_message: MessageRecord | None = None
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
            await self._open_session(result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - branch validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        if selected_message is not None:
            self._edit_draft(user_message_text(selected_message))

    @staticmethod
    def _prepare_loaded_session(
        loaded: LoadedSession,
    ) -> tuple[
        TuiSessionProjection,
        tuple[RunPresentation, ...],
        tuple[str, str],
    ]:
        """Build persisted projections without occupying Textual's event loop."""

        ordered_events = canonical_event_order(loaded.events)
        projection = TuiSessionProjection(loaded.detail.session.session_id)
        projection.restore(
            ordered_events,
            event_high_water=loaded.event_high_water,
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
        self._active_model = model.endpoint_id
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
                f"{TUI_DESIGN.inline_separator}{_format_reasoning_state(self._reasoning_state)}"
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
        choices = tuple(
            Choice(
                value=mode,
                label=mode,
                marker=TUI_DESIGN.selected_choice_marker if mode == current else "",
                trailing="current" if mode == current else "",
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
        await self._set_reasoning(_next_cycle_value(values, _reasoning_value(state.requested)))

    @work(exclusive=True, group="navigation")
    async def _choose_reasoning(self) -> None:
        await self._edit_reasoning()

    async def _edit_reasoning(self) -> None:
        try:
            state = await self._controller.session_reasoning()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        current = _reasoning_value(state.requested)
        arguments = _reasoning_arguments(state)
        choices = tuple(
            Choice(
                value=argument.value,
                label=argument.value,
                marker=(TUI_DESIGN.selected_choice_marker if argument.value == current else ""),
                trailing="current" if argument.value == current else "",
            )
            for argument in arguments
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

        self.run_worker(navigate(), exclusive=True, group="navigation")

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
            if action_id == _THEME_ACTION:
                await self._choose_theme(menu=action_screen)
                return None
            if action_id == _THOUGHT_DISPLAY_ACTION:
                await self._choose_thought_display(menu=action_screen)
                return None
            if action_id == "/sessions":
                await self._choose_session(menu=action_screen)
                return None
            if action_id == _RENAME_SESSION_ACTION:
                await self._choose_session_rename(menu=action_screen)
                return None
            if action_id == _ARCHIVE_SESSION_ACTION:
                await self._choose_session_archive(menu=action_screen)
                return None
            if action_id == _DELETE_SESSION_ACTION:
                await self._choose_session_delete(menu=action_screen)
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

        actions = (
            Choice(
                _THEME_ACTION,
                "Theme color",
                accent=self._tui_theme.accent,
                group="Appearance",
            ),
            Choice(
                _THOUGHT_DISPLAY_ACTION,
                "Model thought display",
                group="Appearance",
            ),
            self._command_action_choice("/new", "Session", "new_session"),
            self._command_action_choice("/sessions", "Session", "sessions"),
            Choice(_RENAME_SESSION_ACTION, "Rename session", group="Session"),
            Choice(_ARCHIVE_SESSION_ACTION, "Archive session", group="Session"),
            Choice(_DELETE_SESSION_ACTION, "Delete session", group="Session"),
            *(
                Choice(
                    f"{_SYSTEM_SETTING_ACTION_PREFIX}{action.action_id}",
                    action.label,
                    trailing=(
                        self._shortcut_for_action("plugins")
                        if action.action_id == "plugins.list"
                        else ""
                    ),
                    group=action.group,
                )
                for action in SYSTEM_SETTING_ACTIONS
            ),
            self._command_action_choice("/help", "Support", "help"),
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

        recent_workspaces = (
            workspace,
            *(item for item in self._preferences.recent_workspaces if item != workspace),
        )[:MAX_RECENT_WORKSPACES]
        if recent_workspaces == self._preferences.recent_workspaces:
            return
        preferences = self._preferences.model_copy(update={"recent_workspaces": recent_workspaces})
        try:
            await asyncio.to_thread(self._preferences_store.save, preferences)
        except OuroError as exc:
            _LOG.warning("Could not persist recent TUI workspaces: %s", exc)
            self.notify(str(exc), severity="warning", markup=False)
            return
        self._preferences = preferences

    def _shortcut_for_action(self, action: str) -> str:
        for binding in Binding.make_bindings(self.BINDINGS):
            if binding.action == action:
                return self.get_key_display(binding)
        return ""

    async def _choose_theme(self, *, menu: ChoiceScreen | None = None) -> None:
        committed_theme = self._tui_theme

        def theme_choices(selected: TuiTheme) -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    theme.theme_id,
                    theme.label,
                    accent=theme.accent,
                    marker=TUI_DESIGN.selected_choice_marker if theme == selected else "",
                )
                for theme in TUI_THEMES.values()
            )

        async def select_theme(theme_id: str) -> tuple[Choice, ...] | None:
            nonlocal committed_theme
            if not await self._set_tui_theme(theme_id, restore_theme=committed_theme):
                return None
            committed_theme = self._tui_theme
            return theme_choices(committed_theme)

        def restore_committed_theme() -> bool:
            self._apply_tui_theme(committed_theme)
            return False

        if menu is not None:
            menu.push_page(
                title="THEME COLOR",
                placeholder="Filter theme colors…",
                choices=theme_choices(committed_theme),
                initial_value=committed_theme.theme_id,
                on_highlight=self._preview_tui_theme,
                on_select=select_theme,
                on_cancel=restore_committed_theme,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title="THEME COLOR",
                placeholder="Filter theme colors…",
                choices=theme_choices(committed_theme),
                initial_value=committed_theme.theme_id,
                on_highlight=self._preview_tui_theme,
                on_select=select_theme,
            )
        )
        self._apply_tui_theme(committed_theme)

    async def _choose_thought_display(self, *, menu: ChoiceScreen | None = None) -> None:
        def display_choices(expanded: bool) -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    value,
                    label,
                    marker=(
                        TUI_DESIGN.selected_choice_marker
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
                title="MODEL THOUGHT DISPLAY",
                placeholder="Choose how thoughts are displayed…",
                choices=display_choices(self._preferences.reasoning_expanded),
                initial_value=("expand" if self._preferences.reasoning_expanded else "collapse"),
                on_select=select_display,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title="MODEL THOUGHT DISPLAY",
                placeholder="Choose how thoughts are displayed…",
                choices=display_choices(self._preferences.reasoning_expanded),
                initial_value=("expand" if self._preferences.reasoning_expanded else "collapse"),
                on_select=select_display,
            )
        )

    def _preview_tui_theme(self, theme_id: str) -> None:
        theme = TUI_THEMES.get(theme_id)
        if theme is not None:
            self._apply_tui_theme(theme)

    async def _set_tui_theme(
        self,
        theme_id: str,
        *,
        restore_theme: TuiTheme | None = None,
    ) -> bool:
        theme = TUI_THEMES.get(theme_id)
        if theme is None:
            self.notify(f"Unknown TUI theme: {theme_id}", severity="error", markup=False)
            return False
        preferences = self._preferences.model_copy(update={"theme": theme_id})
        try:
            await asyncio.to_thread(self._preferences_store.save, preferences)
        except OuroError as exc:
            if restore_theme is not None:
                self._apply_tui_theme(restore_theme)
            self.notify(str(exc), severity="error", markup=False)
            return False
        self._preferences = preferences
        self._apply_tui_theme(theme)
        return True

    async def _set_reasoning_expanded(self, value: str) -> bool:
        expanded = _THOUGHT_DISPLAY_OPTIONS.get(value)
        if expanded is None:
            return False
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

    def _apply_tui_theme(self, theme: TuiTheme) -> None:
        self._session_history_states.clear()
        self._tui_theme = theme
        self.refresh_css(animate=False)
        self._refresh_theme_content()

    def _refresh_theme_content(self) -> None:
        for screen in self.screen_stack:
            for widget in screen.walk_children():
                refresh_theme = getattr(widget, "refresh_theme", None)
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
            self.run_worker(menu.action_cancel(), exclusive=True, group="navigation")
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


def _model_endpoint_name(route: str) -> str:
    _provider_id, separator, endpoint_id = route.partition("/")
    return endpoint_id if separator else route


def _event_record(event: RuntimeEventView) -> EventRecord:
    """Adapt the redacted Host event DTO to the existing TUI reducer input."""

    return EventRecord(
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
    covered_legacy_rounds = sum(
        1
        for message in messages
        if message.role == "assistant" and message.run_id == run_id and message.turn_id is None
    )
    start = 0
    for index, event in enumerate(ordered):
        if event.event_type != "model_message_completed":
            continue
        if event.turn_id is not None and event.turn_id in covered_turns:
            start = index + 1
        elif event.turn_id is None and covered_legacy_rounds:
            covered_legacy_rounds -= 1
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
    capability = state.capability
    if capability.activation == "unsupported":
        return ("default",)
    activation = (
        ("default", "enabled", "disabled")
        if capability.activation == "toggle"
        else ("default", "enabled")
    )
    return (*activation, *capability.supported_efforts)


def _reasoning_arguments(state: ModelReasoningState) -> tuple[TuiCommandArgument, ...]:
    return tuple(
        TuiCommandArgument(value, _reasoning_description(value, state))
        for value in _available_reasoning_values(state)
    )


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


def _truncate_session_title(title: str) -> str:
    rendered = Text(title)
    rendered.truncate(_SESSION_TITLE_MAX_CELLS, overflow="ellipsis")
    return rendered.plain


def _session_choices(
    sessions: tuple[SessionCatalogEntry, ...],
    *,
    selected_session_id: str | None = None,
    today: date | None = None,
) -> tuple[Choice, ...]:
    local_today = today or datetime.now().astimezone().date()
    ordered = (
        *(item for item in sessions if not item.archived),
        *(item for item in sessions if item.archived),
    )
    return tuple(
        Choice(
            value=item.session_id,
            label=_truncate_session_title(("★ " if item.pinned else "") + item.title),
            trailing=item.status,
            marker=(
                TUI_DESIGN.selected_choice_marker if item.session_id == selected_session_id else ""
            ),
            group=(
                "Archived"
                if item.archived
                else _session_date_group(item.last_activity_at, today=local_today)
            ),
        )
        for item in ordered
    )


def _rename_session_choices(
    sessions: tuple[SessionCatalogEntry, ...],
    *,
    selected_session_id: str | None = None,
) -> tuple[Choice, ...]:
    titles = {session.session_id: session.title for session in sessions}
    return tuple(
        replace(choice, edit_value=titles[choice.value])
        for choice in _session_choices(
            sessions,
            selected_session_id=selected_session_id,
        )
    )


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
    if item.skill_name is None:
        return item.prompt
    task = f" {item.prompt}" if item.prompt else ""
    return f"/skill {item.skill_name}{task}"


def _queued_input_preview(item: QueuedUserInputView) -> str:
    return _summarize(_queued_input_draft(item), limit=64) or "(empty message)"


def _summarize(text: str, limit: int = 60) -> str:
    """Collapse whitespace and truncate so user messages fit a single menu line."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"
