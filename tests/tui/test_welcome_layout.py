from __future__ import annotations

import asyncio

import pytest
from textual import events

from ouro.tui.app import OuroTuiApp
from ouro.tui.welcome import WELCOME_STYLES
from ouro.tui.welcome.dot_matrix.widget import DotWelcomePage
from ouro.tui.widgets import CommandPopup, Composer, WelcomePanel
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize(
    "keys",
    [("slash",), ("slash", *"approval", "space"), ("@",), ("@", *"file", "enter")],
    ids=["commands", "arguments", "references", "files"],
)
async def test_welcome_menu_stays_attached_above_input_across_terminal_heights(tmp_path, keys):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 33)) as pilot:
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        input_region = composer.region
        await pilot.press(*keys)
        if workers := [worker for worker in app.workers if worker.group == "attachment-directory"]:
            await app.workers.wait_for_complete(workers)
        await pilot.pause()
        assert composer.region == input_region

        for width, height in ((100, 33), (100, 32), (100, 31), (40, 17), (40, 16), (100, 33)):
            await pilot.resize_terminal(width, height)
            assert popup.display and popup.region in app.screen.region
            assert popup.region.bottom == composer.region.y
            assert popup.region.x == composer.region.x
            assert popup.region.right == composer.region.right
            assert app.focused is composer

        await pilot.press("down", "up")
        input_region = composer.region
        await pilot.press("escape")
        await pilot.pause()
        assert not popup.display and composer.region == input_region


async def test_animation_keeps_editor_layout_and_cursor_stable_during_cjk_edits(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(140, 50)) as pilot:
        welcome = app.query_one(WelcomePanel)
        await welcome.set_welcome_style(WELCOME_STYLES["clifford-torus"], motion=True)
        await pilot.pause()
        assert isinstance(welcome.page, DotWelcomePage)
        matrix = welcome.page.matrix
        composer = app.query_one(Composer)
        before = composer.region, welcome.input_surface.region, matrix.region
        elapsed = matrix.elapsed
        # Committed CJK input is available to the application; native IME preedit
        # stays inside the terminal and is covered by the console boundary tests.
        expected = ""
        for _ in range(4):
            for key in ("你", "好", "世", "界", "backspace", "backspace", "backspace", "backspace"):
                deleting = key == "backspace"
                expected = expected[:-1] if deleting else expected + key
                app.post_message(events.Key(key, None if deleting else key))
                async with asyncio.timeout(2):
                    while composer.text != expected:
                        await asyncio.sleep(0.01)
                assert (composer.region, welcome.input_surface.region, matrix.region) == before
                assert app.cursor_position == composer.cursor_screen_offset
                assert app.focused is composer
        assert composer.text == ""
        assert matrix.elapsed > elapsed
