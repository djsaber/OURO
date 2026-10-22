from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.pilot import OutOfBounds
from textual.widgets import Input

from tests.tui.helpers import resize_terminal


class ResizeApp(App):
    CSS = "Input { dock: bottom; height: 3; }"

    def compose(self) -> ComposeResult:
        yield Input(value="preserved draft")


@pytest.mark.parametrize("initial,target", [((100, 32), (40, 12)), ((40, 12), (100, 32))])
async def test_resize_waits_for_deferred_layout_before_using_widget_coordinates(
    monkeypatch, initial, target
):
    app = ResizeApp()
    async with app.run_test(size=initial) as pilot:
        await app.screen.wait_for_refresh()
        editor = app.query_one(Input)
        before = editor.region
        check_resize = app._check_resize
        resize_returned = asyncio.Event()
        raw_resize = pilot.resize_terminal

        # Textual updates App.size before its timer dispatches Resize to the
        # screen. Hold that dispatch, without changing the layout or coordinates.
        monkeypatch.setattr(app, "_check_resize", lambda: None)

        async def record_resize(width, height):
            await raw_resize(width, height)
            resize_returned.set()

        monkeypatch.setattr(pilot, "resize_terminal", record_resize)
        resizing = asyncio.create_task(resize_terminal(pilot, *target))
        try:
            async with asyncio.timeout(15):
                await resize_returned.wait()
                assert app.size == Size(*target)
                assert app.screen.region.size == Size(*initial)
                assert editor.region == before
                if target[1] < initial[1]:
                    with pytest.raises(OutOfBounds):
                        await pilot.click(editor)
                assert not resizing.done(), "resize returned before the layout was applied"
                check_resize()
                await resizing
                assert app.screen.region.size == Size(*target)
                assert editor.region.bottom == target[1]
                assert editor.region.width == target[0]
                assert await pilot.click(editor)
                assert editor.value == "preserved draft"
        finally:
            check_resize()
            resizing.cancel()
            await asyncio.gather(resizing, return_exceptions=True)
