from __future__ import annotations

from math import exp

import pytest

pytest.importorskip("numpy")

import numpy as np
from ouro_tui_themes.dot_matrix.field import (
    ParticleGrid,
    PhosphorField,
    Spectrum,
    buffer,
    clip,
)


@pytest.mark.parametrize("size", [(1, 1), (1, 3), (3, 1), (3, 2)])
def test_field_matches_scalar_persistence_and_neighbours_without_mutating_sampler(size):
    width, height = size
    grid = ParticleGrid.create(width, height, span=(2.0, 2.0))
    light = np.arange(1, width * height + 1, dtype=np.float64) / 7
    captured = []

    def prepare(actual_grid):
        assert actual_grid is grid

        def sample(seconds):
            captured.append(seconds)
            return light, light, light

        return sample

    engine = PhosphorField(grid, prepare)
    previous = tuple(light)
    for seconds, delta in ((0.0, 0.0), (1.0, 0.1), (2.0, 0.2)):
        before = tuple(light)
        expected_emission = [
            max(value, old * exp(-delta / 0.085))
            for value, old in zip(light, previous, strict=True)
        ]
        expected = []
        for index, emission in enumerate(expected_emission):
            x, y = index % width, index // width
            neighbours = [
                expected_emission[ny * width + nx]
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1))
                if 0 <= nx < width and 0 <= ny < height
            ]
            expected.append((1 - exp(-1.5 * (emission + 0.075 * sum(neighbours)))) ** 0.75)
        for channel in engine.frame(seconds, delta):
            assert list(channel) == pytest.approx(expected)
        assert tuple(light) == before
        previous = expected_emission
        light[:] = buffer(len(light))
    assert captured == [0.0, 1.0, 2.0]


def test_clipping_and_piecewise_interpolation_keep_bounds_and_reuse_output():
    positions = np.array([-1, 0, 0.25, 0.5, 0.75, 1, 2])
    clipped = positions.copy()
    clip(clipped, 0.0, 1.0)
    assert list(clipped) == [0, 0, 0.25, 0.5, 0.75, 1, 1]
    spectrum = Spectrum(len(positions), ((0.0, 0.2, 1.0), (0.5, 0.6, 0.5), (1.0, 1.0, 0.0)))
    result = spectrum.sample(positions, buffer(len(positions), 0.5))
    assert list(result[0]) == pytest.approx([value * 0.5 for value in clipped])
    assert list(result[1]) == pytest.approx([(0.2 + value * 0.8) * 0.5 for value in clipped])
    assert spectrum.sample(positions, buffer(len(positions), 1.0)) is result


def test_preparing_same_grid_creates_independent_state():
    grid = ParticleGrid.create(2, 2, span=(1.0, 1.0))
    light = buffer(4, 1.0)

    def prepare(_):
        return lambda seconds: (light, light, light)

    first, second = PhosphorField(grid, prepare), PhosphorField(grid, prepare)
    first.frame(0, 0)
    light[:] = buffer(4)
    assert any(first.frame(0.01, 0.01)[0])
    assert not any(second.frame(0.01, 0.01)[0])


def test_exposure_preserves_tiny_light_and_saturates_without_overflow():
    grid = ParticleGrid.create(1, 1, span=(1.0, 1.0))
    light = buffer(1)
    field = PhosphorField(grid, lambda _: lambda seconds: (light, light, light))
    for value in (0.0, 1e-300, 0.01, 1.0, 1e100):
        light.fill(value)
        actual = field.frame(0, 1000)[0][0]
        expected = (-np.expm1(-1.5 * value)) ** 0.75
        assert actual == pytest.approx(expected, rel=1e-14, abs=0)


def test_numerical_fault_propagates_without_changing_numpy_policy():
    grid = ParticleGrid.create(1, 1, span=(1.0, 1.0))

    def prepare(_):
        def sample(seconds):
            light = np.divide(buffer(1, 1.0), 0.0)
            return light, light, light

        return sample

    engine = PhosphorField(grid, prepare)
    policy = np.geterr()
    with pytest.raises(FloatingPointError, match="divide by zero"):
        engine.frame(0, 0)
    assert np.geterr() == policy
