"""A slowly turning Julia set, lit by escape time and an interior orbit trap."""

import numpy as np

from ouro.tui.welcome.dot_matrix.field import DotGrid, Field, band, spectrum
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.26, 0.09, 0.48), (0.78, 0.31, 0.83), (1.0, 0.67, 0.47), (1.0, 0.96, 0.54))


def sample(grid: DotGrid, seconds: float) -> Field:
    rotation = np.exp(1j * seconds * 0.065)
    radius = np.hypot(grid.x, grid.y)
    depth = np.sqrt(np.clip(1 - (radius / 1.02) ** 2, 0, 1))
    z = (grid.x + 1j * grid.y) * rotation * 2 / (1 + depth)
    c = -0.745 + 0.028 * np.sin(seconds * 0.11) + 1j * (0.188 + 0.018 * np.cos(seconds * 0.09))
    active = np.ones(z.shape, dtype=bool)
    escape = np.full(z.shape, 36.0)
    trap = np.full(z.shape, 8.0)
    for step in range(36):
        z[active] = z[active] ** 2 + c
        magnitude = np.abs(z)
        trap = np.minimum(trap, np.where(active, np.abs(magnitude - 0.62), 8))
        escaped = active & (magnitude > 8)
        escape[escaped] = step + 1 - np.log2(np.log2(magnitude[escaped]))
        active &= ~escaped
    body = np.clip(escape / 22, 0, 1)
    filigree = band(trap, 0.06)
    illumination = 0.38 + depth * 0.45 - grid.y * 0.15
    light = (0.16 + body**1.2 * 0.55 + filigree * 0.42) * illumination
    rim = band(radius - 0.995, 0.025) * 0.8
    color = spectrum(np.clip(0.5 - grid.y * 0.45 + trap * 0.25 - body * 0.27, 0, 1), _SPECTRUM)
    emission = (
        color * light[..., None] + spectrum(np.ones_like(radius) * 0.85, _SPECTRUM) * rim[..., None]
    )
    return emission * np.clip((1.04 - radius) * 55, 0, 1)[..., None] * 1.8


STYLE = dot_style(
    "julia-reliquary", "Labyrinth", "A violet labyrinth with a molten gold edge.", sample
)
