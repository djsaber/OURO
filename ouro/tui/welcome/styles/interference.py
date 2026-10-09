"""A caustic aperture formed by coherent sources and a slowly drifting phase."""

from array import array

import arrayfunc as af  # type: ignore[import-untyped]

from ouro.tui.welcome.dot_matrix.field import DotGrid, Radiance, Sampler, Spectrum, band, buffer
from ouro.tui.welcome.dot_matrix.widget import dot_style

_SPECTRUM = ((0.025, 0.19, 0.25), (0.07, 0.66, 0.63), (0.49, 0.95, 0.77), (0.95, 1.0, 0.86))


def prepare(grid: DotGrid) -> Sampler:
    x, y = array("d", grid.x), array("d", grid.y)
    left, right, lower, aperture, center = (buffer(len(x)) for _ in range(5))
    af.add(x, 0.83, left)
    af.hypot(left, y)
    af.sub(x, 0.83, right)
    af.hypot(right, y)
    af.sub(y, 1.2, lower)
    af.hypot(x, lower, lower)
    af.sub(left, right, center)
    band(center, 0.05)
    af.mul(center, 0.1)
    af.mul(left, 18.0)
    af.mul(right, 18.0)
    af.mul(lower, 15.0)
    af.truediv(x, 2.1, aperture)
    af.pow(aperture, 6.0)
    af.truediv(y, 0.9, y)
    af.pow(y, 6.0)
    af.add(aperture, y)
    af.neg(aperture)
    af.exp(aperture)
    wave, work, position, light = (buffer(len(x)) for _ in range(4))
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        phase = seconds * 0.55
        af.sub(left, phase, wave)
        af.cos(wave)
        af.add(right, phase, work)
        af.cos(work)
        af.add(wave, work)
        af.add(lower, phase * 0.6, work)
        af.cos(work)
        af.mul(work, 0.65)
        af.add(wave, work)
        af.abs_(wave, position)
        af.truediv(position, 2.65)
        af.pow(position, 3.0, light)
        af.mul(light, 1.7)
        af.add(light, center)
        af.mul(light, aperture)
        return spectrum.sample(position, light)

    return sample


STYLE = dot_style("interference", "Tide", "Jade light woven into a breathing caustic.", prepare)
