from __future__ import annotations

from dataclasses import dataclass

from ouro.tui.menu_labels import APPROVAL_DESCRIPTIONS, MODEL_THINKING_LABEL, TOOL_APPROVAL_LABEL
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme


@dataclass(frozen=True)
class TuiCommandArgument:
    value: str
    description: str
    argument_hint: str | None = None
    label: str | None = None
    accent: str | None = None
    trailing_badge: str = ""
    checked: bool | None = None
    # Free-form suggestions require explicit acceptance when input is present.
    is_suggestion: bool = False

    @property
    def display_label(self) -> str:
        return self.label or self.value

    @property
    def usage(self) -> str:
        return f"{self.value} {self.argument_hint}" if self.argument_hint else self.value

    @property
    def input_hint(self) -> str:
        """Return the argument suffix in directly insertable form."""

        return _input_format(self.argument_hint)

    @property
    def requires_value(self) -> bool:
        return self.argument_hint is not None and "<" in self.argument_hint

    def matches(self, query: str) -> bool:
        needle = query.casefold().strip()
        return any(
            candidate.casefold().startswith(needle)
            for candidate in (self.value, self.display_label)
            if candidate
        )


@dataclass(frozen=True)
class TuiCommand:
    name: str
    title: str
    description: str
    argument_hint: str | None = None
    aliases: tuple[str, ...] = ()
    argument_options: tuple[TuiCommandArgument, ...] = ()
    freeform_arguments: bool = False

    @property
    def usage(self) -> str:
        return f"{self.name} {self.argument_hint}" if self.argument_hint else self.name

    @property
    def input_hint(self) -> str | None:
        """Return syntax that is not already represented by the argument menu."""

        if not self.argument_hint:
            return None
        option_values = {
            option.value.casefold() for option in self.argument_options if option.value
        }
        if not option_values:
            return _input_format(self.argument_hint)

        remaining: list[str] = []
        for branch in _split_usage_branches(self.argument_hint):
            bracket_choices = _bracket_choices(branch)
            if bracket_choices is not None:
                choices = [
                    choice for choice in bracket_choices if choice.casefold() not in option_values
                ]
                if choices:
                    remaining.append("[" + "|".join(choices) + "]")
                continue
            head, _separator, _tail = branch.partition(" ")
            if head.casefold() not in option_values:
                remaining.append(branch)
        return _input_format(" | ".join(remaining)) or None

    @property
    def takes_arguments(self) -> bool:
        return self.argument_hint is not None or bool(self.argument_options)

    def matches(self, query: str) -> bool:
        needle = query.casefold().strip()
        values = (self.name, *self.aliases)
        return any(value.casefold().startswith(needle) for value in values)


TUI_COMMANDS: tuple[TuiCommand, ...] = (
    TuiCommand("/export", "Export current session", "Save the current session and attachments"),
    TuiCommand("/new", "New session", "Start a clean conversation"),
    TuiCommand("/continue", "Continue task", "Resume an interrupted task"),
    TuiCommand("/undo", "Undo message", "Withdraw the latest user message and edit it again"),
    TuiCommand(
        "/workspace",
        "Workspace",
        "Choose a working folder or clear recent folders",
        "[clear|<path>]",
        freeform_arguments=True,
    ),
    TuiCommand(
        "/sessions",
        "Open session",
        "Browse saved conversations",
        aliases=("/resume",),
    ),
    TuiCommand(
        "/actions",
        "Open actions",
        "Open the action menu",
        aliases=("/menu",),
    ),
    TuiCommand(
        "/fork",
        "Copy conversation",
        "Continue from a copy of this conversation",
        "[full|#N]",
        aliases=("/branch",),
    ),
    TuiCommand(
        "/model",
        "Select model",
        "Select the model used by this conversation",
        "<model>",
    ),
    TuiCommand(
        "/approval",
        TOOL_APPROVAL_LABEL,
        "Choose when tools need your approval",
        "[inherit|auto|approval|deny|afk]",
        argument_options=tuple(
            TuiCommandArgument(value, description)
            for value, description in APPROVAL_DESCRIPTIONS.items()
        ),
    ),
    TuiCommand(
        "/thinking",
        MODEL_THINKING_LABEL,
        "Choose how much the model thinks",
        "[setting]",
        aliases=("/reasoning",),
    ),
    TuiCommand(
        "/thoughts",
        "Thinking display",
        "Show or hide model thinking",
        "[expand|collapse]",
        argument_options=(
            TuiCommandArgument("expand", "Show model thinking"),
            TuiCommandArgument("collapse", "Hide model thinking"),
        ),
    ),
    TuiCommand(
        "/compact",
        "Compact context",
        "Compress older conversation history to free up space",
    ),
    TuiCommand(
        "/summary",
        "Session summary",
        "Show the latest saved summary for the current session",
    ),
    TuiCommand(
        "/skill",
        "Use skill",
        "Send a task using a chosen skill",
        "<skill> [task]",
    ),
    TuiCommand(
        "/unqueue",
        "Edit queued message",
        "Move a waiting message back to the input box",
        "<queued-message>",
    ),
    TuiCommand("/clear", "Clear view", "Clear messages from view; keep saved history"),
    TuiCommand(
        "/navigate",
        "Jump to message",
        "Jump to the top, bottom, or a specific user message",
        "[target]",
    ),
    TuiCommand("/abort", "Stop task", "Stop the current task"),
    TuiCommand("/help", "Keyboard help", "Show shortcuts and commands"),
    TuiCommand("/quit", "Quit OURO", "Close the app", aliases=("/exit",)),
)


