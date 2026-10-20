"""A Clifford torus rotated in four dimensions, sampled by inverse projection."""

from array import array
from math import cos, exp, sin

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
)
from ouro.tui.welcome.styles.dot_matrix.raster import DotArtwork
from ouro.tui.welcome.styles.dot_matrix.widget import DotMatrix

_SPECTRUM = ((0.21, 0.12, 0.58), (0.51, 0.34, 1.0), (0.38, 0.86, 1.0), (0.92, 0.98, 1.0))
_DEPTHS = tuple(-2.2 + index * (4.4 / 13) for index in range(13)) + (2.2,)


def prepare(grid: ParticleGrid) -> Sampler:
    x, y = array("d", grid.x), array("d", grid.y)
    af.mul(x, 2.5)
    af.mul(y, 2.5)
    (
        radial,
        angle,
        radius,
        denominator,
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
    af.mul(x, x, radial)
    af.mul(y, y, work)
    af.add(radial, work)
    af.atan2(y, x, angle)
    af.mul(radial, -0.32, falloff)
    af.exp(falloff)
    depths = tuple((depth, exp(-depth * depth * 0.32) * 0.65) for depth in _DEPTHS)
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        a = 0.18 * sin(seconds * 0.19)
        b = 0.55 + 0.35 * sin(seconds * 0.13)
        ca, sa, cb, sb = cos(a), sin(a), cos(b), sin(b)
        af.repeat(light, 0.0)
        af.repeat(transmission, 1.0)
        # Integrate fixed view rays through S3; rotate the projected surface,
        # never the receiving LED grid.
        for depth, attenuation in depths:
            af.add(radial, depth * depth, radius)
            af.add(radius, 1.0, denominator)
            af.mul(x, 2.0 * ca, rx)
            af.sub(radius, 1.0, work)
            af.mul(work, sa)
            af.sub(rx, work)
            af.truediv(rx, denominator)
            af.mul(y, 2.0 * cb, ry)
            af.sub(ry, 2 * depth * sb)
            af.truediv(ry, denominator)
            af.mul(rx, rx, surface)
            af.mul(ry, ry, work)
            af.add(surface, work)
            af.sub(surface, 0.5)
            af.add(rx, ry, weave)
            af.mul(weave, 28.0)
            af.sub(weave, seconds * 0.5)
            af.cos(weave)
            af.mul(weave, weave)
            af.mul(weave, 0.35)
            af.add(weave, 0.65)
            density[:] = surface
            band(density, 0.07)
            af.mul(density, falloff)
            af.mul(density, attenuation)
            af.mul(density, weave, work)
            af.mul(work, transmission)
            af.mul(work, 1 - depth * 0.18)
            af.add(light, work)
            af.mul(density, 0.55, work)
            af.sub(1.0, work, work)
            af.mul(transmission, work)
        af.add(angle, seconds * 0.17, position)
        af.sin(position)
        af.mul(position, 0.5)
        af.add(position, 0.5)
        af.mul(light, 1.8)
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
