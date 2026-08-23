from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from math import ceil
from time import monotonic
from typing import Literal

from pydantic import JsonValue
from rich.cells import cell_len
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region, Size
from textual.layout import DockArrangeResult
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Label, Static, TextArea

from ouro import __version__
from ouro.application.queued_inputs import (
    QueuedInputMode,
    QueuedUserInputView,
)
from ouro.application.session_models import SessionContextStatus
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.builtin_plugins.plan_mode.interaction import (
    PlanConfirmationAction,
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
)
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.messages import MessageRecord
from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.commands import TuiCommand, TuiCommandArgument
from ouro.tui.controller import TuiApprovalPreset, TuiSystemStatus
from ouro.tui.conversation_history import (
    AssistantMarkdownStream,
    AssistantTailLines,
    ConversationHistory,
    ReasoningLines,
    TranscriptEntry,
    split_markdown_blocks,
)
from ouro.tui.question_forms import QuestionField, QuestionForm
from ouro.tui.state import PendingInteraction, RunPresentation, format_duration
from ouro.tui.text_inputs import TuiTextArea
from ouro.tui.theme import (
    ASCII_SPINNER_FRAMES,
    FAILURE_TONE,
    WELCOME_BULLET,
    WELCOME_SEPARATOR,
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
    "completed": "Ready",
    "aborted": "Aborted",
    "failed": "Failed",
    "paused": "Paused",
    "recoverable": "Recovery required",
}
_CONTEXT_PROGRESS_WIDTH = 11
_MUTED_TONE = "#6f7a87"
_WARNING_TONE = "#968264"
_HEADER_MIN_GAP = 3


def _compact_names(names: tuple[str, ...], *, limit: int = 3) -> str:
    visible = names[:limit]
    summary = ", ".join(visible)
    remaining = len(names) - len(visible)
    if remaining:
        summary += f", +{remaining}"
    return summary


def _compact_count(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            precision = 0 if scaled >= 100 else 1
            return f"{scaled:.{precision}f}{suffix}"
    return str(value)


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
        self._workspace = initial_workspace

    def on_mount(self) -> None:
        self._render_header()

    def on_resize(self, event: events.Resize) -> None:
        self._render_header(width=event.size.width)

    def set_context(
        self,
        *,
        session_title: str | None = None,
        workspace: str | None = None,
    ) -> None:
        if session_title is not None:
            self._session_title = session_title
        if workspace is not None:
            self._workspace = workspace
        self._render_header()

    def refresh_theme(self) -> None:
        self._render_header()

    def _render_header(self, *, width: int | None = None) -> None:
        accent = theme_for(self).accent
        available_width = self.size.width if width is None else width
        self.update(
            _brand_header_content(
                self._session_title,
                self._workspace,
                width=available_width,
                accent=accent,
            )
        )


def _brand_header_content(
    session_title: str,
    workspace: str | None,
    *,
    width: int,
    accent: str,
) -> Text:
    """Lay out the title and right-aligned workspace with workspace priority."""

    content = Text()
    content.append(" ▣ OURO ", style=f"bold {accent}")
    if width <= 0:
        content.append(session_title, style="bold")
        if workspace:
            content.append(" " * _HEADER_MIN_GAP)
            content.append(workspace, style=accent)
        return content

    content.truncate(width, overflow="ellipsis")
    remaining = width - content.cell_len
    if remaining <= 0:
        return content

    if not workspace:
        title = Text(session_title, style="bold")
        title.truncate(remaining, overflow="ellipsis")
        content.append_text(title)
        return content

    workspace_width = cell_len(workspace)
    title_width = remaining - workspace_width - _HEADER_MIN_GAP
    if title_width > 0:
        title = Text(session_title, style="bold")
        title.truncate(title_width, overflow="ellipsis")
        content.append_text(title)
        content.append(" " * (width - content.cell_len - workspace_width))
        content.append(workspace, style=accent)
        return content

    # When both fields cannot fit, omit the title before shortening the workspace.
    workspace_gap = min(_HEADER_MIN_GAP, max(1, remaining - workspace_width))
    workspace_text = Text(workspace, style=accent)
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
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._tick, pause=True)
        self._render_indicator()

    def set_activity(
        self,
        activity: str,
        *,
        model_attempt: int | None = None,
        model_max_attempts: int | None = None,
    ) -> None:
        self._activity = activity
        self._model_attempt = model_attempt
        self._model_max_attempts = model_max_attempts
        if self._timer is not None:
            if activity in _BUSY_ACTIVITIES:
                self._timer.resume()
            else:
                self._timer.pause()
                self._frame = 0
        self._render_indicator()

    def set_model(self, model: str) -> None:
        self._model = model
        self._render_indicator()

    def set_approval_mode(self, mode: TuiApprovalPreset) -> None:
        self._approval_mode = mode
        self._render_indicator()

    def set_reasoning(self, value: str | None, *, peak: bool = False) -> None:
        """Show the effective thinking level beside the model.

        ``peak`` highlights the level when it is the highest effort the current
        endpoint supports, signalling that reasoning is maxed out.
        """
        self._reasoning = value
        self._reasoning_peak = peak
        self._render_indicator()

    def refresh_theme(self) -> None:
        self._render_indicator()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(ASCII_SPINNER_FRAMES)
        self._render_indicator()

    def _render_indicator(self) -> None:
        accent = theme_for(self).accent
        busy = self._activity in _BUSY_ACTIVITIES
        ready = self._activity in {"idle", "completed"}
        mark = ASCII_SPINNER_FRAMES[self._frame] if busy else "▣"
        label = _ACTIVITY_LABELS.get(self._activity, self._activity.replace("_", " ").title())
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
            style=f"bold {accent}" if busy or ready else "#8f9aa8",
        )
        content.append(label, style=accent if ready else "#8f9aa8")
        content.append(" • ", style="#4d5560")
        approval_style = "#b97878" if self._approval_mode in {"auto", "afk"} else "#647180"
        content.append(self._approval_mode, style=approval_style)
        content.append(" • ", style="#4d5560")
        content.append(self._model, style="#647180")
        if self._reasoning:
            style = f"bold {accent}" if self._reasoning_peak else "#647180"
            content.append(" • ", style="#4d5560")
            content.append(self._reasoning, style=style)
        self.update(content)


