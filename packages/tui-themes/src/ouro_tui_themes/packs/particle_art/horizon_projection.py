"""Theme-local, sub-dot rotation of radiance on an unchanged particle lattice."""

from math import cos, exp, sin

import numpy as np

from ouro_tui_themes.tools.dot_matrix.field import ParticleGrid, Radiance, buffer


class _RotationMap:
    """Four bounded taps, refreshed only when the angle changes; no frame cache."""

    def __init__(self, grid: ParticleGrid) -> None:
        self.width, self.height = grid.width, grid.height
        pitch = float(grid.x[1] - grid.x[0])
        # Bound rotation to the disk/lens plus its full +/-2 degree sweep. The
        # faint radial outer glow needs no rotation. A dot-pitch guard keeps
        # interpolation inside the envelope even on coarse terminal grids.
        radius = np.hypot(grid.x, grid.y)
        disk_y = grid.x * sin(0.13) - grid.y * cos(0.13)
        guard = radius * 0.035 + pitch * 2
        envelope = (radius > max(0, 0.51 - pitch * 2)) & (
            ((radius < 1.25 + pitch * 2) & (disk_y > -0.025 - guard))
            | ((radius < 2.45 + pitch * 2) & (np.abs(disk_y) < 0.52 + guard))
        )
        self.dots = np.flatnonzero(envelope)
        self.x, self.y = grid.x[self.dots] / pitch, grid.y[self.dots] / pitch
        self.inside = bool(
            np.all(np.abs(self.x) + np.abs(self.y) * 0.035 < (self.width - 1) / 2 - 1)
            and np.all(np.abs(self.y) + np.abs(self.x) * 0.035 < (self.height - 1) / 2 - 1)
        )
        count = len(self.dots)
        self.indices = tuple(np.empty(count, dtype=np.intp) for _ in range(4))
        self.weights = tuple(buffer(count) for _ in range(4))
        self.fx, self.fy, self.work = (buffer(count) for _ in range(3))
        self.result = buffer(count)
        self.valid, self.mask = (np.empty(count, dtype=np.bool_) for _ in range(2))
        self.angle: float | None = None

    def update(self, angle: float) -> None:
        if angle == self.angle:
            return
        self.angle = angle
        c, s = cos(angle), sin(angle)
        np.multiply(self.x, c, out=self.fx)
        np.multiply(self.y, -s, out=self.work)
        self.fx += self.work
        self.fx += (self.width - 1) / 2
        np.multiply(self.y, c, out=self.fy)
        np.multiply(self.x, s, out=self.work)
        self.fy += self.work
        self.fy += (self.height - 1) / 2
        if not self.inside:
            np.greater_equal(self.fx, 0, out=self.valid)
            for coordinates, maximum in ((self.fx, self.width - 1), (self.fy, self.height - 1)):
                np.greater_equal(coordinates, 0, out=self.mask)
                self.valid &= self.mask
                np.less_equal(coordinates, maximum, out=self.mask)
                self.valid &= self.mask
                np.clip(coordinates, 0, maximum, out=coordinates)
        # Reuse two tap buffers for integer x/y before forming flat indices.
        i00, i10, i01, i11 = self.indices
        for coordinates, indices, maximum in (
            (self.fx, i10, self.width - 2),
            (self.fy, i00, self.height - 2),
        ):
            np.floor(coordinates, out=self.work)
            if not self.inside:
                np.minimum(self.work, maximum, out=self.work)
            np.copyto(indices, self.work, casting="unsafe")
            coordinates -= self.work
        np.multiply(i00, self.width, out=i00)
        i00 += i10
        np.add(i00, 1, out=i10)
        np.add(i00, self.width, out=i01)
        np.add(i01, 1, out=i11)
        w00, w10, w01, w11 = self.weights
        np.subtract(1, self.fx, out=w00)
        np.copyto(w01, w00)
        np.copyto(w10, self.fx)
        np.copyto(w11, self.fx)
        np.subtract(1, self.fy, out=self.work)
        w00 *= self.work
        w10 *= self.work
        w01 *= self.fy
        w11 *= self.fy

    def apply(self, channels: Radiance, output: Radiance) -> None:
        for source, target in zip(channels, output, strict=True):
            np.copyto(target, source)
            self.result.fill(0)
            for indices, weights in zip(self.indices, self.weights, strict=True):
                # mode=clip permits writing directly to the supplied work buffer.
                np.take(source, indices, out=self.work, mode="clip")
                self.work *= weights
                self.result += self.work
            if not self.inside:
                self.result *= self.valid
            target[self.dots] = self.result


class HorizonProjection:
    """Match the reference's +/-0.035 radian tilt and 0.045-at-60-Hz easing.

    The UI supplies only the latest normalized x. All smoothing, mapping and
    reusable buffers belong to the numerical worker, before phosphor/Braille.
    """

    def __init__(self, grid: ParticleGrid) -> None:
        self.grid = grid
        self.output: Radiance | None = None
        self.mapping: _RotationMap | None = None
        self.position = 0.0
        self.last_time: float | None = None

    def sample(self, channels: Radiance, target: float, seconds: float) -> Radiance:
        delta = 1 / 30 if self.last_time is None else max(0.0, min(0.1, seconds - self.last_time))
        self.last_time = seconds
        target = max(-1.0, min(1.0, target))
        self.position += (target - self.position) * (1 - exp(-2.7626363100844 * delta))
        if abs(target - self.position) < 0.0001:
            self.position = target
        if self.position == 0 or self.grid.width < 2 or self.grid.height < 2:
            return channels
        if self.mapping is None:
            self.mapping = _RotationMap(self.grid)
            self.output = (
                buffer(len(self.grid.x)),
                buffer(len(self.grid.x)),
                buffer(len(self.grid.x)),
            )
        assert self.output is not None
        # Positive pointer x flattens the baseline disk's clockwise tilt.
        self.mapping.update(self.position * 0.035)
        self.mapping.apply(channels, self.output)
        return self.output
