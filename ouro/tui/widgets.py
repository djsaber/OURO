from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from math import ceil
from time import monotonic
from typing import Literal

from pydantic import JsonValue
from rich.cells import cell_len, get_character_cell_size
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.geometry import Region, Size
from textual.layout import DockArrangeResult
from textual.message import Message
from textual.strip import Strip
from textual.style import Style
from textual.timer import Timer
from textual.widgets import Label, Static, TextArea

from ouro.application.agent_models import AgentTreeNode
from ouro.application.queued_inputs import (
    QueuedInputMode,
    QueuedUserInputView,
)
from ouro.application.session_models import SessionContextStatus
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.builtin_plugins.plan.interaction import (
    PlanConfirmationAction,
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
)
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.messages import MessageRecord, ModelMessage, SkillActivationContent, TextContent
from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.commands import TuiCommand, TuiCommandArgument
from ouro.tui.controller import TuiApprovalPreset, TuiSystemStatus
from ouro.tui.conversation_history import (
    AssistantMarkdownStream,
    AssistantTailLines,
    CompactionPresentation,
    ConversationHistory,
    ConversationHistoryState,
    ReasoningLines,
    ToolNavigationRequested,
    TranscriptEntry,
    _reasoning_label_text,
    split_markdown_blocks,
)
from ouro.tui.question_forms import QuestionField, QuestionForm
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.state import (
    PendingInteraction,
    RunPresentation,
    format_compact_count,
    format_duration,
)
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.text_inputs import TuiTextArea
from ouro.tui.theme import (
    DEFAULT_TUI_THEME,
    TuiTheme,
    theme_for,
)
from ouro.tui.tool_presentations import ToolLineState, ToolTextTone, present_tool

_OURO_LOGO = """
 ██████╗ ▒▒╗   ▒▒╗██████╗  ▒▒▒▒▒▒╗
██╔═══██╗▒▒║   ▒▒║██╔══██╗▒▒╔═══▒▒╗
██║   ██║▒▒║   ▒▒║██████╔╝▒▒║   ▒▒║
██║   ██║▒▒║   ▒▒║██╔══██╗▒▒║   ▒▒║
╚██████╔╝╚▒▒▒▒▒▒╔╝██║  ██║╚▒▒▒▒▒▒╔╝
 ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝
"""

WELCOME_MAX_WIDTH = 116
COMMAND_POPUP_MIN_HEIGHT = 3
COMMAND_POPUP_MAX_HEIGHT = 14
INTERACTION_PANEL_MIN_HEIGHT = 6
INTERACTION_PANEL_MAX_HEIGHT = 10

# The composer grows with its wrapped document between these text bounds. The
# blank row above and below the text is the composer's frame, so the widget's
# outer height is always the visible text rows plus that frame.
COMPOSER_TEXT_MIN_HEIGHT = 1
COMPOSER_TEXT_MAX_HEIGHT = 3
COMPOSER_FRAME_HEIGHT = 2
COMPOSER_MIN_HEIGHT = COMPOSER_TEXT_MIN_HEIGHT + COMPOSER_FRAME_HEIGHT
COMPOSER_MAX_HEIGHT = COMPOSER_TEXT_MAX_HEIGHT + COMPOSER_FRAME_HEIGHT

# The regular surface always retains the Host header, one conversation row, the
# fixed input separator/navigation row, and the status line around the composer.
# Welcome mode instead splits the rows around its centered composer/status pair.
# Both exclude the composer, which contributes its own current height.
_REGULAR_SURFACE_CHROME_HEIGHT = 4
_WELCOME_CENTER_CHROME_HEIGHT = 1
_BUSY_ACTIVITIES = {
    "starting",
    "loading_session",
    "mcp_connecting",
    "queued",
    "preparing_model",
    "waiting_model",
    "streaming_reasoning",
    "streaming_text",
    "streaming_tool_call",
    "preparing_tools",
    "executing_tools",
    "committing_results",
    "finalizing_response",
    "compacting",
    "forking",
    "cancelling",
}
_ACTIVITY_LABELS = {
    "starting": "Starting services",
    "loading_session": "Loading session",
    "mcp_connecting": "Connecting MCP servers",
    "idle": "Ready",
    "queued": "Queued",
    "preparing_model": "Preparing context",
    "waiting_model": "Waiting for model",
    "streaming_reasoning": "Thinking",
    "streaming_text": "Responding",
    "streaming_tool_call": "Preparing tool call",
    "preparing_tools": "Checking tools",
    "waiting_approval": "Approval required",
    "waiting_user": "Input required",
    "waiting_child": "Waiting for subagent",
    "executing_tools": "Running tools",
    "committing_results": "Saving tool results",
    "finalizing_response": "Finalizing",
    "compacting": "Compacting context",
    "forking": "Forking session",
    "cancelling": "Cancelling",
    "completed": "Ready",
    "aborted": "Aborted",
    "failed": "Failed",
    "paused": "Paused",
    "recoverable": "Recovery required",
}
_TRANSCRIPT_BACKGROUND_ENTRY_THRESHOLD = 64
_TRANSCRIPT_BACKGROUND_TEXT_THRESHOLD = 8 * 1024
_PlaceholderTone = Literal["text", "separator", "key"]
_PlaceholderPart = tuple[str, _PlaceholderTone]
_SUBAGENT_ACTIVITY_LABELS = {
    **_ACTIVITY_LABELS,
    "starting": "Dispatching",
    "queued": "Dispatching",
    "completed": "Done",
    "aborted": "Failed",
    "recoverable": "Failed",
}
_ACTIVE_TOOL_STATUSES = frozenset(
    {
        "streaming",
        "planned",
        "ready",
        "resolving_input",
        "running",
        "cancelling",
        "waiting_child",
    }
)
_FAILED_TOOL_STATUSES = frozenset({"failed", "rejected", "aborted"})
_FAILED_SUBAGENT_STATUSES = frozenset({"failed", "aborted", "recoverable"})
_CONTEXT_PROGRESS_WIDTH = 11
_HEADER_MIN_GAP = 3
_TOOL_ARGUMENT_STREAM_WINDOW_CELLS = 48
_SINGLE_LINE_TOOL_ARGUMENT_TRANSLATION = str.maketrans("\r\n\t", "   ")


def _compact_names(names: tuple[str, ...], *, limit: int = 3) -> str:
    visible = names[:limit]
    summary = ", ".join(visible)
    remaining = len(names) - len(visible)
    if remaining:
        summary += f", +{remaining}"
    return summary


class BrandHeader(Static):
    """Static brand identity and session title docked at the top of the screen."""

    def __init__(
        self,
        *,
        initial_context: str = "New session",
        initial_workspace: str | None = None,
    ) -> None:
        super().__init__(id="brand-header")
        self._session_title = initial_context
        self._session_detail: str | None = None
        self._workspace = initial_workspace

    def on_mount(self) -> None:
        self._render_header()

    def on_resize(self, event: events.Resize) -> None:
        self._render_header(width=event.size.width)

    def set_context(
        self,
        *,
        session_title: str | None = None,
        session_detail: str | None = None,
        workspace: str | None = None,
    ) -> None:
        if session_title is not None:
            self._session_title = session_title
        self._session_detail = session_detail
        if workspace is not None:
            self._workspace = workspace
        self._render_header()

    def refresh_theme(self) -> None:
        self._render_header()

    def _render_header(self, *, width: int | None = None) -> None:
        theme = theme_for(self)
        available_width = self.size.width if width is None else width
        self.update(
            _brand_header_content(
                self._session_title,
                self._session_detail,
                self._workspace,
                width=available_width,
                theme=theme,
            )
        )


