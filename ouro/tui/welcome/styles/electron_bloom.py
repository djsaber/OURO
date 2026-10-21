"""Phase beating between angular orbital modes and their radial nodes."""

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

_SPECTRUM = ((0.11, 0.49, 0.83), (0.62, 0.91, 1.0), (1.0, 0.92, 0.72), (1.0, 0.37, 0.08))


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = grid.x, grid.y
    radius, angle, radial, overtone, envelope, nucleus = (buffer(len(x)) for _ in range(6))
    np.hypot(x, y, out=radius)
    np.arctan2(y, x, out=angle)
    np.multiply(radius, 3.6, out=radial)
    np.subtract(radial, 0.4, out=radial)
    np.sin(radial, out=radial)
    np.multiply(radius, 5.3, out=overtone)
    np.sin(overtone, out=overtone)
    np.multiply(overtone, 0.32, out=overtone)
    np.divide(radius, 1.02, out=envelope)
    np.power(envelope, 4.0, out=envelope)
    np.negative(envelope, out=envelope)
    np.exp(envelope, out=envelope)
    nucleus[:] = radius
    band(nucleus, 0.105)
    np.multiply(nucleus, 0.65, out=nucleus)
    # Separate the fixed angular modes from their scalar time phases exactly.
    radial_cos = np.cos(angle * 3.0) * radial
    radial_sin = np.sin(angle * 3.0) * radial
    overtone_sin = np.sin(angle * 2.0) * overtone
    overtone_cos = np.cos(angle * 2.0) * overtone
    amplitude, work, light, position = (buffer(len(x)) for _ in range(4))
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        phase = seconds * 0.24
        np.multiply(radial_cos, cos(phase), out=amplitude)
        np.multiply(radial_sin, sin(phase), out=work)
        np.subtract(amplitude, work, out=amplitude)
        np.multiply(overtone_sin, cos(phase * 0.7), out=work)
        np.add(amplitude, work, out=amplitude)
        np.multiply(overtone_cos, sin(phase * 0.7), out=work)
        np.subtract(amplitude, work, out=amplitude)
        np.multiply(amplitude, amplitude, out=light)
        np.multiply(light, envelope, out=light)
        np.multiply(light, 1.65, out=light)
        np.absolute(amplitude, out=work)
        np.subtract(work, 0.65, out=work)
        band(work, 0.06)
        np.multiply(work, envelope, out=work)
        np.multiply(work, 0.25, out=work)
        np.add(light, work, out=light)
        np.add(light, nucleus, out=light)
        np.multiply(amplitude, 2.4, out=position)
        np.tanh(position, out=position)
        np.multiply(position, 0.5, out=position)
        np.add(position, 0.5, out=position)
        return spectrum.sample(position, light)

    return sample


class BloomPage(CenteredWelcomePage):
    DEFAULT_CSS = """
    BloomPage .phosphor-art {
        layout: vertical; width: 100%; height: 1fr; min-height: 0; margin-bottom: 1;
    }
    BloomPage .art-caption { width: 100%; height: 1; text-align: center; }
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
        caption.append("  /  BLOOM", style=theme.text_lowlight)
        self._caption.update(caption)


def create_page() -> BloomPage:
    return BloomPage()
