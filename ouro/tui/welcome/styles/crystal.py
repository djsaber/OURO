"""Five reciprocal-lattice waves reveal a nonperiodic diffraction rosette."""

from array import array
from math import cos, pi, sin

import arrayfunc as af  # type: ignore[import-untyped]
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
    clip,
)
from ouro.tui.welcome.styles.dot_matrix.raster import DotArtwork
from ouro.tui.welcome.styles.dot_matrix.widget import DotMatrix

_SPECTRUM = ((0.29, 0.05, 0.09), (0.86, 0.21, 0.19), (1.0, 0.64, 0.39), (1.0, 0.97, 0.82))


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = array("d", grid.x), array("d", grid.y)
    work, aperture = buffer(len(x)), buffer(len(x))
    projections = []
    for index in range(5):
        angle = index * pi / 5
        projection = buffer(len(x))
        af.mul(x, cos(angle), projection)
        af.mul(y, sin(angle), work)
        af.add(projection, work)
        af.mul(projection, 15.0)
        projections.append(projection)
    af.hypot(x, y, aperture)
    af.truediv(aperture, 1.05)
    af.pow(aperture, 8.0)
    af.neg(aperture)
    af.exp(aperture)
    wave, quadrature, light, position = (buffer(len(x)) for _ in range(4))
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        af.repeat(wave, 0.0)
        af.repeat(quadrature, 0.0)
        for index, projection in enumerate(projections):
            phase = sin(seconds * 0.17 + index * 2.4) * 1.6
            af.add(projection, phase, work)
            af.cos(work)
            af.add(wave, work)
            af.sub(projection, phase, work)
            af.sin(work)
            af.add(quadrature, work)
        af.abs_(wave)
        af.sub(wave, 0.6, light)
        af.truediv(light, 4.4)
        clip(light, 0.0, 1.0, work)
        af.pow(light, 1.35)
        af.mul(light, 1.7)
        band(quadrature, 0.35)
        af.mul(quadrature, 0.3)
        af.add(light, quadrature)
        af.mul(light, aperture)
        af.mul(light, 1.8)
        af.mul(wave, 0.12, position)
        af.add(position, 0.4)
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
