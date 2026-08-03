from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ouro.tui.commands import slash_command_help


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False),
    ]

    def __init__(self, *, title: str, placeholder: str, choices: tuple[Choice, ...]) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._choices = choices

    def compose(self) -> ComposeResult:
        yield Static(self._title, id="choice-title")
        yield Input(placeholder=self._placeholder, id="choice-filter")
        yield OptionList(*self._options(self._choices), id="choice-list")

    def on_mount(self) -> None:
        self.query_one("#choice-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.casefold().strip()
        choices = tuple(
            choice
            for choice in self._choices
            if not needle
            or needle in choice.label.casefold()
            or needle in choice.description.casefold()
        )
        options = self.query_one("#choice-list", OptionList)
        options.clear_options()
        options.add_options(self._options(choices))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @staticmethod
    def _options(choices: tuple[Choice, ...]) -> list[Option]:
        options: list[Option] = []
        for choice in choices:
            label = Text(choice.label, style="bold")
            if choice.description:
                label.append(f"\n{choice.description}", style="#7d8794")
            options.append(Option(label, id=choice.value))
        return options


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape,question_mark,ctrl+c", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        content = f"""[bold #d9a441]OURO shortcuts[/]

[bold]Enter[/] send                     [bold]Shift+Enter[/] new line
[bold]Ctrl+N[/] new session             [bold]Ctrl+S[/] session picker
[bold]Ctrl+M[/] model picker            [bold]Ctrl+P[/] action palette
[bold]Ctrl+C[/] abort active run / quit [bold]?[/] help

[bold #d9a441]Slash commands[/]

{slash_command_help()}
"""
        yield Static(
            content,
            id="help-panel",
            markup=True,
        )

    def action_close(self) -> None:
        self.dismiss(None)
