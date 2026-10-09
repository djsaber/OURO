"""A Clifford torus rotated in four dimensions, sampled by inverse projection."""

import numpy as np

from ouro.tui.welcome.dot_matrix.field import DotGrid, Field, band, spectrum
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.21, 0.12, 0.58), (0.51, 0.34, 1.0), (0.38, 0.86, 1.0), (0.92, 0.98, 1.0))


def sample(grid: DotGrid, seconds: float) -> Field:
    x, y = grid.x * 2.5, grid.y * 2.5
    # Integrate fixed view rays through the stereographic image of S3. Rotation
    # in XW and YZ changes the projected surface, never the receiving LED grid.
    a = 0.18 * np.sin(seconds * 0.19)
    b = 0.55 + 0.35 * np.sin(seconds * 0.13)
    light = np.zeros_like(x)
    transmission = np.ones_like(x)
    ca, sa, cb, sb = np.cos(a), np.sin(a), np.cos(b), np.sin(b)
    for depth in np.linspace(-2.2, 2.2, 14):
        radius2 = x * x + y * y + depth * depth
        denominator = 1 + radius2
        sx, sy = 2 * x / denominator, 2 * y / denominator
        sz, sw = 2 * depth / denominator, (radius2 - 1) / denominator
        rx = sx * ca - sw * sa
        ry = sy * cb - sz * sb
        surface = rx * rx + ry * ry - 0.5
        weave = 0.65 + 0.35 * np.cos((rx + ry) * 28 - seconds * 0.5) ** 2
        density = band(surface, 0.07) * np.exp(-radius2 * 0.32) * 0.65
        light += density * weave * transmission * (1 - depth * 0.18)
        transmission *= 1 - density * 0.55
    color = spectrum(0.5 + 0.5 * np.sin(np.arctan2(y, x) + seconds * 0.17), _SPECTRUM)
    return color * light[..., None] * 1.8


STYLE = dot_style("clifford-torus", "Iris", "An iris of folded ultraviolet light.", sample)
