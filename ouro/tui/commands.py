from __future__ import annotations

from dataclasses import dataclass


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
    def takes_arguments(self) -> bool:
        return self.argument_hint is not None or bool(self.argument_options)

    def matches(self, query: str) -> bool:
        needle = query.casefold().strip()
        values = (self.name, *self.aliases)
        return any(value.casefold().startswith(needle) for value in values)


TUI_COMMANDS: tuple[TuiCommand, ...] = (
    TuiCommand("/new", "New session", "Start a clean conversation"),
    TuiCommand(
        "/sessions",
        "Open session",
        "Browse durable sessions",
        aliases=("/resume",),
    ),
    TuiCommand(
        "/fork",
        "Fork session",
        "Create an independent branch at a canonical message",
        "[message-id]",
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
        aliases=("/approval-mode",),
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
        "/compact",
        "Compact context",
        "Compact eligible older turns in the current session",
        aliases=("/compaction",),
    ),
    TuiCommand(
        "/skill",
        "Use skill",
        "Run a follow-up with one explicitly selected skill",
        "<skill> [task]",
    ),
    TuiCommand("/clear", "Clear view", "Clear the visible timeline only"),
    TuiCommand(
        "/navigate",
        "Navigate dialogue",
        "Jump to the top, bottom, or a specific user message",
        "[target]",
    ),
    TuiCommand("/abort", "Abort run", "Stop the active run"),
    TuiCommand("/help", "Keyboard help", "Show shortcuts and commands", aliases=("/?",)),
    TuiCommand("/quit", "Quit OURO", "Close the TUI", aliases=("/exit", "/quite")),
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
    if not normalized and command.argument_hint:
        return command.argument_hint
    query, separator, remainder = normalized.partition(" ")
    if (
        selected is not None
        and not separator
        and selected.value.casefold().startswith(query.casefold())
    ):
        completion = selected.value[len(query) :]
        suffix = f" {selected.argument_hint}" if selected.argument_hint else ""
        return completion + suffix
    if selected is not None and separator and selected.value.casefold() == query.casefold():
        if selected.argument_hint and not remainder:
            return selected.argument_hint
        return ""
    return ""


def slash_command_help() -> str:
    """Render command usage as a wrap-friendly, bullet-separated cloud."""
    return " · ".join(command.usage for command in TUI_COMMANDS)
