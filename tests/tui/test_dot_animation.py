from __future__ import annotations

import asyncio
import threading
import weakref
from array import array
from copy import copy

import pytest
from ouro_tui_themes.dot_matrix.raster import DotArtwork, FrameStrip, PhosphorRaster
from ouro_tui_themes.dot_matrix.widget import DotMatrix
from ouro_tui_themes.styles import STYLES
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import App
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import TextArea
from textual.worker import WorkerFailed

from ouro.tui.welcome import welcome_catalog

WELCOME_STYLES = welcome_catalog(STYLES)


class ArtworkApp(App):
    CSS = "DotMatrix {width: 38; height: 8;} TextArea {height: 4;}"

    def compose(self):
        yield DotMatrix(WELCOME_STYLES["event-horizon"].create().artwork)
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
            matrix.set_active(True)
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
        matrix.set_active(False)
        for _ in range(100):
            if matrix._raster is not None and matrix._raster.columns == 32:
                break
            await asyncio.sleep(0.01)
        assert matrix._raster is not None and matrix._raster.columns == 32
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
            while matrix._raster is None or matrix._rendering or matrix._pending_frame:
                await asyncio.sleep(0.01)
        raster = matrix._raster
        before = matrix._lines
        monkeypatch.setattr(PhosphorRaster, "frame", slow_frame)
        matrix.set_active(True)
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            if unmount:
                await asyncio.wait_for(matrix.remove(), 1)
                assert not any(worker.node is matrix for worker in app.workers)
            else:
                matrix.set_active(False)
                assert matrix._lines is before
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await pilot.pause()
        assert matrix._lines == () if unmount else matrix._lines is before
        if not unmount:
            matrix.refresh_theme()
            async with asyncio.timeout(2):
                while matrix._pending_frame is not None or matrix._rendering:
                    await asyncio.sleep(0.01)
            assert matrix._raster is raster
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


def test_compositor_rows_release_without_waiting_for_cyclic_gc():
    row = FrameStrip([Segment("abcd")], 4)
    reference = weakref.ref(row)
    assert row.divide([4])[0] is row
    assert row.divide([4, 8])[0] is row
    assert [part.text for part in row.divide([1, 4])] == ["a", "bcd"]
    assert row.text == "abcd"
    del row
    assert reference() is None


@pytest.mark.parametrize("color_system", ["truecolor", "256", "standard"])
def test_transient_frame_encoding_preserves_styles_links_crops_and_divisions(color_system):
    def segments():
        return [
            Segment("ab", Style(color="#ad4263", bgcolor="#122135", bold=True)),
            Segment("cd", Style(color="#3176be", link="https://example.com")),
        ]

    console = Console(color_system=color_system)
    original = Strip(segments(), 4)
    frame = FrameStrip([Segment(segment.text, copy(segment.style)) for segment in original], 4)
    assert frame.render(console) == original.render(console)
    assert frame.crop(1, 3).render(console) == original.crop(1, 3).render(console)
    assert [part.render(console) for part in frame.divide([1, 4])] == [
        part.render(console) for part in original.divide([1, 4])
    ]


async def test_replacement_engine_uses_same_widget_pipeline_and_one_lifetime_worker():
    prepared, sampled = [], []
    started, release = threading.Event(), threading.Event()

    def unused_sampler(grid):
        raise AssertionError("the replacement engine owns preparation")

    class Engine:
        def __init__(self, grid, prepare):
            prepared.append((grid.width, grid.height, prepare))
            self.channel = array("d", [0.0]) * (grid.width * grid.height)

        def frame(self, seconds, elapsed):
            sampled.append((seconds, elapsed, threading.get_ident()))
            if not started.is_set():
                started.set()
                assert release.wait(5), "initial frame was not released"
            return self.channel, self.channel, self.channel

    class ReplacementApp(App):
        CSS = "DotMatrix {width: 30; height: 6;} TextArea {height: 4;}"

        def compose(self):
            yield DotMatrix(DotArtwork(unused_sampler, span=(4.0, 2.0), engine_factory=Engine))
            yield TextArea()

    app = ReplacementApp()
    async with app.run_test() as pilot:
        matrix = app.query_one(DotMatrix)
        worker = next(worker for worker in app.workers if worker.node is matrix)
        try:
            assert await asyncio.to_thread(started.wait, 5)
            # Theme requests may invalidate initial rendering before its raster
            # is published. They must retain this size's numerical engine.
            for _ in range(3):
                matrix.refresh_theme()
        finally:
            release.set()
        async with asyncio.timeout(3):
            while matrix._raster is None or matrix._rendering or matrix._pending_frame:
                await asyncio.sleep(0)
        for _ in range(3):
            count = len(sampled)
            matrix.on_resize(events.Resize(matrix.size, matrix.virtual_size))
            matrix.refresh_theme()
            async with asyncio.timeout(3):
                while len(sampled) == count or matrix._rendering or matrix._pending_frame:
                    await asyncio.sleep(0)
        assert prepared == [(60, 24, unused_sampler)]
        assert all(thread != threading.get_ident() for _, _, thread in sampled)
        assert list(worker for worker in app.workers if worker.node is matrix) == [worker]
        # CSS and cropping still pass through Textual before the transient row boundary.
        assert all(
            isinstance(line, FrameStrip) for line in matrix.render_lines(Region(0, 0, 15, 2))
        )
        editor = app.query_one(TextArea)
        editor.focus()
        await pilot.press("a", "b", "backspace")
        assert editor.text == "a"
        await matrix.remove()
        assert worker.is_cancelled


def test_unchanged_frame_does_not_construct_terminal_segments(monkeypatch):
    import ouro_tui_themes.dot_matrix.raster as raster_module

    raster = PhosphorRaster(WELCOME_STYLES["electron-bloom"].create().artwork, 38, 8)
    previous = raster.frame(0, 0)

    def unexpected(*args, **kwargs):
        raise AssertionError("unchanged cells must reuse their published rows")

    monkeypatch.setattr(raster_module, "Segment", unexpected)
    current = raster.frame(0, 0)
    assert all(before is after for before, after in zip(previous, current, strict=True))


async def test_numerical_failure_is_reported_by_the_owned_worker():
    def broken(grid):
        raise RuntimeError("particle preparation failed")

    class BrokenApp(App):
        def compose(self):
            yield DotMatrix(DotArtwork(broken, span=(4.0, 2.0)))

    with pytest.raises(WorkerFailed, match="particle preparation failed"):
        async with BrokenApp().run_test() as pilot:
            await pilot.pause()
