"""A Clifford torus rotated in four dimensions, sampled by inverse projection."""

from math import cos, exp, sin

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

_SPECTRUM = ((0.21, 0.12, 0.58), (0.51, 0.34, 1.0), (0.38, 0.86, 1.0), (0.92, 0.98, 1.0))
_DEPTHS = tuple(-2.2 + index * (4.4 / 13) for index in range(13)) + (2.2,)


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = grid.x.copy(), grid.y.copy()
    np.multiply(x, 2.5, out=x)
    np.multiply(y, 2.5, out=y)
    (
        radial,
        angle,
        rotated_x,
        rotated_y,
        rx,
        ry,
        work,
        surface,
        weave,
        density,
        light,
        transmission,
        position,
        falloff,
    ) = (buffer(len(x)) for _ in range(14))
    np.multiply(x, x, out=radial)
    np.multiply(y, y, out=work)
    np.add(radial, work, out=radial)
    np.arctan2(y, x, out=angle)
    np.multiply(radial, -0.32, out=falloff)
    np.exp(falloff, out=falloff)
    # The inverse stereographic denominator depends only on the fixed grid/depth.
    # Keep one reciprocal plane per ray sample, not four projected coordinates.
    depths = tuple(
        (depth, exp(-depth * depth * 0.32) * 0.65, 1.0 / (radial + depth * depth + 1.0))
        for depth in _DEPTHS
    )
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        a = 0.18 * sin(seconds * 0.19)
        b = 0.55 + 0.35 * sin(seconds * 0.13)
        ca, sa, cb, sb = cos(a), sin(a), cos(b), sin(b)
        light.fill(0.0)
        transmission.fill(1.0)
        np.multiply(x, 2.0 * ca, out=rotated_x)
        np.add(rotated_x, 2.0 * sa, out=rotated_x)
        np.multiply(y, 2.0 * cb, out=rotated_y)
        # Integrate fixed view rays through S3; rotate the projected surface,
        # never the receiving LED grid.
        for depth, attenuation, reciprocal in depths:
            np.multiply(rotated_x, reciprocal, out=rx)
            np.subtract(rx, sa, out=rx)
            np.subtract(rotated_y, 2 * depth * sb, out=ry)
            np.multiply(ry, reciprocal, out=ry)
            np.multiply(rx, rx, out=surface)
            np.multiply(ry, ry, out=work)
            np.add(surface, work, out=surface)
            np.subtract(surface, 0.5, out=surface)
            np.add(rx, ry, out=weave)
            np.multiply(weave, 28.0, out=weave)
            np.subtract(weave, seconds * 0.5, out=weave)
            np.cos(weave, out=weave)
            np.multiply(weave, weave, out=weave)
            np.multiply(weave, 0.35, out=weave)
            np.add(weave, 0.65, out=weave)
            density[:] = surface
            band(density, 0.07)
            np.multiply(density, falloff, out=density)
            np.multiply(density, attenuation, out=density)
            np.multiply(density, weave, out=work)
            np.multiply(work, transmission, out=work)
            np.multiply(work, 1 - depth * 0.18, out=work)
            np.add(light, work, out=light)
            np.multiply(density, 0.55, out=work)
            np.subtract(1.0, work, out=work)
            np.multiply(transmission, work, out=transmission)
        np.add(angle, seconds * 0.17, out=position)
        np.sin(position, out=position)
        np.multiply(position, 0.5, out=position)
        np.add(position, 0.5, out=position)
        np.multiply(light, 1.8, out=light)
        return spectrum.sample(position, light)

    return sample


class IrisPage(CenteredWelcomePage):
    DEFAULT_CSS = """
    IrisPage .phosphor-art {
        layout: vertical; width: 100%; height: 1fr; min-height: 0; margin-bottom: 1;
    }
    IrisPage .art-caption { width: 100%; height: 1; text-align: center; }
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
        caption.append("  /  IRIS", style=theme.text_lowlight)
        self._caption.update(caption)


def create_page() -> IrisPage:
    return IrisPage()