def _brand_header_content(
    session_title: str,
    session_detail: str | None,
    workspace: str | None,
    *,
    width: int,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Text:
    """Lay out the title and right-aligned workspace with workspace priority."""

    content = Text()
    content.append(f" {theme.brand_mark} OURO ", style=f"bold {theme.accent}")
    title = Text(session_title, style=theme.text_primary)
    if session_detail:
        title.append(theme.inline_separator, style=theme.separator_tone)
        title.append(session_detail, style=theme.text_lowlight)
    if width <= 0:
        content.append_text(title)
        if workspace:
            content.append(" " * _HEADER_MIN_GAP)
            content.append(workspace, style=theme.accent)
        return content

    content.truncate(width, overflow="ellipsis")
    remaining = width - content.cell_len
    if remaining <= 0:
        return content

    if not workspace:
        title.truncate(remaining, overflow="ellipsis")
        content.append_text(title)
        return content

    workspace_width = cell_len(workspace)
    title_width = remaining - workspace_width - _HEADER_MIN_GAP
    if title_width > 0:
        title.truncate(title_width, overflow="ellipsis")
        content.append_text(title)
        content.append(" " * (width - content.cell_len - workspace_width))
        content.append(workspace, style=theme.accent)
        return content

    # When both fields cannot fit, omit the title before shortening the workspace.
    workspace_gap = min(_HEADER_MIN_GAP, max(1, remaining - workspace_width))
    workspace_text = Text(workspace, style=theme.accent)
    workspace_text.truncate(max(0, remaining - workspace_gap), overflow="ellipsis")
    content.append(" " * workspace_gap)
    content.append_text(workspace_text)
    return content


class ActivityIndicator(Static):
    """Animated activity and model status shown beside the composer."""

    def __init__(self) -> None:
        super().__init__(id="activity-indicator")
        self._frame = 0
        self._activity = "idle"
        self._approval_mode: TuiApprovalPreset = "inherit"
        self._model = "starting"
        self._model_attempt: int | None = None
        self._model_max_attempts: int | None = None
        self._reasoning: str | None = None
        self._reasoning_peak = False
        self._subagent: AgentTreeNode | None = None
        self._session_control_labels: tuple[str, ...] = ()
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._sync_spinner_timer(force=True)
        self._render_indicator()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def set_activity(
        self,
        activity: str,
        *,
        model_attempt: int | None = None,
        model_max_attempts: int | None = None,
    ) -> None:
        if (
            activity == self._activity
            and model_attempt == self._model_attempt
            and model_max_attempts == self._model_max_attempts
        ):
            return
        self._activity = activity
        self._model_attempt = model_attempt
        self._model_max_attempts = model_max_attempts
        self._sync_spinner_timer()
        if activity not in _BUSY_ACTIVITIES:
            self._frame = 0
        self._render_indicator()

    def set_model(self, model: str) -> None:
        if model == self._model:
            return
        self._model = model
        self._render_indicator()

    def set_approval_mode(self, mode: TuiApprovalPreset) -> None:
        if mode == self._approval_mode:
            return
        self._approval_mode = mode
        self._render_indicator()

    def set_reasoning(self, value: str | None, *, peak: bool = False) -> None:
        """Show the effective thinking level beside the model.

        ``peak`` highlights the level when it is the highest effort the current
        endpoint supports, signalling that reasoning is maxed out.
        """
        if value == self._reasoning and peak == self._reasoning_peak:
            return
        self._reasoning = value
        self._reasoning_peak = peak
        self._render_indicator()

    def set_session_controls(self, labels: tuple[str, ...]) -> None:
        unique_labels = tuple(dict.fromkeys(labels))
        if unique_labels == self._session_control_labels:
            return
        self._session_control_labels = unique_labels
        self._render_indicator()

    def set_subagent(self, agent: AgentTreeNode | None) -> None:
        if agent == self._subagent:
            return
        self._subagent = agent
        self._render_indicator()

    def refresh_theme(self) -> None:
        self._frame = 0
        self._sync_spinner_timer(force=True)
        self._render_indicator()

    def _tick(self) -> None:
        theme = theme_for(self)
        self._frame = (self._frame + 1) % len(theme.spinner_frames)
        self._render_indicator()

    def _sync_spinner_timer(self, *, force: bool = False) -> None:
        if not self.is_attached:
            return
        if force and self._timer is not None:
            self._timer.stop()
            self._timer = None
        theme = theme_for(self)
        animated = len(theme.spinner_frames) > 1
        busy = self._activity in _BUSY_ACTIVITIES
        if animated and self._timer is None:
            self._timer = self.set_interval(
                theme.spinner_interval_seconds,
                self._tick,
                pause=not busy,
            )
        elif self._timer is not None:
            if busy:
                self._timer.resume()
            else:
                self._timer.pause()

    def _render_indicator(self) -> None:
        theme = theme_for(self)
        accent = theme.accent
        busy = self._activity in _BUSY_ACTIVITIES
        ready = self._activity in {"idle", "completed"}
        mark = theme.spinner_frames[self._frame] if busy else theme.brand_mark
        label = _ACTIVITY_LABELS.get(self._activity, self._activity.replace("_", " ").title())
        if self._subagent is not None and ready:
            label = _subagent_task_status(self._subagent).title()
        if (
            self._activity == "waiting_model"
            and self._model_attempt is not None
            and self._model_max_attempts is not None
            and 2 <= self._model_attempt <= self._model_max_attempts
        ):
            label = f"{label} {self._model_attempt}/{self._model_max_attempts}"
        content = Text()
        content.append(
            f" {mark} ",
            style=f"bold {accent}" if busy or ready else theme.text_muted,
        )
        for control_label in self._session_control_labels:
            content.append(control_label, style=f"bold {accent}")
            content.append(
                theme.section_separator,
                style=theme.separator_tone,
            )
        content.append(label, style=accent if ready else theme.text_muted)
        content.append(
            theme.section_separator,
            style=theme.separator_tone,
        )
        approval_style = (
            theme.danger if self._approval_mode in {"auto", "afk"} else theme.text_faint
        )
        content.append(self._approval_mode, style=approval_style)
        content.append(
            theme.section_separator,
            style=theme.separator_tone,
        )
        content.append(self._model, style=theme.text_faint)
        if self._reasoning:
            style = f"bold {accent}" if self._reasoning_peak else theme.text_faint
            content.append(
                theme.section_separator,
                style=theme.separator_tone,
            )
            content.append(self._reasoning, style=style)
        # This row is constrained to the single-line remainder of StatusLine;
        # spinner and status changes can only alter its paint, not its geometry.
        self.update(content, layout=False)


class ReasoningLabel(RangeSelectionOnly, Label):
    class ToggleRequested(Message):
        pass

    def __init__(self, content: Text, *, classes: str) -> None:
        self._content = content
        self._hovered = False
        super().__init__(content, classes=classes)

    def set_content(self, content: Text) -> None:
        layout = content.cell_len != self._content.cell_len
        self._content = content
        self._update_displayed_content(layout=layout)

    def on_enter(self, _event: events.Enter) -> None:
        self._hovered = True
        self._update_displayed_content()

    def on_leave(self, _event: events.Leave) -> None:
        self._hovered = False
        self._update_displayed_content()

    def _update_displayed_content(self, *, layout: bool = False) -> None:
        content = self._content.copy()
        if self._hovered:
            content.stylize(theme_for(self).text_highlight)
        self.update(content, layout=layout)

    def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            self.post_message(self.ToggleRequested())
            event.stop()


class ReasoningView(RangeSelectionOnly, Vertical):
    """A plain-text reasoning stream with presentation state independent from the answer.

    Thinking output is shown verbatim rather than parsed as Markdown, so code fences and
    markup inside reasoning are never reformatted into blocks or syntax highlighting.
    Completed lines render once into the strip buffer; only the trailing partial line
    re-renders while thinking continues.
    """

    def __init__(self, *, expanded: bool = True) -> None:
        super().__init__(classes="reasoning-view")
        self.display = False
        self.label = ReasoningLabel(
            _completed_reasoning_label(None, expanded=expanded),
            classes="reasoning-label",
        )
        self._timer: Timer | None = None
        self._elapsed_at_start = 0.0
        self._clock_started_at: float | None = None
        self._completed_seconds: float | None = None
        self._content = ReasoningLines()
        self._content.set_class(not expanded, "collapsed")

    def on_mount(self) -> None:
        self._render_label()

    def refresh_theme(self) -> None:
        self._render_label()

    def compose(self) -> ComposeResult:
        yield self.label
        yield self._content

    def on_unmount(self) -> None:
        self._stop_timer()

    @property
    def text(self) -> str:
        return self._content.text

    @property
    def duration_seconds(self) -> float | None:
        return self._completed_seconds

    @property
    def expanded(self) -> bool:
        return not self._content.has_class("collapsed")

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self.expanded:
            return
        self._content.set_class(not expanded, "collapsed")
        self._render_label()

    async def append(self, text: str, *, started_at: datetime | None = None) -> None:
        if not text:
            return
        self.display = True
        started = self._clock_started_at is None
        if self._clock_started_at is None:
            now = datetime.now(UTC)
            self._elapsed_at_start = (
                max(0.0, (now - started_at).total_seconds()) if started_at is not None else 0.0
            )
            self._clock_started_at = monotonic()
            self._timer = self.set_interval(0.1, self._tick)
        if started:
            self._render_live_label()
        self._content.append(text)

    def finish(self, duration_seconds: float | None = None) -> None:
        if self.display:
            if duration_seconds is not None:
                elapsed = duration_seconds
            elif self._clock_started_at is None and self._completed_seconds is not None:
                elapsed = self._completed_seconds
            else:
                elapsed = self._elapsed_seconds()
            self._completed_seconds = elapsed
            self.label.set_content(
                _completed_reasoning_label(
                    elapsed,
                    expanded=self.expanded,
                    theme=theme_for(self),
                )
            )
        self._stop_timer()

    def _tick(self) -> None:
        self._render_live_label()

    def _render_live_label(self) -> None:
        self.label.set_content(
            _reasoning_label_text(
                "Thinking",
                self._elapsed_seconds(),
                expanded=self.expanded,
                theme=theme_for(self),
            )
        )

    def _render_label(self) -> None:
        if self._clock_started_at is not None:
            self._render_live_label()
            return
        self.label.set_content(
            _completed_reasoning_label(
                self._completed_seconds,
                expanded=self.expanded,
                theme=theme_for(self),
            )
        )

    def _elapsed_seconds(self) -> float:
        if self._clock_started_at is None:
            return self._elapsed_at_start
        return self._elapsed_at_start + monotonic() - self._clock_started_at

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._clock_started_at = None


class AssistantTurn(RangeSelectionOnly, Vertical):
    """Live region for one model round: reasoning plus its mutable answer tail.

    Completed top-level blocks freeze into the history buffer as soon as they
    terminate. The mutable tail is shown verbatim through an append-only line
    buffer; this avoids repeatedly parsing and rendering a block whose final
    Markdown structure is not known yet.
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        after_tool: bool = False,
        reasoning_expanded: bool = True,
    ) -> None:
        classes = "turn assistant-turn after-tool" if after_tool else "turn assistant-turn"
        super().__init__(classes=classes)
        self.run_id = run_id
        self.reasoning = ReasoningView(expanded=reasoning_expanded)
        self.tail = AssistantTailLines()
        self.tail.display = False
        self._stream = AssistantMarkdownStream()
        self._reasoning_pending = True
        self._tail_reset_pending = False

    def compose(self) -> ComposeResult:
        yield self.reasoning
        yield self.tail

    @property
    def source(self) -> str:
        return self._stream.source

    @property
    def has_text(self) -> bool:
        return self._stream.has_source

    @property
    def reasoning_pending(self) -> bool:
        return self._reasoning_pending

    async def feed(self, text: str) -> tuple[str, ...]:
        """Accumulate streamed markdown; return blocks ready to freeze."""
        if not text:
            return ()
        self.reasoning.finish()
        if self._stream.should_parse_in_background(text):
            blocks, tail_reset = await asyncio.to_thread(self._stream.append, text)
        else:
            blocks, tail_reset = self._stream.append(text)
        if tail_reset:
            # Keep the completed source visible while its immutable strips are
            # rendered off-loop. The caller commits the shorter tail immediately
            # after those strips enter history, avoiding a transient blank region.
            self._tail_reset_pending = True
            self.tail.display = True
            self.tail.append(text)
        else:
            self.tail.display = True
            self.tail.append(text)
        return blocks

    def commit_frozen_blocks(self) -> None:
        if not self._tail_reset_pending:
            return
        self._tail_reset_pending = False
        tail = self._stream.tail
        self.tail.display = bool(tail)
        self.tail.replace(tail)

    async def close(self) -> tuple[str, ...]:
        """Freeze the trailing block along with the round."""
        if self._stream.should_close_in_background:
            blocks = await asyncio.to_thread(self._stream.close)
        else:
            blocks = self._stream.close()

        return blocks

    def clear_tail(self) -> None:
        self._tail_reset_pending = False
        self.tail.replace("")
        self.tail.display = False

    def take_reasoning(self) -> tuple[str, float | None]:
        """Detach the live reasoning block for immutable rendering."""
        self._reasoning_pending = False
        self.reasoning.display = False
        return self.reasoning.text, self.reasoning.duration_seconds

    async def append_reasoning(self, text: str, *, started_at: datetime | None = None) -> None:
        await self.reasoning.append(text, started_at=started_at)

    def finish_reasoning(self, duration_seconds: float | None = None) -> None:
        self.reasoning.finish(duration_seconds)

    def set_reasoning_expanded(self, expanded: bool) -> None:
        self.reasoning.set_expanded(expanded)

    @property
    def reasoning_expanded(self) -> bool:
        return self.reasoning.expanded


def _completed_reasoning_label(
    duration_seconds: float | None,
    *,
    expanded: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Text:
    return _reasoning_label_text(
        "Thought",
        duration_seconds,
        expanded=expanded,
        theme=theme,
    )


class ToolActivityRow(RangeSelectionOnly, Static):
    def __init__(
        self,
        tool: RuntimeToolCallActivity,
        *,
        run_id: str | None,
        agent: AgentTreeNode | None = None,
    ) -> None:
        clickable = _tool_is_clickable(tool, run_id=run_id)
        super().__init__(classes="tool-row clickable" if clickable else "tool-row")
        self._clickable = clickable
        self._hovered = False
        self.tool_call_id = tool.tool_call_id
        self.run_id = run_id
        self._tool = tool
        self._agent = agent
        self._frame = 0
        self._timer: Timer | None = None
        self._render_tool()

    def on_mount(self) -> None:
        self._sync_timer()
        self._render_tool(layout=False)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def apply_tool(self, tool: RuntimeToolCallActivity) -> None:
        if tool == self._tool:
            return
        self._tool = tool
        self.tool_call_id = tool.tool_call_id
        clickable = _tool_is_clickable(tool, run_id=self.run_id)
        if clickable != self._clickable:
            self._clickable = clickable
            self.set_class(clickable, "clickable")
        self._sync_timer()
        self._render_tool()

    def apply_agent(self, agent: AgentTreeNode) -> None:
        if agent == self._agent:
            return
        self._agent = agent
        self._render_tool()

    @property
    def tool(self) -> RuntimeToolCallActivity:
        return self._tool

    @property
    def agent(self) -> AgentTreeNode | None:
        return self._agent

    def _sync_timer(self, *, force: bool = False) -> None:
        if not self.is_attached:
            return
        if force and self._timer is not None:
            self._timer.stop()
            self._timer = None
        theme = theme_for(self)
        should_animate = self._is_active and len(theme.spinner_frames) > 1
        if should_animate and self._timer is None:
            self._timer = self.set_interval(theme.spinner_interval_seconds, self._tick)
        elif not should_animate and self._timer is not None:
            self._timer.stop()
            self._timer = None

    @property
    def _is_active(self) -> bool:
        return (
            self._tool.argument_stream_tail is not None
            or self._tool.status in _ACTIVE_TOOL_STATUSES
        )

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(theme_for(self).spinner_frames)
        self._render_tool(layout=False)

    def refresh_theme(self) -> None:
        self._frame = 0
        self._sync_timer(force=True)
        self._render_tool(layout=False)

    def on_enter(self, _event: events.Enter) -> None:
        if self._clickable:
            self._hovered = True
            self._render_tool(layout=False)

    def on_leave(self, _event: events.Leave) -> None:
        if self._hovered:
            self._hovered = False
            self._render_tool(layout=False)

    def on_click(self, event: events.Click) -> None:
        if event.button == 1 and self.run_id is not None and self._tool.tool_name == "SpawnAgent":
            self.post_message(
                ToolNavigationRequested(
                    parent_run_id=self.run_id,
                    parent_tool_call_id=self.tool_call_id,
                )
            )
            event.stop()

    def _render_tool(self, *, layout: bool = True) -> None:
        self.update(
            _tool_activity_text(
                self._tool,
                frame=self._frame,
                theme=theme_for(self),
                agent=self._agent,
                highlighted=self._hovered,
            ),
            layout=layout,
        )


def _tool_activity_text(
    tool: RuntimeToolCallActivity,
    *,
    frame: int,
    theme: TuiTheme,
    agent: AgentTreeNode | None = None,
    highlighted: bool = False,
) -> Text:
    """Render one tool state without coupling it to a mounted widget."""

    if tool.status == "streaming" or tool.argument_stream_tail is not None:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(
            f"{theme.spinner_frames[frame % len(theme.spinner_frames)]} ",
            style=theme.accent,
        )
        text.append(tool.tool_name, style=theme.text_faint)
        if tool.argument_stream_tail is not None:
            text.append(
                theme.inline_separator,
                style=theme.separator_tone,
            )
            text.append(
                _tool_argument_stream_window(tool.argument_stream_tail),
                style=theme.text_faint,
            )
        return text

    presentation = present_tool(tool, theme=theme)
    is_spawn_agent = tool.tool_name.casefold() == "spawnagent"
    subagent = agent if agent is not None and is_spawn_agent else None
    title = presentation.title or tool.tool_name
    if subagent is not None:
        title = _subagent_identity(subagent, theme=theme)
    failed = tool.status in _FAILED_TOOL_STATUSES or (
        subagent is not None and subagent.status in _FAILED_SUBAGENT_STATUSES
    )
    text = Text(no_wrap=True, overflow="ellipsis")
    if tool.status in _ACTIVE_TOOL_STATUSES:
        text.append(
            f"{theme.spinner_frames[frame % len(theme.spinner_frames)]} ",
            style=theme.accent,
        )
    text.append(
        title,
        style=theme.failure if failed else theme.text_primary,
    )
    if presentation.summary:
        text.append(
            f" {theme.inline_separator}{presentation.summary}",
            style=theme.text_detail,
        )
    if subagent is not None:
        status_label = _subagent_task_status(subagent)
        text.append(
            f"  {status_label}",
            style=(theme.failure if status_label == "failed" else theme.text_metadata),
        )
    elif is_spawn_agent:
        status_label = _spawn_agent_preview_status(tool)
        text.append(
            f"  {status_label}",
            style=(theme.failure if status_label == "failed" else theme.text_metadata),
        )
    elapsed = _tool_elapsed_seconds(tool)
    elapsed_label = format_duration(elapsed) if elapsed is not None else None
    if elapsed_label is not None and elapsed_label != "0ms":
        text.append(
            f"{theme.inline_separator}{elapsed_label}",
            style=theme.text_faint,
        )
    line_styles: dict[ToolLineState, str] = {
        "pending": theme.text_detail,
        "active": theme.accent,
        "done": theme.success,
        "muted": theme.text_faint,
    }
    part_styles: dict[ToolTextTone, str] = {
        "default": theme.text_faint,
        "muted": theme.text_faint,
        "added": theme.success,
        "removed": theme.failure,
    }
    for line in presentation.lines:
        text.append(f"\n  {line.marker} ", style=line_styles[line.state])
        for part in line.parts:
            style = part_styles[part.tone]
            text.append(part.text, style=style)
    if subagent is not None:
        _append_subagent_activity(text, subagent, theme=theme)
    elif is_spawn_agent:
        _append_subagent_preview(text, tool, theme=theme)
    if highlighted:
        text.stylize(theme.text_highlight)
    return text


def _tool_argument_stream_window(value: str) -> str:
    """Return the rightmost argument cells in a fixed-width sliding window."""

    value = value.translate(_SINGLE_LINE_TOOL_ARGUMENT_TRANSLATION)
    used = 0
    start = len(value)
    for index in range(len(value) - 1, -1, -1):
        width = get_character_cell_size(value[index])
        if used + width > _TOOL_ARGUMENT_STREAM_WINDOW_CELLS:
            break
        used += width
        start = index
    return f"{value[start:]}{' ' * (_TOOL_ARGUMENT_STREAM_WINDOW_CELLS - used)}"


def _tool_is_clickable(tool: RuntimeToolCallActivity, *, run_id: str | None) -> bool:
    return run_id is not None and tool.tool_name == "SpawnAgent" and tool.status != "streaming"


def _subagent_identity(agent: AgentTreeNode, *, theme: TuiTheme) -> str:
    nickname = agent.nickname.strip() if agent.nickname is not None else ""
    if nickname and nickname != agent.agent_name:
        return theme.inline_separator.join((agent.agent_name, nickname))
    return agent.agent_name


def _subagent_task_status(agent: AgentTreeNode) -> str:
    if agent.status == "completed":
        return "done"
    if agent.status in _FAILED_SUBAGENT_STATUSES:
        return "failed"
    if agent.status == "paused":
        return "paused"
    return _subagent_execution_status(agent.execution_mode)


def _subagent_execution_status(execution_mode: object) -> str:
    return "background" if execution_mode == "async" else "working"


def _spawn_agent_preview_status(tool: RuntimeToolCallActivity) -> str:
    """Project the stable header shape before the durable Agent node exists."""

    if tool.status in _FAILED_TOOL_STATUSES:
        return "failed"
    if tool.status == "completed":
        if tool.arguments.get("execution_mode") != "async":
            return "done"
    return _subagent_execution_status(tool.arguments.get("execution_mode", "sync"))


def _append_subagent_preview(
    text: Text,
    tool: RuntimeToolCallActivity,
    *,
    theme: TuiTheme,
) -> None:
    description = tool.arguments.get("description")
    if not isinstance(description, str) or not description.strip():
        return
    if tool.status in _FAILED_TOOL_STATUSES:
        label = "Failed"
    elif tool.status == "completed" and tool.arguments.get("execution_mode") != "async":
        label = "Done"
    else:
        label = "Dispatching"
    _append_subagent_detail(text, label, description.strip(), theme=theme)


def _append_subagent_activity(
    text: Text,
    agent: AgentTreeNode,
    *,
    theme: TuiTheme,
) -> None:
    activity = agent.activity
    activity_name = activity.activity if activity is not None else agent.status
    label = _SUBAGENT_ACTIVITY_LABELS.get(
        activity_name,
        activity_name.replace("_", " ").title(),
    )
    _append_subagent_detail(text, label, agent.task_description, theme=theme)


def _append_subagent_detail(
    text: Text,
    label: str,
    description: str,
    *,
    theme: TuiTheme,
) -> None:
    text.append(
        f"\n  └ {label}{theme.inline_separator}{description}",
        style=theme.text_lowlight,
    )


def _is_terminal_tool(tool: RuntimeToolCallActivity) -> bool:
    return tool.status in {
        "completed",
        "failed",
        "rejected",
        "aborted",
        "unknown_outcome",
    }


def _tool_elapsed_seconds(tool: RuntimeToolCallActivity) -> float | None:
    if tool.started_at is None:
        return None
    ended_at = tool.ended_at or datetime.now(UTC)
    elapsed = max(0.0, (ended_at - tool.started_at).total_seconds())
    return elapsed if elapsed > 0 else None


_INPUT_TIPS = (
    ("/", "Commands"),
    ("Ctrl+P", "Actions"),
    ("Ctrl+H", "Help"),
)
_USAGE_DAY_META = "ouro.welcome_usage.date"


def _blend_hex(background: str, foreground: str, ratio: float) -> str:
    """Blend two Rich-compatible hex colors without depending on terminal palettes."""

    amount = min(1.0, max(0.0, ratio))
    background_rgb = tuple(int(background[index : index + 2], 16) for index in (1, 3, 5))
    foreground_rgb = tuple(int(foreground[index : index + 2], 16) for index in (1, 3, 5))
    blended = tuple(
        round(start + (end - start) * amount)
        for start, end in zip(background_rgb, foreground_rgb, strict=True)
    )
    return "#" + "".join(f"{channel:02x}" for channel in blended)


def _usage_block_colors(values: tuple[int, ...], theme: TuiTheme) -> tuple[str, ...]:
    """Map daily token totals onto a theme-derived dark-to-accent scale."""

    darkest_ratio = 0.28
    if not values:
        return (_blend_hex(theme.background, theme.accent, darkest_ratio),)
    lowest = min(values)
    highest = max(values)
    if lowest == highest:
        return tuple(theme.accent for _ in values)
    return tuple(
        _blend_hex(
            theme.background,
            theme.accent,
            darkest_ratio + (1.0 - darkest_ratio) * ((value - lowest) / (highest - lowest)),
        )
        for value in values
    )


class _WelcomeSystemBlock(Vertical):
    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome-section welcome-system")
        self._status = status or TuiSystemStatus()
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None
        self._workspace = Static(classes="welcome-value")
        self._providers = Static(classes="welcome-value")
        self._skills = Static(classes="welcome-value")
        self._plugins = Static(classes="welcome-value")
        self._tasks = Static(id="welcome-task-counts", classes="welcome-value")
        self._mcp_label = Static("MCP", id="welcome-mcp-label", classes="welcome-label")
        self._mcp = Static(classes="welcome-value")
        self._render_values()

    def compose(self) -> ComposeResult:
        yield Static("SYSTEM", classes="welcome-title welcome-label")
        for label, value in (
            ("Workspace", self._workspace),
            ("Providers", self._providers),
            ("Skills", self._skills),
            ("Plugins", self._plugins),
            ("Task", self._tasks),
            ("MCP", self._mcp),
        ):
            yield Horizontal(
                self._mcp_label if label == "MCP" else Static(label, classes="welcome-label"),
                value,
                classes="welcome-row",
            )

    def on_mount(self) -> None:
        self._sync_mcp_spinner()
        self._render_values()

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._sync_mcp_spinner()
        self._render_values()

    def _sync_mcp_spinner(self, *, force: bool = False) -> None:
        if force and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        theme = theme_for(self)
        if self._status.mcp_connecting_count and len(theme.spinner_frames) > 1:
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(
                    theme.spinner_interval_seconds,
                    self._advance_mcp_spinner,
                )
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._spinner_frame = 0

    def _advance_mcp_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(theme_for(self).spinner_frames)
        self._render_mcp_label()

    def _render_mcp_label(self) -> None:
        theme = theme_for(self)
        label = Text()
        if self._status.mcp_connecting_count:
            label.append(
                f"{theme.spinner_frames[self._spinner_frame]} ",
                style=f"bold {theme.accent}",
            )
        label.append("MCP")
        self._mcp_label.update(label)

    def _render_values(self) -> None:
        status = self._status
        theme = theme_for(self)
        counts = status.task_counts
        tasks = Text(
            f"{counts.completed_executions}/"
            f"{counts.completed_executions + counts.failed_executions} completed",
            style=theme.text_muted,
        )
        for label, count, color in (
            ("waiting", counts.waiting, theme.text_lowlight),
            ("running", counts.running, theme.accent),
            ("approval", counts.awaiting_approval, theme.warning),
        ):
            if not count:
                continue
            tasks.append(theme.inline_separator, style=theme.text_faint)
            tasks.append(f"{count} {label}", style=color)
        self._tasks.update(tasks)
        self._render_mcp_label()
        if status.task_error:
            self._tasks.update(
                Text(
                    theme.inline_separator.join(("Unavailable", status.task_error)),
                    style=theme.text_muted,
                )
            )
        self._workspace.update(Text(status.workspace or "not set", style=theme.text_primary))

        providers = Text()
        providers.append(f"{status.model_count} model(s)", style=theme.text_muted)
        if status.provider_ids:
            providers.append(
                f"{theme.section_separator}{_compact_names(status.provider_ids)}",
                style=theme.text_faint,
            )
        self._providers.update(providers)

        skills = Text(f"{status.skill_count} available", style=theme.text_muted)
        if status.skill_names:
            skills.append(
                f"{theme.section_separator}{_compact_names(status.skill_names)}",
                style=theme.text_faint,
            )
        self._skills.update(skills)

        plugins = Text(
            f"{status.plugin_active_count}/{status.plugin_total_count} active",
            style=theme.text_muted,
        )
        if status.plugin_active_names:
            plugins.append(
                f"{theme.section_separator}{_compact_names(status.plugin_active_names)}",
                style=theme.text_faint,
            )
        if status.plugin_issue_count:
            plugins.append(
                f"{theme.section_separator}{status.plugin_issue_count} need attention",
                style=theme.critical,
            )
        self._plugins.update(plugins)

        mcp = Text()
        if status.mcp_total_count:
            mcp.append(
                f"{status.mcp_connected_count}/{status.mcp_total_count} connected",
                style=theme.accent,
            )
            mcp.append(
                f"{theme.section_separator}{status.mcp_tool_count} tool(s)",
                style=theme.text_muted,
            )
            if status.mcp_connecting_count:
                mcp.append(
                    f"{theme.section_separator}{status.mcp_connecting_count} connecting",
                    style=theme.accent,
                )
            if status.mcp_failed_count:
                mcp.append(
                    f"{theme.section_separator}{status.mcp_failed_count} failed",
                    style=theme.critical,
                )
        else:
            mcp.append("not configured", style=theme.text_faint)
        self._mcp.update(mcp)

    def refresh_theme(self) -> None:
        self._spinner_frame = 0
        self._sync_mcp_spinner(force=True)
        self._render_values()


class _WelcomeUsageBlock(Vertical):
    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome-section welcome-usage")
        self._status = status or TuiSystemStatus()
        self._chart = Static(classes="welcome-usage-line")
        self._requests = Static(classes="welcome-usage-line")
        self._tokens = Static(classes="welcome-usage-line")
        self._hovered_date: str | None = None
        self._render_values()

    def compose(self) -> ComposeResult:
        yield Static("USAGE", classes="welcome-title")
        yield self._chart
        yield self._requests
        yield self._tokens

    def on_mount(self) -> None:
        self._render_values()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        if self._hovered_date not in {day.date for day in status.usage_daily}:
            self._hovered_date = None
        self._render_values()

    def refresh_theme(self) -> None:
        self._render_values()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        date = None
        if event.style is not None:
            candidate = event.style.meta.get(_USAGE_DAY_META)
            if isinstance(candidate, str):
                date = candidate
        self._set_hovered_date(date)

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hovered_date(None)

    def _set_hovered_date(self, date: str | None) -> None:
        if date == self._hovered_date:
            return
        self._hovered_date = date
        self._render_values()

    def _render_values(self) -> None:
        status = self._status
        theme = theme_for(self)
        daily = status.usage_daily
        daily_tokens = tuple(day.total_tokens for day in daily)
        selected = next((day for day in daily if day.date == self._hovered_date), None)
        chart = Text()
        colors = _usage_block_colors(daily_tokens, theme) if daily else ()
        for day, color in zip(daily, colors, strict=True):
            mark = (
                theme.selected_session_node_mark
                if day.date == self._hovered_date
                else theme.session_node_mark
            )
            chart.append(mark, style=RichStyle(color=color, meta={_USAGE_DAY_META: day.date}))
        if daily:
            chart.append(theme.section_separator, style=theme.separator_tone)
            if selected is not None:
                chart.append(selected.date, style=theme.text_faint)
            else:
                days = len(daily)
                chart.append(
                    f"last {days} day{'s' if days != 1 else ''}",
                    style=theme.text_faint,
                )
        else:
            chart.append(
                theme.session_node_mark,
                style=_usage_block_colors((), theme)[0],
            )
            chart.append(theme.section_separator, style=theme.separator_tone)
            chart.append(
                "no records",
                style=theme.text_faint,
            )
        self._chart.update(chart)

        request_count = (
            selected.request_count if selected is not None else status.usage_request_count
        )
        estimated_cost = (
            selected.estimated_cost if selected is not None else status.usage_estimated_cost
        )
        priced_request_count = (
            selected.priced_request_count
            if selected is not None
            else status.usage_priced_request_count
        )
        requests = Text(
            f"{request_count} request(s)",
            style=theme.text_muted,
        )
        if priced_request_count:
            requests.append(theme.section_separator, style=theme.separator_tone)
            requests.append(f"${estimated_cost:.4f}", style=theme.text_faint)
        self._requests.update(requests)
        input_tokens = selected.input_tokens if selected is not None else status.usage_input_tokens
        output_tokens = (
            selected.output_tokens if selected is not None else status.usage_output_tokens
        )
        cache_read_tokens = (
            selected.cache_read_tokens if selected is not None else status.usage_cache_read_tokens
        )
        tokens = Text(f"{format_compact_count(input_tokens)} in", style=theme.text_faint)
        for value in (
            f"{format_compact_count(output_tokens)} out",
            f"{_cache_hit_rate(cache_read_tokens, input_tokens):.2f}% cache",
        ):
            tokens.append(theme.inline_separator, style=theme.separator_tone)
            tokens.append(value, style=theme.text_faint)
        self._tokens.update(tokens)


def _cache_hit_rate(cache_read_tokens: int, input_tokens: int) -> float:
    return cache_read_tokens / input_tokens * 100 if input_tokens else 0.0


class _WelcomeTipsBlock(Vertical):
    def __init__(self) -> None:
        super().__init__(classes="welcome-section welcome-tips")
        self._title = Static(classes="welcome-title")

    def compose(self) -> ComposeResult:
        yield self._title
        for key, description in _INPUT_TIPS:
            yield Horizontal(
                Static(key, classes="welcome-tip-key"),
                Static(description, classes="welcome-tip-description"),
                classes="welcome-row",
            )

    def on_mount(self) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self._title.update(f"{theme_for(self).section_separator_mark} TIPS")


class WelcomePanel(Vertical):
    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(id="input-region")
        self._status = status or TuiSystemStatus()
        self._logo = Static(classes="welcome-logo")
        self._system = _WelcomeSystemBlock(self._status)
        self._usage = _WelcomeUsageBlock(self._status)
        self._tips = _WelcomeTipsBlock()
        self._lower = Horizontal(
            self._usage,
            Static(classes="welcome-spacer"),
            self._tips,
            classes="welcome-lower",
        )
        self._top = Vertical(
            self._logo,
            classes="welcome-only welcome-top",
        )
        self._bottom = Vertical(
            Static(classes="welcome-gap welcome-section-gap"),
            self._lower,
            Static(classes="welcome-gap welcome-system-gap"),
            self._system,
            classes="welcome-only welcome-bottom",
        )
        self._subagent_navigation = SubagentNavigationPanel()
        self._command_popup = CommandPopup()
        self._interaction_bar = InteractionBar()
        self._composer = Composer()
        self._composer.display = False
        self._viewport_size: Size | None = None
        self._render_logo()

    def compose(self) -> ComposeResult:
        yield self._top
        yield self._subagent_navigation
        yield self._command_popup
        yield AskUserPanel()
        yield PlanConfirmationPanel()
        yield self._interaction_bar
        yield self._composer
        yield QueuedInputPanel()
        yield StatusLine()
        yield self._bottom

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._system.set_system_status(status)
        self._usage.set_system_status(status)

    def on_mount(self) -> None:
        self.set_viewport_size(self.app.screen.size)
        # The composer owns its own auto height; its wrapped document height is
        # the single source both Textual's box model and these sibling bounds
        # resolve from, so growth and re-bounding stay in one layout pass.
        self.watch(
            self._composer,
            "virtual_size",
            self._apply_viewport_constraints,
            init=False,
        )
        self.refresh_theme()

    def set_welcome_mode(self, enabled: bool) -> None:
        self.set_class(enabled, "welcome-page")
        self._subagent_navigation.display = not enabled
        self._apply_viewport_constraints()

    def set_viewport_size(self, size: Size) -> None:
        """Apply bounded constraints while Textual continues to own layout."""

        if size == self._viewport_size:
            return
        self._viewport_size = size
        self._apply_viewport_constraints()

    def _apply_viewport_constraints(self) -> None:
        size = self._viewport_size
        if size is None:
            return
        welcome_mode = self.has_class("welcome-page")
        self.styles.max_width = min(WELCOME_MAX_WIDTH, size.width) if welcome_mode else "100%"
        composer_height = self._composer.projected_height
        available_height = (
            max(1, (size.height - _WELCOME_CENTER_CHROME_HEIGHT - composer_height) // 2)
            if welcome_mode
            else max(1, size.height - _REGULAR_SURFACE_CHROME_HEIGHT - composer_height)
        )
        self._command_popup.styles.max_height = min(
            COMMAND_POPUP_MAX_HEIGHT,
            available_height,
        )
        self._command_popup.styles.min_height = min(
            COMMAND_POPUP_MIN_HEIGHT,
            available_height,
        )
        interaction_height = max(
            1,
            size.height - _REGULAR_SURFACE_CHROME_HEIGHT - composer_height,
        )
        self._interaction_bar.styles.max_height = min(
            INTERACTION_PANEL_MAX_HEIGHT,
            interaction_height,
        )
        self._interaction_bar.styles.min_height = min(
            INTERACTION_PANEL_MIN_HEIGHT,
            interaction_height,
        )

    def refresh_theme(self) -> None:
        self._render_logo()

    def _render_logo(self) -> None:
        self._logo.update(Text(_OURO_LOGO.strip("\n"), style=f"bold {theme_for(self).accent}"))


def _tool_activities_from_messages(
    messages: Iterable[MessageRecord],
) -> dict[str, RuntimeToolCallActivity]:
    """Pair assistant tool-call requests with their tool_result outcomes.

    A persisted tool call spans two messages: the assistant message carrying
    ``tool_calls`` and a following ``tool_result`` message carrying the
    outcome reference. This rebuilds the same ``RuntimeToolCallActivity``
    shape the live event stream produces, so ``ToolActivityRow`` renders
    history and live runs through one code path.

    ``MessageRecord`` does not store tool ``result_data``, so summaries that
    depend on results (read line counts, todo state) degrade to
    argument-only display.
    """
    activities: dict[str, RuntimeToolCallActivity] = {}
    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                activities[call.id] = RuntimeToolCallActivity(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    status="unknown_outcome",
                    source_index=call.source_index,
                    arguments=call.arguments,
                    requested_at=message.created_at,
                )
        elif message.role == "tool_result" and message.tool_result is not None:
            ref = message.tool_result
            activity = activities.get(ref.tool_call_id)
            if activity is not None:
                activities[ref.tool_call_id] = activity.model_copy(
                    update={
                        "status": "failed" if ref.is_error else "completed",
                        "started_at": (
                            message.tool_execution.started_at
                            if message.tool_execution is not None
                            else None
                        ),
                        "ended_at": (
                            message.tool_execution.ended_at
                            if message.tool_execution is not None
                            else None
                        ),
                    }
                )
    return activities


def _compaction_presentation(
    *,
    covered_message_count: int,
    tokens_before: int | None,
    tokens_after: int | None,
    duration_seconds: float | None,
) -> CompactionPresentation:
    tokens_reduced: int | None = None
    if (
        tokens_before is not None
        and tokens_after is not None
        and (tokens_before > 0 or tokens_after > 0)
    ):
        tokens_reduced = max(0, tokens_before - tokens_after)
    return CompactionPresentation(
        covered_message_count=covered_message_count,
        tokens_reduced=tokens_reduced,
        duration_seconds=duration_seconds,
    )


def _presents_message(
    message: MessageRecord,
    *,
    visible_scaffold_delegation_id: str | None,
) -> bool:
    if not message.scaffold:
        return True
    return (
        visible_scaffold_delegation_id is not None
        and message.role == "user"
        and message.metadata.get("session_input_source") == "delegation"
        and message.metadata.get("delegation_id") == visible_scaffold_delegation_id
    )


def _project_transcript_entries(
    messages: tuple[MessageRecord, ...],
    compactions: tuple[CompactionRecord, ...],
    runs: tuple[RunPresentation, ...],
    *,
    mutable_tool_call_keys: frozenset[tuple[str | None, str]],
    visible_scaffold_delegation_id: str | None,
) -> tuple[tuple[TranscriptEntry, ...], frozenset[str]]:
    """Build immutable history away from Textual's event loop.

    Entries are bucketed directly at their message boundary. This preserves the
    canonical message → Run summary → compaction order without constructing and
    sorting an intermediate list of ordering tuples.
    """

    materialized = tuple(
        message
        for message in messages
        if _presents_message(
            message,
            visible_scaffold_delegation_id=visible_scaffold_delegation_id,
        )
    )
    requests = {request.message_id: request for run in runs for request in run.requests}
    tool_activities = _tool_activities_from_messages(materialized)
    message_entries: list[list[TranscriptEntry]] = [[] for _ in materialized]
    message_positions: dict[str, int] = {}
    last_message_by_run: dict[str, int] = {}

    for message_index, message in enumerate(materialized):
        message_positions[message.message_id] = message_index
        if message.run_id is not None:
            last_message_by_run[message.run_id] = message_index
        entries = message_entries[message_index]
        if message.role == "user":
            text = user_message_text(message)
            if text:
                entries.append(
                    TranscriptEntry(
                        kind="user",
                        text=text,
                        created_at=message.created_at,
                    )
                )
            continue
        if message.role != "assistant":
            continue

        request = requests.get(message.message_id)
        if message.reasoning_content:
            entries.append(
                TranscriptEntry(
                    kind="assistant",
                    reasoning=message.reasoning_content,
                    run_id=message.run_id,
                    reasoning_seconds=(request.reasoning_seconds if request is not None else None),
                )
            )
        entries.extend(
            TranscriptEntry(kind="assistant", text=block, run_id=message.run_id)
            for block in split_markdown_blocks(message.text_content)
        )
        for call in message.tool_calls:
            if (message.run_id, call.id) in mutable_tool_call_keys:
                continue
            activity = tool_activities.get(call.id)
            if activity is not None:
                entries.append(
                    TranscriptEntry(
                        kind="tool",
                        run_id=message.run_id,
                        tool=activity,
                    )
                )

    run_entries: dict[int, list[TranscriptEntry]] = {}
    presented_run_ids: set[str] = set()
    for run in runs:
        boundary_index = last_message_by_run.get(run.run_id)
        if boundary_index is None or run.duration_seconds is None:
            continue
        run_entries.setdefault(boundary_index, []).append(
            TranscriptEntry(kind="run", run_id=run.run_id, run=run)
        )
        presented_run_ids.add(run.run_id)

    compaction_entries: dict[int, list[TranscriptEntry]] = {}
    for compaction in compactions:
        boundary_index = message_positions.get(compaction.history_boundary_message_id)
        if boundary_index is None:
            raise ValueError(
                f"compaction {compaction.compaction_id} references missing history "
                f"boundary {compaction.history_boundary_message_id}"
            )
        compaction_entries.setdefault(boundary_index, []).append(
            TranscriptEntry(
                kind="compaction",
                compaction=_compaction_presentation(
                    covered_message_count=len(compaction.covered_message_ids),
                    tokens_before=compaction.estimated_tokens_before,
                    tokens_after=compaction.estimated_tokens_after,
                    duration_seconds=compaction.duration_seconds,
                ),
            )
        )

    transcript: list[TranscriptEntry] = []
    for message_index, entries in enumerate(message_entries):
        transcript.extend(entries)
        transcript.extend(run_entries.get(message_index, ()))
        transcript.extend(compaction_entries.get(message_index, ()))
    return tuple(transcript), frozenset(presented_run_ids)


def _requires_background_transcript_projection(
    messages: tuple[MessageRecord, ...],
    compactions: tuple[CompactionRecord, ...],
    runs: tuple[RunPresentation, ...],
) -> bool:
    """Keep cheap loads synchronous; move only frame-sized work off the UI loop."""

    entry_count = len(messages) + len(compactions) + len(runs)
    text_size = 0
    for message in messages:
        entry_count += len(message.tool_calls)
        text_size += len(message.reasoning_content)
        text_size += sum(
            len(block.text) for block in message.content if isinstance(block, TextContent)
        )
        if (
            entry_count >= _TRANSCRIPT_BACKGROUND_ENTRY_THRESHOLD
            or text_size >= _TRANSCRIPT_BACKGROUND_TEXT_THRESHOLD
        ):
            return True
    entry_count += sum(len(run.requests) for run in runs)
    return entry_count >= _TRANSCRIPT_BACKGROUND_ENTRY_THRESHOLD


class _ConversationTranscript(RangeSelectionOnly, Vertical):
    """Own transcript layout without turning triple-click into select-all."""


class ConversationView(RangeSelectionOnly, VerticalScroll):
    def __init__(
        self,
        *,
        reasoning_expanded: bool = True,
        welcome_state_changed: Callable[[bool], None] | None = None,
        system_status_changed: Callable[[TuiSystemStatus], None] | None = None,
    ) -> None:
        super().__init__(id="conversation")
        self._follow_tail = True
        self._reasoning_expanded = reasoning_expanded
        self._welcome_state_changed = welcome_state_changed
        self._system_status_changed = system_status_changed
        self._history = ConversationHistory(
            _tool_activity_text,
            reasoning_expanded=reasoning_expanded,
        )
        self._transcript = _ConversationTranscript(
            self._history,
            id="conversation-transcript",
        )
        self._system_status = TuiSystemStatus()
        self._showing_welcome = False
        self._restored_scroll_y: float | None = None
        self._assistant: AssistantTurn | None = None
        self._tools: dict[str, ToolActivityRow] = {}
        self._current_run_id: str | None = None
        self._presented_run_ids: set[str] = set()
        self._visible_scaffold_delegation_id: str | None = None

    def compose(self) -> ComposeResult:
        yield self._transcript

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Resolve the scroll position from the viewport used by this layout pass.

        Dynamic siblings change the conversation's height. Scroll correction must use
        this pass's ``size`` so child placement and scrolling reach the screen as one
        coherent frame. A later resize callback is already one frame too late.
        """

        arrangement = super().arrange(size, optimal)
        if not optimal:
            max_scroll_y = max(0, arrangement.total_region.bottom - size.height)
            scroll_y: float
            if self._follow_tail:
                scroll_y = max_scroll_y
            elif self._restored_scroll_y is not None:
                scroll_y = min(self._restored_scroll_y, max_scroll_y)
                self._restored_scroll_y = None
            else:
                scroll_y = min(self.scroll_y, max_scroll_y)
            self.set_scroll(None, scroll_y)
            self.scroll_target_y = scroll_y
        return arrangement

    def anchor(self, anchor: bool = True) -> None:
        self._follow_tail = anchor
        self._restored_scroll_y = None
        if anchor:
            self.scroll_end(immediate=True, animate=False)

    def release_anchor(self) -> None:
        self._follow_tail = False
        self._restored_scroll_y = None
        self.scroll_target_y = self.scroll_y

    def held_scroll_y(self) -> float | None:
        """Return a restorable offset only while the user is reviewing history."""

        if self._follow_tail:
            return None
        return self._restored_scroll_y if self._restored_scroll_y is not None else self.scroll_y

    def _restore_scroll_y(self, scroll_y: float) -> None:
        """Resolve a saved offset against the destination transcript's first layout."""

        self._follow_tail = False
        self._restored_scroll_y = max(0.0, scroll_y)
        self.refresh(layout=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if not self._follow_tail and new_value >= self.max_scroll_y:
            self._follow_tail = True

    def on_mount(self) -> None:
        self.anchor()

    async def show_welcome(self) -> None:
        await self._clear_live_transcript()
        self._history.replace(())
        self._set_showing_welcome(True)
        self._assistant = None
        self._tools.clear()
        self._current_run_id = None
        self._presented_run_ids.clear()
        self.anchor()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._system_status = status
        if self._system_status_changed is not None:
            self._system_status_changed(status)

    @property
    def showing_welcome(self) -> bool:
        return self._showing_welcome

    def _set_showing_welcome(self, showing: bool) -> None:
        self._showing_welcome = showing
        if self._welcome_state_changed is not None:
            self._welcome_state_changed(showing)

    def set_reasoning_expanded(self, expanded: bool) -> None:
        with self.app.batch_update():
            self._reasoning_expanded = expanded
            delta = self._history.set_reasoning_expanded(
                expanded,
                above_line=round(self.scroll_y),
            )
            if self._assistant is not None:
                self._assistant.set_reasoning_expanded(expanded)
            if delta and not self._follow_tail:
                self.scroll_to(y=max(0.0, self.scroll_y + delta), animate=False, immediate=True)

    def on_reasoning_label_toggle_requested(
        self,
        message: ReasoningLabel.ToggleRequested,
    ) -> None:
        turn = self._assistant
        if turn is not None:
            with self.app.batch_update():
                expanding = not turn.reasoning_expanded
                if expanding:
                    self.release_anchor()
                turn.set_reasoning_expanded(expanding)
        message.stop()

    def on_conversation_history_toggle_reasoning_requested(
        self,
        message: ConversationHistory.ToggleReasoningRequested,
    ) -> None:
        with self.app.batch_update():
            if not self._history.is_reasoning_expanded(message.entry_index):
                self.release_anchor()
            self._history.toggle_reasoning(message.entry_index)
        message.stop()

    def take_history_state(self) -> ConversationHistoryState | None:
        """Detach the persisted transcript; live Run widgets are reconstructed."""

        return self._history.take_state()

    async def load_messages(
        self,
        messages: Iterable[MessageRecord],
        compactions: Iterable[CompactionRecord] = (),
        runs: Iterable[RunPresentation] = (),
        *,
        mutable_tool_calls: Iterable[tuple[str | None, str]] = (),
        visible_scaffold_delegation_id: str | None = None,
        held_scroll_y: float | None = None,
        reusable_history: ConversationHistoryState | None = None,
    ) -> None:
        await self._clear_live_transcript()
        self._assistant = None
        self._tools.clear()
        self._showing_welcome = False
        self._current_run_id = None
        self._presented_run_ids.clear()
        self._visible_scaffold_delegation_id = visible_scaffold_delegation_id

        # Canonical history contains both autonomous dialogue and harness
        # scaffolding. A child view deliberately presents only the user-role
        # inputs belonging to its own Delegation; every other scaffold remains
        # hidden according to its explicit presentation provenance.
        materialized = tuple(messages)
        compaction_records = tuple(compactions)
        run_presentations = tuple(runs)
        mutable_tool_call_keys = frozenset(mutable_tool_calls)
        if _requires_background_transcript_projection(
            materialized,
            compaction_records,
            run_presentations,
        ):
            transcript, presented_run_ids = await asyncio.to_thread(
                _project_transcript_entries,
                materialized,
                compaction_records,
                run_presentations,
                mutable_tool_call_keys=mutable_tool_call_keys,
                visible_scaffold_delegation_id=visible_scaffold_delegation_id,
            )
        else:
            transcript, presented_run_ids = _project_transcript_entries(
                materialized,
                compaction_records,
                run_presentations,
                mutable_tool_call_keys=mutable_tool_call_keys,
                visible_scaffold_delegation_id=visible_scaffold_delegation_id,
            )
        self._presented_run_ids.update(presented_run_ids)

        if not transcript:
            # A loaded session with no visible dialogue is still an empty state.
            self._history.replace(())
            self._set_showing_welcome(True)
            self.anchor()
            return

        self._set_showing_welcome(False)
        await self._history.load(transcript, reusable_state=reusable_history)
        if held_scroll_y is None:
            self.anchor()
        else:
            self._restore_scroll_y(held_scroll_y)

    async def _clear_live_transcript(self) -> None:
        live_children = tuple(
            child for child in self._transcript.children if child is not self._history
        )
        if live_children:
            await self._transcript.remove_children(live_children)

    def user_turn_summaries(self) -> tuple[str, ...]:
        return self._history.user_turns

    def scroll_to_user_turn(self, index: int) -> None:
        historical_lines = self._history.user_lines
        if 0 <= index < len(historical_lines):
            self.scroll_to(y=historical_lines[index], animate=False)

    def scroll_to_previous_user_turn(self) -> None:
        historical_lines = self._history.user_lines
        index = bisect_left(historical_lines, self.scroll_y) - 1
        self.scroll_to_user_turn(index)

    def scroll_to_next_user_turn(self) -> None:
        historical_lines = self._history.user_lines
        index = bisect_right(historical_lines, self.scroll_y)
        self.scroll_to_user_turn(index)

    async def _dismiss_welcome(self) -> None:
        """Transition from the empty welcome layout to the conversation layout."""
        if self._showing_welcome:
            self._set_showing_welcome(False)

    async def add_user(self, text: str, *, created_at: datetime | None = None) -> None:
        await self._dismiss_welcome()
        # A new user turn closes the preceding model round. Persist the live
        # assistant before appending the next chronological history boundary.
        await self.finish_assistant()
        self._history.append(
            TranscriptEntry(
                kind="user",
                text=text,
                created_at=created_at or datetime.now().astimezone(),
            )
        )
        await self._history.wait_until_rendered()

    async def add_user_message(self, message: MessageRecord) -> None:
        if message.role != "user" or not self._presents_message(message):
            return
        text = user_message_text(message)
        if text:
            await self.add_user(text, created_at=message.created_at)

    def _presents_message(self, message: MessageRecord) -> bool:
        return _presents_message(
            message,
            visible_scaffold_delegation_id=self._visible_scaffold_delegation_id,
        )

    async def append_assistant(self, text: str) -> None:
        await self._dismiss_welcome()
        turn = self._assistant
        if turn is None:
            turn = self._create_turn()
            self._assistant = turn
            blocks = await turn.feed(text)
            await self._transcript.mount(turn)
        else:
            blocks = await turn.feed(text)
        await self._freeze_blocks(turn, blocks)
        turn.commit_frozen_blocks()

    async def append_reasoning(
        self,
        text: str,
        *,
        started_at: datetime | None = None,
    ) -> None:
        await self._dismiss_welcome()
        if self._assistant is not None and self._assistant.has_text:
            # Reasoning after answer text starts a new model round; the previous
            # round must freeze before the new one renders below it.
            await self.finish_assistant()
        turn = await self._ensure_turn()
        await turn.append_reasoning(text, started_at=started_at)

    async def _ensure_turn(self) -> AssistantTurn:
        if self._assistant is None:
            self._assistant = self._create_turn()
            await self._transcript.mount(self._assistant)
        return self._assistant

    def _create_turn(self) -> AssistantTurn:
        return AssistantTurn(
            run_id=self._current_run_id,
            after_tool=self._history_ends_with_tool,
            reasoning_expanded=self._reasoning_expanded,
        )

    async def _freeze_blocks(self, turn: AssistantTurn, blocks: Iterable[str]) -> None:
        """Freeze completed blocks, promoting pending reasoning ahead of them."""
        materialized = tuple(block for block in blocks if block.strip())
        if not materialized:
            return
        if turn.reasoning_pending:
            reasoning_expanded = turn.reasoning_expanded
            reasoning, seconds = turn.take_reasoning()
            if reasoning:
                self._history.append(
                    TranscriptEntry(
                        kind="assistant",
                        reasoning=reasoning,
                        reasoning_seconds=seconds,
                        run_id=turn.run_id,
                    ),
                    reasoning_expanded=reasoning_expanded,
                )
        await self._history.append_many_rendered(
            TranscriptEntry(kind="assistant", text=block, run_id=turn.run_id)
            for block in materialized
        )

    async def finish_assistant(self) -> None:
        """Freeze the live round into the immutable history."""
        turn = self._assistant
        if turn is None:
            return
        await self._freeze_blocks(turn, await turn.close())
        turn.clear_tail()
        if turn.reasoning_pending:
            reasoning_expanded = turn.reasoning_expanded
            reasoning, seconds = turn.take_reasoning()
            if reasoning:
                self._history.append(
                    TranscriptEntry(
                        kind="assistant",
                        reasoning=reasoning,
                        reasoning_seconds=seconds,
                        run_id=turn.run_id,
                    ),
                    reasoning_expanded=reasoning_expanded,
                )
        self._assistant = None
        await turn.remove()

    @property
    def _history_ends_with_tool(self) -> bool:
        entries = self._history.entries
        return bool(entries and entries[-1].kind == "tool")

    def finish_reasoning(self, duration_seconds: float | None = None) -> None:
        if self._assistant is not None:
            self._assistant.finish_reasoning(duration_seconds)

    def begin_model_request(self, *, run_id: str | None) -> None:
        self._current_run_id = run_id

    async def add_run_summary(self, run: RunPresentation) -> None:
        if run.duration_seconds is None or run.run_id in self._presented_run_ids:
            return
        await self._dismiss_welcome()
        if self._assistant is not None and self._assistant.run_id == run.run_id:
            await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="run", run_id=run.run_id, run=run))
        self._presented_run_ids.add(run.run_id)
        await self._history.wait_until_rendered()

    async def sync_tools(self, tools: tuple[RuntimeToolCallActivity, ...]) -> None:
        await self._dismiss_welcome()
        tools = tuple(tool for tool in tools if tool.tool_name)
        historical_tool_ids = self._history.update_tools(
            tools,
            run_id=self._current_run_id,
        )
        self._adopt_streaming_tool_rows(tools)
        has_new_tool = any(
            tool.tool_call_id not in historical_tool_ids and tool.tool_call_id not in self._tools
            for tool in tools
        )
        if has_new_tool:
            # A tool call is a chronological boundary between model rounds.
            # Freeze the preceding assistant segment before rendering the tool.
            await self.finish_assistant()
        new_rows: list[ToolActivityRow] = []
        for tool in tools:
            if tool.tool_call_id in historical_tool_ids:
                continue
            row = self._tools.get(tool.tool_call_id)
            if row is None:
                row = ToolActivityRow(tool, run_id=self._current_run_id)
                self._tools[tool.tool_call_id] = row
                new_rows.append(row)
            else:
                row.apply_tool(tool)
        if new_rows:
            await self._transcript.mount_all(new_rows)

        # Keep a tool batch live until every row is terminal. Archiving rows one
        # by one would move a later-completing row ahead of an earlier active row
        # because the immutable history always precedes the live widget region.
        if self._tools and all(_is_terminal_tool(row.tool) for row in self._tools.values()):
            terminal_rows = tuple(self._tools.values())
            self._tools.clear()
            await self._history.append_many_rendered(
                TranscriptEntry(
                    kind="tool",
                    run_id=row.run_id,
                    tool=row.tool,
                    agent=row.agent,
                )
                for row in terminal_rows
            )
            await self._transcript.remove_children(terminal_rows)

    def _adopt_streaming_tool_rows(
        self,
        tools: tuple[RuntimeToolCallActivity, ...],
    ) -> None:
        """Re-key provisional stream rows when canonical tool ids arrive."""

        provisional_by_index = {
            row.tool.source_index: tool_call_id
            for tool_call_id, row in self._tools.items()
            if row.tool.status == "streaming"
        }
        for tool in tools:
            if tool.tool_call_id in self._tools:
                continue
            provisional_id = provisional_by_index.get(tool.source_index)
            if provisional_id is None:
                continue
            row = self._tools.pop(provisional_id)
            row.apply_tool(tool)
            self._tools[tool.tool_call_id] = row

    def sync_agents(self, agents: Iterable[AgentTreeNode]) -> None:
        materialized = tuple(agents)
        if not materialized:
            return
        self._history.update_agents(materialized)
        agents_by_call = {
            (agent.parent_run_id, agent.parent_tool_call_id): agent
            for agent in materialized
            if agent.parent_tool_call_id is not None
        }
        for row in self._tools.values():
            if row.run_id is None:
                continue
            agent = agents_by_call.get((row.run_id, row.tool_call_id))
            if agent is not None:
                row.apply_agent(agent)

    async def add_notice(self, message: str, *, error: bool = False) -> None:
        await self._dismiss_welcome()
        # Notices are immutable transcript boundaries as well. Freezing any
        # partial answer keeps presentation order identical to event order.
        await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="notice", text=message, error=error))
        await self._history.wait_until_rendered()

    async def add_compaction(
        self,
        *,
        covered_message_count: int,
        tokens_before: int | None,
        tokens_after: int | None,
        duration_seconds: float | None,
    ) -> None:
        await self._dismiss_welcome()
        await self.finish_assistant()
        self._history.append(
            TranscriptEntry(
                kind="compaction",
                compaction=_compaction_presentation(
                    covered_message_count=covered_message_count,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    duration_seconds=duration_seconds,
                ),
            )
        )
        await self._history.wait_until_rendered()


