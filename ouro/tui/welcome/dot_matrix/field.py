"""Pure contracts for welcome artwork sampled on an immutable LED lattice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Field = NDArray[np.float64]


@dataclass(frozen=True)
class DotGrid:
    """Two by four fixed LEDs per terminal cell, with square physical pitch."""

    columns: int
    rows: int
    x: Field
    y: Field

    @classmethod
    def create(cls, columns: int, rows: int) -> DotGrid:
        if columns < 1 or rows < 1:
            raise ValueError("a dot grid needs positive columns and rows")
        pitch = max(5.8 / (columns * 2), 2.7 / (rows * 4))
        x, y = np.meshgrid(
            (np.arange(columns * 2) - (columns * 2 - 1) / 2) * pitch,
            (np.arange(rows * 4) - (rows * 4 - 1) / 2) * pitch,
        )
        x.flags.writeable = y.flags.writeable = False
        return cls(columns, rows, x, y)


@dataclass(frozen=True)
class DotArtwork:
    """A dot artwork owns its spectrum and radiance function.

    Samples are linear RGB emission (rows * 4, columns * 2, 3), non-negative
    and finite. Time is supplied in seconds; sampling never moves grid points.
    """

    label: str
    sample: Callable[[DotGrid, float], Field]


def spectrum(value: Field, stops: tuple[tuple[float, float, float], ...]) -> Field:
    """Interpolate an artwork's emission spectrum, without a TUI color theme."""
    position = np.clip(value, 0, 1) * (len(stops) - 1)
    low = np.minimum(position.astype(np.intp), len(stops) - 2)
    blend = (position - low)[..., None]
    colors = np.asarray(stops)
    return colors[low] * (1 - blend) + colors[low + 1] * blend


def band(distance: Field, width: float) -> Field:
    return np.exp(-np.square(distance / width))
