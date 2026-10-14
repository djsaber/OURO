"""Phase beating between angular orbital modes and their radial nodes."""

from array import array

import arrayfunc as af  # type: ignore[import-untyped]

from ouro.tui.welcome.dot_matrix.field import DotGrid, Radiance, Sampler, Spectrum, band, buffer

_SPECTRUM = ((0.11, 0.49, 0.83), (0.62, 0.91, 1.0), (1.0, 0.92, 0.72), (1.0, 0.37, 0.08))


def prepare(grid: DotGrid) -> Sampler:
    x, y = array("d", grid.x), array("d", grid.y)
    radius, angle, radial, overtone, envelope, nucleus = (buffer(len(x)) for _ in range(6))
    af.hypot(x, y, radius)
    af.atan2(y, x, angle)
    af.mul(radius, 3.6, radial)
    af.sub(radial, 0.4)
    af.sin(radial)
    af.mul(radius, 5.3, overtone)
    af.sin(overtone)
    af.mul(overtone, 0.32)
    af.truediv(radius, 1.02, envelope)
    af.pow(envelope, 4.0)
    af.neg(envelope)
    af.exp(envelope)
    nucleus[:] = radius
    band(nucleus, 0.105)
    af.mul(nucleus, 0.65)
    amplitude, work, light, position = (buffer(len(x)) for _ in range(4))
    spectrum = Spectrum(len(x), _SPECTRUM)

    def sample(seconds: float) -> Radiance:
        phase = seconds * 0.24
        af.mul(angle, 3.0, amplitude)
        af.add(amplitude, phase)
        af.cos(amplitude)
        af.mul(amplitude, radial)
        af.mul(angle, 2.0, work)
        af.sub(work, phase * 0.7)
        af.sin(work)
        af.mul(work, overtone)
        af.add(amplitude, work)
        af.mul(amplitude, amplitude, light)
        af.mul(light, envelope)
        af.mul(light, 1.65)
        af.abs_(amplitude, work)
        af.sub(work, 0.65)
        band(work, 0.06)
        af.mul(work, envelope)
        af.mul(work, 0.25)
        af.add(light, work)
        af.add(light, nucleus)
        af.mul(amplitude, 2.4, position)
        af.tanh(position)
        af.mul(position, 0.5)
        af.add(position, 0.5)
        return spectrum.sample(position, light)

    return sample
