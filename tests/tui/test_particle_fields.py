from __future__ import annotations

import pytest

pytest.importorskip("numpy")

import numpy as np
from ouro_tui_themes.tools.dot_matrix.field import (
    ParticleGrid,
    PhosphorField,
    buffer,
)


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


def test_exposure_handles_extreme_inputs_without_numerical_faults():
    grid = ParticleGrid.create(1, 1, span=(1.0, 1.0))
    light = buffer(1)
    field = PhosphorField(grid, lambda _: lambda seconds: (light, light, light))
    for value in (0.0, 1e-300, 0.01, 1.0, 1e100):
        light.fill(value)
        actual = field.frame(0, 1000)[0][0]
        assert np.isfinite(actual)
        assert 0 <= actual <= 1


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