class ApprovalPanel(Vertical):
    """Shared keyboard and visual skeleton for inline approval workflows."""

    can_focus = True
    can_focus_children = False
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
        Binding("enter", "accept", "Choose", show=False),
        Binding("tab,shift+tab", "retain_focus", show=False, priority=True),
    ]

    def __init__(self, *, panel_id: str, title_id: str, controls_id: str) -> None:
        super().__init__(id=panel_id, classes="approval-panel")
        self._title = Static(id=title_id, classes="approval-title")
        self._controls = Static(id=controls_id, classes="approval-controls")
        self._cursor = 0

    def action_previous_option(self) -> None:
        self._move_approval_cursor(-1)

    def action_next_option(self) -> None:
        self._move_approval_cursor(1)

    def action_retain_focus(self) -> None:
        self.focus()

    def _move_approval_cursor(self, offset: int) -> None:
        choice_count = self._approval_choice_count()
        if not choice_count or self.disabled:
            return
        self._cursor = (self._cursor + offset) % choice_count
        self._render_approval_controls()
        self.call_after_refresh(self._scroll_approval_selection_into_view)

    def _scroll_approval_selection_into_view(self) -> None:
        if self._cursor == 0:
            self.scroll_home(animate=False)
        else:
            self.scroll_end(animate=False)

    def _render_approval_title(self, title: str) -> None:
        theme = theme_for(self)
        self._title.update(Text(title, style=f"bold {theme.accent_bright}"))

    def _append_approval_option(
        self,
        content: Text,
        *,
        index: int,
        label: str,
        description: str,
        tone: str,
    ) -> None:
        active = index == self._cursor
        theme = theme_for(self)
        content.append("› " if active else "  ", style=theme.accent if active else "")
        content.append(
            label,
            style=f"bold {tone}" if active else f"bold {theme.text_primary}",
        )
        if description:
            content.append(
                f"  {description}",
                style=(theme.text_primary if active else theme.text_lowlight),
            )
        content.append("\n")

    def _append_approval_hint(
        self,
        content: Text,
        *,
        leading: tuple[str, ...] = (),
        trailing: tuple[str, ...] = (),
    ) -> None:
        content.append(
            "\n"
            + theme_for(self).inline_separator.join(
                (*leading, "↑/↓ move", "Enter choose", *trailing)
            ),
            style=theme_for(self).text_lowlight,
        )

    def _approval_choice_count(self) -> int:
        raise NotImplementedError

    def _render_approval_controls(self) -> None:
        raise NotImplementedError


