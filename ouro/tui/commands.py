from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TuiCommand:
    name: str
    title: str
    description: str
    argument_hint: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"{self.name} {self.argument_hint}" if self.argument_hint else self.name

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
        "/model",
        "Select or configure model",
        "Select a model or configure a new endpoint",
        "[selector|new]",
    ),
    TuiCommand(
        "/approval",
        "Session approval",
        "Set tool approval policy for this session",
        "[inherit|auto|approval|deny|afk]",
        aliases=("/approval-mode",),
    ),
    TuiCommand(
        "/thinking",
        "Model thinking",
        "Set reasoning mode or effort for this session",
        "[default|enabled|disabled|effort]",
        aliases=("/reasoning",),
    ),
    TuiCommand("/clear", "Clear view", "Clear the visible timeline only"),
    TuiCommand("/abort", "Abort run", "Stop the active run"),
    TuiCommand("/help", "Keyboard help", "Show shortcuts and commands", aliases=("/?",)),
    TuiCommand("/quit", "Quit OURO", "Close the TUI", aliases=("/exit", "/quite")),
)


def matching_commands(query: str) -> tuple[TuiCommand, ...]:
    if not query.startswith("/") or any(character.isspace() for character in query):
        return ()
    return tuple(command for command in TUI_COMMANDS if command.matches(query))


def resolve_command(value: str) -> tuple[TuiCommand | None, str]:
    command_text, _, argument = value.strip().partition(" ")
    normalized = command_text.casefold()
    for command in TUI_COMMANDS:
        if normalized in {
            command.name.casefold(),
            *(alias.casefold() for alias in command.aliases),
        }:
            return command, argument.strip()
    return None, argument.strip()


def slash_command_help() -> str:
    return "   ".join(command.usage for command in TUI_COMMANDS)
