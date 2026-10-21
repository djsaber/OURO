"""Five reciprocal-lattice waves reveal a nonperiodic diffraction rosette."""

from math import cos, pi, sin

import numpy as np
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from ouro.tui.welcome.centered import CenteredWelcomePage
from ouro_tui_themes.dot_matrix.field import (
    ParticleGrid,
    Radiance,
    Sampler,
    Spectrum,
    band,
    buffer,
    clip,
)
from ouro_tui_themes.dot_matrix.raster import DotArtwork
from ouro_tui_themes.dot_matrix.widget import DotMatrix

_SPECTRUM = ((0.29, 0.05, 0.09), (0.86, 0.21, 0.19), (1.0, 0.64, 0.39), (1.0, 0.97, 0.82))


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = grid.x, grid.y
    work, aperture = buffer(len(x)), buffer(len(x))
    projections = []
    for index in range(5):
        angle = index * pi / 5
        projection = buffer(len(x))
        np.multiply(x, cos(angle), out=projection)
        np.multiply(y, sin(angle), out=work)
        np.add(projection, work, out=projection)
        np.multiply(projection, 15.0, out=projection)
        projections.append((np.cos(projection), np.sin(projection)))
    np.hypot(x, y, out=aperture)
    np.divide(aperture, 1.05, out=aperture)
    np.power(aperture, 8.0, out=aperture)
    np.negative(aperture, out=aperture)
    np.exp(aperture, out=aperture)
    wave, quadrature, light, position = (buffer(len(x)) for _ in range(4))
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        wave.fill(0.0)
        quadrature.fill(0.0)
        for index, (spatial_cos, spatial_sin) in enumerate(projections):
            phase = sin(seconds * 0.17 + index * 2.4) * 1.6
            cp, sp = cos(phase), sin(phase)
            np.multiply(spatial_cos, cp, out=work)
            np.add(wave, work, out=wave)
            np.multiply(spatial_sin, sp, out=work)
            np.subtract(wave, work, out=wave)
            np.multiply(spatial_sin, cp, out=work)
            np.add(quadrature, work, out=quadrature)
            np.multiply(spatial_cos, sp, out=work)
            np.subtract(quadrature, work, out=quadrature)
        np.absolute(wave, out=wave)
        np.subtract(wave, 0.6, out=light)
        np.divide(light, 4.4, out=light)
        clip(light, 0.0, 1.0)
        np.power(light, 1.35, out=light)
        np.multiply(light, 1.7, out=light)
        band(quadrature, 0.35)
        np.multiply(quadrature, 0.3, out=quadrature)
        np.add(light, quadrature, out=light)
        np.multiply(light, aperture, out=light)
        np.multiply(light, 1.8, out=light)
        np.multiply(wave, 0.12, out=position)
        np.add(position, 0.4, out=position)
        return spectrum.sample(position, light)

    return sample


class PrismPage(CenteredWelcomePage):
    DEFAULT_CSS = """
    PrismPage .phosphor-art {
        layout: vertical; width: 100%; height: 1fr; min-height: 0; margin-bottom: 1;
    }
    PrismPage .art-caption { width: 100%; height: 1; text-align: center; }
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
        caption.append("  /  PRISM", style=theme.text_lowlight)
        self._caption.update(caption)


def create_page() -> PrismPage:
    return PrismPage()
