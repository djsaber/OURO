"""Measure complete artwork frames, including sampling and terminal Strip construction."""

from __future__ import annotations

import argparse
import json
import platform
from statistics import median
from time import perf_counter

from ouro.tui.welcome import WELCOME_STYLES
from ouro.tui.welcome.dot_matrix.raster import PhosphorRaster


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=60)
    args = parser.parse_args()
    if args.frames < 20:
        parser.error("--frames must be at least 20")
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version()}))
    for columns, rows in ((80, 14), (114, 20)):
        for identity, style in WELCOME_STYLES.items():
            if identity == "basic":
                continue
            started = perf_counter()
            raster = PhosphorRaster(style.create().artwork, columns, rows)
            raster.frame(0, 0)
            first_ms = (perf_counter() - started) * 1000
            for tick in range(5):
                raster.frame(tick / 30, 1 / 30)
            timings = []
            for tick in range(args.frames):
                started = perf_counter()
                raster.frame(7.5 + tick / 30, 1 / 30)
                timings.append((perf_counter() - started) * 1000)
            print(
                json.dumps(
                    {
                        "style": identity,
                        "size": [columns, rows],
                        "first_ms": round(first_ms, 3),
                        "median_ms": round(median(timings), 3),
                        "p95_ms": round(sorted(timings)[int(args.frames * 0.95) - 1], 3),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
