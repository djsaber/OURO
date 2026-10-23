"""Confirmation menu and decision handling."""

from __future__ import annotations

from textual.widgets import OptionList

from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.theme import DEFAULT_TUI_THEME


class ConfirmationScreen(ChoiceScreen):
    """Present a destructive confirmation with the shared menu chrome."""

    DEFAULT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "Enter select", "Esc cancel")
    )
    BACK_HINT = DEFAULT_HINT
    CONFIRM_VALUE = "__confirmation_confirm__"

    def __init__(
        self,
        *,
        title: str,
        message: str,
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__(
            title=f"CONFIRM {title}",
            placeholder="Choose an action…",
            choices=(
                Choice("__confirmation_cancel__", "Cancel"),
                Choice(self.CONFIRM_VALUE, confirm_label, description=message, destructive=True),
            ),
            initial_value="__confirmation_cancel__",
        )

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self.query_one("#choice-list", OptionList).focus)

    @classmethod
    def confirmed(cls, selected: str | None) -> bool:
        return selected == cls.CONFIRM_VALUE
