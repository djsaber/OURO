"""Compare native cell packing with the scalar Braille and screen-color rules."""

import pytest

pytest.importorskip("numpy")

import numpy as np
from ouro_tui_themes.dot_matrix.raster import DotArtwork, PhosphorRaster

BITS = (1, 8, 2, 16, 4, 32, 64, 128)
THRESHOLDS = (0.08, 0.48, 0.68, 0.28, 0.23, 0.63, 0.43, 0.13)


@pytest.mark.parametrize("size", [(1, 1), (5, 3), (30, 12)])
@pytest.mark.parametrize("monochrome", [False, True])
@pytest.mark.parametrize("background", [(0, 0, 0), (18, 31, 55), (255, 255, 255)])
def test_cell_encoding_matches_scalar_thresholds_colors_and_dot_order(size, monochrome, background):
    columns, rows = size
    channels = np.random.default_rng(42).random((3, rows * 4, columns * 2))
    before = channels.copy()

    class Engine:
        def __init__(self, grid, prepare):
            pass

        def frame(self, seconds, elapsed):
            # Also exercise a borrowed view whose backing data can change each frame.
            return tuple(channel.reshape(-1) for channel in channels)

    artwork = DotArtwork(lambda grid: None, span=(5.8, 2.7), engine_factory=Engine)
    raster = PhosphorRaster(artwork, columns, rows)
    for mode in ("random", "boundary", "dark"):
        thresholds = tuple(t * (1 if monochrome else 0.25) for t in THRESHOLDS)
        if mode == "boundary":
            # Strict threshold comparisons, inclusive contour, and each Braille bit.
            for row in range(rows):
                for column in range(columns):
                    for lane, threshold in enumerate(thresholds):
                        dy, dx = divmod(lane, 2)
                        value = (threshold, np.nextafter(threshold, 1.0), 0.30)[column % 3]
                        channels[:, row * 4 + dy, column * 2 + dx] = value
                    if column % 3 == 2:
                        channels[:, row * 4, column * 2] = 1.0
        elif mode == "dark":
            channels.fill(0)
        expected = []
        for row in range(rows):
            for column in range(columns):
                dots = channels[:, row * 4 : row * 4 + 4, column * 2 : column * 2 + 2]
                luminance = dots.max(axis=0).flatten().tolist()
                contour = 0.0 if monochrome else max(luminance) * 0.30
                mask = sum(
                    bit
                    for value, bit, threshold in zip(luminance, BITS, thresholds, strict=True)
                    if value > threshold and value >= contour
                )
                rgb = tuple(
                    int(min(1, sum(channel.flatten().tolist()) * (1.25 / 8)) * (255 - base) + base)
                    for channel, base in zip(dots, background, strict=True)
                )
                expected.append((chr(0x2800 + mask), rgb) if mask else (" ", None))
        lines = raster.frame(0, 0, monochrome=monochrome, background=background)
        actual = [
            (char, tuple(segment.style.color.triplet) if segment.style.color else None)
            for row in lines
            for segment in row
            for char in segment.text
        ]
        assert actual == expected
        if mode == "random":
            np.testing.assert_array_equal(channels, before)
        assert raster.frame(0, 0, monochrome=monochrome, background=background) is lines
        assert not raster.damage
        raster.invalidate_frame()
        raster.frame(0, 0, monochrome=monochrome, background=background)
        assert sum(region.area for region in raster.damage) == columns * rows
