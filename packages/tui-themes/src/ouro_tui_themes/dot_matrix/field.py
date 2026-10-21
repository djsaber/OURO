"""Shared numerical engine used by the bundled particle themes.

Grid, sampler contract, array operations and the default backend live together.
No UI objects, terminal geometry, theme registry or scheduling enter this module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import exp
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

type Field = NDArray[np.float64]
type Radiance = tuple[Field, Field, Field]
type Sampler = Callable[[float], Radiance]
type Prepare = Callable[[ParticleGrid], Sampler]
type EngineFactory = Callable[[ParticleGrid, Prepare], ParticleEngine]


@dataclass(frozen=True)
class ParticleGrid:
    """Fixed square-pitch coordinates in row-major order, independent of display cells."""

    width: int
    height: int
    x: Field
    y: Field

    @classmethod
    def create(cls, width: int, height: int, *, span: tuple[float, float]) -> ParticleGrid:
        if width < 1 or height < 1:
            raise ValueError("a particle grid needs positive width and height")
        pitch = max(span[0] / width, span[1] / height)
        xs = (np.arange(width, dtype=np.float64) - (width - 1) / 2) * pitch
        ys = (np.arange(height, dtype=np.float64) - (height - 1) / 2) * pitch
        x, y = np.tile(xs, height), np.repeat(ys, width)
        x.flags.writeable = y.flags.writeable = False
        return cls(width, height, x, y)


class ParticleEngine(Protocol):
    """One owner, one size, explicit time, borrowed planar RGB output.

    Each channel has width * height finite values in [0, 1]. Output is valid until
    the next call and must not be mutated by consumers. Factories create independent
    state. Engines receive explicit time and samplers, never widgets or concrete themes.
    Raw samplers supply finite non-negative radiance, with no upper bound.
    """

    def frame(self, seconds: float, elapsed: float) -> Radiance: ...


def buffer(size: int, value: float = 0.0) -> Field:
    return np.full(size, value, dtype=np.float64)


def clip(values: Field, low: float, high: float) -> None:
    """Clamp in one native pass without cancellation at the bounds."""
    np.clip(values, low, high, out=values)


def band(values: Field, width: float) -> None:
    """Replace signed distances with a Gaussian light band."""
    np.divide(values, width, out=values)
    np.square(values, out=values)
    np.negative(values, out=values)
    np.exp(values, out=values)


class Spectrum:
    """Reusable piecewise-linear RGB interpolation buffers for one sampler."""

    def __init__(self, size: int, stops: tuple[tuple[float, float, float], ...]) -> None:
        self.stops = stops
        self.channels: Radiance = (buffer(size), buffer(size), buffer(size))
        self._knots = np.linspace(0.0, 1.0, len(stops))
        self._colors = np.asarray(stops).T.copy()

    def sample(self, position: Field, light: Field) -> Radiance:
        for channel, colors in zip(self.channels, self._colors, strict=True):
            np.multiply(np.interp(position, self._knots, colors), light, out=channel)
        return self.channels


class PhosphorField:
    """Size-owned buffers; no terminal encoding, UI objects or theme knowledge."""

    def __init__(self, grid: ParticleGrid, prepare: Prepare) -> None:
        self.grid = grid
        self._sample = prepare(grid)
        dots = grid.width * grid.height
        self._emission: Radiance = (buffer(dots), buffer(dots), buffer(dots))
        self._mapped: Radiance = (buffer(dots), buffer(dots), buffer(dots))
        self._root = buffer(dots)

    def _map_light(self, light: Field, emission: Field, mapped: Field, decay: float) -> None:
        np.multiply(emission, decay, out=emission)
        np.maximum(light, emission, out=emission)
        shape = (self.grid.height, self.grid.width)
        source, target = emission.reshape(shape), mapped.reshape(shape)
        # A four-neighbour stencil on views: no padding copies or row wraparound.
        target.fill(0.0)
        target[1:] += source[:-1]
        target[:-1] += source[1:]
        target[:, 1:] += source[:, :-1]
        target[:, :-1] += source[:, 1:]
        np.multiply(mapped, 0.075, out=mapped)
        np.add(mapped, emission, out=mapped)
        np.multiply(mapped, -1.5, out=mapped)
        np.expm1(mapped, out=mapped)
        np.negative(mapped, out=mapped)
        # x**(3/4) uses two hardware square roots instead of a general power.
        # Multiplying the roots last also preserves subnormal input light.
        np.sqrt(mapped, out=self._root)
        np.sqrt(self._root, out=mapped)
        np.multiply(mapped, self._root, out=mapped)

    def frame(self, seconds: float, elapsed: float) -> Radiance:
        decay = exp(-max(0, elapsed) / 0.085)
        # Numerical faults still reach the owning worker. Underflow is normal in
        # Gaussian tails; do not alter the application's global NumPy policy.
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            for light, emission, mapped in zip(
                self._sample(seconds), self._emission, self._mapped, strict=True
            ):
                self._map_light(light, emission, mapped, decay)
        return self._mapped
