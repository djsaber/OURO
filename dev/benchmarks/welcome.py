"""Measure complete artwork frames, including sampling and terminal Strip construction."""

from __future__ import annotations

import argparse
import json
import platform
from collections.abc import Callable
from functools import partial
from importlib.metadata import version
from statistics import median
from time import perf_counter

from ouro_tui_themes.dot_matrix.field import ParticleGrid
from ouro_tui_themes.dot_matrix.raster import DotArtwork, PhosphorRaster
from ouro_tui_themes.styles import STYLES

from ouro.tui.welcome import welcome_catalog

WELCOME_STYLES = welcome_catalog(STYLES)


def measure(sample: Callable[[float], object], frames: int) -> dict[str, float]:
    for tick in range(5):
        sample(tick / 30)
    timings = []
    for tick in range(frames):
        started = perf_counter()
        sample(7.5 + tick / 30)
        timings.append((perf_counter() - started) * 1000)
    ordered = sorted(timings)
    return {
        "median_ms": round(median(timings), 3),
        "p95_ms": round(ordered[int(frames * 0.95) - 1], 3),
        "p99_ms": round(ordered[int(frames * 0.99) - 1], 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--size", nargs=2, type=int, action="append", metavar=("COLUMNS", "ROWS"))
    parser.add_argument(
        "--stages", action="store_true", help="Also measure sampler and engine alone"
    )
    args = parser.parse_args()
    if args.frames < 20:
        parser.error("--frames must be at least 20")
    sizes = args.size or ((80, 14), (114, 20))
    if any(columns < 1 or rows < 1 for columns, rows in sizes):
        parser.error("--size dimensions must be positive")
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "numpy": version("numpy"),
                "frames": args.frames,
            }
        )
    )
    for columns, rows in sizes:
        for identity, style in WELCOME_STYLES.items():
            if identity == "basic":
                continue
            started = perf_counter()
            artwork = getattr(style.create(), "artwork", None)
            assert isinstance(artwork, DotArtwork), f"{identity} does not provide dot artwork"
            raster = PhosphorRaster(artwork, columns, rows)
            raster.frame(0, 0)
            first_ms = (perf_counter() - started) * 1000
            frame_times = measure(partial(raster.frame, elapsed=1 / 30), args.frames)
            stages = {}
            if args.stages:
                grid = ParticleGrid.create(columns * 2, rows * 4, span=artwork.span)
                stages["sampler"] = measure(artwork.prepare(grid), args.frames)
                engine = artwork.engine_factory(grid, artwork.prepare)
                stages["engine"] = measure(partial(engine.frame, elapsed=1 / 30), args.frames)
            print(
                json.dumps(
                    {
                        "style": identity,
                        "size": [columns, rows],
                        "first_ms": round(first_ms, 3),
                        **frame_times,
                        **stages,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
