from __future__ import annotations

from dataclasses import replace

import pytest
from rich.table import Table
from textual.app import App
from textual.widgets import OptionList

from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice
from tests.tui.helpers import resize_terminal


class ChoiceApp(App):
    CSS = """
    #choice-panel {width: 72; height: auto; max-height: 27;}
    #choice-title {height: 2;}
    #choice-filter {height: 3;}
    #choice-list {height: auto; max-height: 20;}
    #choice-hint {height: 2;}
    """


@pytest.mark.parametrize("operation", ("open", "update", "resize"))
async def test_large_status_menu_renders_only_visible_tables(monkeypatch, operation):
    choices = tuple(
        Choice(str(index), f"Entry {index}", trailing="Ready", group=f"Group {index // 100}")
        for index in range(1000)
    )
    app = ChoiceApp()
    async with app.run_test(size=(100, 32)) as pilot:
        menu = ChoiceScreen(title="ENTRIES", placeholder="Filter…", choices=choices)
        if operation != "open":
            await app.push_screen(menu)
            await pilot.pause()
        renders = 0
        original = Table.__rich_console__

        def counted_render(self, console, options):
            nonlocal renders
            renders += 1
            return original(self, console, options)

        monkeypatch.setattr(Table, "__rich_console__", counted_render)
        if operation == "open":
            await app.push_screen(menu)
        elif operation == "update":
            menu.update_choices((replace(choices[0], trailing="Changed"), *choices[1:]))
        else:
            await resize_terminal(pilot, 60, 24)
        await pilot.pause()
        assert menu.query_one(OptionList).option_count > len(choices)
        assert renders < 150


@pytest.mark.parametrize("width", (5, 20, 70))
async def test_menu_table_geometry_matches_rich_at_narrow_and_wide_widths(width):
    choices = (
        Choice("status", "Wide 中文 title\nSecond line", trailing="Ready\nMore\nDetails"),
        Choice("badge", "Badge", trailing_badge="[1]", group="Group\nTwo", group_trailing="OK"),
        Choice("palette", "Color\nName", swatches=("#ff0000", "#00ff00")),
    )
    app = ChoiceApp()
    async with app.run_test(size=(100, 32)):
        menu = ChoiceScreen(title="GEOMETRY", placeholder="Filter…", choices=choices)
        await app.push_screen(menu)
        options = menu.query_one(OptionList)
        for option in options.options:
            if isinstance(option.prompt, Table):
                visual = options._get_visual(option)
                expected = len(
                    app.console.render_lines(option.prompt, app.console.options.update_width(width))
                )
                assert visual.get_height(options.styles, width) == expected
