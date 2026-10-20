"""Textual presentation and lifecycle for the selected welcome artwork."""

from __future__ import annotations

import asyncio
from time import monotonic

from textual import events
from textual.geometry import Region, Size
from textual.strip import Strip
from textual.widget import Widget
from textual.worker import Worker

from ouro.tui.welcome.styles.dot_matrix.raster import DotArtwork, FrameStrip, PhosphorRaster


def _render_frame(
    artwork: DotArtwork,
    raster: PhosphorRaster | None,
    size: Size,
    seconds: float,
    delta: float,
    monochrome: bool,
    background: tuple[int, int, int],
) -> tuple[PhosphorRaster, tuple[Strip, ...], tuple[Region, ...]]:
    """Compute with exclusive raster ownership and no access to mounted widgets."""
    if raster is None or (raster.columns, raster.rows) != (size.width, size.height):
        raster = PhosphorRaster(artwork, size.width, size.height)
    lines = raster.frame(seconds, delta, monochrome=monochrome, background=background)
    return raster, lines, raster.damage


class DotMatrix(Widget):
    """Sample off-loop at up to 30 Hz, publishing only the current viewport."""

    DEFAULT_CSS = """
    DotMatrix { width: 100%; height: 1fr; min-height: 0; overflow: hidden; }
    """

    def __init__(self, style: DotArtwork) -> None:
        super().__init__()
        self.artwork = style
        self.active = False
        self.elapsed = 0.0
        self._last_tick: float | None = None
        self._frame_requested = asyncio.Event()
        self._raster: PhosphorRaster | None = None
        self._lines: tuple[Strip, ...] = ()
        self._rendering = False
        self._render_revision = 0
        self._pending_frame: tuple[Size, float, float, bool, tuple[int, int, int]] | None = None

    def on_mount(self) -> None:
        _worker: Worker[None] = self.run_worker(self._render_frames, group="welcome-artwork")
        self._paint(0)

    def on_unmount(self) -> None:
        self._render_revision += 1
        self._pending_frame = None
        self._frame_requested.set()
        self._raster = None
        self._lines = ()

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        self._render_revision += 1
        self._last_tick = None
        self._pending_frame = None
        self._frame_requested.set()
        if self.is_mounted and (
            active
            or self._raster is None
            or (self._raster.columns, self._raster.rows) != (self.size.width, self.size.height)
        ):
            self._paint(0)

    def _tick(self) -> None:
        now = monotonic()
        delta = 0.0 if self._last_tick is None else now - self._last_tick
        self._last_tick = now
        self.elapsed += delta
        self._paint(delta)

    def on_resize(self, event: events.Resize) -> None:
        # A new size needs a frame even when animation playback is paused.
        self._paint(0)

    def refresh_theme(self) -> None:
        if self.is_mounted:
            self._paint(0)

    def _paint(self, delta: float) -> None:
        self._render_revision += 1
        self._pending_frame = None
        size = self.size
        columns, rows = size
        if columns < 1 or rows < 1:
            return
        self._pending_frame = (
            size,
            self.elapsed,
            delta,
            self.app.no_color,
            self.background_colors[1].rgb,
        )
        self._frame_requested.set()

    async def _render_frames(self) -> None:
        """One lifetime-owned worker; no frame queue or per-frame Textual workers.

        Deadlines are measured from the start of computation. Slow frames yield
        before continuing at current playback time, without waiting an extra tick
        or replaying missed frames. Geometry and static repaint requests wake the
        same loop, including when playback is paused.
        """
        deadline = 0.0
        try:
            # A worker can start before the Mount handler has completed.
            # Textual cancels this owned worker when the widget is removed.
            while True:
                if self._pending_frame is None:
                    if self.active and self.size.area:
                        delay = max(0.0, deadline - monotonic())
                        if delay:
                            try:
                                await asyncio.wait_for(self._frame_requested.wait(), delay)
                            except TimeoutError:
                                pass
                        else:
                            await asyncio.sleep(0)
                        if self._pending_frame is None and self.active:
                            self._tick()
                    else:
                        await self._frame_requested.wait()
                self._frame_requested.clear()
                if self._pending_frame is None:
                    continue
                revision = self._render_revision
                size, seconds, delta, monochrome, background = self._pending_frame
                self._pending_frame = None
                self._rendering = True
                started = monotonic()
                # Keep cadence through timer jitter, but do not queue missed frames.
                deadline = (deadline if 0 <= started - deadline < 1 / 30 else started) + 1 / 30
                raster, lines, damage = await asyncio.to_thread(
                    _render_frame,
                    self.artwork,
                    self._raster,
                    size,
                    seconds,
                    delta,
                    monochrome,
                    background,
                )
                self._rendering = False
                if not self.is_mounted:
                    continue
                if size != self.size:
                    self._raster = None
                    continue
                self._raster = raster
                if revision != self._render_revision:
                    # Numerical state belongs to this size; the terminal baseline
                    # is valid only for frames that were actually published.
                    raster.invalidate_frame()
                    continue
                self._lines = lines
                if damage:
                    self.refresh(*damage)
        finally:
            self._rendering = False

    def render_line(self, y: int) -> Strip:
        if y < len(self._lines):
            return (
                self._lines[y]
                .apply_style(self.rich_style)
                .adjust_cell_length(self.size.width, self.rich_style)
            )
        return Strip.blank(self.size.width, self.rich_style)

    def render_lines(self, crop: Region) -> list[Strip]:
        # Preserve Textual CSS, opacity, filters and cropping before transferring
        # disposable frame rows to the compositor.
        return [FrameStrip(line, line.cell_length) for line in super().render_lines(crop)]
