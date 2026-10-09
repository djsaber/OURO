"""Phase beating between angular orbital modes and their radial nodes."""

import numpy as np

from ouro.tui.welcome.dot_matrix.field import DotGrid, Field, band, spectrum
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.11, 0.49, 0.83), (0.62, 0.91, 1.0), (1.0, 0.92, 0.72), (1.0, 0.37, 0.08))


def sample(grid: DotGrid, seconds: float) -> Field:
    x, y = grid.x, grid.y
    radius = np.hypot(x, y)
    angle = np.arctan2(y, x)
    phase = seconds * 0.24
    orbital = np.cos(3 * angle + phase) * np.sin(radius * 3.6 - 0.4)
    overtone = np.sin(2 * angle - phase * 0.7) * np.sin(radius * 5.3) * 0.32
    amplitude = orbital + overtone
    envelope = np.exp(-np.power(radius / 1.02, 4))
    density = amplitude**2 * envelope
    contour = band(np.abs(amplitude) - 0.65, 0.06) * envelope * 0.25
    nucleus = band(radius, 0.105) * 0.65
    emission = density * 1.65 + contour + nucleus
    color = spectrum(0.5 + 0.5 * np.tanh(amplitude * 2.4), _SPECTRUM)
    return color * emission[..., None]


STYLE = dot_style(
    "electron-bloom", "Electron Bloom", "Ice and copper petals exchanging light.", sample
)
