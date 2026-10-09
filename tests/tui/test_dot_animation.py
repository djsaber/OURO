from __future__ import annotations

import asyncio
import threading

import pytest
from textual.app import App
from textual.widgets import TextArea

from ouro.tui.welcome import WELCOME_STYLES
from ouro.tui.welcome.dot_matrix.raster import PhosphorRaster
from ouro.tui.welcome.dot_matrix.widget import DotMatrix


class ArtworkApp(App):
    CSS = "DotMatrix {width: 38; height: 8;} TextArea {height: 4;}"

    def compose(self):
        yield DotMatrix(WELCOME_STYLES["event-horizon"].create().artwork, motion=False)
        yield TextArea()


async def test_frame_computation_does_not_block_editing_or_publish_obsolete_size(monkeypatch):
    started, release = threading.Event(), threading.Event()
    threads = []
    original = PhosphorRaster.frame

    def slow_frame(raster, *args, **kwargs):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(3), "frame computation blocked the input event loop"
        return original(raster, *args, **kwargs)

    app = ArtworkApp()
    async with app.run_test() as pilot:
        matrix = app.query_one(DotMatrix)
        editor = app.query_one(TextArea)
        editor.focus()
        monkeypatch.setattr(PhosphorRaster, "frame", slow_frame)
        try:
            matrix.set_playback(active=True, motion=True)
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            assert all(thread != threading.get_ident() for thread in threads)
            await pilot.press("a", "b", "backspace", "c")
            assert editor.text == "ac"
            assert app.focused is editor
            assert app.cursor_position == editor.cursor_screen_offset
            # Resizes coalesce while the old viewport is still being computed.
            matrix.styles.width = 30
            await pilot.pause()
            matrix.styles.width = 32
            await pilot.pause()
            assert len(threads) == 1
        finally:
            release.set()
        matrix.set_playback(active=False, motion=True)
        for _ in range(100):
            if matrix._raster is not None and matrix._raster.grid.columns == 32:
                break
            await asyncio.sleep(0.01)
        assert matrix._raster is not None and matrix._raster.grid.columns == 32
        assert all(line.cell_length == 32 for line in matrix._lines)


@pytest.mark.parametrize("unmount", [False, True])
async def test_inflight_frame_cannot_publish_after_pause_or_unmount(monkeypatch, unmount):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = PhosphorRaster.frame

    def slow_frame(raster, *args, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return original(raster, *args, **kwargs)
        finally:
            finished.set()

    app = ArtworkApp()
    async with app.run_test() as pilot:
        matrix = app.query_one(DotMatrix)
        async with asyncio.timeout(2):
            while matrix._rendering:
                await asyncio.sleep(0.01)
        before = matrix._lines
        monkeypatch.setattr(PhosphorRaster, "frame", slow_frame)
        matrix.set_playback(active=True, motion=True)
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            if unmount:
                await asyncio.wait_for(matrix.remove(), 1)
                assert matrix._timer is None
            else:
                matrix.set_playback(active=False, motion=True)
                assert matrix._lines is before
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await pilot.pause()
        assert matrix._lines == () if unmount else matrix._lines is before
        if not unmount:
            matrix.set_playback(active=True, motion=False)
            async with asyncio.timeout(2):
                while matrix._rendering:
                    await asyncio.sleep(0.01)
            assert matrix._raster is not None
            assert sum(region.area for region in matrix._raster.damage) == matrix.size.area


def test_raster_reuses_unchanged_rows_and_bounds_damage_to_changed_cells():
    artwork = WELCOME_STYLES["electron-bloom"].create().artwork
    raster = PhosphorRaster(artwork, 80, 20)
    first = raster.frame(0, 0)
    same = raster.frame(0, 0)
    assert all(before is after for before, after in zip(first, same, strict=True))
    assert not raster.damage
    changed = raster.frame(7.5, 1 / 30)
    assert raster.damage

    def cells(row):
        return [(char, segment.style) for segment in row for char in segment.text]

    covered = {
        (x, y) for region in raster.damage for y in region.line_range for x in region.column_range
    }
    different = {
        (x, y)
        for y, (before, after) in enumerate(zip(same, changed, strict=True))
        for x, (left, right) in enumerate(zip(cells(before), cells(after), strict=True))
        if left != right
    }
    assert different and different <= covered
    assert len(covered) < 80 * 20 // 2
