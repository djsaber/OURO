"""A caustic aperture formed by coherent sources and a slowly drifting phase."""

from math import cos, sin

import numpy as np
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from ouro.tui.welcome.centered import CenteredWelcomePage
from ouro.tui.welcome.styles.dot_matrix.field import (
    ParticleGrid,
    Radiance,
    Sampler,
    Spectrum,
    band,
    buffer,
)
from ouro.tui.welcome.styles.dot_matrix.raster import DotArtwork
from ouro.tui.welcome.styles.dot_matrix.widget import DotMatrix

_SPECTRUM = ((0.025, 0.19, 0.25), (0.07, 0.66, 0.63), (0.49, 0.95, 0.77), (0.95, 1.0, 0.86))


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = grid.x, grid.y.copy()
    left, right, lower, aperture, center = (buffer(len(x)) for _ in range(5))
    np.add(x, 0.83, out=left)
    np.hypot(left, y, out=left)
    np.subtract(x, 0.83, out=right)
    np.hypot(right, y, out=right)
    np.subtract(y, 1.2, out=lower)
    np.hypot(x, lower, out=lower)
    np.subtract(left, right, out=center)
    band(center, 0.05)
    np.multiply(center, 0.1, out=center)
    np.multiply(left, 18.0, out=left)
    np.multiply(right, 18.0, out=right)
    np.multiply(lower, 15.0, out=lower)
    np.divide(x, 2.1, out=aperture)
    np.power(aperture, 6.0, out=aperture)
    np.divide(y, 0.9, out=y)
    np.power(y, 6.0, out=y)
    np.add(aperture, y, out=aperture)
    np.negative(aperture, out=aperture)
    np.exp(aperture, out=aperture)
    # Both side sources share a time frequency, so sum their spatial bases once.
    side_cos = np.cos(left) + np.cos(right)
    side_sin = np.sin(left) - np.sin(right)
    lower_cos, lower_sin = np.cos(lower) * 0.65, np.sin(lower) * 0.65
    wave, work, position, light = (buffer(len(x)) for _ in range(4))
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        phase = seconds * 0.55
        np.multiply(side_cos, cos(phase), out=wave)
        np.multiply(side_sin, sin(phase), out=work)
        np.add(wave, work, out=wave)
        np.multiply(lower_cos, cos(phase * 0.6), out=work)
        np.add(wave, work, out=wave)
        np.multiply(lower_sin, sin(phase * 0.6), out=work)
        np.subtract(wave, work, out=wave)
        np.absolute(wave, out=position)
        np.divide(position, 2.65, out=position)
        np.square(position, out=light)
        np.multiply(light, position, out=light)
        np.multiply(light, 1.7, out=light)
        np.add(light, center, out=light)
        np.multiply(light, aperture, out=light)
        return spectrum.sample(position, light)

    return sample


class TidePage(CenteredWelcomePage):
    DEFAULT_CSS = """
    TidePage .phosphor-art {
        layout: vertical; width: 100%; height: 1fr; min-height: 0; margin-bottom: 1;
    }
    TidePage .art-caption { width: 100%; height: 1; text-align: center; }
    """

    def __init__(self) -> None:
        self.artwork = DotArtwork(prepare, span=(5.8, 2.7))
        self.matrix = DotMatrix(self.artwork)
        self._caption = Static(classes="art-caption")
        super().__init__(Vertical(self.matrix, self._caption, classes="phosphor-art"))

    def set_active(self, active: bool) -> None:
        self.matrix.set_active(active)

    def refresh_theme(self) -> None:
        theme = self.tui_theme
        caption = Text("O U R O", style=theme.text_emphasis)
        caption.append("  /  TIDE", style=theme.text_lowlight)
        self._caption.update(caption)


def create_page() -> TidePage:
    return TidePage()
