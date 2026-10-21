"""Scalar equations guard the vectorized spatial/time separation and projection."""

from importlib import import_module
from math import atan2, cos, exp, hypot, pi, sin, tanh

import pytest

pytest.importorskip("numpy")

import numpy as np

from ouro.tui.welcome.styles.dot_matrix.field import ParticleGrid


def band(distance, width):
    return exp(-((distance / width) ** 2))


def scalar(name, x, y, seconds, depths=()):
    if name == "electron_bloom":
        radius, angle, phase = hypot(x, y), atan2(y, x), seconds * 0.24
        envelope = exp(-((radius / 1.02) ** 4))
        amplitude = cos(angle * 3 + phase) * sin(radius * 3.6 - 0.4)
        amplitude += sin(angle * 2 - phase * 0.7) * sin(radius * 5.3) * 0.32
        light = amplitude**2 * envelope * 1.65
        light += band(abs(amplitude) - 0.65, 0.06) * envelope * 0.25
        light += band(radius, 0.105) * 0.65
        return tanh(amplitude * 2.4) * 0.5 + 0.5, light
    if name == "interference":
        left, right, lower = hypot(x + 0.83, y), hypot(x - 0.83, y), hypot(x, y - 1.2)
        phase = seconds * 0.55
        wave = cos(left * 18 - phase) + cos(right * 18 + phase)
        wave += cos(lower * 15 + phase * 0.6) * 0.65
        position = abs(wave) / 2.65
        light = (position**3 * 1.7 + band(left - right, 0.05) * 0.1) * exp(
            -((x / 2.1) ** 6 + (y / 0.9) ** 6)
        )
        return position, light
    if name == "crystal":
        wave = quadrature = 0.0
        for index in range(5):
            angle = index * pi / 5
            projection = (x * cos(angle) + y * sin(angle)) * 15
            phase = sin(seconds * 0.17 + index * 2.4) * 1.6
            wave += cos(projection + phase)
            quadrature += sin(projection - phase)
        wave = abs(wave)
        light = min(1, max(0, (wave - 0.6) / 4.4)) ** 1.35 * 1.7
        light += band(quadrature, 0.35) * 0.3
        return wave * 0.12 + 0.4, light * exp(-((hypot(x, y) / 1.05) ** 8)) * 1.8
    if name == "clifford":
        x, y = x * 2.5, y * 2.5
        radial, angle = x * x + y * y, atan2(y, x)
        a, b = 0.18 * sin(seconds * 0.19), 0.55 + 0.35 * sin(seconds * 0.13)
        light, transmission = 0.0, 1.0
        for depth in depths:
            radius = radial + depth * depth
            rx = (x * 2 * cos(a) - (radius - 1) * sin(a)) / (radius + 1)
            ry = (y * 2 * cos(b) - 2 * depth * sin(b)) / (radius + 1)
            surface = rx * rx + ry * ry - 0.5
            weave = cos((rx + ry) * 28 - seconds * 0.5) ** 2 * 0.35 + 0.65
            density = band(surface, 0.07) * exp(-radial * 0.32)
            density *= exp(-depth * depth * 0.32) * 0.65
            light += density * weave * transmission * (1 - depth * 0.18)
            transmission *= 1 - density * 0.55
        return sin(angle + seconds * 0.17) * 0.5 + 0.5, light * 1.8
    assert name == "event_horizon"
    tilted = y - x * (0.10 + 0.055 * sin(seconds * 0.13))
    radius, angle = hypot(x, tilted), atan2(tilted, x)
    disk_radius = hypot(x, tilted / 0.24)
    turbulence = sin(disk_radius * 21 - seconds * 1.7 + angle * 3) * 0.16 + 0.76
    turbulence += sin(disk_radius * 43 + angle * 7 + seconds * 0.8) * 0.08
    beaming = min(1, max(-0.7, x / -1.6)) * 0.48 + 0.65
    light = band(disk_radius - 1.13, 0.62) * turbulence * beaming
    light += band(radius - 0.64, 0.085) * (cos(angle + 1.1) * 0.36 + 0.64)
    light += band(radius - 0.70, 0.20) * 0.17 + band(radius - 0.535, 0.018) * 0.9
    light *= min(1, max(0, (radius - 0.49) * 55)) * exp(-max(0, radius - 1.9) * 3)
    return light * 1.15, light * 1.65


@pytest.mark.parametrize(
    "name", ["event_horizon", "electron_bloom", "interference", "clifford", "crystal"]
)
def test_sampler_matches_scalar_equations_at_arbitrary_times(name):
    module = import_module(f"ouro.tui.welcome.styles.{name}")
    grid = ParticleGrid.create(11, 9, span=(5.8, 2.7))
    sample = module.prepare(grid)
    coordinates = (grid.x.copy(), grid.y.copy())
    stops = module._SPECTRUM
    for seconds in (0.0, 1 / 30, 7.5, 99999.0, 0.0, 73.2):
        expected = []
        for x, y in zip(grid.x, grid.y, strict=True):
            position, light = scalar(
                name, float(x), float(y), seconds, getattr(module, "_DEPTHS", ())
            )
            scaled = max(0, min(1, position)) * (len(stops) - 1)
            index = min(int(scaled), len(stops) - 2)
            weight = scaled - index
            expected.append(
                [
                    (a + (b - a) * weight) * light
                    for a, b in zip(stops[index], stops[index + 1], strict=True)
                ]
            )
        actual = np.asarray(sample(seconds))
        np.testing.assert_allclose(actual, np.asarray(expected).T, rtol=1e-8, atol=2e-11)
        assert np.isfinite(actual).all() and (actual >= 0).all()
    np.testing.assert_array_equal(grid.x, coordinates[0])
    np.testing.assert_array_equal(grid.y, coordinates[1])
