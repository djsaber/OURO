"""Measure fresh-process imports and a ready headless TUI without user data or network."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
from time import perf_counter

_SAMPLE = """
import asyncio
import json
import tempfile
from time import perf_counter

started = perf_counter()
import ouro.cli.main
from ouro.tui.app import OuroTuiApp
from ouro.tui.widgets import Composer
imported = perf_counter()

async def sample():
    with tempfile.TemporaryDirectory(prefix="ouro-startup-") as home:
        app = OuroTuiApp(home=home, session_backend="files", task_backend="files")
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete(
                [worker for worker in app.workers if worker.name == "tui-start"]
            )
            await pilot.pause()
            assert not app.query_one(Composer).disabled
            ready = perf_counter()
    return {
        "import_ms": (imported - started) * 1000,
        "headless_ready_ms": (ready - started) * 1000,
    }

print(json.dumps(asyncio.run(sample())))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Checkout to measure")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version()}))
    for index in range(args.repeat):
        started = perf_counter()
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", _SAMPLE],
            cwd=args.root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "sample": index + 1,
                    **json.loads(result.stdout),
                    "process_and_shutdown_ms": (perf_counter() - started) * 1000,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
