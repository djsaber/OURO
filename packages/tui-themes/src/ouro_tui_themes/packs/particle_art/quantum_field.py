"""Two coherent spherical waves sampled on a fixed lattice, before phosphor/RGB.

Only the latest pointer scalar crosses from the UI. Geometry and scratch arrays
belong to one size-owned sampler; no events, widgets or scheduling enter here.
"""

from dataclasses import dataclass
from math import cos, exp, sin, tau

import numpy as np

from ouro_tui_themes.tools.dot_matrix.field import ParticleGrid, Radiance, Sampler, buffer


@dataclass(slots=True)
class QuantumScene:
    pointer_y: float = 0.0

    def prepare(self, grid: ParticleGrid) -> Sampler:
        pitch = (
            float(grid.x[1] - grid.x[0])
            if grid.width > 1
            else (float(grid.y[1] - grid.y[0]) if grid.height > 1 else 1.0)
        )
        # One broad oval wraps both sources and their outward waves. Its
        # quintic shoulder reaches zero with no slope or curvature at the edge.
        # Bake it into the existing gains once per size, not once per frame.
        half_width = abs(float(grid.x[-1])) + pitch * 0.5
        half_height = abs(float(grid.y[-1])) + pitch * 0.5
        horizontal = np.abs((grid.x / half_width - 0.04) / 0.90)
        vertical = np.abs(grid.y / (half_height * 0.96))
        radius = (horizontal**2.5 + vertical**2.5) ** 0.4
        edge = np.clip((1 - radius) / 0.26, 0, 1)
        frame_envelope = edge**3 * (edge * (edge * 6 - 15) + 10)
        x = grid.x * cos(0.22) + grid.y * sin(0.22)
        rotated_y = grid.x * sin(0.22) - grid.y * cos(0.22)
        aperture = np.clip((x + 1.6) / 0.8, 0, 1)
        falloff = np.exp(-(x * x + rotated_y * rotated_y) * 0.32)
        falloff *= aperture * aperture * (3 - 2 * aperture)
        falloff *= frame_envelope
        dots = np.flatnonzero(falloff > 0)
        # Match the reference shader's working precision while keeping the
        # engine's float64 radiance contract and full terminal colour precision.
        x_squared = np.asarray((x[dots] + 0.7) ** 2, dtype=np.float32)
        y = np.asarray(rotated_y[dots], dtype=np.float32)
        envelope = np.asarray(falloff[dots], dtype=np.float32)
        # Keep the moving carrier resolved on coarse terminal lattices.
        carrier_frequency = min(19.0, 0.8 / pitch)
        d1, d2, difference, total, light, phase, work, color = (
            np.empty(len(dots), dtype=np.float32) for _ in range(8)
        )
        # Six Gaussian widths cover both cores' complete motion. Outside this
        # envelope their emission is below terminal colour precision.
        nearest_y = np.maximum(np.abs(np.abs(y) - 0.46) - 0.08, 0)
        cores = np.flatnonzero(x_squared + nearest_y * nearest_y < (6 * 0.07) ** 2)
        core_dots = dots[cores]
        core_gain = np.asarray(frame_envelope[core_dots] * 3, dtype=np.float32)
        glow, glow_work = (np.empty(len(cores), dtype=np.float32) for _ in range(2))
        channels: Radiance = (buffer(len(grid.x)), buffer(len(grid.x)), buffer(len(grid.x)))
        anchors = np.arange(len(grid.x))
        random = np.remainder(
            np.sin((anchors % grid.width + 0.5) * 127.1 + (anchors // grid.width + 0.5) * 311.7)
            * 43758.5453,
            1.0,
        )
        stars = np.flatnonzero((random > 0.997) & (frame_envelope > 0))
        star_phase, star_light = random[stars] * 90, buffer(len(stars))
        star_floor, star_swing = frame_envelope[stars] * 0.35, frame_envelope[stars] * 0.25
        position, last_time = 0.0, None

        def sample(seconds: float) -> Radiance:
            nonlocal position, last_time, light, phase, work, color, glow, glow_work, star_light
            delta = 1 / 30 if last_time is None else max(0.0, min(0.1, seconds - last_time))
            last_time = seconds
            target = max(-1.0, min(1.0, self.pointer_y))
            position += (target - position) * (1 - exp(-2.7626363100844 * delta))
            if abs(target - position) < 0.0001:
                position = target
            t = seconds + 12.0  # The reference opens at this phase of the cycle.
            separation = 0.46 + 0.08 * sin(t * 0.15)
            coherence = 0.72 + 0.28 * sin(t * 0.19)
            for distance, offset in ((d1, separation), (d2, -separation)):
                np.subtract(y, offset, out=distance)
                np.square(distance, out=distance)
                distance += x_squared
                np.maximum(distance, 0.04**2, out=distance)
                np.sqrt(distance, out=distance)
            np.subtract(d1, d2, out=difference)
            np.add(d1, d2, out=total)
            np.multiply(d1, d2, out=work)
            np.sqrt(work, out=work)
            np.divide(total, work, out=light)
            np.multiply(difference, min(30 + position * 2, 1.6 / pitch), out=phase)
            # Reduce time phases in double precision before entering float32
            # buffers, so long-running sessions keep sub-frame motion smooth.
            phase += (t * 0.7) % tau
            np.cos(phase, out=phase)
            phase *= 2 * coherence
            light += phase
            light /= work
            light *= 0.3
            np.maximum(light, 0, out=light)
            np.power(light, 1.35, out=light)

            np.multiply(total, carrier_frequency, out=phase)
            phase -= (t * 0.9) % tau
            np.cos(phase, out=phase)
            phase *= 0.5
            phase += 0.5
            np.square(phase, out=work)
            phase *= work
            phase *= 0.65
            phase += 0.35
            light *= phase
            light *= envelope
            light *= 1.5
            np.multiply(difference, 11, out=color)
            np.sin(color, out=color)
            color *= 0.5
            color += 0.5
            np.take(d1, cores, out=glow, mode="clip")
            np.square(glow, out=glow)
            glow *= -1 / 0.07**2
            np.exp(glow, out=glow)
            np.take(d2, cores, out=glow_work, mode="clip")
            np.square(glow_work, out=glow_work)
            glow_work *= -1 / 0.07**2
            np.exp(glow_work, out=glow_work)
            glow += glow_work
            glow *= core_gain
            np.add(star_phase, t * 0.4, out=star_light)
            np.sin(star_light, out=star_light)
            star_light *= star_swing
            star_light += star_floor
            for channel, blue, green, core, star in zip(
                channels,
                (0.12, 0.42, 0.82),
                (0.34, 1.0, 0.81),
                (0.7, 1.0, 0.94),
                (0.13, 0.16, 0.14),
                strict=True,
            ):
                channel[stars] = 0
                np.multiply(color, green - blue, out=phase)
                phase += blue
                phase *= light
                channel[dots] = phase
                np.multiply(glow, core, out=glow_work)
                channel[core_dots] += glow_work
                channel[stars] += star_light * star
            return channels

        return sample
