"""Tool call activity and result presentation."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.cells import get_character_cell_size
from rich.text import Text
from textual import events
from textual.timer import Timer
from textual.widgets import Static

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.tui.conversation.messages import ToolNavigationRequested
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.state import format_duration
from ouro.tui.status import (
    _ACTIVITY_LABELS,
    _FAILED_SUBAGENT_STATUSES,
    _subagent_execution_status,
    _subagent_task_status,
)
from ouro.tui.text_wrapping import _SINGLE_LINE_TRANSLATION
from ouro.tui.theme import TuiTheme, label_text, theme_for
from ouro.tui.tool_presentations import ToolLineState, ToolTextTone, present_tool

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


_TOOL_ARGUMENT_STREAM_WINDOW_CELLS = 48


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
        return self._tool.status in _ACTIVE_TOOL_STATUSES

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

    if tool.status == "streaming":
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(
            f"{theme.spinner_frames[frame % len(theme.spinner_frames)]} ",
            style=theme.accent,
        )
        text.append(tool.tool_name, style=theme.text_lowlight)
        if tool.argument_stream_tail is not None:
            text.append(
                theme.inline_separator,
                style=theme.text_lowlight,
            )
            text.append(
                _tool_argument_stream_window(tool.argument_stream_tail),
                style=theme.text_lowlight,
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
        label_text(title, theme=theme, style=theme.failure if failed else theme.text_lowlight)
    )
    if presentation.summary:
        text.append(
            f" {theme.inline_separator}{presentation.summary}",
            style=theme.text_lowlight,
        )
    if subagent is not None:
        status_label = _subagent_task_status(subagent)
        text.append(
            f"  {status_label}",
            style=(theme.failure if status_label == "failed" else theme.text_lowlight),
        )
    elif is_spawn_agent:
        status_label = _spawn_agent_preview_status(tool)
        text.append(
            f"  {status_label}",
            style=(theme.failure if status_label == "failed" else theme.text_lowlight),
        )
    elapsed = _tool_elapsed_seconds(tool)
    elapsed_label = format_duration(elapsed) if elapsed is not None else None
    if elapsed_label is not None and elapsed_label != "0ms":
        text.append(
            f"{theme.inline_separator}{elapsed_label}",
            style=theme.text_lowlight,
        )
    line_styles: dict[ToolLineState, str] = {
        "pending": theme.text_lowlight,
        "active": theme.accent,
        "done": theme.accent,
        "muted": theme.text_lowlight,
    }
    part_styles: dict[ToolTextTone, str] = {
        "default": theme.text_lowlight,
        "muted": theme.text_lowlight,
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
        text = theme.highlight_text(text)
    return text


def _tool_argument_stream_window(value: str) -> str:
    """Return the rightmost argument cells in a fixed-width sliding window."""

    value = value.translate(_SINGLE_LINE_TRANSLATION)
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


def is_terminal_tool(tool: RuntimeToolCallActivity) -> bool:
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
