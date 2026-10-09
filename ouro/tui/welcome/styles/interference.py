"""A caustic aperture formed by coherent sources and a slowly drifting phase."""

import numpy as np

from ouro.tui.welcome.dot_matrix.field import DotGrid, Field, band, spectrum
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.025, 0.19, 0.25), (0.07, 0.66, 0.63), (0.49, 0.95, 0.77), (0.95, 1.0, 0.86))


def sample(grid: DotGrid, seconds: float) -> Field:
    x, y = grid.x, grid.y
    left = np.hypot(x + 0.83, y)
    right = np.hypot(x - 0.83, y)
    lower = np.hypot(x, y - 1.2)
    phase = seconds * 0.55
    wave = np.cos(left * 18 - phase) + np.cos(right * 18 + phase)
    wave += 0.65 * np.cos(lower * 15 + phase * 0.6)
    aperture = np.exp(-((x / 2.1) ** 6 + (y / 0.9) ** 6))
    ridges = np.power(np.clip(np.abs(wave) / 2.65, 0, 1), 3)
    light = (ridges * 1.7 + band(left - right, 0.05) * 0.1) * aperture
    color = spectrum(np.clip(np.abs(wave) / 2.65, 0, 1), _SPECTRUM)
    return color * light[..., None]


STYLE = dot_style(
    "interference", "Tidal Interference", "Jade light woven into a breathing caustic.", sample
)
