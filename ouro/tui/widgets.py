from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import JsonValue
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Label, Markdown, Static, TextArea

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.core.messages import MessageRecord
from ouro.tui.commands import TuiCommand
from ouro.tui.question_forms import QuestionField, QuestionForm
from ouro.tui.state import PendingInteraction, question_text

_SPINNER = ("◌", "◔", "◑", "◕")
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


class AssistantTurn(Vertical):
    def __init__(self, *, initial: str = "") -> None:
        super().__init__(classes="turn assistant-turn")
        self._initial = initial
        self.markdown = Markdown(initial, classes="assistant-markdown")
        self._stream: _MarkdownWriter | None = None

    def compose(self) -> ComposeResult:
        yield Label("OURO", classes="turn-label assistant-label")
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
        if self._stream is None:
            await self.markdown.append(text)
            return
        await self._stream.write(text)

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


class ConversationView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="conversation")
        self._assistant: AssistantTurn | None = None
        self._tools: dict[str, ToolActivityRow] = {}

    async def show_welcome(self) -> None:
        await self.remove_children()
        self._assistant = None
        self._tools.clear()
        await self.mount(
            Static(
                "[bold #d9a441]◆ OURO[/]\n\n"
                "Tell me what you want to accomplish. Use [bold]/[/] for commands, "
                "[bold]Ctrl+P[/] for actions, and [bold]Ctrl+S[/] for sessions.",
                classes="welcome",
                markup=True,
            )
        )

    async def load_messages(self, messages: Iterable[MessageRecord]) -> None:
        await self.remove_children()
        self._assistant = None
        self._tools.clear()
        for message in messages:
            text = message.text_content
            if not text or message.role not in {"user", "assistant"}:
                continue
            if message.role == "user":
                await self.add_user(text)
            else:
                turn = AssistantTurn(initial=text)
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

    async def sync_tools(self, tools: tuple[RuntimeToolCallActivity, ...]) -> None:
        for tool in tools:
            row = self._tools.get(tool.tool_call_id)
            if row is None:
                row = ToolActivityRow(tool)
                self._tools[tool.tool_call_id] = row
                await self.mount(row)
            else:
                row.apply_tool(tool)
        self.scroll_end(animate=False)

    async def add_notice(self, message: str, *, error: bool = False) -> None:
        classes = "notice error-notice" if error else "notice"
        await self.mount(Static(message, classes=classes))
        self.scroll_end(animate=False)


class InteractionBar(Static):
    can_focus = True
    BINDINGS = [
        Binding("y", "approve", "Allow", show=False),
        Binding("n", "reject", "Deny", show=False),
    ]

    class Decision(Message):
        def __init__(self, approved: bool) -> None:
            super().__init__()
            self.approved = approved

    def __init__(self) -> None:
        super().__init__(id="interaction-bar")
        self.display = False

    def show_interaction(self, interaction: PendingInteraction | None) -> None:
        if interaction is None:
            self.display = False
            self.update("")
            return
        self.display = True
        if interaction.kind == "approval":
            self.update(
                Text.assemble(
                    ("? APPROVAL  ", "bold #d9a441"),
                    (interaction.tool_name, "bold"),
                    " wants to run  ",
                    ("Y", "bold #77b083"),
                    " allow  ",
                    ("N", "bold #d87575"),
                    " deny",
                )
            )
            return
        self.update(
            Text.assemble(
                ("? INPUT  ", "bold #d9a441"),
                question_text(interaction),
                ("  Type your answer below", "#8f9aa8"),
            )
        )

    def action_approve(self) -> None:
        self.post_message(self.Decision(True))

    def action_reject(self) -> None:
        self.post_message(self.Decision(False))


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


class CommandPopup(Static):
    """Non-focus-stealing Slash command popup controlled by the Composer."""

    def __init__(self) -> None:
        super().__init__(id="command-popup")
        self.display = False
        self._commands: tuple[TuiCommand, ...] = ()
        self._selected = 0

    @property
    def selected_command(self) -> TuiCommand | None:
        if not self._commands:
            return None
        return self._commands[self._selected]

    def show_commands(self, commands: tuple[TuiCommand, ...]) -> None:
        self._commands = commands
        self._selected = min(self._selected, max(0, len(commands) - 1))
        self.display = True
        self._render_commands()

    def hide(self) -> None:
        self.display = False
        self._commands = ()
        self._selected = 0
        self.update("")

    def navigate(self, offset: int) -> None:
        if not self._commands:
            return
        self._selected = (self._selected + offset) % len(self._commands)
        self._render_commands()

    def _render_commands(self) -> None:
        if not self._commands:
            self.update(Text("  No matching commands", style="#8f9aa8"))
            return
        content = Text()
        for index, command in enumerate(self._commands):
            selected = index == self._selected
            content.append("› " if selected else "  ", style="#d9a441" if selected else "")
            content.append(command.usage, style="bold #d9a441" if selected else "bold")
            content.append(f"  {command.description}", style="#aeb5bf" if selected else "#6f7a87")
            if index < len(self._commands) - 1:
                content.append("\n")
        self.update(content)


class Composer(TextArea):
    BINDINGS = [
        *TextArea.BINDINGS,
        Binding("enter", "submit", "Send", show=False, priority=True),
        Binding("shift+enter", "newline", "New line", show=False, priority=True),
        Binding("up", "history_previous", show=False, priority=True),
        Binding("down", "history_next", show=False, priority=True),
        Binding("escape", "dismiss_command", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

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
        value = self.text.strip()
        if not value:
            return
        if value.startswith("/"):
            self.post_message(self.CommandSubmitted(value))
            return
        self._history.append(value)
        self._history_index = len(self._history)
        self.clear()
        self.post_message(self.Submitted(value))

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
        yield Static("Enter send · Shift+Enter newline", id="key-hints")
        yield Static("", id="status-detail")

    def set_detail(self, detail: str) -> None:
        self.query_one("#status-detail", Static).update(detail)
