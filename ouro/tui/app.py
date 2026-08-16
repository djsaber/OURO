from __future__ import annotations

import asyncio
import json
import logging
import sys
from calendar import month_name
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from pydantic import JsonValue
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.driver import Driver
from textual.message import Message
from textual.messages import Prune
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import TextArea

from ouro.application.models import AvailableModel, ModelReasoningState
from ouro.application.queued_inputs import QueuedInputMode, QueuedUserInputView
from ouro.application.session_models import SessionCatalogEntry
from ouro.application.skill_activations import SkillInvocationRequest
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError
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
from ouro.tui.plan_confirmation import project_plan_confirmation
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.question_forms import project_question_form
from ouro.tui.screens import (
    Choice,
    ChoiceScreen,
    EditableChoiceScreen,
    HelpScreen,
    MenuDestination,
    MultiChoiceScreen,
)
from ouro.tui.state import (
    PendingInteraction,
    TuiSessionProjection,
    project_agent_interaction,
    project_run_presentation,
    project_run_presentations,
)
from ouro.tui.theme import SELECTED_CHOICE_MARKER, TUI_THEMES, TuiTheme
from ouro.tui.widgets import (
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


_RUNTIME_RENDER_INTERVAL_SECONDS = 0.05
_MCP_STATUS_SYNC_INTERVAL_SECONDS = 0.25
_SESSION_TITLE_MAX_CELLS = 56
_RUNTIME_RENDER_BOUNDARY_EVENTS = {
    "compaction_completed",
    "run_completed",
    "run_aborted",
    "run_failed",
    "run_paused",
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
        *,
        clipboard_writer: Callable[[str], None] = write_system_clipboard,
    ) -> None:
        super().__init__(id="_default")
        self._on_host_prune = on_prune
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


_OURO_TUI_CSS = """
    /* Design tokens — one source of truth for the flat OURO chrome.
       $ouro-theme is the single accent color: borders, titles, the logo, and
       active highlights all derive from it, so the whole TUI recolors by
       changing one value. The owning app injects the persisted TUI palette so
       CSS and Rich-text styling share one source of truth (see theme.py). */
    $ouro-background: #0c0c0c;
    $ouro-panel: #171717;
    $ouro-border-left: #4d5560;
    $ouro-border-right: #34373d;
    $ouro-border-focus: #3f4650;
    $ouro-highlight: #242424;
    $conversation-left-gutter: 2;
    $conversation-right-gutter: 1;
    $welcome-width: 72%;
    $welcome-min-width: 48;
    $welcome-max-width: 116;
    $welcome-inline-padding: 2;
    $welcome-logo-gap: 2;
    $welcome-info-min-height: 15;
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
        color: #d6d9df;
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
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-theme;
    }

    Toast.-information {
        border-left: solid $ouro-theme;
    }

    Toast.-warning {
        border-left: solid #facc15;
    }

    Toast.-error {
        border-left: solid #b97878;
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
        color: #aeb5bf;
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
        min-height: $welcome-info-min-height;
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
        color: #788492;
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
        color: #788492;
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
        color: #788492;
    }

    .welcome-tip-description {
        width: auto;
        height: 1;
        color: #8f9aa8;
    }

    .welcome-version {
        width: 1fr;
        height: 1;
        color: #d6d9df;
        text-style: bold;
        text-align: left;
    }

    #input-region.welcome-page #command-popup {
        overlay: screen;
        offset-y: -100%;
    }

    #input-region.welcome-page #composer,
    #input-region.welcome-page #command-popup,
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
        padding: 0 2;
    }

    .assistant-turn.after-tool {
        padding-top: 1;
    }

    .assistant-markdown {
        height: auto;
        background: transparent;
        padding: 0;
    }

    .reasoning-view {
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
        color: #8f9aa8;
        background: transparent;
    }

    .reasoning-label {
        height: 1;
        color: #aeb5bf;
        text-style: bold;
    }

    .reasoning-content {
        height: auto;
        margin: 1 0 0 1;
        padding: 0 1;
        color: #8f9aa8;
        text-style: dim italic;
        background: transparent;
        border-left: solid #505967;
    }

    Markdown, MarkdownBlock, MarkdownTable {
        background: transparent;
    }

    /* Keep fenced code on the same quiet surface as user turns and the
       composer. The fence selector must come after the generic Markdown
       rules so Textual's built-in translucent black does not leak through. */
    MarkdownFence {
        background: $ouro-panel;
    }

    .tool-row {
        height: auto;
        margin: 0 0 0 2;
        color: #aeb5bf;
        background: transparent;
    }

    #interaction-bar {
        height: auto;
        max-height: 10;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
    }

    #interaction-bar:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    #ask-user-panel {
        height: auto;
        max-height: 18;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
    }

    #ask-user-panel:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    #plan-confirmation-panel {
        height: auto;
        max-height: 24;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
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
        height: auto;
        margin-top: 1;
    }

    #plan-confirmation-panel:focus {
        border-left: solid $ouro-theme;
        border-right: solid $ouro-border-focus;
    }

    #command-popup {
        height: auto;
        max-height: 14;
        margin: 0 2;
        padding: 1 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
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
        color: #e5e7eb;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
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

    #composer .text-area--suggestion {
        color: #626b78;
    }

    #composer .text-area--cursor-line {
        background: transparent;
    }

    #queued-input-panel {
        height: auto;
        max-height: 8;
        margin: 0 2;
        padding: 1 2 0 2;
        color: #d6d9df;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
    }

    #queued-input-content {
        height: auto;
        background: transparent;
    }

    StatusLine {
        height: 1;
        margin: 0;
        padding: 0 $conversation-right-gutter 0 $conversation-left-gutter;
        color: #6f7a87;
        background: $ouro-background;
    }

    #activity-indicator { width: 1fr; }
    #context-detail { width: auto; text-align: right; }

    ModalScreen {
        align: center middle;
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
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
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
        max-height: 20;
        padding: 0 1 1 1;
    }

    /* Shared grouped-menu treatment. Choice and inline-edit screens use the
       same rows, group headers, focus color, and hidden chrome. */
    .grouped-menu-list {
        background: transparent;
        border: none;
        border-left: solid $ouro-border-left;
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
        background: #2a2a2a;
        text-style: not bold;
    }

    #choice-hint {
        height: 2;
        padding: 0 2 1 2;
        color: #6f7a87;
        background: transparent;
        border: none;
        border-left: solid $ouro-border-left;
    }

    #help-panel {
        width: 72;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        background: $ouro-panel;
        border-top: none;
        border-right: solid $ouro-border-right;
        border-bottom: none;
        border-left: solid $ouro-border-left;
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
        self._interaction: PendingInteraction | None = None
        self._models: tuple[AvailableModel, ...] = ()
        self._sessions: tuple[SessionCatalogEntry, ...] = ()
        self._active_model = "starting"
        self._active_title = "New session"
        self._reasoning_state: ModelReasoningState | None = None
        self._system_status = TuiSystemStatus()
        self._host_closing = False
        self._waiting_for_session_event = False
        self._dismissed_command_text: str | None = None
        self._pending_runtime_events: list[EventRecord] = []
        self._runtime_events_ready_posted = False
        self._runtime_events_timer: Timer | None = None
        self._runtime_event_apply_lock = asyncio.Lock()
        self._mcp_status_timer: Timer | None = None

    @property
    def tui_theme(self) -> TuiTheme:
        return self._tui_theme

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), "ouro-theme": self._tui_theme.accent}

    def compose(self) -> ComposeResult:
        header = BrandHeader(initial_context=self._active_title)
        header.display = False
        yield header
        with Vertical(id="main"):
            yield ConversationView(
                reasoning_expanded=self._preferences.reasoning_expanded,
                welcome_state_changed=self._set_welcome_mode,
                system_status_changed=self._set_welcome_system_status,
            )
            yield WelcomePanel()

    def get_driver_class(self) -> type[Driver]:
        driver_class = super().get_driver_class()
        if sys.platform == "win32":
            from textual.drivers.windows_driver import WindowsDriver

            if driver_class is WindowsDriver:
                from ouro.tui.windows_driver import OuroWindowsDriver

                return OuroWindowsDriver
        return driver_class

    def get_default_screen(self) -> Screen:
        return _OuroHostScreen(self._begin_host_close)

    def on_mount(self) -> None:
        self.query_one(StatusLine).set_activity("starting")
        self.query_one(Composer).disabled = True
        self._mcp_status_timer = self.set_interval(
            _MCP_STATUS_SYNC_INTERVAL_SECONDS,
            self._sync_mcp_status,
            pause=True,
        )
        self.run_worker(self._start(), name="tui-start", exclusive=True)
        if self._preferences_error is not None:
            self.notify(self._preferences_error, severity="warning")

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
        if self._mcp_status_timer is not None:
            self._mcp_status_timer.stop()
            self._mcp_status_timer = None
        self.workers.cancel_all()

    async def _start(self) -> None:
        session_load_error: tuple[str, Exception] | None = None
        try:
            bootstrap = await self._controller.start()
        except Exception as exc:  # noqa: BLE001 - surface boundary failures in the Host.
            self.query_one(StatusLine).set_activity("failed")
            self.notify(f"OURO could not start: {exc}", severity="error", markup=False)
            return
        self._apply_bootstrap(bootstrap)
        self._apply_system_status(bootstrap)
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
            composer.disabled = False
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
        """Project the effective thinking level onto the status line."""
        self._reasoning_state = state
        if state.capability.activation == "unsupported":
            value, peak = None, False
        else:
            value = _reasoning_value(state.effective)
            peak = _reasoning_peak(state)
        self.query_one(StatusLine).set_reasoning(value, peak=peak)

    async def _refresh_reasoning_indicator(self) -> None:
        """Refresh the status line thinking level after a model or session change."""
        self._reasoning_state = None
        try:
            state = await self._controller.session_reasoning()
        except Exception:  # noqa: BLE001 - reasoning is optional status, not a blocker.
            self.query_one(StatusLine).set_reasoning(None)
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

    def _apply_system_status(self, bootstrap: TuiBootstrap) -> None:
        self._system_status = bootstrap.system_status
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def _refresh_system_status(self) -> bool:
        try:
            status = await self._controller.system_status()
        except Exception as exc:  # noqa: BLE001 - retain the last valid Host projection.
            self.notify(
                f"Could not refresh system status: {exc}",
                severity="warning",
                markup=False,
            )
            return False
        self._system_status = status
        self.query_one(ConversationView).set_system_status(status)
        return True

    async def _show_welcome(self, *, refresh_status: bool = True) -> None:
        if refresh_status:
            await self._refresh_system_status()
        await self.query_one(ConversationView).show_welcome()
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
        stream_started_at: datetime | None = None

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
            if event.event_type == "run_started":
                assistant_visible = False
            if event.event_type == "model_request_started":
                await flush_stream()
                await conversation.finish_assistant()
                conversation.begin_model_request(run_id=event.run_id)
            if event.event_type in {*_SESSION_INPUT_MESSAGE_EVENTS, "queued_input_consumed"}:
                await flush_stream()
                message_id = event.payload.get("message_id")
                if isinstance(message_id, str):
                    input_message = await self._controller.message(event.session_id, message_id)
                    if input_message.role == "user" and not input_message.scaffold:
                        await conversation.add_user_message(input_message)
                        assistant_visible = False
            if event.event_type == "compaction_completed":
                await flush_stream()
                covered = event.payload.get("covered_message_count")
                if isinstance(covered, int) and covered >= 0:
                    await conversation.add_compaction(covered_message_count=covered)
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
            self.notify(diagnostic, severity="error", markup=False)

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
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
        if result.run.kind == "agent":
            presentation = project_run_presentation(result.run)
            if self._projection.model is not None:
                presentation = replace(presentation, model=self._projection.model)
            await self.query_one(ConversationView).add_run_summary(presentation)
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

    async def _refresh_models(self) -> tuple[AvailableModel, ...]:
        """Synchronize the Host's model projection with the live Runtime catalog."""

        models = await self._controller.models()
        self._models = models
        self._system_status = replace(
            self._system_status,
            provider_ids=tuple(sorted({model.provider_id for model in models})),
            model_count=len(models),
        )
        self.query_one(ConversationView).set_system_status(self._system_status)
        return models

    async def _sync_queued_inputs(self, session_id: str | None = None) -> None:
        panel = self.query_one(QueuedInputPanel)
        if session_id is None and self._controller.session_id is None:
            panel.set_inputs(())
            return
        panel.set_inputs(await self._controller.queued_user_inputs(session_id))

    async def on_text_area_changed(self, message: TextArea.Changed) -> None:
        if message.text_area is not self.query_one(Composer):
            return
        value = message.text_area.text
        message.text_area.suggestion = ""
        popup = self.query_one(CommandPopup)
        ask_user = self.query_one(AskUserPanel)
        plan_panel = self.query_one(PlanConfirmationPanel)
        if (ask_user.display and ask_user.awaiting_freeform) or (
            plan_panel.display and plan_panel.awaiting_feedback
        ):
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
                self.notify(
                    f"Could not refresh models: {exc}",
                    severity="error",
                    markup=False,
                )
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
        value = message.text
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
                selected_value = f"{argument_command.name} {selected_argument.value}"
                if selected_argument.requires_value:
                    self._edit_command(f"{selected_value} ")
                    return
                value = selected_value
        value = selected.name if selected is not None else value
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
            composer = self.query_one(Composer)
            composer.disabled = False
            current = self._interaction
            ask_user = self.query_one(AskUserPanel)
            plan_panel = self.query_one(PlanConfirmationPanel)
            if current is not None and current.kind == "question" and plan_panel.display:
                if plan_panel.awaiting_feedback:
                    composer.focus()
                else:
                    plan_panel.focus()
            elif current is not None and current.kind == "question" and ask_user.display:
                if ask_user.awaiting_freeform:
                    composer.focus()
                else:
                    ask_user.focus()
            else:
                composer.focus()

    def on_interaction_bar_decision(self, message: InteractionBar.Decision) -> None:
        self._resolve_approval(message.interaction, message.approved)

    def on_ask_user_panel_completed(self, message: AskUserPanel.Completed) -> None:
        interaction = self._interaction
        if interaction is not None and interaction.kind == "question":
            self._answer_input(interaction, message.answer)

    def on_ask_user_panel_freeform_requested(
        self,
        _message: AskUserPanel.FreeformRequested,
    ) -> None:
        composer = self.query_one(Composer)
        self._sync_composer_placeholder()
        composer.focus()

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
        composer = self.query_one(Composer)
        self._sync_composer_placeholder()
        composer.focus()

    def _sync_activity(self, activity: RuntimeActivitySnapshot) -> None:
        self.query_one(StatusLine).set_activity(activity.activity)
        status_line = self.query_one(StatusLine)
        status_line.set_context_metrics(activity.effective_context_metrics)
        self._sync_composer_placeholder()

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
        with self.batch_update():
            interaction_bar = self.query_one(InteractionBar)
            ask_user = self.query_one(AskUserPanel)
            plan_panel = self.query_one(PlanConfirmationPanel)
            composer = self.query_one(Composer)
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
                composer.disabled = False
                self._sync_composer_placeholder()
                return
            ask_user.hide_panel()
            plan_panel.hide_panel()
            interaction_bar.show_interaction(interaction)
            self._sync_composer_placeholder()
            if interaction is not None and interaction.kind == "approval":
                composer.disabled = True
                interaction_bar.focus()
            else:
                composer.disabled = False
                composer.focus()

    def _sync_composer_placeholder(self) -> None:
        """Project run and interaction state onto the composer hint."""

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
            placeholder = Composer.ACTIVE_RUN_PLACEHOLDER
        else:
            placeholder = Composer.DEFAULT_PLACEHOLDER
        self.query_one(Composer).placeholder = placeholder

    def _edit_command(self, value: str) -> None:
        composer = self.query_one(Composer)
        composer.load_text(value)
        composer.move_cursor((0, len(value)))
        composer.focus()

    def _edit_draft(self, value: str) -> None:
        composer = self.query_one(Composer)
        composer.load_text(value)
        composer.move_cursor((composer.document.line_count - 1, len(composer.document.lines[-1])))
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
        elif command.name == "/actions":
            self.action_actions()
        elif command.name == "/fork":
            self._fork_session(argument or None)
        elif command.name == "/model":
            if argument:
                await self._select_model(argument)
            else:
                await self._command_usage(command)
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
                self._set_reasoning_expanded(argument.casefold())
            else:
                await self._command_usage(command)
        elif command.name == "/compact":
            if argument:
                self.notify("Usage: /compact", severity="warning", markup=False)
            else:
                self._compact_session()
        elif command.name == "/skill":
            await self._submit_skill(argument)
        elif command.name == "/unqueue":
            if argument:
                await self._unqueue_message(argument)
            else:
                await self._command_usage(command)
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
        navigate_options = self._navigate_options()
        for index, command in enumerate(catalog):
            if command.name == "/navigate":
                catalog[index] = replace(command, argument_options=navigate_options)
            elif command.name == "/thinking" and self._reasoning_state is not None:
                options = _reasoning_arguments(self._reasoning_state)
                catalog[index] = replace(
                    command,
                    argument_hint="[" + "|".join(option.value for option in options) + "]",
                    argument_options=options,
                )
            elif command.name == "/model":
                options = tuple(
                    TuiCommandArgument(
                        value=model.key,
                        description=model.provider_id,
                        label=model.endpoint_id,
                    )
                    for model in self._models
                )
                catalog[index] = replace(command, argument_options=options)
            elif command.name == "/skill":
                options = self._skill_arguments()
                catalog[index] = replace(command, argument_options=options)
            elif command.name == "/unqueue":
                catalog[index] = replace(
                    command,
                    argument_options=self._queued_input_arguments(),
                )
        return (*catalog, *self._plugin_commands())

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
            command = resolve_command("/skill")[0]
            if command is not None:
                await self._command_usage(command)
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

    async def _command_usage(self, command: TuiCommand) -> None:
        self.notify(f"Usage: {command.usage}", severity="warning", markup=False)

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
        for index, text in enumerate(conversation.user_turn_summaries(), start=1):
            options.append(TuiCommandArgument(f"#{index}", _summarize(text)))
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
                self.notify(
                    f"Unknown command: {command_text}. Use /help to see available commands.",
                    severity="warning",
                    markup=False,
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
            self.notify(str(exc), severity="error", markup=False)
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
        try:
            result = await self._controller.compact()
            await self._flush_pending_runtime_events()
            await self._apply_runtime_events(result.events)
            await self._refresh_usage_status()
            if result.skipped or result.compaction is None:
                self.notify("No complete older turn is eligible for compaction")
        except Exception as exc:  # noqa: BLE001 - compaction errors are user-visible.
            self.query_one(StatusLine).set_activity("failed")
            self.notify(str(exc), severity="error", markup=False)
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
        await self._refresh_approval_indicator()
        self.query_one(QueuedInputPanel).set_inputs(())
        await self._show_welcome()

    def action_new_session(self) -> None:
        menu = self._active_navigation_menu()
        if menu is None and len(self.screen_stack) > 1:
            return
        if menu is not None:
            menu.dismiss(None)
        self.run_worker(self._new_session(), exclusive=True, group="navigation")

    def action_sessions(self) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before switching sessions", severity="warning")
            return
        self._request_menu_navigation(_MENU_SESSIONS)

    def action_conversation_top(self) -> None:
        self._navigate_to("top")

    def action_conversation_bottom(self) -> None:
        self._navigate_to("bottom")

    def action_previous_user_message(self) -> None:
        self.query_one(ConversationView).scroll_to_previous_user_turn()

    def action_next_user_message(self) -> None:
        self.query_one(ConversationView).scroll_to_next_user_turn()

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
            self.notify("No saved sessions yet")
            return
        choices = _session_choices(self._sessions)
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
            self.notify("No saved sessions yet")
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
            return _session_choices(self._sessions)

        menu.push_page(
            title="ARCHIVE SESSIONS",
            placeholder="Filter sessions…",
            choices=_session_choices(self._sessions),
            on_select=toggle_archive,
            retain_focus_position_on_select=True,
        )

    async def _choose_session_rename(self, *, menu: ChoiceScreen) -> None:
        if not await self._refresh_sessions():
            return
        active = tuple(session for session in self._sessions if not session.archived)
        if not active:
            self.notify("No active sessions are available to rename")
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
            return _rename_session_choices(active_sessions)

        screen = EditableChoiceScreen(
            title="RENAME SESSION",
            placeholder="Filter sessions…",
            choices=_rename_session_choices(active),
            on_save=rename_session,
        )
        await menu.open_child(screen)

    async def _choose_session_delete(self, *, menu: ChoiceScreen) -> None:
        if not await self._refresh_sessions():
            return
        archived = tuple(session for session in self._sessions if session.archived)
        if not archived:
            self.notify("No archived sessions are available to delete")
            return

        async def delete_selected(
            session_ids: tuple[str, ...],
        ) -> tuple[Choice, ...] | None:
            try:
                removed = await self._controller.delete_archived_sessions(session_ids)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error", markup=False)
                return None
            if self._controller.session_id in removed:
                await self._new_session()
            if not await self._refresh_sessions():
                return None
            archived_sessions = tuple(session for session in self._sessions if session.archived)
            return _session_choices(archived_sessions)

        delete_screen = MultiChoiceScreen(
            title="DELETE SESSIONS",
            placeholder="Filter archived sessions…",
            choices=_session_choices(archived),
            on_confirm=delete_selected,
            selected_marker=SELECTED_CHOICE_MARKER,
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
        try:
            loaded = await self._controller.load_session(session_id)
            # The selected Session now owns the surface. Clear every interaction
            # projected by the previous Session before yielding while its history
            # is mounted; the new projection will restore its own pending request.
            self._projection = None
            self._interaction = None
            self._sync_interaction(None)
            conversation = self.query_one(ConversationView)
            await conversation.load_messages(
                loaded.messages,
                loaded.compactions,
                project_run_presentations(
                    (run for run in loaded.runs if run.kind == "agent"),
                    loaded.messages,
                ),
            )
            self._set_welcome_mode(conversation.showing_welcome)
            self.query_one(QueuedInputPanel).set_inputs(loaded.queued_inputs)
            await self._restore_projection(loaded)
            session = loaded.detail.session
            self._active_title = session.title
            if session.current_model is not None:
                self._active_model = _model_endpoint_name(session.current_model)
            self.query_one(BrandHeader).set_context(session_title=self._active_title)
            status_line.set_model(self._active_model)
            await self._refresh_approval_indicator()
            await self._refresh_reasoning_indicator()
        except Exception:
            if previous_projection is None:
                status_line.set_activity("idle")
            else:
                self._sync_activity(previous_projection.activity)
            raise

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
        message_id: str | None = None,
    ) -> None:
        if self._has_active_run():
            self.notify("Abort the active run before forking the session", severity="warning")
            return
        if self._controller.session_id is None:
            self.notify("No active session to fork")
            return
        try:
            points = await self._controller.branch_points()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        if not points:
            self.notify("No user messages are available as fork points")
            return
        points_by_id = {point.message_id: point for point in points}
        selected = message_id
        if selected is None:
            selected = await self.push_screen_wait(
                ChoiceScreen(
                    title="FORK SESSION",
                    placeholder="Filter user messages…",
                    choices=_branch_point_choices(points),
                )
            )
        if selected is None:
            return
        await self._fork_from_message(selected, points_by_id)

    async def _fork_from_message(
        self,
        message_id: str,
        points_by_id: dict[str, MessageRecord],
    ) -> None:
        selected_message = points_by_id.get(message_id)
        if selected_message is None:
            self.notify(
                f"user message is not a fork point in this session: {message_id}",
                severity="error",
                markup=False,
            )
            return
        try:
            result = await self._controller.branch_session(message_id)
            await self._open_session(result.session.session_id)
        except Exception as exc:  # noqa: BLE001 - branch validation is user-visible.
            self.notify(str(exc), severity="error", markup=False)
            return
        self._edit_draft(user_message_text(selected_message))

    async def _restore_projection(self, loaded: LoadedSession) -> None:
        projection = TuiSessionProjection(loaded.detail.session.session_id)
        projection.restore(loaded.events)
        projection.activity = projection.activity.model_copy(
            update={
                "context_status": loaded.context_status,
                "effective_context_metrics": loaded.effective_context_metrics,
            }
        )
        self._projection = projection
        self._sync_activity(projection.activity)
        await self._refresh_interaction(refresh_agents=True)
        self.run_worker(
            self.query_one(ConversationView).sync_tools(projection.activity.tool_calls),
            group="restore-tools",
            exclusive=True,
        )

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
                f" · {_format_reasoning_state(self._reasoning_state)}"
                if self._reasoning_state is not None
                else ""
            )
            self.notify(f"Model: {self._active_model}{reasoning} (next request)")

    def action_approval_settings(self) -> None:
        self._choose_approval_mode()

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
                marker=SELECTED_CHOICE_MARKER if mode == current else "",
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
            available = "|".join(TUI_APPROVAL_PRESETS)
            self.notify(f"Usage: /approval <{available}>", severity="warning", markup=False)
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
        self._choose_reasoning()

    @work(exclusive=True, group="navigation")
    async def _choose_reasoning(self) -> None:
        await self._edit_reasoning()

    async def _edit_reasoning(self) -> None:
        if self._has_active_run():
            self.notify(
                "Abort the active run before changing thinking settings",
                severity="warning",
            )
            return
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
                marker=(SELECTED_CHOICE_MARKER if argument.value == current else ""),
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
            available = "|".join(_REASONING_REQUESTS)
            self.notify(f"Usage: /thinking <{available}>", severity="warning", markup=False)
            return
        try:
            state = await self._controller.set_session_reasoning(request)
        except Exception as exc:  # noqa: BLE001 - Runtime owns capability validation.
            self.notify(str(exc), severity="error", markup=False)
            return
        self._apply_reasoning_state(state)

    def action_actions(self) -> None:
        self._request_menu_navigation(_MENU_ACTIONS)

    def action_plugins(self) -> None:
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
            self._command_action_choice("/abort", "Runtime", "abort"),
            self._command_action_choice("/help", "Support", "help"),
        )
        choices = (*self._recent_action_choices(actions), *actions)
        action_screen: ChoiceScreen

        async def select_action(selected: str) -> tuple[Choice, ...] | None:
            action_id = selected.removeprefix(_RECENT_ACTION_PREFIX)
            self._remember_action(action_id)
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
                action_screen.show_child(HelpScreen())
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
            )
            return
        action_screen = ChoiceScreen(
            navigation_id=_MENU_ACTIONS,
            retain_for_switching=True,
            title="ACTIONS",
            placeholder="Type an action…",
            choices=choices,
            on_select=select_action,
        )
        self.push_screen(action_screen)

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

    def _remember_action(self, action_id: str) -> None:
        """Persist a distinct three-entry MRU using canonical action IDs."""

        recent_actions = (
            action_id,
            *(item for item in self._preferences.recent_actions if item != action_id),
        )[:3]
        if recent_actions == self._preferences.recent_actions:
            return
        preferences = self._preferences.model_copy(update={"recent_actions": recent_actions})
        try:
            self._preferences_store.save(preferences)
        except OuroError as exc:
            _LOG.warning("Could not persist recent TUI actions: %s", exc)
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
                    marker=SELECTED_CHOICE_MARKER if theme == selected else "",
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
                        SELECTED_CHOICE_MARKER
                        if _THOUGHT_DISPLAY_OPTIONS[value] == expanded
                        else ""
                    ),
                )
                for value, label in _THOUGHT_DISPLAY_LABELS.items()
            )

        async def select_display(value: str) -> tuple[Choice, ...] | None:
            if not self._set_reasoning_expanded(value):
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
            self._preferences_store.save(preferences)
        except OuroError as exc:
            if restore_theme is not None:
                self._apply_tui_theme(restore_theme)
            self.notify(str(exc), severity="error", markup=False)
            return False
        self._preferences = preferences
        self._apply_tui_theme(theme)
        return True

    def _set_reasoning_expanded(self, value: str) -> bool:
        expanded = _THOUGHT_DISPLAY_OPTIONS.get(value)
        if expanded is None:
            self.notify(
                "Usage: /thoughts <expand|collapse>",
                severity="warning",
                markup=False,
            )
            return False
        preferences = self._preferences.model_copy(update={"reasoning_expanded": expanded})
        try:
            self._preferences_store.save(preferences)
        except OuroError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return False
        self._preferences = preferences
        self.query_one(ConversationView).set_reasoning_expanded(expanded)
        return True

    def _apply_tui_theme(self, theme: TuiTheme) -> None:
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
                self.push_screen(HelpScreen())
            return
        menu.show_child(HelpScreen())

    def action_abort(self) -> None:
        if not self._has_active_run():
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
            self.notify(str(exc), severity="error", markup=False)


def _model_endpoint_name(route: str) -> str:
    _provider_id, separator, endpoint_id = route.partition("/")
    return endpoint_id if separator else route


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
) -> tuple[Choice, ...]:
    titles = {session.session_id: session.title for session in sessions}
    return tuple(
        replace(choice, edit_value=titles[choice.value]) for choice in _session_choices(sessions)
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


def _branch_point_label(message: MessageRecord) -> str:
    preview = " ".join(message.text_content.split())
    if not preview:
        preview = "empty message"
    return _truncate_session_title(preview)


def _branch_point_choices(
    points: tuple[MessageRecord, ...],
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            value=message.message_id,
            label=_branch_point_label(message),
            trailing=message.created_at.astimezone().strftime("%H:%M"),
        )
        for message in points
    )


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