class InteractionBar(ApprovalPanel):
    """Tool approval projected through the shared approval panel."""

    class Decision(Message):
        def __init__(self, interaction: PendingInteraction, approved: bool) -> None:
            super().__init__()
            self.interaction = interaction
            self.approved = approved

    def __init__(self) -> None:
        super().__init__(
            panel_id="interaction-bar",
            title_id="interaction-title",
            controls_id="interaction-controls",
        )
        self._summary = Static(id="interaction-summary")
        self.display = False
        self.disabled = True
        self._interaction: PendingInteraction | None = None
        self._identity: tuple[str, str] | None = None
        self._sequence_owner: tuple[str | None, str] | None = None
        self._sequence_position = 0
        self._sequence_total = 0

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._summary
        yield self._controls

    @property
    def selected_approved(self) -> bool:
        return self._cursor == 0

    def show_interaction(
        self,
        interaction: PendingInteraction | None,
        *,
        pending_count: int = 1,
    ) -> None:
        with self.app.batch_update():
            if interaction is None or interaction.kind != "approval":
                self.display = False
                self._interaction = None
                self._identity = None
                self._sequence_owner = None
                self._sequence_position = 0
                self._sequence_total = 0
                self._cursor = 0
                return
            identity = (interaction.run_id, interaction.request_id or interaction.tool_call_id)
            if identity != self._identity:
                self._cursor = 0
                owner = (interaction.session_id, interaction.run_id)
                if self._identity is None or owner != self._sequence_owner:
                    self._sequence_owner = owner
                    self._sequence_position = 1
                    self._sequence_total = max(1, pending_count)
                else:
                    self._sequence_position += 1
                    self._sequence_total = max(
                        self._sequence_total,
                        self._sequence_position + max(1, pending_count) - 1,
                    )
            self._interaction = interaction
            self._identity = identity
            self.disabled = False
            self.display = True
            self._render_approval()

    def update_pending_count(self, pending_count: int) -> None:
        if self._interaction is None:
            return
        total = max(
            self._sequence_total,
            self._sequence_position + max(1, pending_count) - 1,
        )
        if total != self._sequence_total:
            self._sequence_total = total
            self._render_approval()

    def resume(self) -> None:
        if self._interaction is None:
            return
        with self.app.batch_update():
            self.disabled = False
            self.display = True
            self._render_approval()
            self.focus()

    def refresh_theme(self) -> None:
        self._render_approval()

    def action_accept(self) -> None:
        self._decide(self.selected_approved)

    def _approval_choice_count(self) -> int:
        return 2 if self._interaction is not None else 0

    def _render_approval_controls(self) -> None:
        self._render_approval()

    def _decide(self, approved: bool) -> None:
        interaction = self._interaction
        if interaction is None or self.disabled:
            return
        self.disabled = True
        self.post_message(self.Decision(interaction, approved))

    def _render_approval(self) -> None:
        interaction = self._interaction
        if interaction is None:
            return
        theme = theme_for(self)
        title = (
            f"APPROVAL  {self._sequence_position}/{self._sequence_total}"
            if self._sequence_total > 1
            else "APPROVAL"
        )
        self._render_approval_title(title)
        summary = Text()
        if interaction.is_subagent:
            summary.append(
                f"Subagent {interaction.agent_name or interaction.delegation_id}\n",
                style=theme.text_lowlight,
            )
        summary.append(
            f"{interaction.tool_name} wants to run",
            style=f"bold {theme.text_primary}",
        )
        self._summary.update(summary)
        content = Text()
        options = (
            ("Allow", "Run this tool", theme.success),
            ("Deny", "Reject this tool", theme.failure),
        )
        for index, (label, description, tone) in enumerate(options):
            self._append_approval_option(
                content,
                index=index,
                label=label,
                description=description,
                tone=tone,
            )
        self._append_approval_hint(content)
        self._controls.update(content)
        self.call_after_refresh(self._scroll_approval_selection_into_view)


