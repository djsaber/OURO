"""An occulted accretion disk and its bent upper and lower light paths."""

from math import sin

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
    clip,
)
from ouro.tui.welcome.styles.dot_matrix.raster import DotArtwork
from ouro.tui.welcome.styles.dot_matrix.widget import DotMatrix

_SPECTRUM = ((0.30, 0.045, 0.008), (1.0, 0.27, 0.045), (1.0, 0.78, 0.38), (1.0, 0.98, 0.86))


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = grid.x, grid.y
    (tilted, radius, angle, disk_radius, turbulence, work, disk, light, position, beaming) = (
        buffer(len(x)) for _ in range(10)
    )
    np.divide(x, -1.6, out=beaming)
    clip(beaming, -0.7, 1.0)
    np.multiply(beaming, 0.48, out=beaming)
    np.add(beaming, 0.65, out=beaming)
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        tilt = 0.10 + 0.055 * sin(seconds * 0.13)
        np.multiply(x, tilt, out=tilted)
        np.subtract(y, tilted, out=tilted)
        np.hypot(x, tilted, out=radius)
        np.arctan2(tilted, x, out=angle)
        np.divide(tilted, 0.24, out=work)
        np.hypot(x, work, out=disk_radius)
        np.multiply(disk_radius, 21.0, out=turbulence)
        np.subtract(turbulence, seconds * 1.7, out=turbulence)
        np.multiply(angle, 3.0, out=work)
        np.add(turbulence, work, out=turbulence)
        np.sin(turbulence, out=turbulence)
        np.multiply(turbulence, 0.16, out=turbulence)
        np.add(turbulence, 0.76, out=turbulence)
        np.multiply(disk_radius, 43.0, out=work)
        np.multiply(angle, 7.0, out=disk)
        np.add(work, disk, out=work)
        np.add(work, seconds * 0.8, out=work)
        np.sin(work, out=work)
        np.multiply(work, 0.08, out=work)
        np.add(turbulence, work, out=turbulence)
        np.subtract(disk_radius, 1.13, out=disk)
        band(disk, 0.62)
        np.multiply(disk, turbulence, out=disk)
        np.multiply(disk, beaming, out=light)
        # The far-side disk is seen over the shadow; the near side hides its base.
        np.add(angle, 1.1, out=work)
        np.cos(work, out=work)
        np.multiply(work, 0.36, out=work)
        np.add(work, 0.64, out=work)
        np.subtract(radius, 0.64, out=disk)
        band(disk, 0.085)
        np.multiply(disk, work, out=disk)
        np.add(light, disk, out=light)
        np.subtract(radius, 0.70, out=work)
        band(work, 0.20)
        np.multiply(work, 0.17, out=work)
        np.add(light, work, out=light)
        np.subtract(radius, 0.535, out=work)
        band(work, 0.018)
        np.multiply(work, 0.9, out=work)
        np.add(light, work, out=light)
        np.subtract(radius, 0.49, out=work)
        np.multiply(work, 55.0, out=work)
        clip(work, 0.0, 1.0)
        np.multiply(light, work, out=light)
        np.subtract(radius, 1.9, out=work)
        np.absolute(work, out=disk)
        np.add(work, disk, out=work)
        np.multiply(work, -1.5, out=work)
        np.exp(work, out=work)
        np.multiply(light, work, out=light)
        np.multiply(light, 1.15, out=position)
        np.multiply(light, 1.65, out=light)
        return spectrum.sample(position, light)

    return sample


class HorizonPage(CenteredWelcomePage):
    DEFAULT_CSS = """
    HorizonPage .phosphor-art {
        layout: vertical; width: 100%; height: 1fr; min-height: 0; margin-bottom: 1;
    }
    HorizonPage .art-caption { width: 100%; height: 1; text-align: center; }
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
        caption.append("  /  HORIZON", style=theme.text_lowlight)
        self._caption.update(caption)


def create_page() -> HorizonPage:
    return HorizonPage()
