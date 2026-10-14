"""A slowly turning Julia set, lit by escape time and an interior orbit trap."""

from array import array
from math import cos, hypot, log2, sin

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

_SPECTRUM = ((0.26, 0.09, 0.48), (0.78, 0.31, 0.83), (1.0, 0.67, 0.47), (1.0, 0.96, 0.54))


def prepare(grid: DotGrid) -> Sampler:
    output: Radiance = (buffer(len(grid.x)), buffer(len(grid.x)), buffer(len(grid.x)))
    # The aperture is identically zero outside this disk. Iterate only its
    # support, then scatter emission back onto the unchanged full dot lattice.
    indices = tuple(
        index for index, (x, y) in enumerate(zip(grid.x, grid.y, strict=True)) if hypot(x, y) < 1.04
    )
    if not indices:
        return lambda _: output
    x = array("d", (grid.x[index] for index in indices))
    y = array("d", (grid.y[index] for index in indices))
    size = len(x)
    (
        radius,
        depth,
        scale,
        aperture,
        rim,
        illumination,
        real,
        imag,
        next_real,
        next_imag,
        magnitude,
        active,
        inactive,
        escape,
        trap,
        work,
        scratch,
        light,
        position,
    ) = (buffer(size) for _ in range(19))
    escaped_indices = array("q", [0]) * size
    af.hypot(x, y, radius)
    af.truediv(radius, 1.02, depth)
    af.mul(depth, depth)
    af.sub(1.0, depth, depth)
    clip(depth, 0.0, 1.0, scratch)
    af.sqrt(depth)
    af.add(depth, 1.0, scale)
    af.sub(1.04, radius, aperture)
    af.mul(aperture, 55.0)
    clip(aperture, 0.0, 1.0, scratch)
    af.sub(radius, 0.995, rim)
    band(rim, 0.025)
    af.mul(rim, 0.8)
    af.mul(depth, 0.45, illumination)
    af.add(illumination, 0.38)
    af.mul(y, 0.15, work)
    af.sub(illumination, work)
    spectrum = Spectrum(size, _SPECTRUM)
    # The rim uses a fixed position on the same piecewise-linear spectrum.
    rim_color = tuple(a * 0.45 + b * 0.55 for a, b in zip(_SPECTRUM[2], _SPECTRUM[3], strict=True))

    def sample(seconds: float) -> Radiance:
        ca, sa = cos(seconds * 0.065), sin(seconds * 0.065)
        af.mul(x, ca, real)
        af.mul(y, sa, work)
        af.sub(real, work)
        af.mul(real, 2.0)
        af.truediv(real, scale)
        af.mul(x, sa, imag)
        af.mul(y, ca, work)
        af.add(imag, work)
        af.mul(imag, 2.0)
        af.truediv(imag, scale)
        cr = -0.745 + 0.028 * sin(seconds * 0.11)
        ci = 0.188 + 0.018 * cos(seconds * 0.09)
        af.repeat(active, 1.0)
        af.repeat(escape, 36.0)
        af.repeat(trap, 8.0)
        remaining = size
        for step in range(36):
            af.sub(1.0, active, inactive)
            af.mul(real, real, next_real)
            af.mul(imag, imag, work)
            af.sub(next_real, work)
            af.add(next_real, cr)
            af.mul(real, imag, next_imag)
            af.mul(next_imag, 2.0)
            af.add(next_imag, ci)
            af.mul(next_real, active)
            af.mul(real, inactive)
            af.add(real, next_real)
            af.mul(next_imag, active)
            af.mul(imag, inactive)
            af.add(imag, next_imag)
            af.hypot(real, imag, magnitude)
            af.sub(magnitude, 0.62, work)
            af.abs_(work)
            af.mul(work, active)
            af.mul(inactive, 8.0, scratch)
            af.add(work, scratch)
            # Elementwise minimum, with the orbit of escaped points frozen.
            af.sub(trap, work, scratch)
            af.abs_(scratch)
            af.add(trap, work)
            af.sub(trap, scratch)
            af.mul(trap, 0.5)
            af.mul(magnitude, active, work)
            count = af.findindices(">", work, escaped_indices, 8.0)
            for index in escaped_indices[:count]:
                escape[index] = step + 1 - log2(log2(magnitude[index]))
                active[index] = 0.0
            remaining -= count
            if not remaining:
                break
        af.truediv(escape, 22.0)
        clip(escape, 0.0, 1.0, scratch)
        work[:] = trap
        band(work, 0.06)
        af.mul(work, 0.42)
        af.pow(escape, 1.2, light)
        af.mul(light, 0.55)
        af.add(light, 0.16)
        af.add(light, work)
        af.mul(light, illumination)
        af.mul(y, -0.45, position)
        af.add(position, 0.5)
        af.mul(trap, 0.25, work)
        af.add(position, work)
        af.mul(escape, 0.27, work)
        af.sub(position, work)
        channels = spectrum.sample(position, light)
        for target, channel, color in zip(output, channels, rim_color, strict=True):
            af.mul(rim, color, work)
            af.add(channel, work)
            af.mul(channel, aperture)
            af.mul(channel, 1.8)
            for index, value in zip(indices, channel, strict=True):
                target[index] = value
        return output

    return sample