class AskUserPanel(Static):
    """Keyboard-first renderer for a Host-projected question form."""

    can_focus = True
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
        Binding("left", "previous_question", show=False),
        Binding("right", "next_question", show=False),
        Binding("space", "toggle_option", show=False),
        Binding("enter", "accept", show=False),
        Binding("1", "choose(0)", show=False),
        Binding("2", "choose(1)", show=False),
        Binding("3", "choose(2)", show=False),
        Binding("4", "choose(3)", show=False),
        Binding("5", "choose(4)", show=False),
    ]

    class Completed(Message):
        def __init__(self, answer: JsonValue) -> None:
            super().__init__()
            self.answer = answer

    class FreeformRequested(Message):
        def __init__(self, question_id: str) -> None:
            super().__init__()
            self.question_id = question_id

    def __init__(self) -> None:
        super().__init__(id="ask-user-panel")
        self.display = False
        self.disabled = True
        self._request: QuestionForm | None = None
        self._question_index = 0
        self._cursor = 0
        self._selected: set[int] = set()
        self._answers: dict[str, list[str]] = {}
        self._awaiting_freeform = False

    @property
    def awaiting_freeform(self) -> bool:
        return self._awaiting_freeform

    def show_request(self, request: QuestionForm) -> None:
        with self.app.batch_update():
            self._request = request
            self._question_index = 0
            self._cursor = 0
            self._selected.clear()
            self._answers.clear()
            self._awaiting_freeform = False
            self.disabled = False
            self.display = True
            self._show_question(0)

    def hide_panel(self) -> None:
        with self.app.batch_update():
            self.display = False
            self._request = None
            self._awaiting_freeform = False

    def resume(self) -> None:
        if self._request is not None:
            with self.app.batch_update():
                self.disabled = False
                self.display = True
                self._render_question()
                self.focus()

    def accept_freeform(self, value: str) -> None:
        if not self._awaiting_freeform or self._request is None:
            return
        normalized = value.strip()
        if not normalized:
            return
        question = self._question
        answers = [question.options[index].label for index in sorted(self._selected)]
        answers.append(normalized)
        self._awaiting_freeform = False
        self._finish_question(question, answers)

    def action_previous_option(self) -> None:
        self._move_cursor(-1)

    def action_next_option(self) -> None:
        self._move_cursor(1)

    def action_previous_question(self) -> None:
        self._move_question(-1)

    def action_next_question(self) -> None:
        self._move_question(1)

    def action_toggle_option(self) -> None:
        request = self._request
        if request is None or not self._question.multi_select:
            return
        if self._cursor >= len(self._question.options):
            self._request_freeform()
            return
        if self._cursor in self._selected:
            self._selected.remove(self._cursor)
        else:
            self._selected.add(self._cursor)
        self._render_question()

    def action_accept(self) -> None:
        request = self._request
        if request is None:
            return
        question = self._question
        if self._cursor >= len(question.options):
            self._request_freeform()
            return
        if question.multi_select:
            if not self._selected:
                self._selected.add(self._cursor)
            answers = [question.options[index].label for index in sorted(self._selected)]
        else:
            answers = [question.options[self._cursor].label]
        self._finish_question(question, answers)

    def action_choose(self, index: int) -> None:
        if self._request is None or index > len(self._question.options):
            return
        self._cursor = index
        if self._question.multi_select and index < len(self._question.options):
            self.action_toggle_option()
            return
        self.action_accept()

    @property
    def _question(self) -> QuestionField:
        assert self._request is not None
        return self._request.questions[self._question_index]

    def _move_cursor(self, offset: int) -> None:
        if self._request is None:
            return
        option_count = len(self._question.options) + 1
        self._cursor = (self._cursor + offset) % option_count
        self._render_question()

    def _request_freeform(self) -> None:
        self._awaiting_freeform = True
        self.post_message(self.FreeformRequested(self._question.question_id))

    def _finish_question(self, question: QuestionField, answers: list[str]) -> None:
        self._answers[question.question_id] = answers
        assert self._request is not None
        if self._question_index + 1 < len(self._request.questions):
            self._show_question(self._question_index + 1)
            return
        answer = self._request.encode(self._answers)
        self.disabled = True
        self.post_message(self.Completed(answer))

    def _move_question(self, offset: int) -> None:
        request = self._request
        if request is None or self._awaiting_freeform:
            return
        target = self._question_index + offset
        if not 0 <= target < len(request.questions):
            return
        if offset > 0 and self._question.question_id not in self._answers:
            return
        self._show_question(target)

    def _show_question(self, index: int) -> None:
        assert self._request is not None
        self._question_index = index
        self._cursor = 0
        self._selected.clear()
        self._awaiting_freeform = False
        question = self._question
        answers = self._answers.get(question.question_id, ())
        option_indexes = {
            option.label: option_index for option_index, option in enumerate(question.options)
        }
        self._selected.update(
            option_indexes[answer] for answer in answers if answer in option_indexes
        )
        unmatched_answer = any(answer not in option_indexes for answer in answers)
        if unmatched_answer:
            self._cursor = len(question.options)
        elif question.multi_select and self._selected:
            self._cursor = min(self._selected)
        elif answers:
            self._cursor = option_indexes.get(answers[0], 0)
        self._render_question()
        self.focus()
        if not question.options:
            self._request_freeform()

    def _render_question(self) -> None:
        request = self._request
        if request is None:
            return
        question = self._question
        total = len(request.questions)
        theme = theme_for(self)
        content = Text()
        content.append(
            f"{question.header.upper()}  {self._question_index + 1}/{total}\n",
            style=f"bold {theme.accent_bright}",
        )
        content.append(
            f"{question.prompt}\n\n",
            style=f"bold {theme.text_primary}",
        )
        for index, option in enumerate(question.options):
            active = index == self._cursor
            checked = index in self._selected
            if question.multi_select:
                marker = "[x]" if checked else "[ ]"
            else:
                marker = theme.selected_choice_marker if active else theme.unselected_choice_marker
            content.append("› " if active else "  ", style=theme.accent if active else "")
            content.append(
                f"{index + 1}. {marker} {option.label}",
                style=f"bold {theme.text_primary}" if active else "",
            )
            content.append(
                f"  {option.description}\n",
                style=(theme.text_primary if active else theme.text_lowlight),
            )
        other_index = len(question.options)
        other_active = self._cursor == other_index
        content.append("› " if other_active else "  ", style=theme.accent if other_active else "")
        content.append(
            f"{other_index + 1}. Other…",
            style=(f"bold {theme.accent_bright}" if other_active else f"bold {theme.text_primary}"),
        )
        navigation = ("←/→ questions",) if total > 1 else ()
        controls = (
            (*navigation, "↑/↓ move", "Space toggle", "Enter continue")
            if question.multi_select
            else (*navigation, "↑/↓ move", "Enter choose")
        )
        hint = "\n\n" + theme.inline_separator.join(controls)
        content.append(hint, style=theme.text_lowlight)
        self.update(content)

    def refresh_theme(self) -> None:
        self._render_question()


