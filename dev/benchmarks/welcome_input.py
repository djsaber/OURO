"""Measure editor latency and terminal output under welcome animation load.

Uses Textual's real compositor and ANSI serialization, with output captured by
a driver so the benchmark does not take over a terminal. OS terminal and IME
rendering are outside this measurement.
"""

from __future__ import annotations

import asyncio
import json
import sys
from statistics import median
from threading import Thread
from time import monotonic, sleep
from typing import cast

from textual import events
from textual.app import App
from textual.drivers.headless_driver import HeadlessDriver

from ouro.tui.welcome import WELCOME_STYLES
from ouro.tui.welcome.dot_matrix.widget import DotMatrix, DotWelcomePage
from ouro.tui.widgets import Composer


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


class ArtworkInputApp(App):
    CSS = "DotMatrix {width:114; height:30;} Composer {width:114; height:4;}"

    def __init__(self, *, motion: bool) -> None:
        super().__init__(driver_class=OutputSink)
        page = WELCOME_STYLES["clifford-torus"].create()
        assert isinstance(page, DotWelcomePage)
        self.matrix = DotMatrix(page.artwork, motion=motion)
        self.output_bytes = 0
        self.key_latencies: list[float] = []
        self.edit_times: list[float] = []

    def compose(self):
        yield self.matrix
        yield TimedComposer()

    def on_mount(self) -> None:
        self.matrix.set_playback(active=True, motion=self.matrix.motion)
        self.query_one(Composer).focus()


async def measure(motion: bool) -> dict:
    app = ArtworkInputApp(motion=motion)
    async with app.run_test(headless=False, size=(130, 45)):
        await asyncio.sleep(0.5)
        loop = asyncio.get_running_loop()

        def type_keys() -> None:
            for index in range(120):
                key = events.Key("x", "x") if index % 2 == 0 else events.Key("backspace", None)
                loop.call_soon_threadsafe(app.post_message, key)
                sleep(1 / 30)

        app.output_bytes = 0
        started = monotonic()
        producer = Thread(target=type_keys)
        producer.start()
        while producer.is_alive() or len(app.key_latencies) < 120:
            await asyncio.sleep(0.02)
            if monotonic() - started > 10:
                raise TimeoutError("input events were not drained")
        duration = monotonic() - started
        producer.join()
        assert app.query_one(Composer).text == ""
        return {
            "motion": motion,
            "size": [114, 30],
            "keys": len(app.key_latencies),
            "latency_median_ms": round(median(app.key_latencies), 3),
            "latency_p95_ms": round(sorted(app.key_latencies)[113], 3),
            "edit_median_ms": round(median(app.edit_times), 3),
            "output_KiB_s": round(app.output_bytes / duration / 1024, 1),
        }


async def main() -> None:
    output = sys.stdout
    for motion in (False, True):
        print(json.dumps(await measure(motion)), file=output, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
