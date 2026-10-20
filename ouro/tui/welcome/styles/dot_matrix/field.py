"""Shared numerical engine used by the bundled particle themes.

Grid, sampler contract, array operations and the default backend live together.
No UI objects, terminal geometry, theme registry or scheduling enter this module.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from math import exp
from typing import Protocol

import arrayfunc as af  # type: ignore[import-untyped]

type Field = array[float]
type Radiance = tuple[Field, Field, Field]
type Sampler = Callable[[float], Radiance]
type Prepare = Callable[[ParticleGrid], Sampler]
type EngineFactory = Callable[[ParticleGrid, Prepare], ParticleEngine]


@dataclass(frozen=True)
class ParticleGrid:
    """Fixed square-pitch coordinates in row-major order, independent of display cells."""

    width: int
    height: int
    x: tuple[float, ...]
    y: tuple[float, ...]

    @classmethod
    def create(cls, width: int, height: int, *, span: tuple[float, float]) -> ParticleGrid:
        if width < 1 or height < 1:
            raise ValueError("a particle grid needs positive width and height")
        pitch = max(span[0] / width, span[1] / height)
        xs = tuple((x - (width - 1) / 2) * pitch for x in range(width))
        ys = tuple((y - (height - 1) / 2) * pitch for y in range(height))
        return cls(width, height, xs * height, tuple(y for y in ys for _ in xs))


class ParticleEngine(Protocol):
    """One owner, one size, explicit time, borrowed planar RGB output.

    Each channel has width * height finite values in [0, 1]. Output is valid until
    the next call and must not be mutated by consumers. Factories create independent
    state. Engines receive explicit time and samplers, never widgets or concrete themes.
    Raw samplers supply finite non-negative radiance, with no upper bound.
    """

    def frame(self, seconds: float, elapsed: float) -> Radiance: ...


def buffer(size: int, value: float = 0.0) -> Field:
    return array("d", [value]) * size


def clip(values: Field, low: float, high: float, scratch: Field) -> None:
    """Clamp in C with positive ramps that keep both bounds under rounding."""
    af.sub(values, low)
    af.abs_(values, scratch)
    af.add(values, scratch)
    af.fma(values, -0.5, high - low)
    af.abs_(values, scratch)
    af.add(values, scratch)
    af.fma(values, -0.5, high)


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
                af.fma(self._weight, high - low, channel, channel)
        for channel in self.channels:
            af.mul(channel, light)
        return self.channels


class PhosphorField:
    """Size-owned buffers; no terminal encoding, UI objects or theme knowledge."""

    def __init__(self, grid: ParticleGrid, prepare: Prepare) -> None:
        self.grid = grid
        self._sample = prepare(grid)
        dots = grid.width * grid.height
        self._emission: Radiance = (buffer(dots), buffer(dots), buffer(dots))
        self._mapped: Radiance = (buffer(dots), buffer(dots), buffer(dots))
        self._scratch = buffer(dots)
        self._padded = buffer(dots + grid.width * 2)
        self._zero_column = buffer(grid.height)

    def _map_light(self, light: Field, emission: Field, mapped: Field, decay: float) -> None:
        af.mul(emission, decay)
        maximum(light, emission, emission, self._scratch)
        width = self.grid.width
        padded = self._padded
        padded[width:-width] = emission
        af.add(padded[: -2 * width], padded[2 * width :], mapped)
        left = padded[width - 1 : -width - 1]
        right = padded[width + 1 : -width + 1] if width > 1 else padded[2:]
        # Horizontal neighbours stop at row boundaries; vertical padding is zero.
        left[::width] = self._zero_column
        right[width - 1 :: width] = self._zero_column
        af.add(mapped, left)
        af.add(mapped, right)
        af.fma(mapped, 0.075, emission)
        af.mul(mapped, -1.5)
        af.expm1(mapped)
        af.neg(mapped)
        af.pow(mapped, 0.75)

    def frame(self, seconds: float, elapsed: float) -> Radiance:
        decay = exp(-max(0, elapsed) / 0.085)
        for light, emission, mapped in zip(
            self._sample(seconds), self._emission, self._mapped, strict=True
        ):
            self._map_light(light, emission, mapped, decay)
        return self._mapped
