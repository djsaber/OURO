from __future__ import annotations

from dataclasses import dataclass

from ouro.tui.theme import TUI_DESIGN


@dataclass(frozen=True)
class TuiCommandArgument:
    value: str
    description: str
    argument_hint: str | None = None
    label: str | None = None

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
    TuiCommand("/new", "New session", "Start a clean conversation"),
    TuiCommand(
        "/workspace",
        "Select workspace",
        "Set the workspace for this new session",
        "<path>",
    ),
    TuiCommand(
        "/sessions",
        "Open session",
        "Browse durable sessions",
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
        "Fork session",
        "Create an independent branch of the current session",
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
        "Session approval",
        "Set tool approval policy for this session",
        "[inherit|auto|approval|deny|afk]",
        argument_options=tuple(
            TuiCommandArgument(value, description)
            for value, description in (
                ("inherit", "Use the configured default approval policy"),
                ("auto", "Run tools without approval prompts"),
                ("approval", "Ask before protected tool calls"),
                ("deny", "Deny protected tool calls"),
                ("afk", "Use unattended approval behavior"),
            )
        ),
    ),
    TuiCommand(
        "/thinking",
        "Model thinking",
        "Set reasoning mode or effort for this session",
        "[default|enabled|disabled|effort]",
        aliases=("/reasoning",),
        argument_options=tuple(
            TuiCommandArgument(value, description)
            for value, description in (
                ("default", "Use the endpoint reasoning default"),
                ("enabled", "Enable model reasoning"),
                ("disabled", "Disable model reasoning"),
            )
        ),
    ),
    TuiCommand(
        "/thoughts",
        "Thought display",
        "Expand or collapse model thoughts in the conversation",
        "[expand|collapse]",
        argument_options=(
            TuiCommandArgument("expand", "Show model thought content"),
            TuiCommandArgument("collapse", "Hide model thought content"),
        ),
    ),
    TuiCommand(
        "/compact",
        "Compact context",
        "Compact eligible older turns in the current session",
    ),
    TuiCommand(
        "/skill",
        "Use skill",
        "Run a follow-up with one explicitly selected skill",
        "<skill> [task]",
    ),
    TuiCommand(
        "/unqueue",
        "Unqueue message",
        "Return a queued message to the composer",
        "<queued-message>",
    ),
    TuiCommand("/clear", "Clear view", "Clear the visible timeline only"),
    TuiCommand(
        "/navigate",
        "Navigate dialogue",
        "Jump to the top, bottom, or a specific user message",
        "[target]",
    ),
    TuiCommand("/abort", "Abort run", "Stop the active run"),
    TuiCommand("/help", "Keyboard help", "Show shortcuts and commands"),
    TuiCommand("/quit", "Quit OURO", "Close the TUI", aliases=("/exit",)),
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
    query, separator, _remainder = argument.lstrip().partition(" ")
    if separator:
        return tuple(
            option
            for option in command.argument_options
            if option.value.casefold() == query.casefold()
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


def slash_command_help() -> str:
    """Render command usage as a wrap-friendly, bullet-separated cloud."""
    return TUI_DESIGN.inline_separator.join(command.usage for command in TUI_COMMANDS)
