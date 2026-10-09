"""An occulted accretion disk and its bent upper and lower light paths."""

import numpy as np

from ouro.tui.welcome.dot_matrix.field import DotGrid, Field, band, spectrum
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.30, 0.045, 0.008), (1.0, 0.27, 0.045), (1.0, 0.78, 0.38), (1.0, 0.98, 0.86))


def sample(grid: DotGrid, seconds: float) -> Field:
    tilt = 0.10 + 0.055 * np.sin(seconds * 0.13)
    x, y = grid.x, grid.y - grid.x * tilt
    radius = np.hypot(x, y)
    angle = np.arctan2(y, x)
    disk_radius = np.hypot(x, y / 0.24)
    turbulence = 0.76 + 0.16 * np.sin(disk_radius * 21 - seconds * 1.7 + angle * 3)
    turbulence += 0.08 * np.sin(disk_radius * 43 + angle * 7 + seconds * 0.8)
    disk = band(disk_radius - 1.13, 0.62) * turbulence
    # The far-side disk is seen over the shadow; the near side hides its base.
    arc = band(radius - 0.64, 0.085) * (0.64 + 0.36 * np.cos(angle + 1.1))
    halo = band(radius - 0.70, 0.20) * 0.17
    photon = band(radius - 0.535, 0.018) * 0.9
    beaming = 0.65 + 0.48 * np.clip(-x / 1.6, -0.7, 1)
    light = (disk * beaming + arc + halo + photon) * np.clip((radius - 0.49) * 55, 0, 1)
    light *= np.exp(-np.maximum(radius - 1.9, 0) * 3)
    color = spectrum(np.clip(light * 1.15, 0, 1), _SPECTRUM)
    return color * light[..., None] * 1.65


STYLE = dot_style("event-horizon", "Horizon", "An ember disk folded around a dark sun.", sample)
