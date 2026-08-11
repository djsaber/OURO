from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ouro.tui.commands import slash_command_help
from ouro.tui.theme import theme_for


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""
    accent: str | None = None
    trailing: str = ""
    group: str = ""
    group_trailing: str = ""
    bold_label: bool = False
    disabled: bool = False
    marker: str = ""


ChoiceSelectionHandler = Callable[[str], Awaitable[tuple[Choice, ...] | None]]


class _ChoiceGroupHeader(Table):
    """Marks a table-backed group heading without conflating it with disabled rows."""


class _ChoiceGroupHeaderText(Text):
    """Marks a text-backed group heading without conflating it with disabled rows."""


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("left", "previous_group", "Previous group", show=False, priority=True),
        Binding("right", "next_group", "Next group", show=False, priority=True),
        Binding("enter", "accept_choice", "Select", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._choices = choices
        self._initial_value = initial_value
        self._on_highlight = on_highlight
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-panel"):
            yield Static(self._title, id="choice-title")
            yield Input(placeholder=self._placeholder, id="choice-filter")
            yield OptionList(
                *self._options(self._choices, group_accent=theme_for(self).accent),
                id="choice-list",
            )
            yield Static(
                "↑/↓ move · ←/→ group · Enter select · Esc cancel",
                id="choice-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#choice-filter", Input).focus()
        options = self.query_one("#choice-list", OptionList)
        if options.option_count:
            options.highlighted = self._highlight_index(
                options,
                preferred_value=self._initial_value,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_options(needle=event.value, preferred_value=None)

    def _refresh_options(
        self,
        *,
        needle: str,
        preferred_value: str | None,
    ) -> None:
        normalized_needle = needle.casefold().strip()
        choices = tuple(
            choice
            for choice in self._choices
            if not normalized_needle
            or normalized_needle in choice.label.casefold()
            or normalized_needle in choice.description.casefold()
            or normalized_needle in choice.group.casefold()
        )
        options = self.query_one("#choice-list", OptionList)
        options.clear_options()
        options.add_options(self._options(choices, group_accent=theme_for(self).accent))
        options.highlighted = (
            self._highlight_index(options, preferred_value=preferred_value) if choices else None
        )

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        options = self.query_one("#choice-list", OptionList)
        if options.option_count and options.highlighted is None:
            options.highlighted = self._highlight_index(options)
        options.focus()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        if event.option.id is None:
            return
        selected = str(event.option.id)
        if self._on_select is None:
            self.dismiss(selected)
            return
        choices = await self._on_select(selected)
        if choices is None:
            return
        self._choices = choices
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=selected,
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._on_highlight is not None and event.option.id is not None:
            self._on_highlight(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_previous_choice(self) -> None:
        self._move_choice(-1)

    def action_next_choice(self) -> None:
        self._move_choice(1)

    def _move_choice(self, direction: int) -> None:
        options = self.query_one("#choice-list", OptionList)
        previous = options.highlighted
        if direction < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()
        self._reveal_group_header_after_upward_move(options, previous)

    def action_previous_group(self) -> None:
        self._move_group(-1)

    def action_next_group(self) -> None:
        self._move_group(1)

    def _move_group(self, direction: int) -> None:
        options = self.query_one("#choice-list", OptionList)
        starts = tuple(
            index + 1
            for index in range(options.option_count - 1)
            if self._is_group_header(options.get_option_at_index(index))
            and not options.get_option_at_index(index + 1).disabled
        )
        if not starts:
            return
        highlighted = options.highlighted
        if highlighted is None:
            options.highlighted = starts[0]
            return
        current_group = max(
            (index for index, start in enumerate(starts) if start <= highlighted),
            default=0,
        )
        options.highlighted = starts[(current_group + direction) % len(starts)]
        self._reveal_group_header_after_upward_move(options, highlighted)

    @classmethod
    def _reveal_group_header_after_upward_move(
        cls,
        options: OptionList,
        previous: int | None,
    ) -> None:
        highlighted = options.highlighted
        if (
            previous is None
            or highlighted is None
            or highlighted >= previous
            or highlighted == 0
            or not cls._is_group_header(options.get_option_at_index(highlighted - 1))
        ):
            return
        # OptionList has already made the selected row visible. Moving upward
        # can leave its one-line group header just above the viewport; reveal
        # that context without changing downward navigation's valid scroll.
        options.scroll_relative(
            y=-1,
            animate=False,
            force=True,
            immediate=True,
        )

    @staticmethod
    def _is_group_header(option: Option) -> bool:
        return option.disabled and isinstance(
            option.prompt,
            (_ChoiceGroupHeader, _ChoiceGroupHeaderText),
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"previous_group", "next_group"}:
            return any(choice.group for choice in self._choices)
        return super().check_action(action, parameters)

    def action_accept_choice(self) -> None:
        self.query_one("#choice-list", OptionList).action_select()

    @staticmethod
    def _options(
        choices: tuple[Choice, ...],
        *,
        group_accent: str = "#7d8794",
    ) -> list[Option]:
        options: list[Option] = []
        previous_group: object = object()
        for choice in choices:
            if choice.group != previous_group:
                if choice.group:
                    if isinstance(previous_group, str) and previous_group:
                        options.append(Option(Text(" "), disabled=True))
                    group_header: Text | Table
                    if choice.group_trailing:
                        row = _ChoiceGroupHeader.grid(expand=True, padding=0)
                        row.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
                        row.add_column(justify="right", no_wrap=True)
                        row.add_row(
                            Text(choice.group, style=f"bold {group_accent}"),
                            Text(choice.group_trailing, style="#7d8794"),
                        )
                        group_header = row
                    else:
                        group_header = _ChoiceGroupHeaderText(
                            choice.group,
                            style=f"bold {group_accent}",
                            overflow="ellipsis",
                            no_wrap=True,
                        )
                    options.append(
                        Option(
                            group_header,
                            disabled=True,
                        )
                    )
                previous_group = choice.group
            label_style = (
                (f"bold {choice.accent}" if choice.accent else "bold")
                if choice.bold_label
                else (choice.accent or "")
            )
            label = Text(overflow="ellipsis", no_wrap=True)
            if choice.marker:
                label.append(f"{choice.marker} ", style=f"bold {group_accent}")
            label.append(choice.label, style=label_style)
            if choice.trailing:
                row = Table.grid(expand=True, padding=0)
                row.add_column(ratio=2, overflow="ellipsis", no_wrap=True)
                row.add_column(ratio=1)
                row.add_column(width=2)
                row.add_column(justify="right", no_wrap=True)
                row.add_row(label, "", "", Text(choice.trailing, style="#7d8794"))
                options.append(Option(row, id=choice.value, disabled=choice.disabled))
                continue
            if choice.description:
                label.append(f"\n{choice.description}", style="#7d8794")
            options.append(Option(label, id=choice.value, disabled=choice.disabled))
        return options

    @staticmethod
    def _highlight_index(
        options: OptionList,
        *,
        preferred_value: str | None = None,
    ) -> int | None:
        first_enabled: int | None = None
        for index in range(options.option_count):
            option = options.get_option_at_index(index)
            if option.disabled:
                continue
            if first_enabled is None:
                first_enabled = index
            if preferred_value is not None and option.id == preferred_value:
                return index
        return first_enabled


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding(
            "escape,question_mark,ctrl+c",
            "close",
            "Close",
            show=False,
            priority=True,
        ),
    ]

    def compose(self) -> ComposeResult:
        # Build the panel from a Text so padding tracks visible width, not
        # markup length — keeping the shortcut columns aligned in the flat panel.
        text = Text()
        accent = theme_for(self).accent
        text.append("OURO shortcuts\n\n", style=f"bold {accent}")
        pairs = (
            ("Enter", "send / queue", "Shift+Enter", "new line"),
            ("Ctrl+Enter / Ctrl+J", "guide active run", "↑ / ↓", "input history"),
            ("Ctrl+N", "new session", "Ctrl+S", "sessions"),
            ("Alt+M", "model", "Ctrl+P", "actions"),
            ("Ctrl+↑", "conversation top", "Ctrl+↓", "conversation bottom"),
            ("Ctrl+←", "previous user message", "Ctrl+→", "next user message"),
            ("Ctrl+C", "abort run", "Ctrl+Q", "quit"),
            ("Esc", "close menu", "?", "help"),
        )
        for left_key, left_desc, right_key, right_desc in pairs:
            text.append(left_key, style="bold")
            text.append(f" {left_desc}")
            text.append(" " * max(0, 40 - len(left_key) - len(left_desc) - 1))
            text.append(right_key, style="bold")
            text.append(f" {right_desc}\n")
        text.append("\nBuilt-in slash commands\n\n", style=f"bold {accent}")
        text.append(slash_command_help(), style="#aeb5bf")
        text.append(
            "\n\nType / to search built-in and active plugin commands. Unique plugin "
            "commands use the short form; ambiguous names use /plugin.id:command.",
            style="#8f9aa8",
        )
        yield Static(text, id="help-panel")

    def action_close(self) -> None:
        self.dismiss(None)
