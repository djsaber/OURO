"""Measure editor latency and terminal output under welcome animation load.

Uses Textual's real compositor and ANSI serialization, with output captured by
a driver so the benchmark does not take over a terminal. OS terminal and IME
rendering are outside this measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
import sys
from itertools import pairwise
from statistics import median
from threading import Thread
from time import monotonic, sleep
from typing import cast

from ouro_tui_themes.dot_matrix.widget import DotMatrix
from ouro_tui_themes.styles import STYLES
from textual import events
from textual.app import App
from textual.drivers.headless_driver import HeadlessDriver

from ouro.tui.composer.input import Composer
from ouro.tui.welcome import welcome_catalog

WELCOME_STYLES = welcome_catalog(STYLES)


class OutputSink(HeadlessDriver):
    @property
    def is_headless(self) -> bool:
        return False

    def write(self, data: str) -> None:
        cast(ArtworkInputApp, self._app).output_bytes += len(data.encode("utf-8"))


class TimedComposer(Composer):
    async def _on_key(self, event: events.Key) -> None:
        started = monotonic()
        await super()._on_key(event)
        app = cast(ArtworkInputApp, self.app)
        app.key_latencies.append((monotonic() - event.time) * 1000)
        app.edit_times.append((monotonic() - started) * 1000)


class TimedMatrix(DotMatrix):
    def refresh(self, *regions, **kwargs):
        if regions and self.is_mounted:
            cast(ArtworkInputApp, self.app).frames.append(monotonic())
        return super().refresh(*regions, **kwargs)


class ArtworkInputApp(App):
    CSS = "DotMatrix {width:114; height:30;} Composer {width:114; height:4;}"

    def __init__(self, *, motion: bool, style: str) -> None:
        super().__init__(driver_class=OutputSink)
        page = WELCOME_STYLES[style].create()
        self.matrix = TimedMatrix(page.artwork)
        self.measure_motion = motion
        self.output_bytes = 0
        self.key_latencies: list[float] = []
        self.edit_times: list[float] = []
        self.frames: list[float] = []

    def compose(self):
        yield self.matrix
        yield TimedComposer()

    def on_mount(self) -> None:
        self.matrix.set_active(self.measure_motion)
        self.query_one(Composer).focus()


def distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "median": round(median(values), 3),
        "p95": round(ordered[max(0, int(len(values) * 0.95) - 1)], 3),
        "p99": round(ordered[max(0, int(len(values) * 0.99) - 1)], 3),
        "max": round(ordered[-1], 3),
    }


async def measure(motion: bool, *, style: str, keys: int, pattern: str) -> dict:
    app = ArtworkInputApp(motion=motion, style=style)
    loop_lags: list[float] = []
    collections: list[float] = []
    slow_collections: list[dict] = []
    collection_start = 0.0

    def collected(phase, info):
        nonlocal collection_start
        if phase == "start":
            collection_start = monotonic()
        else:
            duration = (monotonic() - collection_start) * 1000
            collections.append(duration)
            if duration > 16:
                slow_collections.append({**info, "ms": round(duration, 3)})

    async def observe_loop():
        while True:
            started = monotonic()
            await asyncio.sleep(0.005)
            loop_lags.append(max(0, monotonic() - started - 0.005) * 1000)

    async with app.run_test(headless=False, size=(130, 45)):
        await asyncio.sleep(0.5)
        loop = asyncio.get_running_loop()

        def type_keys() -> None:
            for index in range(keys):
                insert = index % 2 == 0 if pattern == "alternate" else index < keys // 2
                key = events.Key("x", "x") if insert else events.Key("backspace", None)
                loop.call_soon_threadsafe(app.post_message, key)
                sleep(1 / 30)

        app.output_bytes = 0
        app.frames.clear()
        started = monotonic()
        producer = Thread(target=type_keys)
        observer = asyncio.create_task(observe_loop())
        gc.callbacks.append(collected)
        try:
            producer.start()
            while producer.is_alive() or len(app.key_latencies) < keys:
                await asyncio.sleep(0.02)
                if monotonic() - started > keys / 30 + 10:
                    raise TimeoutError("input events were not drained")
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            gc.callbacks.remove(collected)
            await asyncio.to_thread(producer.join)
        duration = monotonic() - started
        assert app.query_one(Composer).text == ""
        return {
            "style": style,
            "motion": motion,
            "pattern": pattern,
            "size": [114, 30],
            "keys": len(app.key_latencies),
            "latency_ms": distribution(app.key_latencies),
            "edit_ms": distribution(app.edit_times),
            "loop_lag_ms": distribution(loop_lags),
            "publish_interval_ms": distribution(
                [(right - left) * 1000 for left, right in pairwise(app.frames)]
            ),
            "gc_ms": distribution(collections),
            "slow_gc_collections": slow_collections,
            "output_KiB_s": round(app.output_bytes / duration / 1024, 1),
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--style", choices=tuple(WELCOME_STYLES)[1:], default="clifford-torus")
    parser.add_argument("--keys", type=int, default=240)
    parser.add_argument("--pattern", choices=("alternate", "grow-delete"), default="grow-delete")
    args = parser.parse_args()
    if args.keys < 20 or args.keys % 2:
        parser.error("--keys must be an even number of at least 20")
    output = sys.stdout
    print(
        json.dumps({"platform": platform.platform(), "python": platform.python_version()}),
        file=output,
    )
    for motion in (False, True):
        result = await measure(motion, style=args.style, keys=args.keys, pattern=args.pattern)
        print(json.dumps(result), file=output, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
