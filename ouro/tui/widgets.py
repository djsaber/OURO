from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import JsonValue
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.geometry import Region
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Label, Markdown, Static, TextArea

from ouro.application.queued_inputs import (
    QueuedInputMode,
    QueuedUserInputView,
)
from ouro.application.session_models import SessionContextStatus
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.core.messages import MessageRecord
from ouro.tui.commands import TuiCommand, TuiCommandArgument
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.question_forms import QuestionField, QuestionForm
from ouro.tui.state import PendingInteraction

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
    def __init__(self) -> None:
        super().__init__(id="brand-header")
        self._frame = 0
        self._activity = "idle"
        self._model = "starting"
        self._session_title = "New session"
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._tick, pause=True)
        self._render_header()

    def set_context(self, *, model: str | None = None, session_title: str | None = None) -> None:
        if model is not None:
            self._model = model
        if session_title is not None:
            self._session_title = session_title
        self._render_header()

    def set_activity(self, activity: str) -> None:
        self._activity = activity
        if self._timer is not None:
            if activity in _BUSY_ACTIVITIES:
                self._timer.resume()
            else:
                self._timer.pause()
                self._frame = 0
        self._render_header()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._render_header()

    def _render_header(self) -> None:
        busy = self._activity in _BUSY_ACTIVITIES
        mark = _SPINNER[self._frame] if busy else "◆"
        label = _ACTIVITY_LABELS.get(self._activity, self._activity.replace("_", " ").title())
        content = Text()
        content.append(f" {mark} OURO ", style="bold #d9a441")
        content.append(f" {self._session_title}", style="bold")
        content.append(f"  {label}", style="#8f9aa8")
        content.append(f"  {self._model} ", style="#647180")
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
        self.text = initial
        self._content = Static(initial, classes="reasoning-content")

    def compose(self) -> ComposeResult:
        yield self.label
        yield self._content

    async def append(self, text: str) -> None:
        if not text:
            return
        self.display = True
        self.label.update("THINKING")
        self.text += text
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
        yield Label("OURO", classes="turn-label assistant-label")
        yield self.reasoning
        yield self.markdown

    def on_mount(self) -> None:
        self._stream = Markdown.get_stream(self.markdown)

    async def on_unmount(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    async def append(self, text: str) -> None:
        if not text:
            return
        self.reasoning.finish()
        if self._stream is None:
            await self.markdown.append(text)
            return
        await self._stream.write(text)

    async def append_reasoning(self, text: str) -> None:
        await self.reasoning.append(text)

    def finish_reasoning(self) -> None:
        self.reasoning.finish()


class ToolActivityRow(Static):
    def __init__(self, tool: RuntimeToolCallActivity) -> None:
        super().__init__(classes="tool-row")
        self.tool_call_id = tool.tool_call_id
        self.apply_tool(tool)

    def apply_tool(self, tool: RuntimeToolCallActivity) -> None:
        marks = {
            "planned": "·",
            "ready": "·",
            "awaiting_approval": "?",
            "waiting_user": "?",
            "resolving_input": "◌",
            "running": "◌",
            "completed": "✓",
            "failed": "×",
            "rejected": "×",
            "aborted": "×",
            "unknown_outcome": "!",
            "waiting_child": "◌",
        }
        styles = {
            "completed": "#77b083",
            "failed": "#d87575",
            "rejected": "#d87575",
            "aborted": "#d87575",
            "awaiting_approval": "#d9a441",
            "waiting_user": "#d9a441",
        }
        text = Text()
        text.append(f"  {marks.get(tool.status, '·')} ", style=styles.get(tool.status, "#8f9aa8"))
        text.append(tool.tool_name, style="bold")
        text.append(f"  {tool.status.replace('_', ' ')}", style="#647180")
        self.update(text)


class WelcomePanel(Static):
    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome")
        self._status = status or TuiSystemStatus()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._render_panel()

    def on_mount(self) -> None:
        self._render_panel()

    def _render_panel(self) -> None:
        status = self._status
        content = Text()
        content.append(_OURO_LOGO, style="bold #d9a441")
        content.append("\n\n")
        content.append(
            "Tell me what you want to accomplish. Use ",
            style="#aeb5bf",
        )
        content.append("/", style="bold")
        content.append(" for commands, ", style="#aeb5bf")
        content.append("Ctrl+P", style="bold")
        content.append(" for actions, and ", style="#aeb5bf")
        content.append("Ctrl+S", style="bold")
        content.append(" for sessions.\n\n", style="#aeb5bf")
        content.append("SYSTEM\n", style="bold #788492")

        content.append("Workspace ", style="bold #788492")
        content.append(status.workspace or "not set", style="#d6d9df")
        content.append("\n")
        provider_names = _compact_names(status.provider_ids) or "none configured"
        content.append("Models   ", style="bold #788492")
        content.append(provider_names, style="#d6d9df")
        content.append(
            f" · {status.model_count} model(s)\n",
            style="#8f9aa8",
        )
        content.append("Skills   ", style="bold #788492")
        content.append(f"{status.skill_count} available", style="#8f9aa8")
        if status.skill_names:
            content.append(f" · {_compact_names(status.skill_names)}", style="#647180")
        content.append("\n")
        content.append("Plugins  ", style="bold #788492")
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
        content.append("MCP      ", style="bold #788492")
        if status.mcp_total_count:
            content.append(
                f"{status.mcp_connected_count}/{status.mcp_total_count} connected"
                f" · {status.mcp_tool_count} tool(s)",
                style="#8f9aa8",
            )
            if status.mcp_failed_count:
                content.append(
                    f" · {status.mcp_failed_count} failed",
                    style="#e08a8a",
                )
        else:
            content.append("not configured", style="#647180")
        content.append("\n")
        content.append("Usage    ", style="bold #788492")
        content.append(f"{status.usage_request_count} request(s)", style="#8f9aa8")
        content.append(
            f" · {_compact_count(status.usage_input_tokens)} in"
            f" / {_compact_count(status.usage_output_tokens)} out",
            style="#647180",
        )
        if status.usage_priced_request_count:
            content.append(f" · ${status.usage_estimated_cost:.4f}", style="#647180")
        content.append("\n")
        content.append("Health   ", style="bold #788492")
        if status.startup_diagnostic_count:
            content.append(
                f"{status.startup_diagnostic_count} startup diagnostic(s)",
                style="#e08a8a",
            )
        else:
            content.append("Ready", style="#77b083")
        self.update(content)


class ConversationView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="conversation")
        self._system_status = TuiSystemStatus()
        self._welcome: WelcomePanel | None = None
        self._assistant: AssistantTurn | None = None
        self._tools: dict[str, ToolActivityRow] = {}

    async def show_welcome(self) -> None:
        await self.remove_children()
        self._welcome = None
        self._assistant = None
        self._tools.clear()
        await self._mount_welcome()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._system_status = status
        if self._welcome is not None:
            self._welcome.set_system_status(status)

    async def _mount_welcome(self) -> None:
        self._welcome = WelcomePanel(self._system_status)
        await self.mount(self._welcome)

    async def load_messages(self, messages: Iterable[MessageRecord]) -> None:
        await self.remove_children()
        self._welcome = None
        self._assistant = None
        self._tools.clear()
        await self._mount_welcome()
        for message in messages:
            text = message.text_content
            if message.role not in {"user", "assistant"}:
                continue
            if message.role == "user":
                if not text:
                    continue
                await self.add_user(text)
            else:
                if not text and not message.reasoning_content:
                    continue
                turn = AssistantTurn(
                    initial=text,
                    reasoning=message.reasoning_content,
                )
                await self.mount(turn)
        self.scroll_end(animate=False)

    async def add_user(self, text: str) -> None:
        self._assistant = None
        await self.mount(
            Static(
                Text.assemble(("YOU\n", "bold #6ca7d9"), text),
                classes="turn user-turn",
            )
        )
        self.scroll_end(animate=False)

    async def append_assistant(self, text: str) -> None:
        if self._assistant is None:
            self._assistant = AssistantTurn()
            await self.mount(self._assistant)
        await self._assistant.append(text)
        self.scroll_end(animate=False)

    async def append_reasoning(self, text: str) -> None:
        if self._assistant is None:
            self._assistant = AssistantTurn()
            await self.mount(self._assistant)
        await self._assistant.append_reasoning(text)
        self.scroll_end(animate=False)

    def finish_reasoning(self) -> None:
        if self._assistant is not None:
            self._assistant.finish_reasoning()

    async def sync_tools(self, tools: tuple[RuntimeToolCallActivity, ...]) -> None:
        mounted = False
        for tool in tools:
            row = self._tools.get(tool.tool_call_id)
            if row is None:
                row = ToolActivityRow(tool)
                self._tools[tool.tool_call_id] = row
                await self.mount(row)
                mounted = True
            else:
                row.apply_tool(tool)
        if mounted:
            # A tool call is a chronological boundary between model rounds.
            # Later model output must mount after the tool rows, not mutate the
            # assistant segment that preceded them.
            self._assistant = None
        self.scroll_end(animate=False)

    async def add_notice(self, message: str, *, error: bool = False) -> None:
        classes = "notice error-notice" if error else "notice"
        await self.mount(Static(message, classes=classes))
        self.scroll_end(animate=False)


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
        content = Text()
        content.append("? APPROVAL\n", style="bold #d9a441")
        if interaction.is_subagent:
            content.append(
                f"Subagent {interaction.agent_name or interaction.delegation_id}\n",
                style="#6f7a87",
            )
        content.append(f"{interaction.tool_name} wants to run\n\n", style="bold")
        options = (
            ("Allow", "Run this tool", "#77b083"),
            ("Deny", "Reject this tool", "#d87575"),
        )
        for index, (label, description, tone) in enumerate(options):
            active = index == self._selected
            content.append("› " if active else "  ", style="#d9a441" if active else "")
            content.append(f"{index + 1}. {label}", style=f"bold {tone}" if active else "bold")
            content.append(f"  {description}\n", style="#aeb5bf" if active else "#6f7a87")
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
        content = Text()
        content.append(
            f"? {question.header.upper()}  {self._question_index + 1}/{total}\n",
            style="bold #d9a441",
        )
        content.append(f"{question.prompt}\n\n", style="bold")
        for index, option in enumerate(question.options):
            active = index == self._cursor
            checked = index in self._selected
            if question.multi_select:
                marker = "[x]" if checked else "[ ]"
            else:
                marker = "●" if active else "○"
            content.append("› " if active else "  ", style="#d9a441" if active else "")
            content.append(f"{index + 1}. {marker} {option.label}", style="bold" if active else "")
            content.append(f"  {option.description}\n", style="#aeb5bf" if active else "#6f7a87")
        other_index = len(question.options)
        other_active = self._cursor == other_index
        content.append("› " if other_active else "  ", style="#d9a441" if other_active else "")
        content.append(
            f"{other_index + 1}. Other…",
            style="bold #d9a441" if other_active else "bold",
        )
        hint = (
            "\n\n↑/↓ move · Space toggle · Enter continue"
            if question.multi_select
            else "\n\n↑/↓ move · Enter choose"
        )
        content.append(hint, style="#6f7a87")
        self.update(content)


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

    def _render_commands(self) -> None:
        if not self._commands:
            self._content.update(Text("  No matching commands", style="#8f9aa8"))
            return
        rows: list[Text] = []
        for index, command in enumerate(self._commands):
            selected = index == self._selected
            row = Text()
            row.append("› " if selected else "  ", style="#d9a441" if selected else "")
            row.append(command.usage, style="bold #d9a441" if selected else "bold")
            row.append(
                f"  {command.description}",
                style="#aeb5bf" if selected else "#6f7a87",
            )
            rows.append(row)
        self._render_rows(tuple(rows))

    def _render_arguments(self) -> None:
        command = self._argument_command
        if command is None:
            return
        if not self._arguments:
            content = Text()
            content.append("› ", style="#d9a441")
            content.append(command.argument_hint or "argument", style="bold #7f8996")
            content.append(f"  {command.description}", style="#6f7a87")
            self._content.update(content)
            return
        rows: list[Text] = []
        for index, argument in enumerate(self._arguments):
            selected = index == self._selected
            row = Text()
            row.append("› " if selected else "  ", style="#d9a441" if selected else "")
            row.append(argument.usage, style="bold #d9a441" if selected else "bold")
            row.append(
                f"  {argument.description}",
                style="#aeb5bf" if selected else "#6f7a87",
            )
            rows.append(row)
        self._render_rows(tuple(rows))

    def _render_rows(self, rows: tuple[Text, ...]) -> None:
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

    def compose(self) -> ComposeResult:
        yield self._content

    def set_inputs(self, inputs: tuple[QueuedUserInputView, ...]) -> None:
        if not inputs:
            self.display = False
            self._content.update("")
            return
        content = Text()
        content.append(f"QUEUED INPUTS  {len(inputs)}\n", style="bold #788492")
        for index, item in enumerate(inputs, start=1):
            label = "FOLLOW" if item.mode == "follow_up" else "GUIDE"
            tone = "#6ca7d9" if item.mode == "follow_up" else "#d9a441"
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

    def __init__(self) -> None:
        super().__init__(
            id="composer",
            placeholder="Message OURO…",
            soft_wrap=True,
            show_line_numbers=False,
            compact=True,
        )
        self._history: list[str] = []
        self._history_index = 0

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
        yield Static(
            "Enter follow · Ctrl+Enter guide · Shift+Enter newline",
            id="key-hints",
        )
        yield Static("Context —", id="context-detail")
        yield Static("", id="status-detail")

    def set_detail(self, detail: str) -> None:
        self.query_one("#status-detail", Static).update(detail)

    def set_context(self, status: SessionContextStatus | None) -> None:
        if status is None:
            label = "Context —"
        else:
            metrics = status.metrics
            reported = status.provider_reported_input_tokens
            used = reported if reported is not None else metrics.estimated_input_tokens
            prefix = "" if reported is not None else "~"
            budget = metrics.input_budget_tokens
            if budget is None:
                label = f"Context {prefix}{_compact_count(used)}"
            else:
                ratio = used / budget
                label = (
                    f"Context {prefix}{_compact_count(used)}/{_compact_count(budget)} · {ratio:.0%}"
                )
        self.query_one("#context-detail", Static).update(label)
