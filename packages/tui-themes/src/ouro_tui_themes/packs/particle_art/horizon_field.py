"""Horizon's art direction; only numerical grid/radiance contracts cross this boundary.

Fixed geometry and noise planes are prepared once per viewport. Only the luminous
disk and lensed arc need orbital evaluation each frame; the rest is static light.
No widget, input-event object, scheduler, terminal encoding or engine policy lives here.
"""

from dataclasses import dataclass
from math import cos, floor, sin

import numpy as np

from ouro_tui_themes.packs.particle_art.horizon_projection import HorizonProjection
from ouro_tui_themes.tools.dot_matrix.field import Field, ParticleGrid, Radiance, Sampler, buffer


@dataclass(slots=True)
class HorizonScene:
    """Latest scalar pointer target shared with the numerical worker.

    The UI only replaces this scalar. The sampler owns every array and reads one
    snapshot per frame; the worker never reads a widget or a mutable event object.
    """

    pointer_x: float = 0.0

    def prepare(self, grid: ParticleGrid) -> Sampler:
        return _prepare(grid, self)


def _smooth(low: float, high: float, values: Field) -> Field:
    weight = np.clip((values - low) / (high - low), 0.0, 1.0)
    return weight * weight * (3.0 - 2.0 * weight)


def _band(distance: Field, width: float) -> Field:
    return np.exp(-np.square(distance / width))


