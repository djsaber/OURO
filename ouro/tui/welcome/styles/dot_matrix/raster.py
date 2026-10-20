"""Adapt a numerical particle engine to Braille cells and damaged terminal rows."""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import arrayfunc as af  # type: ignore[import-untyped]
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip

from ouro.tui.welcome.styles.dot_matrix.field import (
    EngineFactory,
    Field,
    ParticleGrid,
    PhosphorField,
    Prepare,
    buffer,
    clip,
    maximum,
)

_BRAILLE_BITS = (1, 8, 2, 16, 4, 32, 64, 128)
_THRESHOLDS = (0.08, 0.48, 0.68, 0.28, 0.23, 0.63, 0.43, 0.13)
_COLOR_THRESHOLDS = tuple(value * 0.25 for value in _THRESHOLDS)


@dataclass(frozen=True)
class DotArtwork:
    """Theme sampler, sampling span and replaceable numerical backend."""

    prepare: Prepare
    span: tuple[float, float]
    engine_factory: EngineFactory = PhosphorField


class FrameStrip(Strip):
    """Transient rows without self-referential no-op division caches.

    Textual's full-width divide caches [self], retaining dynamic rows until cyclic
    GC. A no-op needs no cache; real divisions retain Textual's implementation.
    """

    __slots__ = ("__weakref__",)

    def divide(self, cuts: Iterable[int]) -> Sequence[Strip]:
        cuts = tuple(cut for cut in cuts if cut <= self.cell_length)
        if cuts == (self.cell_length,):
            return (self,)
        return super().divide(cuts)


@lru_cache(maxsize=4096)
def _led_style(rgb: tuple[int, int, int] | None) -> Style:
    return Style() if rgb is None else Style(color=Color.from_rgb(*rgb))


class PhosphorRaster:
    """One numerical engine and reusable terminal encoding buffers per size."""

    def __init__(self, style: DotArtwork, columns: int, rows: int) -> None:
        self.columns, self.rows = columns, rows
        grid = ParticleGrid.create(columns * 2, rows * 4, span=style.span)
        self._engine = style.engine_factory(grid, style.prepare)
        dots, cells = len(grid.x), columns * rows
        self._luminance, self._scratch = buffer(dots), buffer(dots)
        self._lanes = tuple(buffer(cells) for _ in range(8))
        self._color, self._color_scratch = buffer(cells), buffer(cells)
        self._rgb = tuple(array("B", [0]) * cells for _ in range(3))
        self._cells = array("I", [0]) * cells
        self._lines: tuple[Strip, ...] = ()
        self.damage: tuple[Region, ...] = ()

    def invalidate_frame(self) -> None:
        """Require a full repaint while retaining the size-owned numerical engine."""
        self._lines = ()
        self.damage = ()

    def _split_dots(self, field: Field) -> None:
        """Project dot-row storage onto the eight fixed positions of each cell."""
        columns = self.columns
        width = columns * 2
        for dot, lane in enumerate(self._lanes):
            dy, dx = divmod(dot, 2)
            for row in range(self.rows):
                start = (row * 4 + dy) * width
                lane[row * columns : (row + 1) * columns] = field[start + dx : start + width : 2]

    def frame(
        self,
        seconds: float,
        elapsed: float,
        *,
        monochrome: bool = False,
        background: tuple[int, int, int] = (0, 0, 0),
    ) -> tuple[Strip, ...]:
        mapped_channels = self._engine.frame(seconds, elapsed)
        maximum(mapped_channels[0], mapped_channels[1], self._luminance, self._scratch)
        maximum(self._luminance, mapped_channels[2], self._luminance, self._scratch)
        self._split_dots(self._luminance)
        masks = []
        thresholds = _THRESHOLDS if monochrome else _COLOR_THRESHOLDS
        minimum = min(thresholds)
        for values in zip(*self._lanes, strict=True):
            peak = max(values)
            if peak <= minimum:
                mask = 0
            else:
                contour = 0.0 if monochrome else peak * 0.30
                # Fade by coverage as well as color: terminals may raise the
                # contrast of dark foregrounds, but cannot brighten absent dots.
                mask = sum(
                    bit
                    for value, bit, threshold in zip(values, _BRAILLE_BITS, thresholds, strict=True)
                    if value >= contour and value > threshold
                )
            masks.append(mask)
        for mapped, rgb, base in zip(mapped_channels, self._rgb, background, strict=True):
            self._split_dots(mapped)
            self._color[:] = self._lanes[0]
            for lane in self._lanes[1:]:
                af.add(self._color, lane)
            af.mul(self._color, 1.25 / 8)
            clip(self._color, 0.0, 1.0, self._color_scratch)
            # Screen-compose emitted light over the actual canvas. Zero light
            # leaves the background unchanged, including in individual channels.
            af.fma(self._color, float(255 - base), float(base))
            af.convert(self._color, rgb)
        rows = []
        damage = []
        colors = zip(*self._rgb, strict=True)
        for row in range(self.rows):
            start, end = row * self.columns, (row + 1) * self.columns
            row_colors = [next(colors) for _ in range(self.columns)]
            first, last = self.columns, -1
            # Compare final cells before constructing Rich objects. In particular,
            # dark or unchanged rows must not allocate throwaway Segments each frame.
            for column, (red, green, blue) in enumerate(row_colors):
                index = start + column
                mask = masks[index]
                cell = (red << 24) | (green << 16) | (blue << 8) | mask if mask else 0
                if not self._lines or self._cells[index] != cell:
                    first, last = min(first, column), column
                    self._cells[index] = cell
            if last < 0:
                rows.append(self._lines[row])
                continue
            segments: list[Segment] = []
            run = ""
            previous: tuple[int, int, int] | None = None
            for index, color in zip(range(start, end), row_colors, strict=True):
                mask = masks[index]
                key = color if mask else None
                if run and key != previous:
                    segments.append(Segment(run, _led_style(previous)))
                    run = ""
                run += chr(0x2800 + mask) if mask else " "
                previous = key
            if run:
                segments.append(Segment(run, _led_style(previous)))
            rows.append(Strip(segments, self.columns))
            damage.append(Region(first, row, last - first + 1, 1))
        self._lines = tuple(rows)
        self.damage = tuple(damage)
        return self._lines