class ReasoningLabel(Label):
    class ToggleRequested(Message):
        pass

    def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            self.post_message(self.ToggleRequested())
            event.stop()


class ReasoningView(Vertical):
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
            _completed_reasoning_label(None),
            classes="reasoning-label",
        )
        self._timer: Timer | None = None
        self._elapsed_at_start = 0.0
        self._clock_started_at: float | None = None
        self._completed_seconds: float | None = None
        self._content = ReasoningLines()
        self._content.set_class(not expanded, "collapsed")

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
        self._content.set_class(not expanded, "collapsed")

    async def append(self, text: str, *, started_at: datetime | None = None) -> None:
        if not text:
            return
        self.display = True
        if self._clock_started_at is None:
            now = datetime.now(UTC)
            self._elapsed_at_start = (
                max(0.0, (now - started_at).total_seconds()) if started_at is not None else 0.0
            )
            self._clock_started_at = monotonic()
            self._timer = self.set_interval(0.1, self._tick)
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
            self.label.update(_completed_reasoning_label(elapsed))
        self._stop_timer()

    def _tick(self) -> None:
        self._render_live_label()

    def _render_live_label(self) -> None:
        self.label.update(f"THINKING · {self._elapsed_seconds():.1f}s")

    def _elapsed_seconds(self) -> float:
        if self._clock_started_at is None:
            return self._elapsed_at_start
        return self._elapsed_at_start + monotonic() - self._clock_started_at

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._clock_started_at = None


class AssistantTurn(Vertical):
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

    def feed(self, text: str) -> tuple[str, ...]:
        """Accumulate streamed markdown; return blocks ready to freeze."""
        if not text:
            return ()
        self.reasoning.finish()
        blocks, tail_reset = self._stream.append(text)
        if tail_reset:
            tail = self._stream.tail
            self.tail.display = bool(tail)
            self.tail.replace(tail)
        else:
            self.tail.display = True
            self.tail.append(text)
        return blocks

    def close(self) -> tuple[str, ...]:
        """Freeze the trailing block along with the round."""
        blocks = self._stream.close()
        self.tail.replace("")
        self.tail.display = False
        return blocks

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


