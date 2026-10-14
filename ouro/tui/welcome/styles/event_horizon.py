"""An occulted accretion disk and its bent upper and lower light paths."""

from array import array
from math import sin

import arrayfunc as af  # type: ignore[import-untyped]

from ouro.tui.welcome.dot_matrix.field import (
    DotGrid,
    Radiance,
    Sampler,
    Spectrum,
    band,
    buffer,
    clip,
)

_SPECTRUM = ((0.30, 0.045, 0.008), (1.0, 0.27, 0.045), (1.0, 0.78, 0.38), (1.0, 0.98, 0.86))


def prepare(grid: DotGrid) -> Sampler:
    x, y = array("d", grid.x), array("d", grid.y)
    (tilted, radius, angle, disk_radius, turbulence, work, disk, light, position, beaming) = (
        buffer(len(x)) for _ in range(10)
    )
    af.truediv(x, -1.6, beaming)
    clip(beaming, -0.7, 1.0, work)
    af.mul(beaming, 0.48)
    af.add(beaming, 0.65)
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        tilt = 0.10 + 0.055 * sin(seconds * 0.13)
        af.mul(x, tilt, tilted)
        af.sub(y, tilted, tilted)
        af.hypot(x, tilted, radius)
        af.atan2(tilted, x, angle)
        af.truediv(tilted, 0.24, work)
        af.hypot(x, work, disk_radius)
        af.mul(disk_radius, 21.0, turbulence)
        af.sub(turbulence, seconds * 1.7)
        af.mul(angle, 3.0, work)
        af.add(turbulence, work)
        af.sin(turbulence)
        af.mul(turbulence, 0.16)
        af.add(turbulence, 0.76)
        af.mul(disk_radius, 43.0, work)
        af.mul(angle, 7.0, disk)
        af.add(work, disk)
        af.add(work, seconds * 0.8)
        af.sin(work)
        af.mul(work, 0.08)
        af.add(turbulence, work)
        af.sub(disk_radius, 1.13, disk)
        band(disk, 0.62)
        af.mul(disk, turbulence)
        af.mul(disk, beaming, light)
        # The far-side disk is seen over the shadow; the near side hides its base.
        af.add(angle, 1.1, work)
        af.cos(work)
        af.mul(work, 0.36)
        af.add(work, 0.64)
        af.sub(radius, 0.64, disk)
        band(disk, 0.085)
        af.mul(disk, work)
        af.add(light, disk)
        af.sub(radius, 0.70, work)
        band(work, 0.20)
        af.mul(work, 0.17)
        af.add(light, work)
        af.sub(radius, 0.535, work)
        band(work, 0.018)
        af.mul(work, 0.9)
        af.add(light, work)
        af.sub(radius, 0.49, work)
        af.mul(work, 55.0)
        clip(work, 0.0, 1.0, disk)
        af.mul(light, work)
        af.sub(radius, 1.9, work)
        af.abs_(work, disk)
        af.add(work, disk)
        af.mul(work, -1.5)
        af.exp(work)
        af.mul(light, work)
        af.mul(light, 1.15, position)
        af.mul(light, 1.65)
        return spectrum.sample(position, light)

    return sample
