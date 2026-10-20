from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App

from ouro.tui.commands import TuiCommand, TuiCommandArgument
from ouro.tui.widgets import CommandPopup


class PopupApp(App):
    CSS = """
    CommandPopup {height: auto; max-height: 14; padding: 1 2; overflow-y: hidden;}
    #command-popup-heading {dock: top; height: 1;}
    #command-popup-content {width: 100%; height: auto;}
    """

    def compose(self):
        yield CommandPopup()


@pytest.mark.parametrize("operation", ("open", "navigate", "resize"))
@pytest.mark.parametrize("count", (2000, 10000))
async def test_large_menu_formats_only_viewport_rows(monkeypatch, operation, count):
    arguments = tuple(TuiCommandArgument(f"file-{index:05}.txt", "") for index in range(count))
    app = PopupApp()
    async with app.run_test(size=(100, 32)) as pilot:
        popup = app.query_one(CommandPopup)
        truncated = 0
        original = Text.truncate

        def counted_truncate(self, *args, **kwargs):
            nonlocal truncated
            truncated += 1
            return original(self, *args, **kwargs)

        if operation != "open":
            popup.show_arguments(TuiCommand("@file", "Files", ""), arguments, heading="Files")
            await pilot.pause()
        monkeypatch.setattr(Text, "truncate", counted_truncate)
        if operation == "open":
            popup.show_arguments(TuiCommand("@file", "Files", ""), arguments, heading="Files")
        elif operation == "navigate":
            popup.navigate(1)
        else:
            await pilot.resize_terminal(44, 22)
        await pilot.pause()
        assert popup.selected_argument == arguments[operation == "navigate"]
        # Several layout/paint passes are allowed; formatting the whole catalog is not.
        assert truncated < 100


async def test_navigation_does_not_traverse_the_catalog(monkeypatch):
    arguments = tuple(TuiCommandArgument(f"file-{index:05}.txt", "") for index in range(10000))
    app = PopupApp()
    async with app.run_test(size=(44, 22)) as pilot:
        popup = app.query_one(CommandPopup)
        popup.show_arguments(TuiCommand("@file", "Files", ""), arguments, heading="Files")
        await pilot.pause()
        label_reads = 0
        original = TuiCommandArgument.display_label.fget

        def display_label(self):
            nonlocal label_reads
            label_reads += 1
            return original(self)

        monkeypatch.setattr(TuiCommandArgument, "display_label", property(display_label))
        for offset, expected in ((-1, 9999), (1, 0), (50, 50), (-25, 25)):
            popup.navigate(offset)
            await pilot.pause()
            assert popup.selected_argument == arguments[expected]
            row_y = popup._content.region.y + expected
            assert popup._heading_widget.region.bottom <= row_y < popup.content_region.bottom
            strip = popup._content.render_line(expected)
            assert strip.text.startswith("› ")
        assert label_reads < 100


async def test_rows_keep_single_line_geometry_and_cell_alignment():
    arguments = (
        TuiCommandArgument("宽名字", "description"),
        TuiCommandArgument("n\na\tme", "description"),
        TuiCommandArgument("long" * 100, ""),
    )
    app = PopupApp()
    async with app.run_test(size=(44, 22)) as pilot:
        popup = app.query_one(CommandPopup)
        popup.show_arguments(TuiCommand("@file", "Files", ""), arguments[:2])
        await pilot.pause()
        rows = [popup._content.render_line(index) for index in range(2)]
        assert "\n" not in rows[1].text and "\t" not in rows[1].text
        assert "n a me" in rows[1].text
        assert [Text(row.text.split("description")[0]).cell_len for row in rows] == [10, 10]
        popup.show_arguments(TuiCommand("@file", "Files", ""), arguments[2:])
        await pilot.pause()
        row = popup._content.render_line(0)
        assert row.cell_length == popup.scrollable_content_region.width
        assert row.text.endswith("…")
        assert popup._content.size.height == 1


async def test_suggestion_snapshot_tracks_query_replacement_aliases_and_dismissal():
    command = TuiCommand(
        "/control",
        "Control",
        "",
        "[start|stop]",
        aliases=("/CTL",),
        argument_options=(TuiCommandArgument("start", ""), TuiCommandArgument("stop", "")),
    )
    app = PopupApp()
    async with app.run_test():
        popup = app.query_one(CommandPopup)
        popup.show_arguments(command, command.argument_options, query="st")
        assert popup.suggestion_for("/control st") == "art"
        popup.navigate(1)
        assert popup.suggestion_for("/ctl st") == "op"
        assert popup.suggestion_for("/control s") is None
        assert popup.suggestion_for("/other st") is None
        popup.show_arguments(command, (command.argument_options[0],), query="sta")
        assert popup.suggestion_for("/control st") is None
        assert popup.suggestion_for("/control sta") == "rt"
        popup.hide()
        assert popup.suggestion_for("/control sta") is None