class PlanConfirmationPanel(ApprovalPanel):
    """Keyboard-first decision workflow for the builtin ExitPlanMode request."""

    BINDINGS = [
        *ApprovalPanel.BINDINGS,
        Binding("left,pageup", "scroll_plan_up", show=False),
        Binding("right,pagedown", "scroll_plan_down", show=False),
        Binding("escape", "back", show=False),
    ]

    class Completed(Message):
        def __init__(self, answer: JsonValue) -> None:
            super().__init__()
            self.answer = answer

    class FeedbackRequested(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            panel_id="plan-confirmation-panel",
            title_id="plan-confirmation-title",
            controls_id="plan-confirmation-controls",
        )
        self.display = False
        self.disabled = True
        self._plan_body = Static(id="plan-confirmation-body")
        self._document = VerticalScroll(
            self._plan_body,
            id="plan-confirmation-document",
            can_focus=False,
        )
        self._request: PlanConfirmationRequest | None = None
        self._stage: Literal["action", "option", "feedback"] = "action"
        self._awaiting_feedback = False

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._document
        yield self._controls

    @property
    def awaiting_feedback(self) -> bool:
        return self._awaiting_feedback

    @property
    def selected_action(self) -> PlanConfirmationAction | None:
        if self._request is None or self._stage != "action":
            return None
        return self._request.actions[self._cursor]

    def show_request(self, request: PlanConfirmationRequest) -> None:
        with self.app.batch_update():
            self._request = request
            self._stage = "action"
            self._cursor = 0
            self._awaiting_feedback = False
            self.disabled = False
            self.display = True
            self._render_panel()
            self._document.scroll_home(animate=False)
            self.focus()

    def hide_panel(self) -> None:
        with self.app.batch_update():
            self.display = False
            self._request = None
            self._stage = "action"
            self._cursor = 0
            self._awaiting_feedback = False

    def resume(self) -> None:
        if self._request is None:
            return
        with self.app.batch_update():
            self.disabled = False
            self.display = True
            self._render_panel()
            if self._awaiting_feedback:
                self.post_message(self.FeedbackRequested())
            else:
                self.focus()

    def accept_feedback(self, value: str) -> None:
        if self._request is None or not self._awaiting_feedback:
            return
        feedback = value.strip()
        if not feedback:
            return
        self._awaiting_feedback = False
        self._stage = "action"
        self._complete(PlanConfirmationAnswer(action="revise", feedback=feedback))

    def action_scroll_plan_up(self) -> None:
        self._document.scroll_page_up(animate=False)

    def action_scroll_plan_down(self) -> None:
        self._document.scroll_page_down(animate=False)

    def action_accept(self) -> None:
        request = self._request
        if request is None or self.disabled or self._stage == "feedback":
            return
        if self._stage == "option":
            selected_option = request.options[self._cursor]
            self._complete(PlanConfirmationAnswer(action="accept", option=selected_option.label))
            return

        action = request.actions[self._cursor]
        if action == "accept":
            if len(request.options) > 1:
                self._stage = "option"
                self._cursor = 0
                self._render_controls(request)
                return
            option_label = request.options[0].label if request.options else None
            self._complete(PlanConfirmationAnswer(action="accept", option=option_label))
            return
        if action == "revise":
            self._stage = "feedback"
            self._awaiting_feedback = True
            self._render_controls(request)
            self.post_message(self.FeedbackRequested())
            return
        self._complete(PlanConfirmationAnswer(action="reject"))

    def action_back(self) -> None:
        if self._request is None or self.disabled or self._stage == "action":
            return
        self._stage = "action"
        self._cursor = 0
        self._awaiting_feedback = False
        self._render_controls(self._request)
        self.focus()

    def refresh_theme(self) -> None:
        self._render_panel()

    def _choices(self) -> tuple[object, ...]:
        if self._request is None:
            return ()
        if self._stage == "action":
            return self._request.actions
        if self._stage == "option":
            return self._request.options
        return ()

    def _approval_choice_count(self) -> int:
        return len(self._choices())

    def _render_approval_controls(self) -> None:
        if self._request is not None:
            self._render_controls(self._request)

    def _complete(self, answer: PlanConfirmationAnswer) -> None:
        self.disabled = True
        self.post_message(self.Completed(answer.to_json()))

    def _render_panel(self) -> None:
        request = self._request
        if request is None:
            return
        theme = theme_for(self)
        with self.app.batch_update():
            self._render_approval_title("PLAN APPROVAL")
            self._plan_body.update(Text(request.plan, style=theme.text_primary))
            self._render_controls(request)

    def _render_controls(self, request: PlanConfirmationRequest) -> None:
        theme = theme_for(self)
        content = Text()
        if request.error:
            content.append(f"{request.error}\n\n", style=theme.danger)
        if self._stage == "action":
            content.append(
                "Choose how Plan mode should continue\n\n",
                style=theme.text_secondary,
            )
            presentations = {
                "accept": ("Accept", "Approve the plan and exit Plan mode", theme.success),
                "revise": ("Revise", "Keep planning and provide feedback", theme.accent),
                "reject": (
                    "Reject",
                    "Reject the plan and keep Plan mode active",
                    theme.danger,
                ),
            }
            for index, action in enumerate(request.actions):
                label, description, tone = presentations[action]
                self._append_approval_option(
                    content,
                    index=index,
                    label=label,
                    description=description,
                    tone=tone,
                )
            self._append_approval_hint(
                content,
                leading=("←/→ or PgUp/PgDn plan",),
            )
        elif self._stage == "option":
            content.append(
                "Choose an implementation approach\n\n",
                style=theme.text_secondary,
            )
            for index, option in enumerate(request.options):
                self._append_approval_option(
                    content,
                    index=index,
                    label=option.label,
                    description=option.description,
                    tone=theme.accent,
                )
            self._append_approval_hint(
                content,
                leading=("←/→ or PgUp/PgDn plan",),
                trailing=("Esc back",),
            )
        else:
            content.append("REVISION FEEDBACK\n", style=f"bold {theme.accent_bright}")
            content.append(
                "Type the requested plan changes below, then press Enter",
                style=theme.text_lowlight,
            )
        self._controls.update(content)


