"""Phosphor persistence and a fixed Braille LED lattice projected to Strips."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

from ouro.tui.welcome.dot_matrix.field import DotArtwork, DotGrid

_BRAILLE_BITS = np.array(((1, 8), (2, 16), (4, 32), (64, 128)), dtype=np.uint8)


@lru_cache(maxsize=4096)
def _led_style(red: int, green: int, blue: int) -> Style:
    return Style(color=Color.from_rgb(red, green, blue))


class PhosphorRaster:
    """One size, one artwork, one bounded persistence buffer; no I/O or clocks."""

    def __init__(self, style: DotArtwork, columns: int, rows: int) -> None:
        self.grid = DotGrid.create(columns, rows)
        self.style = style
        self._emission = np.zeros((*self.grid.x.shape, 3))

    def frame(
        self, seconds: float, elapsed: float, *, monochrome: bool = False
    ) -> tuple[Strip, ...]:
        light = self.style.sample(self.grid, seconds)
        # Persistence stays at each LED address; it never advects or blurs positions.
        self._emission = np.maximum(light, self._emission * np.exp(-max(0, elapsed) / 0.085))
        padded = np.pad(self._emission, ((1, 1), (1, 1), (0, 0)))
        glow = (padded[:-2, 1:-1] + padded[2:, 1:-1] + padded[1:-1, :-2] + padded[1:-1, 2:]) * 0.075
        mapped = np.power(-np.expm1(-(self._emission + glow) * 1.5), 0.75)
        cells = mapped.reshape(self.grid.rows, 4, self.grid.columns, 2, 3)
        luminance = cells.max(axis=4)
        peak = luminance.max(axis=(1, 3))
        # All glyph bits address permanent physical dots. Dark cells retain the
        # unlit lattice, while local radiance resolves silhouettes within a cell.
        lit = luminance >= peak[:, None, :, None] * 0.30
        if monochrome:
            thresholds = np.array(((0.08, 0.48), (0.68, 0.28), (0.23, 0.63), (0.43, 0.13)))
            lit = luminance > thresholds[None, :, None, :]
        else:
            lit |= peak[:, None, :, None] < 0.05
        masks = (lit * _BRAILLE_BITS[None, :, None, :]).sum(axis=(1, 3))
        colors = cells.mean(axis=(1, 3)) * 1.25
        colors = np.maximum(colors, np.array((0.055, 0.065, 0.067)))
        # Preserve the full terminal truecolor spectrum; only Style storage is bounded.
        rgb = (np.clip(colors, 0, 1) * 255).astype(np.uint8)
        rows = []
        for row_colors, row_masks in zip(rgb.tolist(), masks.tolist(), strict=True):
            segments: list[Segment] = []
            run = ""
            previous: tuple[int, int, int] | None = None
            for color, mask in zip(row_colors, row_masks, strict=True):
                key = (color[0], color[1], color[2])
                if previous is not None and key != previous:
                    segments.append(Segment(run, _led_style(*previous)))
                    run = ""
                run += chr(0x2800 + mask)
                previous = key
            if previous is not None:
                segments.append(Segment(run, _led_style(*previous)))
            rows.append(Strip(segments, self.grid.columns))
        return tuple(rows)
