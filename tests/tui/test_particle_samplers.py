"""Sampler contracts, bounded resources and pointer-state isolation."""

from math import cos, sin

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


@pytest.mark.parametrize(
    ("scene_type", "pointer"), [(HorizonScene, "pointer_x"), (QuantumScene, "pointer_y")]
)
def test_pointer_state_is_local_and_returns_to_neutral(scene_type, pointer):
    grid = ParticleGrid.create(64, 40, span=(5.8, 3.6))
    scene, other = scene_type(), scene_type()
    sample, control = scene.prepare(grid), other.prepare(grid)
    np.testing.assert_array_equal(sample(0), control(0))
    setattr(scene, pointer, 1)
    for tick in range(1, 121):
        sample(tick / 30)
    assert getattr(other, pointer) == 0
    np.testing.assert_array_equal(control(4), scene_type().prepare(grid)(4))
    setattr(scene, pointer, 0)
    for tick in range(121, 241):
        actual = sample(tick / 30)
    np.testing.assert_array_equal(actual, control(8))


@pytest.mark.parametrize("scene_type", [HorizonScene, QuantumScene])
@pytest.mark.parametrize("size", [(1, 1), (1, 80), (80, 1), (480, 280)])
def test_sampler_handles_tiny_and_large_grids_without_numerical_faults(scene_type, size):
    grid = ParticleGrid.create(*size, span=(5.8, 3.6))
    with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
        frame = np.asarray(scene_type(1).prepare(grid)(99999))
    assert frame.shape == (3, size[0] * size[1])
    assert np.isfinite(frame).all() and (frame >= 0).all()


@pytest.mark.parametrize("size", [(40, 32), (40, 4)])
@pytest.mark.parametrize("angle", [-0.035, 0.035])
def test_horizon_rotation_preserves_linear_fields_and_source_buffers(size, angle):
    from ouro_tui_themes.packs.particle_art.horizon_projection import _RotationMap

    grid = ParticleGrid.create(*size, span=(5.8, 2.7))
    mapping = _RotationMap(grid)
    source = (grid.x, grid.y, np.ones_like(grid.x))
    original = np.array(source)
    output = tuple(np.empty_like(grid.x) for _ in range(3))
    mapping.update(angle)
    mapping.apply(source, output)
    # Bilinear interpolation is exact for linear fields, independent of tap order.
    x = grid.x[mapping.dots] * cos(angle) - grid.y[mapping.dots] * sin(angle)
    y = grid.x[mapping.dots] * sin(angle) + grid.y[mapping.dots] * cos(angle)
    valid = (x >= grid.x.min()) & (x <= grid.x.max()) & (y >= grid.y.min()) & (y <= grid.y.max())
    expected = original.copy()
    expected[:, mapping.dots] = np.asarray((x, y, np.ones_like(x))) * valid
    np.testing.assert_allclose(output, expected, atol=1e-13)
    np.testing.assert_array_equal(source, original)


def test_horizon_pointer_easing_is_frame_rate_independent_and_reuses_bounded_buffers():
    from ouro_tui_themes.packs.particle_art.horizon_projection import HorizonProjection

    grid = ParticleGrid.create(60, 40, span=(5.8, 2.7))
    channels = (grid.x**2, grid.y**2, np.ones_like(grid.x))
    slow, fast = HorizonProjection(grid), HorizonProjection(grid)
    for projection, hz in ((slow, 30), (fast, 60)):
        assert projection.sample(channels, 0, 0) is channels
        assert projection.mapping is projection.output is None
        for tick in range(1, hz + 1):
            projection.sample(channels, 1, tick / hz)
    assert slow.position == pytest.approx(fast.position)
    np.testing.assert_allclose(slow.output, fast.output, atol=1e-13)
    mapping, output = slow.mapping, slow.output
    for tick in range(31, 151):
        slow.sample(channels, sin(tick) * 10, tick / 30)
        assert -1 <= slow.position <= 1
        assert slow.mapping is mapping and slow.output is output
    for tick in range(151, 301):
        result = slow.sample(channels, 0, tick / 30)
    assert result is channels and slow.position == 0


def test_quantum_pointer_easing_is_frame_rate_independent_with_reused_output():
    grid = ParticleGrid.create(60, 40, span=(4.0, 2.7))
    results = []
    for hz in (30, 60):
        scene = QuantumScene()
        sample = scene.prepare(grid)
        channels = sample(0)
        scene.pointer_y = 1
        for tick in range(1, hz + 1):
            assert sample(tick / hz) is channels
        results.append(np.array(channels))
    np.testing.assert_allclose(results[0], results[1], atol=1e-13)