class CommandPopup(VerticalScroll):
    """Composer-controlled command panel with keyboard-owned hidden scrolling."""

    def __init__(self) -> None:
        super().__init__(id="command-popup")
        self.display = False
        self._commands: tuple[TuiCommand, ...] = ()
        self._argument_command: TuiCommand | None = None
        self._arguments: tuple[TuiCommandArgument, ...] = ()
        self._selected = 0
        self._content = Static(id="command-popup-content")

    def compose(self) -> ComposeResult:
        yield self._content

    @property
    def selected_command(self) -> TuiCommand | None:
        if self._argument_command is not None or not self._commands:
            return None
        return self._commands[self._selected]

    @property
    def argument_command(self) -> TuiCommand | None:
        return self._argument_command

    @property
    def selected_argument(self) -> TuiCommandArgument | None:
        if self._argument_command is None or not self._arguments:
            return None
        return self._arguments[self._selected]

    def show_commands(self, commands: tuple[TuiCommand, ...]) -> None:
        if not commands:
            self.hide()
            return
        with self.app.batch_update():
            selected_name = (
                self.selected_command.name if self.selected_command is not None else None
            )
            self._commands = commands
            self._argument_command = None
            self._arguments = ()
            self._selected = next(
                (index for index, item in enumerate(commands) if item.name == selected_name),
                0,
            )
            self.display = True
            self._render_commands()

    def show_arguments(
        self,
        command: TuiCommand,
        arguments: tuple[TuiCommandArgument, ...],
    ) -> None:
        with self.app.batch_update():
            selected_value = (
                self.selected_argument.value if self.selected_argument is not None else None
            )
            self._commands = ()
            self._argument_command = command
            self._arguments = arguments
            self._selected = next(
                (index for index, item in enumerate(arguments) if item.value == selected_value),
                0,
            )
            self.display = True
            self._render_arguments()

    def hide(self) -> None:
        if (
            not self.display
            and not self._commands
            and self._argument_command is None
            and not self._arguments
            and self._selected == 0
        ):
            return
        with self.app.batch_update():
            self.display = False
            self._commands = ()
            self._argument_command = None
            self._arguments = ()
            self._selected = 0

    def navigate(self, offset: int) -> None:
        items = self._arguments if self._argument_command is not None else self._commands
        if not items:
            return
        self._selected = (self._selected + offset) % len(items)
        if self._argument_command is not None:
            self._render_arguments()
        else:
            self._render_commands()

    def refresh_theme(self) -> None:
        if self._argument_command is not None:
            self._render_arguments()
        elif self._commands:
            self._render_commands()

    def _render_commands(self) -> None:
        if not self._commands:
            return
        theme = theme_for(self)
        name_width = max(len(command.name) for command in self._commands)
        rows: list[Text] = []
        for index, command in enumerate(self._commands):
            selected = index == self._selected
            row = Text(style=f"on {theme.highlight}" if selected else "")
            row.append("› " if selected else "  ", style=theme.accent if selected else "")
            row.append(
                command.name.ljust(name_width),
                style=(theme.accent_bright if selected else theme.text_primary),
            )
            row.append(
                f"  {command.title}",
                style=(theme.text_primary if selected else theme.text_lowlight),
            )
            rows.append(row)
        self._render_rows(tuple(rows))

    def _render_arguments(self) -> None:
        command = self._argument_command
        if command is None:
            return
        theme = theme_for(self)
        if not self._arguments:
            content = Text()
            content.append("› ", style=theme.accent)
            content.append(command.title, style=theme.text_primary)
            self._content.update(content)
            return
        value_width = max(len(argument.display_label) for argument in self._arguments)
        rows: list[Text] = []
        for index, argument in enumerate(self._arguments):
            selected = index == self._selected
            row = Text(style=f"on {theme.highlight}" if selected else "")
            row.append("› " if selected else "  ", style=theme.accent if selected else "")
            row.append(
                argument.display_label.ljust(value_width),
                style=(
                    argument.accent or (theme.accent_bright if selected else theme.text_primary)
                ),
            )
            if argument.description:
                row.append(
                    f"  {argument.description}",
                    style=(theme.text_secondary if selected else theme.text_lowlight),
                )
            rows.append(row)
        self._render_rows(tuple(rows))

    def _render_rows(self, rows: tuple[Text, ...]) -> None:
        self._render_rows_at_width(rows, retry_after_layout=True)

    def _render_rows_at_width(
        self,
        rows: tuple[Text, ...],
        *,
        retry_after_layout: bool,
    ) -> None:
        width = self.scrollable_content_region.width
        if width <= 0:
            self._content.update(Text("\n").join(rows))
            if retry_after_layout:
                self.call_after_refresh(
                    self._render_rows_at_width,
                    rows,
                    retry_after_layout=False,
                )
            return

        fitted_rows = tuple(row.copy() for row in rows)
        for row in fitted_rows:
            row.truncate(width, overflow="ellipsis", pad=True)
        self._content.styles.width = width
        self._content.update(Text("\n").join(fitted_rows))
        self.call_after_refresh(self._scroll_selected_into_view, fitted_rows)

    def _scroll_selected_into_view(self, rows: tuple[Text, ...]) -> None:
        if not rows or self._selected >= len(rows):
            return
        width = self.scrollable_content_region.width
        if width <= 0:
            return
        if self._content.content_region.width != width:
            self._content.styles.width = width
            self.call_after_refresh(self._scroll_selected_into_view, rows)
            return
        heights = tuple(max(1, len(row.wrap(self.app.console, width))) for row in rows)
        selected_region = Region(
            0,
            sum(heights[: self._selected]),
            width,
            heights[self._selected],
        )
        self.scroll_to_region(
            selected_region,
            animate=False,
            # The popup suppresses scrollbar chrome, but selection still owns its viewport.
            force=True,
            x_axis=False,
        )


class QueuedInputPanel(VerticalScroll):
    """Live projection of direct user inputs awaiting a Runtime boundary."""

    def __init__(self) -> None:
        super().__init__(id="queued-input-panel")
        self.display = False
        self._content = Static(id="queued-input-content")
        self._inputs: tuple[QueuedUserInputView, ...] = ()

    def compose(self) -> ComposeResult:
        yield self._content

    @property
    def inputs(self) -> tuple[QueuedUserInputView, ...]:
        return self._inputs

    def refresh_theme(self) -> None:
        self.set_inputs(self._inputs)

    def set_inputs(self, inputs: tuple[QueuedUserInputView, ...]) -> None:
        with self.app.batch_update():
            self._inputs = inputs
            if not inputs:
                self.display = False
                return
            self.display = True
            theme = theme_for(self)
            content = Text()
            for index, item in enumerate(inputs, start=1):
                label = "Follow-up" if item.mode == "follow_up" else "Guide"
                tone = theme.accent if item.mode == "follow_up" else theme.accent_bright
                prompt = " ↵ ".join(line.strip() for line in item.prompt.splitlines())
                if item.skill_name is not None:
                    prompt = f"/skill {item.skill_name}{f' {prompt}' if prompt else ''}"
                content.append(f"{index}. {label}", style=tone)
                content.append(
                    theme.inline_separator,
                    style=theme.separator_tone,
                )
                content.append(prompt, style=theme.text_lowlight)
                if index < len(inputs):
                    content.append("\n")
            self._content.update(content)
            self.scroll_end(animate=False)