def matching_commands(
    query: str,
    commands: tuple[TuiCommand, ...] = TUI_COMMANDS,
) -> tuple[TuiCommand, ...]:
    if not query.startswith("/") or any(character.isspace() for character in query):
        return ()
    return tuple(command for command in commands if command.matches(query))


def resolve_command(
    value: str,
    commands: tuple[TuiCommand, ...] = TUI_COMMANDS,
) -> tuple[TuiCommand | None, str]:
    command_text, _, argument = value.strip().partition(" ")
    normalized = command_text.casefold()
    for command in commands:
        if normalized in {
            command.name.casefold(),
            *(alias.casefold() for alias in command.aliases),
        }:
            return command, argument.strip()
    return None, argument.strip()


def matching_arguments(
    command: TuiCommand,
    argument: str,
) -> tuple[TuiCommandArgument, ...]:
    query, separator, remainder = argument.lstrip().partition(" ")
    if separator:
        return tuple(
            option
            for option in command.argument_options
            if option.value.casefold() == query.casefold()
            and (not remainder.strip() or option.argument_hint or option.is_suggestion)
        )
    return tuple(option for option in command.argument_options if option.matches(query))


def argument_suggestion(
    command: TuiCommand,
    argument: str,
    selected: TuiCommandArgument | None,
) -> str:
    normalized = argument.lstrip()
    if not normalized:
        return command.input_hint or ""
    query, separator, remainder = normalized.partition(" ")
    if (
        selected is not None
        and not separator
        and selected.value.casefold().startswith(query.casefold())
    ):
        completion = selected.value[len(query) :]
        suffix = f" {selected.input_hint}" if selected.input_hint else ""
        return completion + suffix
    if selected is not None and separator and selected.value.casefold() == query.casefold():
        if selected.argument_hint and not remainder:
            return selected.input_hint
        return ""
    return ""


def _input_format(hint: str | None) -> str:
    """Project display grammar onto text that may be inserted into the composer."""

    return hint.translate(str.maketrans("", "", "[]")).strip() if hint else ""


def _split_usage_branches(usage: str) -> tuple[str, ...]:
    """Split a display grammar on top-level alternatives."""

    pairs = {"[": "]", "(": ")", "{": "}", "<": ">"}
    closing = set(pairs.values())
    stack: list[str] = []
    branches: list[str] = []
    start = 0
    for index, character in enumerate(usage):
        if character in pairs:
            stack.append(pairs[character])
        elif character in closing:
            if stack and stack[-1] == character:
                stack.pop()
        elif character == "|" and not stack:
            branches.append(usage[start:index].strip())
            start = index + 1
    branches.append(usage[start:].strip())
    return tuple(branch for branch in branches if branch)


def _bracket_choices(branch: str) -> tuple[str, ...] | None:
    if not (branch.startswith("[") and branch.endswith("]")):
        return None
    choices = tuple(choice.strip() for choice in branch[1:-1].split("|"))
    if len(choices) < 2 or any(not choice or " " in choice for choice in choices):
        return None
    return choices


def slash_command_help(*, theme: TuiTheme = DEFAULT_TUI_THEME) -> str:
    """Render command usage as a wrap-friendly, bullet-separated cloud."""
    return theme.inline_separator.join(command.usage for command in TUI_COMMANDS)
