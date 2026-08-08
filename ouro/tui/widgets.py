from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.geometry import Region
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Label, Markdown, Static, TextArea

from ouro.application.queued_inputs import (
    QueuedInputMode,
    QueuedUserInputView,
)
from ouro.application.session_models import SessionContextStatus
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.core.messages import MessageRecord
from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.commands import TuiCommand, TuiCommandArgument
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.question_forms import QuestionField, QuestionForm
from ouro.tui.state import PendingInteraction
from ouro.tui.theme import theme_for
from ouro.tui.tool_presentations import ToolLineState, present_tool

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

_OURO_LOGO = """
 ██████╗ ▓▓╗   ▓▓╗██████╗  ▓▓▓▓▓▓╗ 
██╔═══██╗▓▓║   ▓▓║██╔══██╗▓▓╔═══▓▓╗
██║   ██║▓▓║   ▓▓║██████╔╝▓▓║   ▓▓║
██║   ██║▓▓║   ▓▓║██╔══██╗▓▓║   ▓▓║
╚██████╔╝╚▓▓▓▓▓▓╔╝██║  ██║╚▓▓▓▓▓▓╔╝
 ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝
"""
_BUSY_ACTIVITIES = {
    "starting",
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


class _MarkdownWriter(Protocol):
    async def write(self, markdown_fragment: str) -> None: ...

    async def stop(self) -> None: ...


class BrandHeader(Static):
    """Static brand identity and session title docked at the top of the screen."""

    def __init__(self) -> None:
        super().__init__(id="brand-header")
        self._session_title = "New session"

    def on_mount(self) -> None:
        self._render_header()

    def set_context(self, *, session_title: str | None = None) -> None:
        if session_title is not None:
            self._session_title = session_title
        self._render_header()

    def refresh_theme(self) -> None:
        self._render_header()

    def _render_header(self) -> None:
        accent = theme_for(self).accent
        content = Text()
        content.append(" ▣ OURO ", style=f"bold {accent}")
        content.append(self._session_title, style="bold")
        self.update(content)


class ActivityIndicator(Static):
    """Animated activity and model status shown beside the composer."""

    def __init__(self) -> None:
        super().__init__(id="activity-indicator")
        self._frame = 0
        self._activity = "idle"
        self._model = "starting"
        self._reasoning: str | None = None
        self._reasoning_peak = False
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._tick, pause=True)
        self._render_indicator()

    def set_activity(self, activity: str) -> None:
        self._activity = activity
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
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._render_indicator()

    def _render_indicator(self) -> None:
        accent = theme_for(self).accent
        busy = self._activity in _BUSY_ACTIVITIES
        ready = self._activity in {"idle", "completed"}
        mark = _SPINNER[self._frame] if busy else "▣"
        label = _ACTIVITY_LABELS.get(self._activity, self._activity.replace("_", " ").title())
        content = Text()
        content.append(
            f" {mark} ",
            style=f"bold {accent}" if busy or ready else "#8f9aa8",
        )
        content.append(label, style=accent if ready else "#8f9aa8")
        content.append(f"  {self._model}", style="#647180")
        if self._reasoning:
            style = f"bold {accent}" if self._reasoning_peak else "#647180"
            content.append(f"  {self._reasoning}", style=style)
        self.update(content)


class ReasoningView(Vertical):
    """A plain-text reasoning stream with presentation state independent from the answer.

    Thinking output is shown verbatim rather than parsed as Markdown, so code fences and
    markup inside reasoning are never reformatted into blocks or syntax highlighting.
    """

    def __init__(self, *, initial: str = "") -> None:
        super().__init__(classes="reasoning-view")
        self.display = bool(initial)
        self.label = Label("THOUGHT", classes="reasoning-label")
        self._text_parts = [initial] if initial else []
        self._text_cache: str | None = initial
        self._content = Static(
            initial,
            classes="reasoning-content",
            markup=False,
        )

    def compose(self) -> ComposeResult:
        yield self.label
        yield self._content

    @property
    def text(self) -> str:
        if self._text_cache is None:
            self._text_cache = "".join(self._text_parts)
        return self._text_cache

    async def append(self, text: str) -> None:
        if not text:
            return
        self.display = True
        self.label.update("THINKING")
        self._text_parts.append(text)
        self._text_cache = None
        self._content.update(self.text)

    def finish(self) -> None:
        if self.display:
            self.label.update("THOUGHT")


