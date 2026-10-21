"""Adapt a numerical particle engine to Braille cells and damaged terminal rows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip

from ouro_tui_themes.dot_matrix.field import (
    EngineFactory,
    Field,
    ParticleGrid,
    PhosphorField,
    Prepare,
)

_BRAILLE_BITS = (1, 8, 2, 16, 4, 32, 64, 128)
_THRESHOLDS = (0.08, 0.48, 0.68, 0.28, 0.23, 0.63, 0.43, 0.13)
_COLOR_THRESHOLDS = tuple(value * 0.25 for value in _THRESHOLDS)
_GLYPHS = (" ",) + tuple(chr(0x2800 + mask) for mask in range(1, 256))


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
def _led_style(rgb: int | None) -> Style:
    return (
        Style()
        if rgb is None
        else Style(color=Color.from_rgb(rgb >> 16, (rgb >> 8) & 255, rgb & 255))
    )


class PhosphorRaster:
    """One numerical engine and reusable terminal encoding buffers per size."""

    def __init__(self, style: DotArtwork, columns: int, rows: int) -> None:
        self.columns, self.rows = columns, rows
        grid = ParticleGrid.create(columns * 2, rows * 4, span=style.span)
        self._engine = style.engine_factory(grid, style.prepare)
        self._luminance = np.empty(grid.width * grid.height, dtype=np.float64)
        self._lanes = self._dot_views(self._luminance)
        shape = (rows, columns)
        self._peak = np.empty(shape, dtype=np.float64)
        self._color = np.empty(shape, dtype=np.float64)
        self._above = np.empty(shape, dtype=np.bool_)
        self._visible = np.empty(shape, dtype=np.bool_)
        self._masks = np.empty(shape, dtype=np.uint32)
        self._channel = np.empty(shape, dtype=np.uint32)
        self._packed = np.empty(shape, dtype=np.uint32)
        self._cells = np.zeros(shape, dtype=np.uint32)
        self._changed = np.empty(shape, dtype=np.bool_)
        self._lines: tuple[Strip, ...] = ()
        self.damage: tuple[Region, ...] = ()

    def invalidate_frame(self) -> None:
        """Require a full repaint while retaining the size-owned numerical engine."""
        self._lines = ()
        self.damage = ()

    def _dot_views(self, field: Field) -> tuple[Field, ...]:
        """Eight strided views of row-major dots; no dot-to-cell transpose/copy."""
        blocks = np.asarray(field).reshape(self.rows, 4, self.columns, 2)
        return tuple(blocks[:, dy, :, dx] for dy in range(4) for dx in range(2))

    def frame(
        self,
        seconds: float,
        elapsed: float,
        *,
        monochrome: bool = False,
        background: tuple[int, int, int] = (0, 0, 0),
    ) -> tuple[Strip, ...]:
        mapped_channels = self._engine.frame(seconds, elapsed)
        np.maximum(mapped_channels[0], mapped_channels[1], out=self._luminance)
        np.maximum(self._luminance, mapped_channels[2], out=self._luminance)
        self._peak[:] = self._lanes[0]
        for lane in self._lanes[1:]:
            np.maximum(self._peak, lane, out=self._peak)
        if not monochrome:
            self._peak *= 0.30
        self._masks.fill(0)
        thresholds = _THRESHOLDS if monochrome else _COLOR_THRESHOLDS
        for lane, bit, threshold in zip(self._lanes, _BRAILLE_BITS, thresholds, strict=True):
            np.greater(lane, threshold, out=self._above)
            if not monochrome:
                np.greater_equal(lane, self._peak, out=self._visible)
                self._above &= self._visible
            # Fade by coverage as well as color. Dark foregrounds may be raised
            # by terminals, but absent dots cannot be brightened.
            np.bitwise_or(self._masks, bit, out=self._masks, where=self._above)
        self._packed[:] = self._masks
        for mapped, base, shift in zip(mapped_channels, background, (24, 16, 8), strict=True):
            lanes = self._dot_views(mapped)
            self._color[:] = lanes[0]
            for lane in lanes[1:]:
                self._color += lane
            self._color *= 1.25 / 8
            np.clip(self._color, 0.0, 1.0, out=self._color)
            # Screen-compose emitted light over the actual canvas. Zero light
            # leaves the background unchanged, including in individual channels.
            self._color *= 255 - base
            self._color += base
            np.copyto(self._channel, self._color, casting="unsafe")
            np.left_shift(self._channel, shift, out=self._channel)
            self._packed |= self._channel
        np.equal(self._masks, 0, out=self._above)
        np.copyto(self._packed, 0, where=self._above)
        np.not_equal(self._packed, self._cells, out=self._changed)
        if not self._lines:
            self._changed.fill(True)
        changed_rows = np.flatnonzero(self._changed.any(axis=1))
        if not len(changed_rows):
            self.damage = ()
            return self._lines
        self._cells[:] = self._packed
        rows = list(self._lines) if self._lines else [Strip.blank(self.columns)] * self.rows
        damage = []
        for row in changed_rows.tolist():
            changed = np.flatnonzero(self._changed[row])
            first, last = int(changed[0]), int(changed[-1])
            segments: list[Segment] = []
            run: list[str] = []
            previous: int | None = None
            # Cross the Python/Rich boundary only for rows with changed final cells.
            for cell in self._packed[row].tolist():
                key = cell >> 8 if cell else None
                if run and key != previous:
                    segments.append(Segment("".join(run), _led_style(previous)))
                    run.clear()
                run.append(_GLYPHS[cell & 255])
                previous = key
            if run:
                segments.append(Segment("".join(run), _led_style(previous)))
            rows[row] = Strip(segments, self.columns)
            damage.append(Region(first, row, last - first + 1, 1))
        self._lines = tuple(rows)
        self.damage = tuple(damage)
        return self._lines
