"""Sampler contracts and bounded resources."""

from math import sin

import pytest

pytest.importorskip("numpy")

import numpy as np
from ouro_tui_themes.packs.particle_art.horizon_field import HorizonScene
from ouro_tui_themes.packs.particle_art.quantum_field import QuantumScene
from ouro_tui_themes.tools.dot_matrix.field import ParticleGrid


@pytest.mark.parametrize("scene_type", [HorizonScene, QuantumScene])
def test_sampler_reuses_buffers_and_handles_nonmonotonic_time_without_mutating_grid(scene_type):
    prepare = scene_type().prepare
    grid = ParticleGrid.create(80, 64, span=(5.8, 3.6))
    coordinates = (grid.x.copy(), grid.y.copy())
    sample = prepare(grid)
    outputs = sample(0)
    assert all(channel.dtype == np.float64 for channel in outputs)
    for seconds in (0, 1 / 30, 7.5, 8.34, 99999, 0, 73.2):
        actual = sample(seconds)
        assert all(a is b for a, b in zip(actual, outputs, strict=True))
        assert np.asarray(actual).shape == (3, grid.width * grid.height)
        assert np.isfinite(actual).all() and (np.asarray(actual) >= 0).all()
        # Cached state must give the same result as a fresh sampler at that time.
        np.testing.assert_array_equal(actual, prepare(grid)(seconds))
    np.testing.assert_array_equal(grid.x, coordinates[0])
    np.testing.assert_array_equal(grid.y, coordinates[1])


@pytest.mark.parametrize("scene_type", [HorizonScene, QuantumScene])
@pytest.mark.parametrize("size", [(1, 1), (1, 80), (80, 1), (480, 280)])
def test_sampler_handles_tiny_and_large_grids_without_numerical_faults(scene_type, size):
    grid = ParticleGrid.create(*size, span=(5.8, 3.6))
    with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
        frame = np.asarray(scene_type(1).prepare(grid)(99999))
    assert frame.shape == (3, size[0] * size[1])
    assert np.isfinite(frame).all() and (frame >= 0).all()


def test_horizon_pointer_updates_reuse_bounded_buffers():
    from ouro_tui_themes.packs.particle_art.horizon_projection import HorizonProjection

    grid = ParticleGrid.create(60, 40, span=(5.8, 2.7))
    channels = (grid.x**2, grid.y**2, np.ones_like(grid.x))
    projection = HorizonProjection(grid)
    assert projection.sample(channels, 0, 0) is channels
    assert projection.mapping is projection.output is None
    projection.sample(channels, 1, 1 / 30)
    mapping, output = projection.mapping, projection.output
    assert mapping is not None and output is not None
    for tick in range(2, 151):
        projection.sample(channels, sin(tick) * 10, tick / 30)
        assert -1 <= projection.position <= 1
        assert projection.mapping is mapping and projection.output is output
