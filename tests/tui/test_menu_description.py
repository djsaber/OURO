from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Input, OptionList, Static

from ouro.tui.app import OuroTuiApp
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice
from tests.tui.app.support import _application
from tests.tui.helpers import run_ready_app


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


@pytest.mark.parametrize("description", ("Skill overview", "Plugin overview\n\nv1.0.0 • By ouro"))
async def test_description_spacing_follows_page_search_visibility(tmp_path, description):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        menu = ChoiceScreen(
            title="DETAIL",
            placeholder=None,
            description=description,
            choices=(Choice("action", "Action", group="ACTIONS"),),
        )
        await app.push_screen(menu)
        await pilot.pause()
        summary = menu.query_one("#choice-description", Static)
        options = menu.query_one(OptionList)
        assert options.region.y == summary.region.bottom + 1
        menu.push_page(
            title="CATALOG",
            placeholder="Filter…",
            description="Searchable overview",
            choices=(Choice("item", "Item"),),
        )
        await pilot.pause()
        assert summary.styles.margin.bottom == 0
        assert menu.query_one(Input).region.y == summary.region.bottom
        menu.pop_page()
        await pilot.pause()
        assert options.region.y == summary.region.bottom + 1
        menu.replace_page(title="ACTIONS", placeholder=None, choices=(Choice("close", "Close"),))
        await pilot.pause()
        assert not summary.display
        assert options.region.y == menu.query_one("#choice-title", Static).region.bottom + 1
