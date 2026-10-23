from __future__ import annotations

from textual.app import App
from textual.widgets import Input, Static

from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice


async def test_menu_description_survives_filtering_and_back_navigation():
    app = App()
    description = "[bold]Plugin overview[/bold] " + "配置功能说明。" * 60
    async with app.run_test() as pilot:
        menu = ChoiceScreen(title="PLUGINS", placeholder="Filter…", choices=(Choice("one", "One"),))
        await app.push_screen(menu)
        menu.push_page(
            title="PLUGIN • One",
            description=description,
            placeholder="Choose an action…",
            choices=tuple(
                Choice(str(index), f"Action {index}", group="ACTIONS") for index in range(20)
            ),
        )
        await pilot.pause()
        summary = menu.query_one("#choice-description", Static)
        assert summary.display and summary.visual.plain == description
        assert str(summary.content) == description
        menu.query_one(Input).value = "Action 1"
        await pilot.pause()
        assert summary.display and str(summary.content) == description
        menu.push_page(title="CHILD", placeholder="Filter…", choices=(Choice("two", "Two"),))
        await pilot.pause()
        assert not summary.display
        menu.pop_page()
        await pilot.pause()
        assert summary.display and str(summary.content) == description
        menu.pop_page()
        await pilot.pause()
        assert not summary.display
