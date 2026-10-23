"""Multiple-choice menu selection and submission."""

from __future__ import annotations

from dataclasses import replace

from textual.binding import Binding
from textual.widgets import Input, OptionList, Static

from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice, MultiChoiceConfirmationHandler
from ouro.tui.menus.rows import _choice_option
from ouro.tui.text_inputs import TuiInput
from ouro.tui.theme import DEFAULT_TUI_THEME, label_text, theme_for


class _MultiChoiceFilterInput(TuiInput):
    BINDINGS = [
        Binding("space", "toggle_choice", "Toggle", show=False, priority=True),
    ]

    def action_toggle_choice(self) -> None:
        screen = self.screen
        if isinstance(screen, MultiChoiceScreen):
            screen.action_toggle_choice()


class MultiChoiceScreen(ChoiceScreen):
    """Keep a filtered selection set while Enter confirms one or many choices."""

    DEFAULT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Space toggle", "Enter confirm", "Esc close")
    )
    BACK_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Space toggle", "Enter confirm", "Esc back")
    )
    CONFIRM_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "Enter select", "Esc cancel")
    )

    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("left", "previous_group", "Previous group", show=False, priority=True),
        Binding("right", "next_group", "Next group", show=False, priority=True),
        Binding("space", "toggle_choice", "Toggle", show=False, priority=True),
        Binding("enter", "accept_choice", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        on_confirm: MultiChoiceConfirmationHandler,
        selected_marker: str,
        unselected_marker: str = "",
        confirm_label: str = "Confirm",
        destructive: bool = False,
        selected_values: tuple[str, ...] = (),
        interaction_hint: str | None = None,
    ) -> None:
        super().__init__(
            title=title, placeholder=placeholder, choices=choices, interaction_hint=interaction_hint
        )
        self._on_confirm = on_confirm
        self._selected = set(selected_values).intersection(choice.value for choice in choices)
        self._selected_marker = selected_marker
        self._unselected_marker = unselected_marker
        self._confirm_label = confirm_label
        self._destructive = destructive
        self._confirmation_values: tuple[str, ...] = ()
        self._catalog_choices: tuple[Choice, ...] = ()
        self._catalog_filter = ""
        self._choices = self._project_markers(self._choices)

    def _filter_input(self) -> Input:
        return _MultiChoiceFilterInput(
            placeholder=self._placeholder,
            id="choice-filter",
            select_on_focus=False,
        )

    def action_toggle_choice(self) -> None:
        if self._confirmation_values:
            return
        selected = self._highlighted_value()
        if selected is None:
            return
        if selected in self._selected:
            self._selected.remove(selected)
        else:
            self._selected.add(selected)
        self._choices = self._project_markers(self._choices)
        choice = next(choice for choice in self._choices if choice.value == selected)
        options = self.query_one("#choice-list", OptionList)
        option_index = options.get_option_index(selected)
        prompt = _choice_option(
            choice,
            group_accent=theme_for(self).accent,
            theme=theme_for(self),
        ).prompt
        options.replace_option_prompt_at_index(
            option_index,
            prompt,
        )

    async def _select_choice(self, highlighted: str) -> None:
        if self._confirmation_values:
            if highlighted == "__multi_cancel__":
                self._leave_confirmation(self._catalog_choices)
                return
            if highlighted != "__multi_confirm__":
                return
            values = self._confirmation_values
            revision = self._page_revision
            choices = await self._on_confirm(values)
            if not self._accepts_choice_updates() or revision != self._page_revision:
                return
            if self._confirmation_values is values:
                self._leave_confirmation(self._catalog_choices if choices is None else choices)
            elif not self._confirmation_values and choices is not None:
                self._selected.intersection_update(choice.value for choice in choices)
                self.update_choices(
                    self._project_markers(choices), preferred_value=self._selected_value()
                )
            return
        selected = tuple(
            choice.value for choice in self._choices if choice.value in self._selected
        ) or (highlighted,)
        self._enter_confirmation(selected)

    async def action_cancel(self) -> None:
        if self._confirmation_values:
            self._leave_confirmation(self._catalog_choices)
            return
        await super().action_cancel()

    def _enter_confirmation(self, selected: tuple[str, ...]) -> None:
        self._confirmation_values = selected
        self._catalog_choices = self._choices
        filter_input = self.query_one("#choice-filter", Input)
        self._catalog_filter = filter_input.value
        self._choices = (
            Choice("__multi_cancel__", "Cancel"),
            Choice(
                "__multi_confirm__",
                f"{self._confirm_label} selected ({len(selected)})",
                description=(
                    "Permanently delete the selected items? This cannot be undone."
                    if self._destructive
                    else ""
                ),
                destructive=self._destructive,
            ),
        )
        self.query_one("#choice-title", Static).update(
            label_text(f"CONFIRM {self._title}", theme=theme_for(self))
        )
        self._show_hint(self.CONFIRM_HINT)
        filter_input.value = ""
        self._refresh_options(needle="", preferred_value="__multi_cancel__")

    def _leave_confirmation(self, choices: tuple[Choice, ...]) -> None:
        self._confirmation_values = ()
        remaining = {choice.value for choice in choices}
        self._selected.intersection_update(remaining)
        self._choices = self._project_markers(choices)
        self.query_one("#choice-title", Static).update(
            label_text(self._title, theme=theme_for(self))
        )
        self._show_hint()
        filter_input = self.query_one("#choice-filter", Input)
        filter_input.value = self._catalog_filter
        self._refresh_options(
            needle=self._catalog_filter,
            preferred_value=None,
        )
        self._catalog_choices = ()
        self._catalog_filter = ""

    def _highlighted_value(self) -> str | None:
        options = self.query_one("#choice-list", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return None
        option = options.get_option_at_index(highlighted)
        return str(option.id) if option.id is not None and not option.disabled else None

    def _project_markers(self, choices: tuple[Choice, ...]) -> tuple[Choice, ...]:
        return tuple(
            replace(
                choice,
                selection_marker=(
                    self._selected_marker
                    if choice.value in self._selected
                    else self._unselected_marker
                ),
            )
            for choice in choices
        )
