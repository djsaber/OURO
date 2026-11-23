"""Selection behavior shared by multi-choice menus."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Input, OptionList

from ouro.tui.menus.models import Choice
from ouro.tui.menus.multiple import MultiChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.helpers import wait_for_menu_action


@pytest.mark.parametrize("focus_filter", [True, False])
async def test_select_all_respects_filter_disabled_rows_and_confirmation(
    focus_filter: bool,
) -> None:
    choices = (
        Choice("a", "Match Alpha", group="First"),
        Choice("b", "Match Beta", group="Second"),
        Choice("blocked", "Match Unavailable", disabled=True, group="Second"),
        Choice("c", "Other Gamma", group="Second"),
    )
    confirmed = []

    async def confirm(values: tuple[str, ...]) -> tuple[Choice, ...]:
        confirmed.append(values)
        return choices

    menu = MultiChoiceScreen(
        title="ITEMS",
        placeholder="Filter…",
        choices=choices,
        selected_values=("c",),
        selected_marker=DEFAULT_TUI_THEME.selected_choice_marker,
        on_confirm=confirm,
    )
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(menu)
        field = menu.query_one(Input)
        options = menu.query_one(OptionList)
        field.value = "Match"
        await pilot.pause()
        (field if focus_filter else options).focus()
        highlighted = options.highlighted
        await pilot.press("ctrl+a")
        assert menu._selected == {"a", "b", "c"}
        assert field.value == "Match"
        assert options.highlighted == highlighted
        assert not confirmed

        await pilot.press("ctrl+a")
        assert menu._selected == {"c"}
        for needle in ("no matches", "Unavailable"):
            field.value = needle
            await pilot.pause()
            await pilot.press("ctrl+a")
            assert menu._selected == {"c"}

        field.value = ""
        await pilot.pause()
        await pilot.press("ctrl+a")
        assert menu._selected == {"a", "b", "c"}
        await pilot.press("ctrl+a")
        assert menu._selected == set()
        await pilot.press("ctrl+a", "enter")
        await wait_for_menu_action(pilot, menu)
        assert menu._confirmation_values == ("a", "b", "c")
        assert not menu.query_one(Input).display
        assert app.focused is menu.query_one(OptionList)
        await pilot.press("ctrl+a")
        assert menu._confirmation_values == ("a", "b", "c")
        assert menu._selected == {"a", "b", "c"}
        assert not confirmed
        await pilot.press("down", "enter")
        await wait_for_menu_action(pilot, menu)
        assert confirmed == [("a", "b", "c")]
        assert menu.query_one(Input).display
        assert app.focused is menu.query_one(Input)