class Composer(TuiTextArea):
    DEFAULT_PLACEHOLDER = "Ask anything..."
    ACTIVE_RUN_PLACEHOLDER = DEFAULT_TUI_THEME.inline_separator.join(
        ("Enter queues a follow-up", "Ctrl+Enter guides the active run…")
    )
    QUESTION_PLACEHOLDER = "Choose an option above…"
    FREEFORM_PLACEHOLDER = "Type another answer…"
    PLAN_FEEDBACK_PLACEHOLDER = "Describe the requested plan changes…"
    BINDINGS = [
        *TextArea.BINDINGS,
        Binding("enter", "submit", "Send", show=False, priority=True),
        # Terminals without enhanced key reporting encode Ctrl+Enter as LF / Ctrl+J.
        Binding("ctrl+enter,ctrl+j", "submit_steer", "Guide", show=False, priority=True),
        Binding("shift+enter", "newline", "New line", show=False, priority=True),
        Binding("up", "history_previous", show=False, priority=True),
        Binding("down", "history_next", show=False, priority=True),
        Binding("escape", "dismiss_command", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str, mode: QueuedInputMode) -> None:
            super().__init__()
            self.text = text
            self.mode = mode

    class CommandSubmitted(Message):
        def __init__(self, text: str, *, command_text: str, draft: str) -> None:
            super().__init__()
            self.text = text
            self.command_text = command_text
            self.draft = draft

    class CommandNavigated(Message):
        def __init__(self, offset: int) -> None:
            super().__init__()
            self.offset = offset

    class CommandDismissed(Message):
        pass

    def __init__(
        self,
        *,
        clipboard_reader: Callable[[], str] = read_system_clipboard,
    ) -> None:
        super().__init__(
            id="composer",
            placeholder=self.DEFAULT_PLACEHOLDER,
            soft_wrap=True,
            show_line_numbers=False,
            compact=True,
        )
        self._history: list[str] = []
        self._history_index = 0
        self._clipboard_reader = clipboard_reader
        self._observed_text = ""
        self._command_draft: str | None = None
        self._suggestion_resolver: Callable[[], str] | None = None
        self._placeholder_parts: tuple[_PlaceholderPart, ...] = ()
        self._placeholder_kind: Literal["active_run", "subagent"] | None = None
        # Some terminals emit Paste alongside the right-click mouse event.
        self._right_click_paste_pending = False
        self._right_click_paste_handled = False

    @property
    def projected_height(self) -> int:
        """The outer height Textual resolves for the current wrapped document.

        ``height: auto`` on a ``ScrollView`` resolves through the virtual size,
        so the wrapped document height bounded by the composer's text limits is
        the same value the box model arrives at. Reading it here lets adjacent
        panels re-bound in the layout pass that grows the composer.
        """

        text_height = min(
            COMPOSER_TEXT_MAX_HEIGHT,
            max(COMPOSER_TEXT_MIN_HEIGHT, self.wrapped_document.height),
        )
        return text_height + COMPOSER_FRAME_HEIGHT

    def set_suggestion_resolver(self, resolver: Callable[[], str]) -> None:
        """Resolve inline suggestions synchronously with text edits."""

        self._suggestion_resolver = resolver
        self.update_suggestion()

    def set_plain_placeholder(self, placeholder: str) -> None:
        if not self._placeholder_parts and self.placeholder == placeholder:
            return
        self._placeholder_parts = ()
        self._placeholder_kind = None
        self.placeholder = placeholder

    def set_active_run_placeholder(self) -> None:
        self._set_styled_placeholder("active_run")

    def set_subagent_placeholder(self) -> None:
        self._set_styled_placeholder("subagent")

    def _set_styled_placeholder(
        self,
        kind: Literal["active_run", "subagent"],
        *,
        force: bool = False,
    ) -> None:
        theme = theme_for(self)
        if kind == "active_run":
            parts: tuple[_PlaceholderPart, ...] = (
                ("Enter", "key"),
                (" queues a follow-up", "text"),
                (theme.inline_separator, "separator"),
                ("Ctrl+Enter", "key"),
                (" guides the active run…", "text"),
            )
        else:
            parts = (
                ("Subagent view is read-only", "text"),
                (theme.inline_separator, "separator"),
                ("Alt+↑", "key"),
                (" main", "text"),
                (theme.inline_separator, "separator"),
                ("Alt+←/→", "key"),
                (" switch", "text"),
            )
        if not force and self._placeholder_parts == parts:
            return
        self._placeholder_kind = kind
        self._placeholder_parts = parts
        self._refresh_styled_placeholder()

    def _refresh_styled_placeholder(self) -> None:
        placeholder = self._render_styled_placeholder(self._placeholder_parts)
        # Content equality ignores span styling, so replace the reactive value
        # explicitly before notifying Textual about the visual mutation.
        self.set_reactive(TextArea.placeholder, placeholder)
        self.mutate_reactive(TextArea.placeholder)

    def _render_styled_placeholder(
        self,
        parts: tuple[_PlaceholderPart, ...],
    ) -> Content:
        theme = theme_for(self)
        key_style = Style(
            foreground=Color.parse(theme.accent_bright),
            bold=False,
        )
        lowlight_style = Style(foreground=Color.parse(theme.text_lowlight))
        separator_style = Style(foreground=Color.parse(theme.separator_tone))
        styles = {
            "text": lowlight_style,
            "separator": separator_style,
            "key": key_style,
        }
        return Content.assemble(
            *((text, styles[tone]) for text, tone in parts),
        )

    def refresh_theme(self) -> None:
        if self._placeholder_kind is not None:
            self._set_styled_placeholder(self._placeholder_kind, force=True)

    def render_line(self, y: int) -> Strip:
        if isinstance(self.placeholder, Content) and (
            not self.text or self.has_class("read-only-session")
        ):
            lines = self.placeholder.wrap(self.content_size.width)
            if y < len(lines):
                content = lines[y]
                # Preserve TextArea's native cursor lifecycle while retaining
                # per-span placeholder colors that its uniform style would erase.
                if self._draw_cursor and y == 0:
                    theme = self._theme
                    cursor_style = theme.cursor_style if theme is not None else None
                    if cursor_style is not None:
                        content = content.stylize(
                            Style.from_rich_style(cursor_style),
                            0,
                            1,
                        )
                return Strip(
                    content.render_segments(self.visual_style),
                    content.cell_length,
                )
            if self.has_class("read-only-session"):
                return Strip.blank(self.content_size.width, self.visual_style)
        return super().render_line(y)

    def update_suggestion(self) -> None:
        """Keep the suggestion aligned with the document before it is redrawn."""

        if self._suggestion_resolver is not None:
            self.suggestion = self._suggestion_resolver()

    def observe_command_change(self) -> tuple[str, str] | None:
        """Return the active command fragment and the draft behind it."""

        value = self.text
        previous = self._observed_text
        draft = self._command_draft
        if draft is None and previous and value.endswith(previous):
            inserted = value[: len(value) - len(previous)]
            if inserted.startswith("/") and "\n" not in inserted:
                draft = previous
                self._command_draft = draft
        self._observed_text = value
        return self.command_context()

    def command_context(self) -> tuple[str, str] | None:
        """Return command text separately from any pre-existing draft."""

        draft = self._command_draft
        if draft is not None:
            if self.text.endswith(draft):
                command_text = self.text[: len(self.text) - len(draft)]
                if command_text.startswith("/") and "\n" not in command_text:
                    return command_text, draft
            self._command_draft = None
        if self.text.startswith("/") and "\n" not in self.text:
            return self.text, ""
        return None

    def command_cursor_at_end(self) -> bool:
        """Return whether a suggestion may be inserted at the command boundary."""

        context = self.command_context()
        return bool(
            context is not None
            and self.selection.is_empty
            and self.cursor_location == (0, len(context[0]))
        )

    def on_text_area_selection_changed(self, message: TextArea.SelectionChanged) -> None:
        if message.text_area is self and not self.command_cursor_at_end():
            self.suggestion = ""

    def load_command(self, command_text: str, *, draft: str = "") -> None:
        """Edit a command while keeping an existing user draft intact."""

        value = f"{command_text}{draft}"
        self._command_draft = draft or None
        self._observed_text = value
        self.load_text(value)
        self.move_cursor((0, len(command_text)))

    def restore_command_draft(self, draft: str) -> None:
        """Finish a transient command and restore its original user draft."""

        self._command_draft = None
        self._observed_text = draft
        self.load_text(draft)
        self.move_cursor((self.document.line_count - 1, len(self.document.lines[-1])))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3:
            return
        event.prevent_default()
        event.stop()
        self._right_click_paste_pending = True
        self._right_click_paste_handled = False
        self.focus()

    def on_click(self, event: events.Click) -> None:
        if event.button != 3:
            return
        event.prevent_default()
        event.stop()
        if not self._right_click_paste_pending:
            return
        handled = self._right_click_paste_handled
        self._right_click_paste_pending = False
        if not handled:
            self._paste_system_clipboard()

    def on_paste(self, event: events.Paste) -> None:
        event.prevent_default()
        event.stop()
        if self._right_click_paste_pending:
            if self._right_click_paste_handled:
                return
            self._right_click_paste_handled = True
        self._insert_paste(event.text)

    @work(group="composer-clipboard")
    async def _paste_system_clipboard(self) -> None:
        try:
            text = await asyncio.to_thread(self._clipboard_reader)
        except ClipboardReadError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self._insert_paste(text)

    def _insert_paste(self, text: str) -> None:
        if not text or self.read_only:
            return
        result = self.replace(
            text,
            *self.selection,
            maintain_selection_offset=False,
        )
        self.move_cursor(result.end_location)

    def action_submit(self) -> None:
        self._submit("follow_up")

    def action_submit_steer(self) -> None:
        self._submit("steer")

    def _submit(self, mode: QueuedInputMode) -> None:
        value = self.text.strip()
        if not value:
            return
        context = self.command_context()
        if context is not None:
            command_text, draft = context
            self.post_message(
                self.CommandSubmitted(
                    value,
                    command_text=command_text.strip(),
                    draft=draft,
                )
            )
            return
        self._history.append(value)
        self._history_index = len(self._history)
        self.clear()
        self.post_message(self.Submitted(value, mode))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_cursor_right(self, select: bool = False) -> None:
        if self.suggestion and not self.command_cursor_at_end():
            self.suggestion = ""
        super().action_cursor_right(select)

    def action_history_previous(self) -> None:
        if self.command_context() is not None:
            self.post_message(self.CommandNavigated(-1))
            return
        if "\n" in self.text or self.cursor_location[0] > 0:
            self.action_cursor_up()
            return
        if self._history_index > 0:
            self._history_index -= 1
            self.load_text(self._history[self._history_index])
            self.move_cursor((self.document.line_count - 1, len(self.document.lines[-1])))

    def action_history_next(self) -> None:
        if self.command_context() is not None:
            self.post_message(self.CommandNavigated(1))
            return
        if "\n" in self.text or self.cursor_location[0] < self.document.line_count - 1:
            self.action_cursor_down()
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.load_text(self._history[self._history_index])
        elif self._history_index < len(self._history):
            self._history_index = len(self._history)
            self.clear()

    def action_dismiss_command(self) -> None:
        if self.command_context() is not None:
            self.post_message(self.CommandDismissed())


class StatusLine(Horizontal):
    def __init__(self) -> None:
        super().__init__()
        self._context_metrics: ContextMetrics | None = None
        self._context_width = 0

    def compose(self) -> ComposeResult:
        yield ActivityIndicator()
        yield Static("", id="context-detail")

    def set_activity(
        self,
        activity: str,
        *,
        model_attempt: int | None = None,
        model_max_attempts: int | None = None,
    ) -> None:
        self.query_one(ActivityIndicator).set_activity(
            activity,
            model_attempt=model_attempt,
            model_max_attempts=model_max_attempts,
        )

    def set_model(self, model: str) -> None:
        self.query_one(ActivityIndicator).set_model(model)

    def set_reasoning(self, value: str | None, *, peak: bool = False) -> None:
        self.query_one(ActivityIndicator).set_reasoning(value, peak=peak)

    def set_approval_mode(self, mode: TuiApprovalPreset) -> None:
        self.query_one(ActivityIndicator).set_approval_mode(mode)

    def set_session_controls(self, labels: tuple[str, ...]) -> None:
        self.query_one(ActivityIndicator).set_session_controls(labels)

    def set_subagent(self, agent: AgentTreeNode | None) -> None:
        self.query_one(ActivityIndicator).set_subagent(agent)

    def set_context(self, status: SessionContextStatus | None) -> None:
        self.set_context_metrics(status.metrics if status is not None else None)

    def set_context_metrics(self, metrics: ContextMetrics | None) -> None:
        if metrics == self._context_metrics:
            return
        self._context_metrics = metrics
        self._render_context()

    def refresh_theme(self) -> None:
        self._render_context()

    def _render_context(self) -> None:
        metrics = self._context_metrics
        theme = theme_for(self)
        if metrics is None:
            content = Text()
        else:
            used = metrics.estimated_input_tokens
            budget = metrics.input_budget_tokens
            if budget is None:
                content = Text(format_compact_count(used))
            else:
                ratio = used / budget
                bounded_ratio = min(ratio, 1.0)
                completed = ceil(bounded_ratio * _CONTEXT_PROGRESS_WIDTH)
                content = Text(f"{format_compact_count(used)} ")
                content.append("█" * completed, style=theme.accent)
                content.append(
                    "▒" * (_CONTEXT_PROGRESS_WIDTH - completed),
                    style=theme.progress_track,
                )
                percentage_tone = theme.warning if ratio > 0.8 else theme.text_lowlight
                content.append(f" {ratio:.0%}", style=percentage_tone)
        content_width = content.cell_len
        self.query_one("#context-detail", Static).update(
            content,
            layout=content_width != self._context_width,
        )
        self._context_width = content_width


def user_message_text(message: MessageRecord | ModelMessage) -> str:
    activations = tuple(
        block for block in message.content if isinstance(block, SkillActivationContent)
    )
    if not activations:
        return message.text_content
    task = message.text_content.strip()
    return f"/skill {activations[0].name}{f' {task}' if task else ''}"