def _completed_reasoning_label(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return "THOUGHT"
    return f"THOUGHT · {format_duration(duration_seconds)}"


class ToolActivityRow(Static):
    def __init__(self, tool: RuntimeToolCallActivity) -> None:
        super().__init__(classes="tool-row")
        self.tool_call_id = tool.tool_call_id
        self._tool = tool
        self._frame = 0
        self._timer: Timer | None = None
        self.apply_tool(tool)

    def on_mount(self) -> None:
        if self._is_active:
            self._timer = self.set_interval(0.1, self._tick)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def apply_tool(self, tool: RuntimeToolCallActivity) -> None:
        self._tool = tool
        self._sync_timer()
        self._render_tool()

    @property
    def tool(self) -> RuntimeToolCallActivity:
        return self._tool

    def _sync_timer(self) -> None:
        if not self.is_attached:
            return
        if self._is_active and self._timer is None:
            self._timer = self.set_interval(0.1, self._tick)
        elif not self._is_active and self._timer is not None:
            self._timer.stop()
            self._timer = None

    @property
    def _is_active(self) -> bool:
        return self._tool.status in {
            "planned",
            "ready",
            "resolving_input",
            "running",
            "waiting_child",
        }

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(ASCII_SPINNER_FRAMES)
        self._render_tool()

    def refresh_theme(self) -> None:
        self._render_tool()

    def _render_tool(self) -> None:
        self.update(_tool_activity_text(self._tool, frame=self._frame, theme=theme_for(self)))


def _tool_activity_text(
    tool: RuntimeToolCallActivity,
    *,
    frame: int,
    theme: TuiTheme,
) -> Text:
    """Render one tool state without coupling it to a mounted widget."""

    active = tool.status in {
        "planned",
        "ready",
        "resolving_input",
        "running",
        "waiting_child",
    }
    marks = {
        "awaiting_approval": "?",
        "waiting_user": "?",
        "completed": "✓",
        "failed": "×",
        "rejected": "×",
        "aborted": "×",
        "unknown_outcome": "!",
    }
    mark = ASCII_SPINNER_FRAMES[frame] if active else marks.get(tool.status, "·")
    state_styles = {
        "completed": "#77b083",
        "failed": FAILURE_TONE,
        "rejected": FAILURE_TONE,
        "aborted": FAILURE_TONE,
        "unknown_outcome": theme.accent,
        "awaiting_approval": theme.accent,
        "waiting_user": theme.accent,
    }
    status_labels = {
        "planned": "queued",
        "ready": "ready",
        "awaiting_approval": "approval required",
        "waiting_user": "waiting for input",
        "resolving_input": "resolving input",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "rejected": "rejected",
        "aborted": "aborted",
        "unknown_outcome": "outcome unknown",
        "waiting_child": "waiting for subagent",
    }
    accent = state_styles.get(tool.status, theme.accent if active else "#8f9aa8")
    presentation = present_tool(tool)
    text = Text()
    text.append(f"  {mark} ", style=f"bold {accent}")
    text.append(tool.tool_name, style="bold #d6d9df")
    if presentation.subject:
        text.append(f"  {presentation.subject}", style="#8f9aa8")
    if presentation.summary:
        text.append(f"  ·  {presentation.summary}", style="#788492")
    text.append(f"  {status_labels[tool.status]}", style=accent)
    elapsed = _tool_elapsed_seconds(tool)
    if elapsed is not None:
        text.append(f" · {elapsed}s", style="#647180")
    line_styles: dict[ToolLineState, str] = {
        "pending": "#788492",
        "active": theme.accent,
        "done": "#77b083",
        "muted": "#647180",
    }
    part_styles: dict[ToolTextTone, str] = {
        "default": "#8f9aa8",
        "muted": "#647180",
        "added": "#77b083",
        "removed": "#d87575",
    }
    for line in presentation.lines:
        text.append(f"\n    {line.marker} ", style=line_styles[line.state])
        for part in line.parts:
            style = part_styles[part.tone]
            if line.state == "done" and part.tone == "default":
                style = "#788492"
            text.append(part.text, style=style)
    return text


def _is_terminal_tool(tool: RuntimeToolCallActivity) -> bool:
    return tool.status in {
        "completed",
        "failed",
        "rejected",
        "aborted",
        "unknown_outcome",
    }


def _tool_elapsed_seconds(tool: RuntimeToolCallActivity) -> int | None:
    if tool.started_at is None:
        return None
    ended_at = tool.ended_at or datetime.now(UTC)
    return max(0, int((ended_at - tool.started_at).total_seconds()))


_INPUT_TIPS = (
    ("/", "Commands"),
    ("Ctrl+P", "Actions"),
    ("Ctrl+S", "Sessions"),
)


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


def _usage_block_colors(values: tuple[int, ...], accent: str) -> tuple[str, ...]:
    """Map daily token totals onto a theme-derived dark-to-accent scale."""

    darkest_ratio = 0.28
    if not values:
        return (_blend_hex("#0c0c0c", accent, darkest_ratio),)
    lowest = min(values)
    highest = max(values)
    if lowest == highest:
        return tuple(accent for _ in values)
    return tuple(
        _blend_hex(
            "#0c0c0c",
            accent,
            darkest_ratio
            + (1.0 - darkest_ratio) * ((value - lowest) / (highest - lowest)),
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
        self._mcp_label = Static("MCP", id="welcome-mcp-label", classes="welcome-label")
        self._mcp = Static(classes="welcome-value")
        self._version = Static(f"v{__version__}", classes="welcome-version")
        self._render_values()

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Static("SYSTEM", classes="welcome-title welcome-label"),
            self._version,
            classes="welcome-system-heading",
        )
        for label, value in (
            ("Workspace", self._workspace),
            ("Providers", self._providers),
            ("Skills", self._skills),
            ("Plugins", self._plugins),
            ("MCP", self._mcp),
        ):
            yield Horizontal(
                self._mcp_label if label == "MCP" else Static(label, classes="welcome-label"),
                value,
                classes="welcome-row",
            )

    def on_mount(self) -> None:
        self._sync_mcp_spinner()

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._sync_mcp_spinner()
        self._render_values()

    def _sync_mcp_spinner(self) -> None:
        if self._status.mcp_connecting_count:
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(
                    0.12,
                    self._advance_mcp_spinner,
                )
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._spinner_frame = 0

    def _advance_mcp_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(ASCII_SPINNER_FRAMES)
        self._render_mcp_label()

    def _render_mcp_label(self) -> None:
        label = Text()
        if self._status.mcp_connecting_count:
            label.append(
                f"{ASCII_SPINNER_FRAMES[self._spinner_frame]} ",
                style=f"bold {theme_for(self).accent}",
            )
        label.append("MCP")
        self._mcp_label.update(label)

    def _render_values(self) -> None:
        status = self._status
        self._render_mcp_label()
        self._workspace.update(Text(status.workspace or "not set", style="#d6d9df"))

        providers = Text(_compact_names(status.provider_ids) or "none configured", style="#d6d9df")
        providers.append(f"{WELCOME_SEPARATOR}{status.model_count} model(s)", style="#8f9aa8")
        self._providers.update(providers)

        skills = Text(f"{status.skill_count} available", style="#8f9aa8")
        if status.skill_names:
            skills.append(
                f"{WELCOME_SEPARATOR}{_compact_names(status.skill_names)}",
                style="#647180",
            )
        self._skills.update(skills)

        plugins = Text(
            f"{status.plugin_active_count}/{status.plugin_total_count} active",
            style="#8f9aa8",
        )
        if status.plugin_active_names:
            plugins.append(
                f"{WELCOME_SEPARATOR}{_compact_names(status.plugin_active_names)}",
                style="#647180",
            )
        if status.plugin_issue_count:
            plugins.append(
                f"{WELCOME_SEPARATOR}{status.plugin_issue_count} need attention",
                style="#e08a8a",
            )
        self._plugins.update(plugins)

        mcp = Text()
        if status.mcp_total_count:
            mcp.append(
                f"{status.mcp_connected_count}/{status.mcp_total_count} connected",
                style=theme_for(self).accent,
            )
            mcp.append(f"{WELCOME_SEPARATOR}{status.mcp_tool_count} tool(s)", style="#8f9aa8")
            if status.mcp_connecting_count:
                mcp.append(
                    f"{WELCOME_SEPARATOR}{status.mcp_connecting_count} connecting",
                    style=theme_for(self).accent,
                )
            if status.mcp_failed_count:
                mcp.append(
                    f"{WELCOME_SEPARATOR}{status.mcp_failed_count} failed",
                    style="#e08a8a",
                )
        else:
            mcp.append("not configured", style="#647180")
        self._mcp.update(mcp)

    def refresh_theme(self) -> None:
        self._render_values()


class _WelcomeUsageBlock(Vertical):
    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome-section welcome-usage")
        self._status = status or TuiSystemStatus()
        self._chart = Static(classes="welcome-usage-line")
        self._requests = Static(classes="welcome-usage-line")
        self._tokens = Static(classes="welcome-usage-line")
        self._render_values()

    def compose(self) -> ComposeResult:
        yield Static("USAGE", classes="welcome-title")
        yield self._chart
        yield self._requests
        yield self._tokens

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._render_values()

    def refresh_theme(self) -> None:
        self._render_values()

    def _render_values(self) -> None:
        status = self._status
        accent = theme_for(self).accent
        daily_tokens = status.usage_daily_tokens
        chart = Text()
        for color in _usage_block_colors(daily_tokens, accent):
            chart.append("■", style=color)
        if daily_tokens:
            days = len(daily_tokens)
            chart.append(
                f"{WELCOME_SEPARATOR}last {days} day{'s' if days != 1 else ''}",
                style="#647180",
            )
        else:
            chart.append(f"{WELCOME_SEPARATOR}no records", style="#647180")
        self._chart.update(chart)

        requests = Text(f"{status.usage_request_count} request(s)", style="#8f9aa8")
        if status.usage_priced_request_count:
            requests.append(
                f"{WELCOME_SEPARATOR}${status.usage_estimated_cost:.4f}",
                style="#647180",
            )
        self._requests.update(requests)
        self._tokens.update(
            Text(
                f"{_compact_count(status.usage_input_tokens)} in"
                f" / {_compact_count(status.usage_output_tokens)} out",
                style="#647180",
            )
        )


class _WelcomeTipsBlock(Vertical):
    def __init__(self) -> None:
        super().__init__(classes="welcome-section welcome-tips")

    def compose(self) -> ComposeResult:
        yield Static(f"{WELCOME_BULLET} TIPS", classes="welcome-title")
        for key, description in _INPUT_TIPS:
            yield Horizontal(
                Static(key, classes="welcome-tip-key"),
                Static(description, classes="welcome-tip-description"),
                classes="welcome-row",
            )


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
            self._lower,
            self._system,
            classes="welcome-only welcome-bottom",
        )
        self._composer = Composer()
        self._composer.display = False
        self._render_logo()

    def compose(self) -> ComposeResult:
        yield self._top
        yield CommandPopup()
        yield AskUserPanel()
        yield PlanConfirmationPanel()
        yield InteractionBar()
        yield self._composer
        yield QueuedInputPanel()
        yield StatusLine()
        yield self._bottom

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._system.set_system_status(status)
        self._usage.set_system_status(status)

    def on_mount(self) -> None:
        self.refresh_theme()

    def set_welcome_mode(self, enabled: bool) -> None:
        self.set_class(enabled, "welcome-page")

    def refresh_theme(self) -> None:
        self._render_logo()
        self._system.refresh_theme()
        self._usage.refresh_theme()

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
                        "ended_at": message.created_at,
                    }
                )
    return activities


