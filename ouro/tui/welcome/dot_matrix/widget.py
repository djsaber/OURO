"""Textual presentation and lifecycle for the selected welcome artwork."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from time import monotonic

from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.geometry import Region, Size
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from ouro.tui.theme import theme_for
from ouro.tui.welcome.centered import CenteredWelcomePage
from ouro.tui.welcome.dot_matrix.field import DotArtwork, DotGrid, Sampler
from ouro.tui.welcome.dot_matrix.raster import PhosphorRaster
from ouro.tui.welcome.model import WelcomeStyle


def _render_frame(
    artwork: DotArtwork,
    raster: PhosphorRaster | None,
    size: Size,
    seconds: float,
    delta: float,
    monochrome: bool,
) -> tuple[PhosphorRaster, tuple[Strip, ...], tuple[Region, ...]]:
    """Compute with exclusive raster ownership and no access to mounted widgets."""
    if raster is None:
        raster = PhosphorRaster(artwork, size.width, size.height)
    lines = raster.frame(seconds, delta, monochrome=monochrome)
    return raster, lines, raster.damage


class DotMatrix(Widget):
    """Sample off-loop at up to 30 Hz, publishing only the current viewport."""

    DEFAULT_CSS = """
    DotMatrix { width: 100%; height: 1fr; min-height: 0; overflow: hidden; }
    """

    def __init__(self, style: DotArtwork, *, motion: bool) -> None:
        super().__init__()
        self.artwork = style
        self.motion = motion
        self.active = False
        self.elapsed = 0.0
        self._last_tick: float | None = None
        self._timer: Timer | None = None
        self._raster: PhosphorRaster | None = None
        self._lines: tuple[Strip, ...] = ()
        self._rendering = False
        self._render_revision = 0
        self._pending_frame: tuple[Size, float, float, bool] | None = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(1 / 30, self._tick, pause=True)
        self._sync_timer()

    def on_unmount(self) -> None:
        self._render_revision += 1
        self._pending_frame = None
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._raster = None
        self._lines = ()

    def set_playback(self, *, active: bool, motion: bool) -> None:
        if (active, motion) == (self.active, self.motion):
            return
        self.active, self.motion = active, motion
        self._render_revision += 1
        self._sync_timer()
        if self.is_mounted and (active or self._raster is None):
            self._paint(0)

    def _sync_timer(self) -> None:
        self._last_tick = None
        if self._timer is not None:
            if self.active and self.motion:
                self._timer.resume()
            else:
                self._timer.pause()

    def _tick(self) -> None:
        if (
            not self.active
            or not self.motion
            or self.app.screen is not self.screen
            or not self.visible
            or not self.display
        ):
            self._last_tick = None
            return
        if self._rendering:
            return
        now = monotonic()
        delta = 0.0 if self._last_tick is None else min(0.1, now - self._last_tick)
        self._last_tick = now
        self.elapsed += delta
        self._paint(delta)

    def on_resize(self, event: events.Resize) -> None:
        self._raster = None
        # A new size needs a frame even when animation playback is paused.
        self._paint(0)

    def _paint(self, delta: float) -> None:
        self._render_revision += 1
        self._pending_frame = None
        size = self.size
        columns, rows = size
        if columns < 1 or rows < 1:
            return
        self._pending_frame = size, self.elapsed, delta, self.app.no_color
        if not self._rendering:
            self._rendering = True
            self.run_worker(self._render_frames(), group="welcome-artwork")

    async def _render_frames(self) -> None:
        try:
            while self._pending_frame is not None:
                revision = self._render_revision
                size, seconds, delta, monochrome = self._pending_frame
                self._pending_frame = None
                raster, lines, damage = await asyncio.to_thread(
                    _render_frame,
                    self.artwork,
                    self._raster,
                    size,
                    seconds,
                    delta,
                    monochrome,
                )
                if not self.is_mounted or revision != self._render_revision:
                    # Its persistence and damage buffers advanced without being
                    # displayed. The next accepted frame needs a fresh baseline.
                    if self._raster is raster:
                        self._raster = None
                    continue
                self._raster, self._lines = raster, lines
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


class DotWelcomePage(CenteredWelcomePage):
    """A phosphor package surface; captions and LED rendering are package-owned."""

    DEFAULT_CSS = """
    DotWelcomePage .phosphor-art {
        layout: vertical; width: 100%; height: 1fr; min-height: 0; margin-bottom: 1;
    }
    DotWelcomePage .art-caption { width: 100%; height: 1; text-align: center; }
    """

    def __init__(self, artwork: DotArtwork) -> None:
        self.artwork = artwork
        self.matrix = DotMatrix(artwork, motion=True)
        self._caption = Static(classes="art-caption")
        super().__init__(Vertical(self.matrix, self._caption, classes="phosphor-art"))

    def on_mount(self) -> None:
        self.refresh_theme()

    def set_playback(self, *, active: bool, motion: bool) -> None:
        self.matrix.set_playback(active=active, motion=motion)

    def refresh_theme(self) -> None:
        theme = theme_for(self)
        caption = Text("O U R O", style=theme.text_primary)
        caption.append(f"  /  {self.artwork.label.upper()}", style=theme.text_faint)
        self._caption.update(caption)


def dot_style(
    style_id: str, label: str, description: str, prepare: Callable[[DotGrid], Sampler]
) -> WelcomeStyle:
    """Package factory shortcut; the welcome framework has no sampling contract."""
    return WelcomeStyle(
        style_id, label, description, partial(DotWelcomePage, DotArtwork(label, prepare))
    )
