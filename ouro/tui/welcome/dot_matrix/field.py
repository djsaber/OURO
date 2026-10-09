"""Fixed dot coordinates and prepared, planar RGB sampling for welcome artwork."""

from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

import arrayfunc as af  # type: ignore[import-untyped]

type Field = array[float]
type Radiance = tuple[Field, Field, Field]
type Sampler = Callable[[float], Radiance]


def buffer(size: int, value: float = 0.0) -> Field:
    return array("d", [value]) * size


@dataclass(frozen=True)
class DotGrid:
    """Two by four fixed LEDs per cell, in dot-row order with square pitch."""

    columns: int
    rows: int
    x: tuple[float, ...]
    y: tuple[float, ...]

    @classmethod
    def create(cls, columns: int, rows: int) -> DotGrid:
        if columns < 1 or rows < 1:
            raise ValueError("a dot grid needs positive columns and rows")
        pitch = max(5.8 / (columns * 2), 2.7 / (rows * 4))
        xs = tuple((x - (columns * 2 - 1) / 2) * pitch for x in range(columns * 2))
        ys = tuple((y - (rows * 4 - 1) / 2) * pitch for y in range(rows * 4))
        return cls(columns, rows, xs * (rows * 4), tuple(y for y in ys for _ in xs))


@dataclass(frozen=True)
class DotArtwork:
    """Prepare size-owned geometry and buffers once, then sample explicit time.

    Each sample supplies three flat, finite, non-negative double arrays in dot-row
    order. Buffers may be reused by the next call; consumers must not mutate them.
    Preparing again creates independent state for another page or viewport size.
    """

    label: str
    prepare: Callable[[DotGrid], Sampler]


def clip(values: Field, low: float, high: float, scratch: Field) -> None:
    """Clamp in C with positive ramps that keep both bounds under rounding."""
    af.sub(values, low)
    af.abs_(values, scratch)
    af.add(values, scratch)
    af.mul(values, 0.5)
    af.sub(high - low, values, values)
    af.abs_(values, scratch)
    af.add(values, scratch)
    af.mul(values, 0.5)
    af.sub(high, values, values)


def maximum(left: Field, right: Field, out: Field, scratch: Field) -> None:
    af.sub(left, right, scratch)
    af.abs_(scratch)
    af.add(left, right, out)
    af.add(out, scratch)
    af.mul(out, 0.5)


def band(values: Field, width: float) -> None:
    """Replace signed distances with a Gaussian light band."""
    af.truediv(values, width)
    af.mul(values, values)
    af.neg(values)
    af.exp(values)


class Spectrum:
    """Reusable piecewise-linear RGB interpolation buffers for one sampler."""

    def __init__(self, size: int, stops: tuple[tuple[float, float, float], ...]) -> None:
        self.stops = stops
        self.channels: Radiance = (buffer(size), buffer(size), buffer(size))
        self._position, self._weight, self._scratch = (buffer(size) for _ in range(3))

    def sample(self, position: Field, light: Field) -> Radiance:
        af.mul(position, float(len(self.stops) - 1), self._position)
        for channel, initial in zip(self.channels, self.stops[0], strict=True):
            af.repeat(channel, initial)
        for index, (start, end) in enumerate(pairwise(self.stops)):
            af.sub(self._position, float(index), self._weight)
            clip(self._weight, 0.0, 1.0, self._scratch)
            for channel, low, high in zip(self.channels, start, end, strict=True):
                af.mul(self._weight, high - low, self._scratch)
                af.add(channel, self._scratch)
        for channel in self.channels:
            af.mul(channel, light)
        return self.channels