def _compaction_notice(compaction: CompactionRecord | int) -> str:
    covered = (
        len(compaction.covered_message_ids)
        if isinstance(compaction, CompactionRecord)
        else compaction
    )
    noun = "message" if covered == 1 else "messages"
    return f"Context compacted · {covered} {noun} summarized"


class ConversationView(VerticalScroll):
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
        self._transcript = Vertical(self._history, id="conversation-transcript")
        self._system_status = TuiSystemStatus()
        self._showing_welcome = False
        self._assistant: AssistantTurn | None = None
        self._tools: dict[str, ToolActivityRow] = {}
        self._current_run_id: str | None = None
        self._presented_run_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        yield self._transcript

    def text_select_all(self) -> None:
        """Keep range selection without selecting the entire conversation."""

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Resolve the scroll position from the viewport used by this layout pass.

        Dynamic siblings change the conversation's height. Scroll correction must use
        this pass's ``size`` so child placement and scrolling reach the screen as one
        coherent frame. A later resize callback is already one frame too late.
        """

        arrangement = super().arrange(size, optimal)
        if not optimal:
            max_scroll_y = max(0, arrangement.total_region.bottom - size.height)
            scroll_y = max_scroll_y if self._follow_tail else self.scroll_y
            self.set_scroll(None, scroll_y)
            self.scroll_target_y = scroll_y
        return arrangement

    def anchor(self, anchor: bool = True) -> None:
        self._follow_tail = anchor
        if anchor:
            self.scroll_end(immediate=True, animate=False)

    def release_anchor(self) -> None:
        self._follow_tail = False
        self.scroll_target_y = self.scroll_y

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

    async def load_messages(
        self,
        messages: Iterable[MessageRecord],
        compactions: Iterable[CompactionRecord] = (),
        runs: Iterable[RunPresentation] = (),
    ) -> None:
        await self._clear_live_transcript()
        self._assistant = None
        self._tools.clear()
        self._showing_welcome = False
        self._current_run_id = None
        self._presented_run_ids.clear()

        # Canonical history contains both autonomous dialogue and harness
        # scaffolding. Presentation provenance is explicit on each message;
        # role, semantic tags, and producer source must not be used to infer it.
        materialized = [message for message in messages if not message.scaffold]
        run_presentations = tuple(runs)
        requests = {
            request.message_id: request for run in run_presentations for request in run.requests
        }
        tool_activities = _tool_activities_from_messages(materialized)
        chronological: list[tuple[int, int, TranscriptEntry]] = []
        message_positions = {
            message.message_id: index for index, message in enumerate(materialized)
        }

        def append_entry(
            message_index: int,
            entry: TranscriptEntry,
        ) -> None:
            chronological.append((message_index * 3, len(chronological), entry))

        for message_index, message in enumerate(materialized):
            if message.role == "user":
                text = user_message_text(message)
                if not text:
                    continue
                append_entry(message_index, TranscriptEntry(kind="user", text=text))
            elif message.role == "assistant":
                text = message.text_content
                request = requests.get(message.message_id)
                if message.reasoning_content:
                    append_entry(
                        message_index,
                        TranscriptEntry(
                            kind="assistant",
                            reasoning=message.reasoning_content,
                            run_id=message.run_id,
                            reasoning_seconds=(
                                request.reasoning_seconds if request is not None else None
                            ),
                        ),
                    )
                # Reloaded messages split into the same top-level blocks that
                # streaming froze, so live and reloaded rendering stay identical.
                for block in split_markdown_blocks(text):
                    append_entry(
                        message_index,
                        TranscriptEntry(
                            kind="assistant",
                            text=block,
                            run_id=message.run_id,
                        ),
                    )
                for call in message.tool_calls:
                    activity = tool_activities.get(call.id)
                    if activity is None:
                        continue
                    append_entry(
                        message_index,
                        TranscriptEntry(kind="tool", tool=activity),
                    )

        last_message_by_run: dict[str, int] = {}
        for message_index, message in enumerate(materialized):
            if message.run_id is not None:
                last_message_by_run[message.run_id] = message_index
        for run in run_presentations:
            boundary_index = last_message_by_run.get(run.run_id)
            if boundary_index is None or run.duration_seconds is None:
                continue
            chronological.append(
                (
                    boundary_index * 3 + 1,
                    len(chronological),
                    TranscriptEntry(kind="run", run_id=run.run_id, run=run),
                )
            )
            self._presented_run_ids.add(run.run_id)

        for compaction in compactions:
            boundary_index = message_positions.get(compaction.history_boundary_message_id)
            if boundary_index is None:
                raise ValueError(
                    f"compaction {compaction.compaction_id} references missing history "
                    f"boundary {compaction.history_boundary_message_id}"
                )
            chronological.append(
                (
                    boundary_index * 3 + 2,
                    len(chronological),
                    TranscriptEntry(kind="notice", text=_compaction_notice(compaction)),
                )
            )
        transcript = [entry for *_ordering, entry in sorted(chronological)]

        if not transcript:
            # A loaded session with no visible dialogue is still an empty state.
            self._history.replace(())
            self._set_showing_welcome(True)
            self.anchor()
            return

        self._set_showing_welcome(False)
        await self._history.load(transcript)
        self.anchor()

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

    async def add_user(self, text: str) -> None:
        await self._dismiss_welcome()
        # A new user turn closes the preceding model round. Persist the live
        # assistant before appending the next chronological history boundary.
        await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="user", text=text))
        await self._history.wait_until_rendered()

    async def add_user_message(self, message: MessageRecord) -> None:
        if message.role != "user" or message.scaffold:
            return
        text = user_message_text(message)
        if text:
            await self.add_user(text)

    async def append_assistant(self, text: str) -> None:
        await self._dismiss_welcome()
        turn = await self._ensure_turn()
        for block in turn.feed(text):
            await self._freeze_block(turn, block)

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
            self._assistant = AssistantTurn(
                run_id=self._current_run_id,
                after_tool=self._history_ends_with_tool,
                reasoning_expanded=self._reasoning_expanded,
            )
            await self._transcript.mount(self._assistant)
        return self._assistant

    async def _freeze_block(self, turn: AssistantTurn, block: str) -> None:
        """Freeze one completed block, promoting pending reasoning ahead of it."""
        if not block.strip():
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
        self._history.append(TranscriptEntry(kind="assistant", text=block, run_id=turn.run_id))

    async def finish_assistant(self) -> None:
        """Freeze the live round into the immutable history."""
        turn = self._assistant
        if turn is None:
            return
        for block in turn.close():
            await self._freeze_block(turn, block)
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
        historical_tool_ids = self._history.update_tools(tools)
        has_new_tool = any(
            tool.tool_call_id not in historical_tool_ids and tool.tool_call_id not in self._tools
            for tool in tools
        )
        if has_new_tool:
            # A tool call is a chronological boundary between model rounds.
            # Freeze the preceding assistant segment before rendering the tool.
            await self.finish_assistant()
        for tool in tools:
            if tool.tool_call_id in historical_tool_ids:
                continue
            row = self._tools.get(tool.tool_call_id)
            if row is None:
                row = ToolActivityRow(tool)
                self._tools[tool.tool_call_id] = row
                await self._transcript.mount(row)
            else:
                row.apply_tool(tool)

        # Keep a tool batch live until every row is terminal. Archiving rows one
        # by one would move a later-completing row ahead of an earlier active row
        # because the immutable history always precedes the live widget region.
        if self._tools and all(_is_terminal_tool(row.tool) for row in self._tools.values()):
            terminal_rows = tuple(self._tools.values())
            self._tools.clear()
            for row in terminal_rows:
                self._history.append(TranscriptEntry(kind="tool", tool=row.tool))
                await row.remove()

    async def add_notice(self, message: str, *, error: bool = False) -> None:
        await self._dismiss_welcome()
        # Notices are immutable transcript boundaries as well. Freezing any
        # partial answer keeps presentation order identical to event order.
        await self.finish_assistant()
        self._history.append(TranscriptEntry(kind="notice", text=message, error=error))
        await self._history.wait_until_rendered()

    async def add_compaction(self, *, covered_message_count: int) -> None:
        await self.add_notice(_compaction_notice(covered_message_count))


class InteractionBar(Static):
    """Keyboard-first approval choice with an explicit selected option."""

    can_focus = True
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
        Binding("left", "previous_option", show=False),
        Binding("right", "next_option", show=False),
        Binding("enter", "accept", "Choose", show=False),
        Binding("y", "approve", "Allow", show=False),
        Binding("shift+y", "approve", "Allow", show=False),
        Binding("n", "reject", "Deny", show=False),
        Binding("shift+n", "reject", "Deny", show=False),
    ]

    class Decision(Message):
        def __init__(self, interaction: PendingInteraction, approved: bool) -> None:
            super().__init__()
            self.interaction = interaction
            self.approved = approved

    def __init__(self) -> None:
        super().__init__(id="interaction-bar")
        self.display = False
        self.disabled = True
        self._interaction: PendingInteraction | None = None
        self._identity: tuple[str, str] | None = None
        self._selected = 0

    @property
    def selected_approved(self) -> bool:
        return self._selected == 0

    def show_interaction(self, interaction: PendingInteraction | None) -> None:
        with self.app.batch_update():
            if interaction is None or interaction.kind != "approval":
                self.display = False
                self._interaction = None
                self._identity = None
                self._selected = 0
                return
            identity = (interaction.run_id, interaction.request_id or interaction.tool_call_id)
            if identity != self._identity:
                self._selected = 0
            self._interaction = interaction
            self._identity = identity
            self.disabled = False
            self.display = True
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

    def action_previous_option(self) -> None:
        self._move_selection(-1)

    def action_next_option(self) -> None:
        self._move_selection(1)

    def action_accept(self) -> None:
        self._decide(self.selected_approved)

    def action_approve(self) -> None:
        self._selected = 0
        self._render_approval()
        self._decide(True)

    def action_reject(self) -> None:
        self._selected = 1
        self._render_approval()
        self._decide(False)

    def _move_selection(self, offset: int) -> None:
        if self._interaction is None or self.disabled:
            return
        self._selected = (self._selected + offset) % 2
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
        content = Text()
        content.append("? APPROVAL\n", style=f"bold {theme.accent_bright}")
        if interaction.is_subagent:
            content.append(
                f"Subagent {interaction.agent_name or interaction.delegation_id}\n",
                style="#6f7a87",
            )
        content.append(f"{interaction.tool_name} wants to run\n\n", style="bold #d6d9df")
        options = (
            ("Allow", "Run this tool", "#77b083"),
            ("Deny", "Reject this tool", "#d87575"),
        )
        for index, (label, description, tone) in enumerate(options):
            active = index == self._selected
            content.append("› " if active else "  ", style=theme.accent if active else "")
            content.append(
                f"{index + 1}. {label}",
                style=f"bold {tone}" if active else "bold #d6d9df",
            )
            content.append(
                f"  {description}\n",
                style="#d6d9df" if active else "#6f7a87",
            )
        content.append("\n↑/↓ or ←/→ move · Enter choose · Y/N direct", style="#6f7a87")
        self.update(content)


class AskUserPanel(Static):
    """Keyboard-first renderer for a Host-projected question form."""

    can_focus = True
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
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
            self._render_question()
            self.focus()
            if not self._question.options:
                self._request_freeform()

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
            self._question_index += 1
            self._cursor = 0
            self._selected.clear()
            self._awaiting_freeform = False
            self._render_question()
            self.focus()
            if not self._question.options:
                self._request_freeform()
            return
        answer = self._request.encode(self._answers)
        self.disabled = True
        self.post_message(self.Completed(answer))

    def _render_question(self) -> None:
        request = self._request
        if request is None:
            return
        question = self._question
        total = len(request.questions)
        theme = theme_for(self)
        content = Text()
        content.append(
            f"? {question.header.upper()}  {self._question_index + 1}/{total}\n",
            style=f"bold {theme.accent_bright}",
        )
        content.append(f"{question.prompt}\n\n", style="bold #d6d9df")
        for index, option in enumerate(question.options):
            active = index == self._cursor
            checked = index in self._selected
            if question.multi_select:
                marker = "[x]" if checked else "[ ]"
            else:
                marker = "●" if active else "○"
            content.append("› " if active else "  ", style=theme.accent if active else "")
            content.append(
                f"{index + 1}. {marker} {option.label}",
                style="bold #d6d9df" if active else "",
            )
            content.append(
                f"  {option.description}\n",
                style="#d6d9df" if active else "#6f7a87",
            )
        other_index = len(question.options)
        other_active = self._cursor == other_index
        content.append("› " if other_active else "  ", style=theme.accent if other_active else "")
        content.append(
            f"{other_index + 1}. Other…",
            style=f"bold {theme.accent_bright}" if other_active else "bold #d6d9df",
        )
        hint = (
            "\n\n↑/↓ move · Space toggle · Enter continue"
            if question.multi_select
            else "\n\n↑/↓ move · Enter choose"
        )
        content.append(hint, style="#6f7a87")
        self.update(content)

    def refresh_theme(self) -> None:
        self._render_question()


class PlanConfirmationPanel(Vertical):
    """Keyboard-first decision workflow for the builtin ExitPlanMode request."""

    can_focus = True
    can_focus_children = False
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
        Binding("left", "previous_option", show=False),
        Binding("right", "next_option", show=False),
        Binding("pageup", "scroll_plan_up", show=False),
        Binding("pagedown", "scroll_plan_down", show=False),
        Binding("enter", "accept", show=False),
        Binding("escape", "back", show=False),
        Binding("1", "choose(0)", show=False),
        Binding("2", "choose(1)", show=False),
        Binding("3", "choose(2)", show=False),
    ]

    class Completed(Message):
        def __init__(self, answer: JsonValue) -> None:
            super().__init__()
            self.answer = answer

    class FeedbackRequested(Message):
        pass

    def __init__(self) -> None:
        super().__init__(id="plan-confirmation-panel")
        self.display = False
        self.disabled = True
        self._title = Static(id="plan-confirmation-title")
        self._plan_body = Static(id="plan-confirmation-body")
        self._document = VerticalScroll(
            self._plan_body,
            id="plan-confirmation-document",
            can_focus=False,
        )
        self._controls = Static(id="plan-confirmation-controls")
        self._request: PlanConfirmationRequest | None = None
        self._stage: Literal["action", "option", "feedback"] = "action"
        self._cursor = 0
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

    def action_previous_option(self) -> None:
        self._move_cursor(-1)

    def action_next_option(self) -> None:
        self._move_cursor(1)

    def action_scroll_plan_up(self) -> None:
        self._document.scroll_page_up(animate=False)

    def action_scroll_plan_down(self) -> None:
        self._document.scroll_page_down(animate=False)

    def action_choose(self, index: int) -> None:
        if index < 0 or index >= len(self._choices()):
            return
        self._cursor = index
        self.action_accept()

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

    def _move_cursor(self, offset: int) -> None:
        choices = self._choices()
        if not choices or self.disabled or self._stage == "feedback":
            return
        self._cursor = (self._cursor + offset) % len(choices)
        assert self._request is not None
        self._render_controls(self._request)

    def _choices(self) -> tuple[object, ...]:
        if self._request is None:
            return ()
        if self._stage == "action":
            return self._request.actions
        if self._stage == "option":
            return self._request.options
        return ()

    def _complete(self, answer: PlanConfirmationAnswer) -> None:
        self.disabled = True
        self.post_message(self.Completed(answer.to_json()))

    def _render_panel(self) -> None:
        request = self._request
        if request is None:
            return
        with self.app.batch_update():
            theme = theme_for(self)
            title = Text("? PLAN APPROVAL", style=f"bold {theme.accent_bright}")
            self._title.update(title)
            self._plan_body.update(Text(request.plan, style="#d6d9df"))
            self._render_controls(request)

    def _render_controls(self, request: PlanConfirmationRequest) -> None:
        theme = theme_for(self)
        content = Text()
        if request.error:
            content.append(f"{request.error}\n\n", style="#b97878")
        if self._stage == "action":
            content.append("Choose how Plan mode should continue\n\n", style="#aeb5bf")
            presentations = {
                "accept": ("Accept", "Approve the plan and exit Plan mode", "#77b083"),
                "revise": ("Revise", "Keep planning and provide feedback", theme.accent),
                "reject": ("Reject", "Reject the plan and keep Plan mode active", "#b97878"),
            }
            for index, action in enumerate(request.actions):
                label, description, tone = presentations[action]
                self._append_option(
                    content,
                    index=index,
                    label=label,
                    description=description,
                    tone=tone,
                )
            content.append(
                "\nPgUp/PgDn plan · ↑/↓ or ←/→ move · Enter choose · 1-3 direct",
                style="#6f7a87",
            )
        elif self._stage == "option":
            content.append("Choose an implementation approach\n\n", style="#aeb5bf")
            for index, option in enumerate(request.options):
                self._append_option(
                    content,
                    index=index,
                    label=option.label,
                    description=option.description,
                    tone=theme.accent,
                )
            content.append(
                "\nPgUp/PgDn plan · ↑/↓ or ←/→ move · Enter choose · 1-3 direct · Esc back",
                style="#6f7a87",
            )
        else:
            content.append("REVISION FEEDBACK\n", style=f"bold {theme.accent_bright}")
            content.append(
                "Type the requested plan changes below, then press Enter",
                style="#6f7a87",
            )
        self._controls.update(content)

    def _append_option(
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
            f"{index + 1}. {label}",
            style=f"bold {tone}" if active else "bold #d6d9df",
        )
        if description:
            content.append(
                f"  {description}",
                style="#d6d9df" if active else "#6f7a87",
            )
        content.append("\n")


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
            self._content.update(Text("  No matching commands", style="#8f9aa8"))
            return
        theme = theme_for(self)
        name_width = max(len(command.name) for command in self._commands)
        rows: list[Text] = []
        for index, command in enumerate(self._commands):
            selected = index == self._selected
            row = Text(style="on #242424" if selected else "")
            row.append("› " if selected else "  ", style=theme.accent if selected else "")
            row.append(
                command.name.ljust(name_width),
                style=theme.accent_bright if selected else "#d6d9df",
            )
            row.append(
                f"  {command.title}",
                style="#d6d9df" if selected else "#6f7a87",
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
            content.append(command.title, style="#d6d9df")
            self._content.update(content)
            return
        value_width = max(len(argument.display_label) for argument in self._arguments)
        rows: list[Text] = []
        for index, argument in enumerate(self._arguments):
            selected = index == self._selected
            row = Text(style="on #242424" if selected else "")
            row.append("› " if selected else "  ", style=theme.accent if selected else "")
            row.append(
                argument.display_label.ljust(value_width),
                style=theme.accent_bright if selected else "#d6d9df",
            )
            row.append(
                f"  {argument.description}",
                style="#aeb5bf" if selected else "#6f7a87",
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
                label = "FOLLOW" if item.mode == "follow_up" else "GUIDE"
                tone = theme.accent if item.mode == "follow_up" else theme.accent_bright
                prompt = " ↵ ".join(line.strip() for line in item.prompt.splitlines())
                if item.skill_name is not None:
                    prompt = f"/skill {item.skill_name}{f' {prompt}' if prompt else ''}"
                content.append(f"{index}. {label:<6} ", style=f"bold {tone}")
                content.append(prompt, style="#d6d9df")
                if index < len(inputs):
                    content.append("\n")
            self._content.update(content)
            self.scroll_end(animate=False)


class Composer(TuiTextArea):
    DEFAULT_PLACEHOLDER = "Ask anything..."
    ACTIVE_RUN_PLACEHOLDER = "Enter queues a follow-up · Ctrl+Enter guides the active run…"
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
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

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

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3:
            return
        event.prevent_default()
        event.stop()
        self.focus()
        self._paste_system_clipboard()

    @work(group="composer-clipboard")
    async def _paste_system_clipboard(self) -> None:
        try:
            text = await asyncio.to_thread(self._clipboard_reader)
        except ClipboardReadError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
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
        if value.startswith("/"):
            self.post_message(self.CommandSubmitted(value))
            return
        self._history.append(value)
        self._history_index = len(self._history)
        self.clear()
        self.post_message(self.Submitted(value, mode))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_history_previous(self) -> None:
        if self.text.startswith("/") and "\n" not in self.text:
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
        if self.text.startswith("/") and "\n" not in self.text:
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
        if self.text.startswith("/"):
            self.post_message(self.CommandDismissed())


class StatusLine(Horizontal):
    def __init__(self) -> None:
        super().__init__()
        self._context_metrics: ContextMetrics | None = None

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

    def set_context(self, status: SessionContextStatus | None) -> None:
        self.set_context_metrics(status.metrics if status is not None else None)

    def set_context_metrics(self, metrics: ContextMetrics | None) -> None:
        self._context_metrics = metrics
        self._render_context()

    def refresh_theme(self) -> None:
        self._render_context()

    def _render_context(self) -> None:
        metrics = self._context_metrics
        if metrics is None:
            content = Text()
        else:
            used = metrics.estimated_input_tokens
            budget = metrics.input_budget_tokens
            if budget is None:
                content = Text(_compact_count(used))
            else:
                ratio = used / budget
                bounded_ratio = min(ratio, 1.0)
                completed = ceil(bounded_ratio * _CONTEXT_PROGRESS_WIDTH)
                content = Text(f"{_compact_count(used)} ")
                content.append("█" * completed, style=theme_for(self).accent)
                content.append(
                    "▒" * (_CONTEXT_PROGRESS_WIDTH - completed),
                    style="#34373d",
                )
                percentage_tone = _WARNING_TONE if ratio > 0.8 else _MUTED_TONE
                content.append(f" {ratio:.0%}", style=percentage_tone)
        self.query_one("#context-detail", Static).update(content)


def user_message_text(message: MessageRecord) -> str:
    activations = message.skill_activations
    if not activations:
        return message.text_content
    task = message.text_content.strip()
    return f"/skill {activations[0].name}{f' {task}' if task else ''}"
