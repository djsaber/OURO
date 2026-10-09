"""Phosphor persistence and a fixed Braille LED lattice projected to Strips."""

from __future__ import annotations

from array import array
from functools import lru_cache
from math import exp

import arrayfunc as af  # type: ignore[import-untyped]
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.geometry import Region
from textual.strip import Strip

from ouro.tui.welcome.dot_matrix.field import DotArtwork, DotGrid, Field, buffer, clip, maximum

_BRAILLE_BITS = (1, 8, 2, 16, 4, 32, 64, 128)
_THRESHOLDS = (0.08, 0.48, 0.68, 0.28, 0.23, 0.63, 0.43, 0.13)


@lru_cache(maxsize=4096)
def _led_style(red: int, green: int, blue: int) -> Style:
    return Style(color=Color.from_rgb(red, green, blue))


class PhosphorRaster:
    """One size, one prepared sampler, bounded persistence and encoding buffers."""

    def __init__(self, style: DotArtwork, columns: int, rows: int) -> None:
        self.grid = DotGrid.create(columns, rows)
        self.style = style
        self._sample = style.prepare(self.grid)
        dots, cells = len(self.grid.x), columns * rows
        self._emission = tuple(buffer(dots) for _ in range(3))
        self._mapped = tuple(buffer(dots) for _ in range(3))
        self._luminance, self._scratch = buffer(dots), buffer(dots)
        self._padded = buffer(dots + columns * 4)
        self._zero_column = buffer(rows * 4)
        self._lanes = tuple(buffer(cells) for _ in range(8))
        self._color, self._color_scratch = buffer(cells), buffer(cells)
        self._rgb = tuple(array("B", [0]) * cells for _ in range(3))
        self._cells = array("I", [0]) * cells
        self._lines: tuple[Strip, ...] = ()
        self.damage: tuple[Region, ...] = ()

    def _map_light(self, light: Field, emission: Field, mapped: Field, decay: float) -> None:
        af.mul(emission, decay)
        maximum(light, emission, emission, self._scratch)
        width = self.grid.columns * 2
        padded = self._padded
        padded[width:-width] = emission
        af.add(padded[: -2 * width], padded[2 * width :], mapped)
        left = padded[width - 1 : -width - 1]
        right = padded[width + 1 : -width + 1]
        # Horizontal neighbours stop at row boundaries; vertical padding is zero.
        left[::width] = self._zero_column
        right[width - 1 :: width] = self._zero_column
        af.add(mapped, left)
        af.add(mapped, right)
        af.mul(mapped, 0.075)
        af.add(mapped, emission)
        af.mul(mapped, -1.5)
        af.expm1(mapped)
        af.neg(mapped)
        af.pow(mapped, 0.75)

    def _split_dots(self, field: Field) -> None:
        """Project dot-row storage onto the eight fixed positions of each cell."""
        columns = self.grid.columns
        width = columns * 2
        for dot, lane in enumerate(self._lanes):
            dy, dx = divmod(dot, 2)
            for row in range(self.grid.rows):
                start = (row * 4 + dy) * width
                lane[row * columns : (row + 1) * columns] = field[start + dx : start + width : 2]

    def frame(
        self, seconds: float, elapsed: float, *, monochrome: bool = False
    ) -> tuple[Strip, ...]:
        decay = exp(-max(0, elapsed) / 0.085)
        for light, emission, mapped in zip(
            self._sample(seconds), self._emission, self._mapped, strict=True
        ):
            self._map_light(light, emission, mapped, decay)
        maximum(self._mapped[0], self._mapped[1], self._luminance, self._scratch)
        maximum(self._luminance, self._mapped[2], self._luminance, self._scratch)
        self._split_dots(self._luminance)
        masks = []
        for values in zip(*self._lanes, strict=True):
            if monochrome:
                mask = sum(
                    bit
                    for value, bit, threshold in zip(
                        values, _BRAILLE_BITS, _THRESHOLDS, strict=True
                    )
                    if value > threshold
                )
            else:
                peak = max(values)
                mask = (
                    255
                    if peak < 0.05
                    else sum(
                        bit
                        for value, bit in zip(values, _BRAILLE_BITS, strict=True)
                        if value >= peak * 0.30
                    )
                )
            masks.append(mask)
        for mapped, minimum, rgb in zip(
            self._mapped, (0.055, 0.065, 0.067), self._rgb, strict=True
        ):
            self._split_dots(mapped)
            self._color[:] = self._lanes[0]
            for lane in self._lanes[1:]:
                af.add(self._color, lane)
            af.mul(self._color, 1.25 / 8)
            clip(self._color, minimum, 1.0, self._color_scratch)
            af.mul(self._color, 255.0)
            af.convert(self._color, rgb)
        rows = []
        damage = []
        colors = zip(*self._rgb, strict=True)
        for row in range(self.grid.rows):
            segments: list[Segment] = []
            run = ""
            previous: tuple[int, int, int] | None = None
            first, last = self.grid.columns, -1
            for index in range(row * self.grid.columns, (row + 1) * self.grid.columns):
                key = next(colors)
                cell = (key[0] << 24) | (key[1] << 16) | (key[2] << 8) | masks[index]
                if not self._lines or self._cells[index] != cell:
                    column = index % self.grid.columns
                    first, last = min(first, column), column
                    self._cells[index] = cell
                if previous is not None and key != previous:
                    segments.append(Segment(run, _led_style(*previous)))
                    run = ""
                run += chr(0x2800 + masks[index])
                previous = key
            if previous is not None:
                segments.append(Segment(run, _led_style(*previous)))
            if last < 0:
                rows.append(self._lines[row])
            else:
                rows.append(Strip(segments, self.grid.columns))
                damage.append(Region(first, row, last - first + 1, 1))
        self._lines = tuple(rows)
        self.damage = tuple(damage)
        return self._lines
