from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from ouro.tui.text_inputs import TuiInput, TuiTextArea


class _InitiallyHiddenTextInputApp(App[None]):
    AUTO_FOCUS = None

    CSS = """
    TuiInput, TuiTextArea {
        margin: 3 7;
        width: 30;
        height: 3;
    }
    """

    def __init__(self, input_type: type[TuiInput] | type[TuiTextArea]) -> None:
        super().__init__()
        self.text_input = input_type()
        self.text_input.display = False

    def compose(self) -> ComposeResult:
        yield self.text_input


@pytest.mark.parametrize("input_type", [TuiInput, TuiTextArea])
async def test_focused_text_input_syncs_terminal_cursor_after_becoming_visible(
    input_type: type[TuiInput] | type[TuiTextArea],
) -> None:
    app = _InitiallyHiddenTextInputApp(input_type)

    async with app.run_test(size=(80, 24)) as pilot:
        with app.batch_update():
            app.text_input.display = True
            app.text_input.focus()
        await pilot.pause()

        assert app.text_input.has_focus is True
        assert app.text_input.cursor_screen_offset.x > 0
        assert app.text_input.cursor_screen_offset.y > 0
        assert app.cursor_position == app.text_input.cursor_screen_offset

        initial_cursor_position = app.cursor_position
        app.text_input.styles.margin = (5, 12)
        await pilot.pause()

        assert app.text_input.cursor_screen_offset != initial_cursor_position
        assert app.cursor_position == app.text_input.cursor_screen_offset


async def test_text_area_keeps_cursor_visible_in_same_edit_cycle() -> None:
    app = _InitiallyHiddenTextInputApp(TuiTextArea)

    async with app.run_test(size=(80, 24)) as pilot:
        app.text_input.display = True
        app.text_input.focus()
        app.text_input.load_text("first")
        app.text_input.move_cursor((0, len("first")))
        await pilot.pause()

        assert app.text_input.cursor_screen_offset in app.text_input.scrollable_content_region

        app.text_input.insert("\n")
        await pilot.pause()

        assert app.text_input.cursor_location == (1, 0)
        assert app.text_input.scroll_y == 1
        assert app.text_input.cursor_screen_offset in app.text_input.scrollable_content_region

        app.text_input.insert("\n")
        await pilot.pause()

        assert app.text_input.cursor_location == (2, 0)
        assert app.text_input.scroll_y == 2
        assert app.text_input.cursor_screen_offset in app.text_input.scrollable_content_region