class _NoiseSlice:
    """Two cached xy planes of smooth 3D value noise, private to this artwork.

    Time selects/interpolates z planes; hashes are never evaluated for every dot
    every frame. The cache is bounded and supports arbitrary/reversed times.
    """

    def __init__(self, x: Field, y: Field, scale: float, offset: float) -> None:
        ix, iy = np.floor(x), np.floor(y)
        fx, fy = x - ix, y - iy
        fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
        self.phase = ix * 127.1 + iy * 311.7
        self.weights = ((1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy)
        self.scale, self.offset = scale, offset
        self.level: int | None = None
        self.lower, self.upper, self.work = (buffer(len(x)) for _ in range(3))

    def _plane(self, level: int, out: Field) -> None:
        out.fill(0)
        for offset, weight in zip((0, 127.1, 311.7, 438.8), self.weights, strict=True):
            np.add(self.phase, level * 74.7 + offset, out=self.work)
            np.sin(self.work, out=self.work)
            self.work *= 43758.5453
            np.remainder(self.work, 1.0, out=self.work)
            self.work *= weight
            out += self.work

    def add(self, seconds: float, amplitude: float, out: Field) -> None:
        z = seconds * self.scale + self.offset
        level = floor(z)
        if level != self.level:
            if self.level is not None and level == self.level + 1:
                self.lower, self.upper = self.upper, self.lower
            else:
                self._plane(level, self.lower)
            self._plane(level + 1, self.upper)
            self.level = level
        fraction = z - level
        weight = fraction * fraction * (3 - 2 * fraction)
        np.subtract(self.upper, self.lower, out=self.work)
        self.work *= weight
        self.work += self.lower
        self.work *= amplitude
        out += self.work


def _heat(light: Field) -> Radiance:
    warm, white = _smooth(0.05, 1.8, light), _smooth(1.4, 3.8, light)
    return (
        light.copy(),
        ((0.24 + 0.54 * warm) * (1 - white) + 0.96 * white) * light,
        ((0.045 + 0.385 * warm) * (1 - white) + 0.82 * white) * light,
    )


def _prepare(grid: ParticleGrid, scene: HorizonScene) -> Sampler:
    """An inclined disk, upper lensed image, photon ring and sparse fixed stars.

    The screen coordinates remain fixed within the artwork's own viewport.
    Ring frequency is capped below the lattice Nyquist limit, reducing temporal
    aliasing on small terminals without reducing dot count or colour depth.
    """
    pitch = (
        float(grid.x[1] - grid.x[0])
        if grid.width > 1
        else (float(grid.y[1] - grid.y[0]) if grid.height > 1 else 1.0)
    )
    x = grid.x * cos(0.13) + grid.y * sin(0.13)
    y = grid.x * sin(0.13) - grid.y * cos(0.13)
    radius = np.hypot(x, y)
    angle = np.arctan2(y, x)
    dr = np.hypot(x, (y + 0.012) / 0.205)
    da = np.arctan2((y + 0.012) / 0.205, x)
    lr = np.hypot(x, y * 1.04)
    shadow = _smooth(0.519, 0.543, radius)
    disk = _smooth(0.66, 0.83, dr) * (1 - _smooth(1.65, 2.45, dr))
    disk *= (1.55 - 0.27 * x) * _smooth(0.515, 0.58, radius) * shadow
    lens = _band(lr - 0.75, 0.14) * _smooth(-0.025, 0.13, y)
    lens *= np.maximum(0, 2.6 - 0.65 * x) * shadow
    # Resolve the narrow photon ring even when its width is below one sample.
    photon_width = max(0.012, pitch * 0.55)
    photon = _band(radius - 0.559, photon_width) * (0.012 / photon_width)
    photon *= 1.8 + 0.6 * np.sin(angle - 0.8)
    steady = photon + _band(radius - 0.586, 0.043) * 0.45
    steady += np.exp(-radius * 1.7) * 0.07 * _smooth(0.57, 0.9, radius)
    steady *= shadow
    edge = _band(radius - 0.545, max(0.009, pitch * 0.4)) * shadow
    channels = _heat(steady * 1.45)
    for channel, tint in zip(channels, (0.11, 0.15, 0.19), strict=True):
        channel += tint * edge

    # Sparse selection bounds animation work to the artwork, not the full canvas.
    animated = (disk > 0) | (lens > 1e-5)
    indices = np.flatnonzero(animated)
    disk, lens, steady, edge = (v[indices] for v in (disk, lens, steady, edge))
    disk_phase = dr[indices] * min(142.0, 1.6 / pitch)
    disk_angle = da[indices] * 5
    lens_phase = lr[indices] * min(185.0, 1.6 / pitch) + angle[indices] * 3
    lens_angle = angle[indices] * 9
    nx, ny = x[indices] * 4.5, y[indices] * 19
    noise = []
    scale, offset = 0.12, 0.0
    for octave in range(4):
        noise.append((_NoiseSlice(nx, ny, scale, offset), 0.5 ** (octave + 1)))
        nx, ny = nx * 2.03 + 17.1, ny * 2.03 + 17.1
        scale, offset = scale * 2.03, offset * 2.03 + 17.1
    turbulence, wave, light, work, warm, white = (buffer(len(indices)) for _ in range(6))

    # Hash fixed anchors once. Stars occupy the artwork's negative space; they
    # do not fill the black-hole shadow or repaint an idle background lattice.
    anchors = np.arange(len(x), dtype=np.float64)
    random = np.remainder(np.sin(anchors * 127.1 + 311.7) * 43758.5453, 1.0)
    stars = np.flatnonzero((random > 0.997) & (radius > 1.0) & ~animated)
    star_phase = random[stars] * 90
    star_light = buffer(len(stars))
    star_base = tuple(channel[stars] for channel in channels)
    projection = HorizonProjection(grid)

    def sample(seconds: float) -> Radiance:
        nonlocal wave, light, work, star_light
        turbulence.fill(0)
        for plane, amplitude in noise:
            plane.add(seconds, amplitude, turbulence)
        np.subtract(disk_angle, seconds * 0.36, out=wave)
        np.sin(wave, out=wave)
        wave *= 2.7
        wave += disk_phase
        np.multiply(turbulence, 5, out=work)
        wave += work
        wave -= seconds * 1.7
        np.sin(wave, out=wave)
        wave *= 0.5
        wave += 0.5
        np.square(wave, out=wave)
        wave *= 1.15
        np.multiply(turbulence, 0.65, out=work)
        wave += work
        wave += 0.28
        np.multiply(wave, disk, out=light)

        np.subtract(lens_angle, seconds, out=wave)
        np.sin(wave, out=wave)
        wave *= 1.5
        wave += lens_phase
        wave += seconds * 1.2
        np.sin(wave, out=wave)
        wave *= 0.5
        wave += 0.5
        np.square(wave, out=wave)
        wave *= 0.6
        wave += 0.4
        wave *= lens
        light += wave
        light += steady
        light *= 1.45

        # Heat interpolation stays in reusable buffers, with no per-frame RGB
        # tuples per dot or full-grid interpolation arrays.
        for target, low, high in ((warm, 0.05, 1.8), (white, 1.4, 3.8)):
            np.subtract(light, low, out=target)
            target /= high - low
            np.clip(target, 0, 1, out=target)
            np.multiply(target, -2, out=work)
            work += 3
            np.square(target, out=target)
            target *= work
        for channel, base, gold, ivory, rim in zip(
            channels,
            (1, 0.24, 0.045),
            (1, 0.78, 0.43),
            (1, 0.96, 0.82),
            (0.11, 0.15, 0.19),
            strict=True,
        ):
            np.multiply(warm, gold - base, out=wave)
            wave += base
            np.subtract(ivory, wave, out=work)
            work *= white
            wave += work
            wave *= light
            np.multiply(edge, rim, out=work)
            wave += work
            channel[indices] = wave
        np.add(star_phase, seconds * 0.4, out=star_light)
        np.sin(star_light, out=star_light)
        star_light *= 0.25
        star_light += 0.35
        pointer_target = scene.pointer_x
        if pointer_target or projection.position:
            for channel, star_background in zip(channels, star_base, strict=True):
                channel[stars] = star_background
        output = projection.sample(channels, pointer_target, seconds)
        for channel, star_background, tint in zip(
            output, star_base, (0.13, 0.16, 0.14), strict=True
        ):
            if output is channels:
                channel[stars] = star_background + tint * star_light
            else:
                # Stars stay in screen coordinates, not on the disk.
                channel[stars] += tint * star_light
        return output

    return sample
