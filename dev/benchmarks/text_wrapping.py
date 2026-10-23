"""Measure Unicode wrapping, synchronous editing, and streaming flush costs.

Run separately from pytest. Timings exclude terminal paint and IME processing;
allocation peaks are measured separately so tracing does not distort timings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import tracemalloc
from collections.abc import Callable
from functools import partial
from statistics import median
from time import perf_counter

from rich.console import Console
from textual.app import App, ComposeResult

from ouro.tui.conversation.history_rendering import AssistantTailLines
from ouro.tui.text_inputs import TuiTextArea
from ouro.tui.text_wrapping import ProseText, wrap_offsets


class WrappingApp(App[None]):
    CSS = """
    Screen { overflow: hidden; }
    TuiTextArea {
        width: 100%; height: 8; border: none; padding: 0; scrollbar-size-vertical: 0;
    }
    AssistantTailLines { height: 8; }
    """

    def compose(self) -> ComposeResult:
        yield TuiTextArea(soft_wrap=True)
        yield AssistantTailLines()


def samples(operation: Callable[[], object], repeat: int) -> list[float]:
    result = []
    for _ in range(repeat):
        started = perf_counter()
        operation()
        result.append((perf_counter() - started) * 1000)
    return result


def report(scenario: str, characters: int, timings: list[float], **details: object) -> None:
    print(
        json.dumps(
            {
                "scenario": scenario,
                "characters": characters,
                "samples_ms": [round(value, 3) for value in timings],
                "median_ms": round(median(timings), 3),
                "p95_ms": round(sorted(timings)[max(0, (len(timings) * 95 + 99) // 100 - 1)], 3),
                **details,
            }
        ),
        flush=True,
    )


async def main(repeat: int) -> None:
    print(json.dumps({"python": sys.version, "platform": platform.platform(), "width": 80}))
    console = Console(width=80)
    app = WrappingApp()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        editor = app.query_one(TuiTextArea)
        tail = app.query_one(AssistantTailLines)
        for alphabet, unit in (("ascii", "plain words "), ("mixed", "中文混排 scope ")):
            for size in (1024, 4096, 16384):
                source = (unit * (size // len(unit) + 1))[:size]
                text = ProseText(source)
                report(
                    f"{alphabet}_rich_wrap", size, samples(partial(text.wrap, console, 80), repeat)
                )
                if alphabet == "mixed":
                    timings = samples(partial(wrap_offsets, source, 80), repeat)
                    tracemalloc.start()
                    wrap_offsets(source, 80)
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    report("mixed_offsets", size, timings, peak_bytes=peak)
                editor.load_text(source)
                editor.move_cursor((0, len(source)))
                await pilot.pause()
                report(
                    f"{alphabet}_editor_insert",
                    size,
                    samples(partial(editor.insert, unit[0]), repeat),
                )
                tail.replace("")
                await pilot.pause()
                timings = []
                chunk_size = 32
                for offset in range(0, len(source), chunk_size):
                    fragment = source[offset : offset + chunk_size]
                    timings.extend(samples(partial(tail.append, fragment), 1))
                assert tail.text == source
                report(f"{alphabet}_stream_append", size, timings, chunk_size=chunk_size)
                await pilot.pause()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=15)
    arguments = parser.parse_args()
    if arguments.repeat <= 0:
        parser.error("--repeat must be positive")
    asyncio.run(main(arguments.repeat))