class AssistantTurn(Vertical):
    def __init__(self, *, initial: str = "", reasoning: str = "") -> None:
        super().__init__(classes="turn assistant-turn")
        self.reasoning = ReasoningView(initial=reasoning)
        self.markdown = Markdown(initial, classes="assistant-markdown")
        self._stream: _MarkdownWriter | None = None

    def compose(self) -> ComposeResult:
        yield self.reasoning
        yield self.markdown

    async def on_unmount(self) -> None:
        await self.finish()

    async def append(self, text: str) -> None:
        if not text:
            return
        self.reasoning.finish()
        if self._stream is None:
            self._stream = Markdown.get_stream(self.markdown)
        await self._stream.write(text)

    async def finish(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    async def append_reasoning(self, text: str) -> None:
        await self.reasoning.append(text)

    def finish_reasoning(self) -> None:
        self.reasoning.finish()


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
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._render_tool()

    def refresh_theme(self) -> None:
        self._render_tool()

    def _render_tool(self) -> None:
        theme = theme_for(self)
        tool = self._tool
        active = self._is_active
        marks = {
            "awaiting_approval": "?",
            "waiting_user": "?",
            "completed": "✓",
            "failed": "×",
            "rejected": "×",
            "aborted": "×",
            "unknown_outcome": "!",
        }
        mark = _SPINNER[self._frame] if active else marks.get(tool.status, "·")
        state_styles = {
            "completed": "#77b083",
            "failed": "#d87575",
            "rejected": "#d87575",
            "aborted": "#d87575",
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
        for line in presentation.lines:
            text.append(f"\n    {line.marker} ", style=line_styles[line.state])
            text.append(line.text, style="#8f9aa8" if line.state != "done" else "#788492")
        self.update(text)


def _tool_elapsed_seconds(tool: RuntimeToolCallActivity) -> int | None:
    if tool.started_at is None:
        return None
    ended_at = tool.ended_at or datetime.now(UTC)
    return max(0, int((ended_at - tool.started_at).total_seconds()))


_SYSTEM_LABEL_WIDTH = 9
_TIP_KEY_WIDTH = 11
_INPUT_TIPS = (
    ("Enter", "Send a follow-up message"),
    ("Ctrl+Enter", "Guide the active run"),
    ("Shift+Enter", "Insert a new line"),
    ("/", "Commands"),
    ("Ctrl+P", "Actions"),
    ("Ctrl+S", "Sessions"),
)


class WelcomePanel(Static):
    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome")
        self._status = status or TuiSystemStatus()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._render_panel()

    def on_mount(self) -> None:
        self._render_panel()

    def refresh_theme(self) -> None:
        self._render_panel()

    def _render_panel(self) -> None:
        status = self._status
        accent = theme_for(self).accent
        content = Text()
        content.append(_OURO_LOGO, style=f"bold {accent}")
        content.append("\n\n")
        content.append("Tell me what you want to accomplish.", style="#aeb5bf")
        content.append("\n\n")
        content.append("TIPS\n", style="bold #788492")
        for key, description in _INPUT_TIPS:
            content.append(f"{key.ljust(_TIP_KEY_WIDTH)}  ", style="bold #788492")
            content.append(f"{description}\n", style="#8f9aa8")
        content.append("\n")
        content.append("SYSTEM\n", style="bold #788492")

        content.append(f"{'Workspace'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        content.append(status.workspace or "not set", style="#d6d9df")
        content.append("\n")
        provider_names = _compact_names(status.provider_ids) or "none configured"
        content.append(f"{'Models'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        content.append(provider_names, style="#d6d9df")
        content.append(
            f" · {status.model_count} model(s)\n",
            style="#8f9aa8",
        )
        content.append(f"{'Skills'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        content.append(f"{status.skill_count} available", style="#8f9aa8")
        if status.skill_names:
            content.append(f" · {_compact_names(status.skill_names)}", style="#647180")
        content.append("\n")
        content.append(f"{'Plugins'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        content.append(
            f"{status.plugin_active_count}/{status.plugin_total_count} active",
            style="#8f9aa8",
        )
        if status.plugin_active_names:
            content.append(
                f" · {_compact_names(status.plugin_active_names)}",
                style="#647180",
            )
        if status.plugin_issue_count:
            content.append(
                f" · {status.plugin_issue_count} need attention",
                style="#e08a8a",
            )
        content.append("\n")
        content.append(f"{'MCP'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        if status.mcp_total_count:
            content.append(
                f"{status.mcp_connected_count}/{status.mcp_total_count} connected",
                style=accent,
            )
            content.append(f" · {status.mcp_tool_count} tool(s)", style="#8f9aa8")
            if status.mcp_connecting_count:
                content.append(
                    f" · {status.mcp_connecting_count} connecting",
                    style=accent,
                )
            if status.mcp_failed_count:
                content.append(
                    f" · {status.mcp_failed_count} failed",
                    style="#e08a8a",
                )
        else:
            content.append("not configured", style="#647180")
        content.append("\n")
        content.append(f"{'Usage'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        content.append(f"{status.usage_request_count} request(s)", style="#8f9aa8")
        content.append(
            f" · {_compact_count(status.usage_input_tokens)} in"
            f" / {_compact_count(status.usage_output_tokens)} out",
            style="#647180",
        )
        if status.usage_priced_request_count:
            content.append(f" · ${status.usage_estimated_cost:.4f}", style="#647180")
        content.append("\n")
        content.append(f"{'Health'.ljust(_SYSTEM_LABEL_WIDTH)} ", style="bold #788492")
        if status.startup_diagnostic_count:
            content.append(
                f"{status.startup_diagnostic_count} startup diagnostic(s)",
                style="#e08a8a",
            )
        else:
            content.append("Ready", style="#77b083")
        self.update(content)


def _user_turn(text: str) -> Static:
    return Static(
        text,
        classes="turn user-turn",
        markup=False,
    )


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


class ConversationView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="conversation")
        self._transcript = Vertical(id="conversation-transcript")
        self._system_status = TuiSystemStatus()
        self._welcome: WelcomePanel | None = None
        self._assistant: AssistantTurn | None = None
        self._tools: dict[str, ToolActivityRow] = {}

    def compose(self) -> ComposeResult:
        yield self._transcript

    def on_mount(self) -> None:
        self.anchor()

    async def show_welcome(self) -> None:
        await self._transcript.remove_children()
        self._welcome = None
        self._assistant = None
        self._tools.clear()
        await self._mount_welcome()
        self.anchor()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._system_status = status
        if self._welcome is not None:
            self._welcome.set_system_status(status)

    async def _mount_welcome(self) -> None:
        self._welcome = WelcomePanel(self._system_status)
        await self._transcript.mount(self._welcome)

    async def load_messages(self, messages: Iterable[MessageRecord]) -> None:
        await self._transcript.remove_children()
        self._assistant = None
        self._tools.clear()
        self._welcome = None

        materialized = list(messages)
        tool_activities = _tool_activities_from_messages(materialized)
        transcript: list[Widget] = []

        for message in materialized:
            if message.role == "user":
                text = message.text_content
                if not text:
                    continue
                transcript.append(_user_turn(text))
            elif message.role == "assistant":
                text = message.text_content
                if text or message.reasoning_content:
                    transcript.append(
                        AssistantTurn(
                            initial=text,
                            reasoning=message.reasoning_content,
                        )
                    )
                for call in message.tool_calls:
                    activity = tool_activities.get(call.id)
                    if activity is None:
                        continue
                    row = ToolActivityRow(activity)
                    self._tools[call.id] = row
                    transcript.append(row)

        if not transcript:
            # A loaded session with no visible dialogue is still an empty state.
            await self._mount_welcome()
            self.anchor()
            return

        await self._transcript.mount_all(transcript)
        self.anchor()

    async def _dismiss_welcome(self) -> None:
        """Transition from the empty welcome state to the conversation state.

        The welcome panel is an empty-state placeholder shown only while the
        conversation has no content. As soon as any dialogue content arrives —
        a user message, an assistant turn, a tool call, or a notice — the
        welcome panel yields to the conversation, so the LOGO welcome frame
        stays visible only at startup and when a new session begins.
        """
        if self._welcome is None:
            return
        welcome = self._welcome
        self._welcome = None
        await welcome.remove()

    async def add_user(self, text: str) -> None:
        await self._dismiss_welcome()
        self._assistant = None
        await self._transcript.mount(_user_turn(text))

    async def append_assistant(self, text: str) -> None:
        await self._dismiss_welcome()
        if self._assistant is None:
            self._assistant = AssistantTurn()
            await self._transcript.mount(self._assistant)
        await self._assistant.append(text)

    async def append_reasoning(self, text: str) -> None:
        await self._dismiss_welcome()
        if self._assistant is None:
            self._assistant = AssistantTurn()
            await self._transcript.mount(self._assistant)
        await self._assistant.append_reasoning(text)

    def finish_reasoning(self) -> None:
        if self._assistant is not None:
            self._assistant.finish_reasoning()

    async def finish_assistant(self) -> None:
        if self._assistant is not None:
            await self._assistant.finish()

    async def sync_tools(self, tools: tuple[RuntimeToolCallActivity, ...]) -> None:
        await self._dismiss_welcome()
        mounted = False
        for tool in tools:
            row = self._tools.get(tool.tool_call_id)
            if row is None:
                row = ToolActivityRow(tool)
                self._tools[tool.tool_call_id] = row
                await self._transcript.mount(row)
                mounted = True
            else:
                row.apply_tool(tool)
        if mounted:
            # A tool call is a chronological boundary between model rounds.
            # Later model output must mount after the tool rows, not mutate the
            # assistant segment that preceded them.
            await self.finish_assistant()
            self._assistant = None

    async def add_notice(self, message: str, *, error: bool = False) -> None:
        await self._dismiss_welcome()
        classes = "notice error-notice" if error else "notice"
        await self._transcript.mount(Static(message, classes=classes))


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
        self._interaction: PendingInteraction | None = None
        self._identity: tuple[str, str] | None = None
        self._selected = 0

    @property
    def selected_approved(self) -> bool:
        return self._selected == 0

    def show_interaction(self, interaction: PendingInteraction | None) -> None:
        if interaction is None or interaction.kind != "approval":
            self.display = False
            self.disabled = False
            self._interaction = None
            self._identity = None
            self._selected = 0
            self.update("")
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
        self.display = False
        self.disabled = False
        self._request = None
        self._awaiting_freeform = False
        self.update("")

    def resume(self) -> None:
        if self._request is not None:
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


class CommandPopup(VerticalScroll):
    """Composer-controlled popup whose keyboard navigation owns its hidden scrolling."""

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

    def on_resize(self, _: Resize) -> None:
        width = self.scrollable_content_region.width
        if width > 0 and self._content.content_region.width != width:
            self._content.styles.width = width

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
        selected_name = self.selected_command.name if self.selected_command is not None else None
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
        self.display = False
        self._commands = ()
        self._argument_command = None
        self._arguments = ()
        self._selected = 0
        self._content.update("")

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
                style=f"bold {theme.accent_bright}" if selected else "bold #d6d9df",
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
            content.append(command.title, style="bold #d6d9df")
            self._content.update(content)
            return
        value_width = max(len(argument.value) for argument in self._arguments)
        rows: list[Text] = []
        for index, argument in enumerate(self._arguments):
            selected = index == self._selected
            row = Text(style="on #242424" if selected else "")
            row.append("› " if selected else "  ", style=theme.accent if selected else "")
            row.append(
                argument.value.ljust(value_width),
                style=f"bold {theme.accent_bright}" if selected else "bold #d6d9df",
            )
            row.append(
                f"  {argument.description}",
                style="#aeb5bf" if selected else "#6f7a87",
            )
            rows.append(row)
        self._render_rows(tuple(rows))

    def _render_rows(self, rows: tuple[Text, ...]) -> None:
        width = self.scrollable_content_region.width
        if width > 0:
            for row in rows:
                row.truncate(width, overflow="ellipsis", pad=True)
        self._content.update(Text("\n").join(rows))
        self.call_after_refresh(self._scroll_selected_into_view, rows)

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

    def refresh_theme(self) -> None:
        self.set_inputs(self._inputs)

    def set_inputs(self, inputs: tuple[QueuedUserInputView, ...]) -> None:
        self._inputs = inputs
        if not inputs:
            self.display = False
            self._content.update("")
            return
        theme = theme_for(self)
        content = Text()
        content.append(f"QUEUED INPUTS  {len(inputs)}\n", style="bold #788492")
        for index, item in enumerate(inputs, start=1):
            label = "FOLLOW" if item.mode == "follow_up" else "GUIDE"
            tone = theme.accent if item.mode == "follow_up" else theme.accent_bright
            prompt = " ↵ ".join(line.strip() for line in item.prompt.splitlines())
            content.append(f"{index}. {label:<6} ", style=f"bold {tone}")
            content.append(prompt, style="#d6d9df")
            if index < len(inputs):
                content.append("\n")
        self._content.update(content)
        self.display = True
        self.scroll_end(animate=False)


class Composer(TextArea):
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
            placeholder="Ask anything…",
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

    def on_blur(self) -> None:
        if self.text.startswith("/"):
            self.post_message(self.CommandDismissed())


class StatusLine(Horizontal):
    def compose(self) -> ComposeResult:
        yield ActivityIndicator()
        yield Static("Context —", id="context-detail")
        yield Static("", id="status-detail")

    def set_activity(self, activity: str) -> None:
        self.query_one(ActivityIndicator).set_activity(activity)

    def set_model(self, model: str) -> None:
        self.query_one(ActivityIndicator).set_model(model)

    def set_reasoning(self, value: str | None, *, peak: bool = False) -> None:
        self.query_one(ActivityIndicator).set_reasoning(value, peak=peak)

    def set_detail(self, detail: str) -> None:
        self.query_one("#status-detail", Static).update(detail)

    def set_context(self, status: SessionContextStatus | None) -> None:
        if status is None:
            label = "Context —"
        else:
            metrics = status.metrics
            used = metrics.estimated_input_tokens
            budget = metrics.input_budget_tokens
            if budget is None:
                label = f"Context ~{_compact_count(used)}"
            else:
                ratio = used / budget
                label = (
                    f"Context ~{_compact_count(used)}/{_compact_count(budget)} · {ratio:.0%}"
                )
        self.query_one("#context-detail", Static).update(label)
