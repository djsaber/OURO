from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Input, Static

from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import Choice, ChoiceScreen
from tests.tui.helpers import run_ready_app, two_route_application


async def test_menu_description_survives_filtering_navigation_and_narrow_layout(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    description = "[bold]Plugin overview[/bold] " + "配置功能说明。" * 60
    async with run_ready_app(app, size=(100, 34)) as pilot:
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
        for size in ((48, 18), (40, 12), (100, 34)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            panel = menu.query_one("#choice-panel", Vertical)
            hint = menu.query_one("#choice-hint", Static)
            assert panel.region.bottom <= size[1]
            assert hint.region.bottom <= panel.region.bottom
            assert summary.region.height <= 3
        menu.push_page(title="CHILD", placeholder="Filter…", choices=(Choice("two", "Two"),))
        await pilot.pause()
        assert not summary.display
        menu.pop_page()
        await pilot.pause()
        assert summary.display and str(summary.content) == description
        menu.pop_page()
        await pilot.pause()
        assert not summary.display
