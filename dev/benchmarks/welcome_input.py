"""Measure full welcome pages with real composition/ANSI output and normal GC.

The sink excludes OS terminal painting and IME. Run long samples serially in
fresh processes. Timed runs repeat bounded typing/deletion bursts. No GC settings
are changed; thirty-second windows expose recurring or growing latency.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import platform
import sys
from collections import Counter
from importlib.metadata import version
from itertools import pairwise
from math import ceil
from pathlib import Path
from statistics import median
from threading import Event, Thread, current_thread
from time import monotonic

from ouro_tui_themes.packs import PACKAGES
from rich.color import ColorSystem
from textual import events
from textual.app import App
from textual.cache import FIFOCache
from textual.drivers.headless_driver import HeadlessDriver
from textual.screen import Screen

from ouro.tui.composer.input import Composer
from ouro.tui.rendering import TuiCompositor
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.welcome import welcome_catalog
from ouro.tui.welcome.host import WelcomeHost

WELCOME_STYLES = welcome_catalog(PACKAGES)


def distribution(values) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "median": round(median(values), 3),
        "p95": round(ordered[max(0, int(len(values) * 0.95) - 1)], 3),
        "p99": round(ordered[max(0, int(len(values) * 0.99) - 1)], 3),
        "max": round(ordered[-1], 3),
    }


class OutputSink(HeadlessDriver):
    @property
    def is_headless(self) -> bool:
        return False

    def write(self, data: str) -> None:
        self._app.output_bytes += len(data.encode("utf-8"))


class TimedComposer(Composer):
    async def _on_key(self, event: events.Key) -> None:
        started = monotonic()
        await super()._on_key(event)
        now = monotonic()
        app = self.app
        app.key_times.append(now)
        app.key_latencies.append((now - event.time) * 1000)
        # GC can prevent the producer itself from acquiring the GIL. Include
        # that delay against intended arrival time, not just event creation.
        app.scheduled_latencies.append((now - event.scheduled_time) * 1000)
        app.edit_times.append((now - started) * 1000)


class ArtworkInputApp(App):
    CSS = "TimedComposer {width:100%; height:4;} Screen {background:#0c0c0c;}"

    def __init__(self, style: str) -> None:
        super().__init__(driver_class=OutputSink)
        self.console._color_system = ColorSystem.TRUECOLOR
        self.style_id = style
        self.output_bytes = 0
        self.frame_revision = 0
        self.displayed_revision = 0
        self.frames: list[float] = []
        self.key_times: list[float] = []
        self.key_latencies: list[float] = []
        self.scheduled_latencies: list[float] = []
        self.edit_times: list[float] = []

    def get_css_variables(self):
        return {**super().get_css_variables(), **DEFAULT_TUI_THEME.css_variables()}

    def get_default_screen(self):
        screen = Screen(id="_default")
        screen._compositor = TuiCompositor()
        return screen

    def compose(self):
        self.host = WelcomeHost(TimedComposer(), WELCOME_STYLES[self.style_id])
        page = self.host.page
        if self.style_id == "matrix":
            page._seed = 42
        animation = getattr(page, "matrix", page)
        refresh = animation.refresh

        def frame_refresh(*regions, **kwargs):
            self.frame_revision += 1
            return refresh(*regions, **kwargs)

        animation.refresh = frame_refresh
        self.host.set_welcome_mode(True)
        yield self.host

    def on_mount(self) -> None:
        self.query_one(Composer).focus()

    def _display(self, screen, renderable):
        super()._display(screen, renderable)
        if renderable is not None and self.displayed_revision != self.frame_revision:
            self.displayed_revision = self.frame_revision
            self.frames.append(monotonic())


async def measure(args, motion: bool) -> dict:
    app = ArtworkInputApp(args.style)
    loop_times, loop_lags = [], []
    gc_times, gc_durations, gc_generations = [], [], []
    slow_collections = []
    collection_start = 0.0
    cache_allocations = 0
    initialize_cache = FIFOCache.__init__
    stop = Event()
    output = sys.stdout

    def counted_cache(self, *args, **kwargs):
        nonlocal cache_allocations
        cache_allocations += 1
        initialize_cache(self, *args, **kwargs)

    def collected(phase, info):
        nonlocal collection_start
        if phase == "start":
            collection_start = monotonic()
        else:
            duration = (monotonic() - collection_start) * 1000
            gc_times.append(collection_start)
            gc_durations.append(duration)
            gc_generations.append(info["generation"])
            if duration > 16:
                slow_collections.append(
                    {
                        **info,
                        "ms": round(duration, 3),
                        "at_s": collection_start - started,
                        "thread": current_thread().name,
                    }
                )

    async def observe_loop():
        while True:
            before = monotonic()
            await asyncio.sleep(0.005)
            loop_times.append(before)
            loop_lags.append(max(0, monotonic() - before - 0.005) * 1000)

    async with app.run_test(headless=False, size=tuple(args.size)):
        await asyncio.sleep(args.warmup)
        app.host.page.set_active(motion)
        loop = asyncio.get_running_loop()
        keys = args.keys if args.seconds is None else ceil(args.seconds * 30 / 240) * 240
        sent = 0
        started = monotonic()

        def type_keys() -> None:
            nonlocal sent
            for index in range(keys):
                deadline = started + index / 30
                if stop.wait(max(0, deadline - monotonic())):
                    return
                position = index % 240 if args.seconds is not None else index
                half = 120 if args.seconds is not None else keys // 2
                insert = index % 2 == 0 if args.pattern == "alternate" else position < half
                key = events.Key("x", "x") if insert else events.Key("backspace", None)
                key.scheduled_time = deadline
                loop.call_soon_threadsafe(app.post_message, key)
                sent += 1

        app.output_bytes = 0
        app.frames.clear()
        observer = asyncio.create_task(observe_loop())
        producer = Thread(target=type_keys, name="benchmark-keys")
        thresholds = gc.get_threshold()
        if args.count_caches:
            FIFOCache.__init__ = counted_cache
        gc.callbacks.append(collected)
        try:
            producer.start()
            next_progress = started + 30
            while producer.is_alive() or len(app.key_latencies) < keys:
                await asyncio.sleep(0.02)
                now = monotonic()
                if now > next_progress:
                    print(
                        json.dumps(
                            {
                                "progress_s": round(now - started),
                                "keys": sent,
                                "style": args.style,
                                "gc_counts": dict(Counter(gc_generations)),
                                "slow_gc": len(slow_collections),
                            }
                        ),
                        file=output,
                        flush=True,
                    )
                    next_progress += 30
                if now - started > keys / 30 + 20:
                    raise TimeoutError("input events were not drained")
        finally:
            stop.set()
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            gc.callbacks.remove(collected)
            FIFOCache.__init__ = initialize_cache
            await asyncio.to_thread(producer.join)
        duration = monotonic() - started
        assert gc.isenabled() and gc.get_threshold() == thresholds
        assert app.query_one(Composer).text == ""
        intervals = [(right - left) * 1000 for left, right in pairwise(app.frames)]
        windows = []
        for start in range(0, int(duration) + 1, 30):

            def window(times, values, start=start):
                return [
                    value
                    for when, value in zip(times, values, strict=True)
                    if start <= when - started < start + 30
                ]

            windows.append(
                {
                    "start_s": start,
                    "key_ms": distribution(window(app.key_times, app.scheduled_latencies)),
                    "loop_ms": distribution(window(loop_times, loop_lags)),
                    "frame_ms": distribution(window(app.frames[1:], intervals)),
                    "gc_ms": distribution(window(gc_times, gc_durations)),
                    "gc_counts": dict(Counter(window(gc_times, gc_generations))),
                }
            )
        return {
            "style": args.style,
            "motion": motion,
            "pattern": args.pattern,
            "size": args.size,
            "seconds": round(duration, 2),
            "warmup_s": args.warmup,
            "keys": len(app.key_latencies),
            "cache_allocations": cache_allocations if args.count_caches else None,
            "gc_thresholds": thresholds,
            "latency_ms": distribution(app.key_latencies),
            "scheduled_latency_ms": distribution(app.scheduled_latencies),
            "edit_ms": distribution(app.edit_times),
            "loop_lag_ms": distribution(loop_lags),
            "frame_interval_ms": distribution(intervals),
            "frames": len(app.frames),
            "gc_ms": distribution(gc_durations),
            "gc_counts": dict(Counter(gc_generations)),
            "slow_gc_collections": slow_collections,
            "windows": windows,
            "output_KiB_s": round(app.output_bytes / duration / 1024, 1),
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--style", choices=tuple(WELCOME_STYLES), default="quantum-veil")
    parser.add_argument("--keys", type=int, default=240)
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--size", type=int, nargs=2, default=[160, 50])
    parser.add_argument("--motion", choices=("both", "active", "paused"), default="both")
    parser.add_argument("--pattern", choices=("alternate", "grow-delete"), default="grow-delete")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--count-caches",
        action="store_true",
        help="Count cache constructions; use separately from latency samples",
    )
    args = parser.parse_args()
    if args.keys < 20 or args.keys % 2 or (args.seconds is not None and args.seconds < 8):
        parser.error("keys must be even and >=20; seconds must be >=8")
    if min(args.size) < 1 or args.warmup < 0:
        parser.error("size must be positive; warmup must be nonnegative")
    if not gc.isenabled():
        parser.error("benchmark requires normal automatic GC")
    os.environ.pop("NO_COLOR", None)
    output = sys.stdout
    metadata = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "textual": version("textual"),
        "rich": version("rich"),
        "themes_source": str(Path(sys.modules["ouro_tui_themes"].__file__).resolve()),
    }
    print(json.dumps(metadata), file=output, flush=True)
    results = []
    for motion in (False, True) if args.motion == "both" else (args.motion == "active",):
        result = await measure(args, motion)
        results.append(result)
        print(json.dumps(result), file=output, flush=True)
    if args.output:
        args.output.write_text(
            json.dumps({**metadata, "results": results}, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    asyncio.run(main())
