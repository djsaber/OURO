from __future__ import annotations

import ast
from array import array
from math import exp
from pathlib import Path

import pytest

pytest.importorskip("arrayfunc")

from ouro.tui.welcome.styles.dot_matrix.field import (
    ParticleGrid,
    PhosphorField,
    Spectrum,
    buffer,
    clip,
)


@pytest.mark.parametrize("module", ["__init__", "field", "raster", "widget"])
def test_shared_module_does_not_depend_on_theme_pages_or_framework_contracts(module):
    source = Path(__file__).parents[2] / f"ouro/tui/welcome/styles/dot_matrix/{module}.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules = [node.module or ""]
        else:
            continue
        for dependency in modules:
            if module == "field":
                assert dependency.split(".")[0] not in {"ouro", "rich", "textual"}
            elif dependency.startswith("ouro."):
                assert dependency.startswith("ouro.tui.welcome.styles.dot_matrix.")


@pytest.mark.parametrize("size", [(1, 1), (1, 3), (3, 2)])
def test_field_matches_scalar_persistence_and_neighbours_without_mutating_sampler(size):
    width, height = size
    grid = ParticleGrid.create(width, height, span=(2.0, 2.0))
    light = array("d", (index / 7 for index in range(1, width * height + 1)))
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
    positions = array("d", [-1, 0, 0.25, 0.5, 0.75, 1, 2])
    clipped = positions[:]
    clip(clipped, 0.0, 1.0, buffer(len(positions)))
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
