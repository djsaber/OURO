"""Measure directory reads and popup paint latency without taking over the terminal."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from pathlib import Path
from statistics import median
from time import perf_counter

from textual.app import App

from ouro.tui.commands import TuiCommand
from ouro.tui.composer.attachment_picker import AttachmentPicker
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.theme import DEFAULT_TUI_THEME


class MenuApp(App):
    CSS = """
    CommandPopup {height: auto; max-height: 14; padding: 1 2; overflow-y: hidden;}
    #command-popup-heading {dock: top; height: 1;}
    #command-popup-content {width: 100%; height: auto;}
    """

    def compose(self):
        yield CommandPopup()


async def measure(directory: Path, keys: int) -> dict:
    picker = AttachmentPicker(directory)
    started = perf_counter()
    picker.entries = await asyncio.to_thread(picker.read_directory, picker.directory)
    read_ms = (perf_counter() - started) * 1000
    started = perf_counter()
    arguments = picker.arguments("", DEFAULT_TUI_THEME)
    arguments_ms = (perf_counter() - started) * 1000
    app = MenuApp()
    async with app.run_test(size=(100, 32)):
        popup = app.query_one(CommandPopup)

        async def painted() -> None:
            done = asyncio.Event()
            app.call_after_refresh(done.set)
            await done.wait()

        started = perf_counter()
        popup.show_arguments(TuiCommand("@file", "Files", ""), arguments, heading=str(directory))
        await painted()
        open_ms = (perf_counter() - started) * 1000
        latencies = []
        for _ in range(keys):
            started = perf_counter()
            popup.navigate(1)
            await painted()
            latencies.append((perf_counter() - started) * 1000)
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "size": [100, 32],
        "directory": str(directory),
        "entries": len(picker.entries),
        "keys": keys,
        "read_ms": round(read_ms, 2),
        "arguments_ms": round(arguments_ms, 2),
        "open_to_paint_ms": round(open_ms, 2),
        "navigate_to_paint_median_ms": round(median(latencies), 2),
        "navigate_to_paint_p95_ms": round(sorted(latencies)[int((len(latencies) - 1) * 0.95)], 2),
        "navigate_to_paint_samples_ms": [round(value, 2) for value in latencies],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--keys", type=int, default=60)
    args = parser.parse_args()
    if args.keys < 1:
        parser.error("--keys must be positive")
    print(json.dumps(asyncio.run(measure(args.directory, args.keys)), ensure_ascii=False))


if __name__ == "__main__":
    main()
