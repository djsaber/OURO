"""Five reciprocal-lattice waves reveal a nonperiodic diffraction rosette."""

import numpy as np

from ouro.tui.welcome.dot_matrix.field import DotGrid, Field, band, spectrum
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.29, 0.05, 0.09), (0.86, 0.21, 0.19), (1.0, 0.64, 0.39), (1.0, 0.97, 0.82))


def sample(grid: DotGrid, seconds: float) -> Field:
    x, y = grid.x, grid.y
    wave = np.zeros_like(x)
    quadrature = np.zeros_like(x)
    for index in range(5):
        angle = index * np.pi / 5
        projection = (x * np.cos(angle) + y * np.sin(angle)) * 15
        phase = np.sin(seconds * 0.17 + index * 2.4) * 1.6
        wave += np.cos(projection + phase)
        quadrature += np.sin(projection - phase)
    radius = np.hypot(x, y)
    aperture = np.exp(-np.power(radius / 1.05, 8))
    facets = np.clip((np.abs(wave) - 0.6) / 4.4, 0, 1) ** 1.35
    veins = band(quadrature, 0.35) * 0.3
    light = (facets * 1.7 + veins) * aperture
    color = spectrum(np.clip(0.4 + np.abs(wave) * 0.12, 0, 1), _SPECTRUM)
    return color * light[..., None] * 1.8


STYLE = dot_style("quasicrystal", "Prism", "An aperiodic rose cut from copper and light.", sample)
