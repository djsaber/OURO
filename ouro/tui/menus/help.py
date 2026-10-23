"""Menu help text, sizing and keyboard hint presentation."""

from __future__ import annotations

from ouro.tui.commands import REFERENCE_COMMANDS, TUI_COMMANDS, TuiCommand
from ouro.tui.menus.models import Choice

_HELP_SHORTCUTS = (
    ("Enter", "send / queue / confirm", "Ctrl+Enter", "new line"),
    ("Ctrl+G", "guide current task", "↑ / ↓", "cursor / history when empty"),
    ("Ctrl+N", "new session", "Ctrl+O", "sessions"),
    ("Ctrl+T", "tasks", "Ctrl+S", "skills"),
    ("Alt+T", "cycle model thinking", "Alt+A", "cycle tool approval"),
    ("Alt+← / Alt+→", "previous / next agent", "Alt+↑ / Alt+↓", "main / last agent"),
    ("Alt+P", "plugins", "Ctrl+P", "actions"),
    ("F1", "help", "Alt+Home / Alt+End", "conversation top / bottom"),
    ("Alt+PageUp", "previous user message", "Alt+PageDown", "next user message / bottom"),
    ("Ctrl+← / Ctrl+→", "move by word", "Ctrl+A", "select all input"),
    ("Ctrl+Home / Ctrl+End", "input start / end", "Ctrl+C", "copy selection / stop task"),
    ("Ctrl+X / Ctrl+V", "cut / paste", "Ctrl+Z / Ctrl+Y", "undo / redo in composer"),
    ("Tab", "focus / switch menu domain", "Space", "toggle multi-selection"),
    ("Ctrl+Q", "quit", "Esc", "close menu"),
)


def help_choices(
    *,
    plugin_commands: tuple[TuiCommand, ...] = (),
) -> tuple[Choice, ...]:
    """Project help onto the shared searchable menu surface."""

    shortcuts = tuple(
        Choice(
            f"shortcut:{row}:{side}",
            key,
            trailing=description,
            group="SHORTCUTS",
        )
        for row, pair in enumerate(_HELP_SHORTCUTS)
        for side, (key, description) in enumerate((pair[:2], pair[2:]))
    )
    commands = tuple(
        Choice(
            f"command:{command.name}",
            command.usage,
            trailing=command.description,
            group="COMMANDS",
        )
        for command in TUI_COMMANDS
    )
    plugins = tuple(
        Choice(
            f"plugin-command:{command.name}",
            command.usage,
            trailing=command.title,
            group="PLUGIN COMMANDS",
        )
        for command in plugin_commands
    )
    return (
        *shortcuts,
        *(
            Choice(
                f"command:{command.name}",
                command.name,
                description=(
                    "Type to filter; Space selects; "
                    "Enter opens folders or adds references; Esc closes."
                ),
                trailing=command.title,
                group="COMMANDS",
            )
            for command in REFERENCE_COMMANDS
        ),
        *commands,
        *plugins,
    )
