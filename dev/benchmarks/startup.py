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
from pathlib import Path
from time import perf_counter

started = perf_counter()
import ouro.cli.main
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.local_application import LocalApplication
imported = perf_counter()
phases = {}
build_application = LocalApplication.build
start_application = LocalApplication.start

def build(*args, **kwargs):
    began = perf_counter()
    try:
        return build_application(*args, **kwargs)
    finally:
        phases["application_build_ms"] = (perf_counter() - began) * 1000

async def start(self):
    began = perf_counter()
    try:
        return await start_application(self)
    finally:
        phases["application_start_ms"] = (perf_counter() - began) * 1000

LocalApplication.build = staticmethod(build)
LocalApplication.start = start

async def sample():
    with tempfile.TemporaryDirectory(prefix="ouro-startup-") as home:
        began = perf_counter()
        app = OuroTuiApp(home=home, session_backend="files", task_backend="files")
        constructed = perf_counter()
        phases["app_construct_ms"] = (constructed - began) * 1000
        bootstrap = app._controller.start

        async def measured_bootstrap():
            began = perf_counter()
            try:
                return await bootstrap()
            finally:
                phases["bootstrap_ms"] = (perf_counter() - began) * 1000

        app._controller.start = measured_bootstrap
        async with app.run_test(size=(120, 40)):
            await app.workers.wait_for_complete(
                [worker for worker in app.workers if worker.name == "tui-start"]
            )
            assert not app.query_one(Composer).disabled
            assert app.focused is app.query_one(Composer)
            interactive = perf_counter()
            await app.screen.wait_for_refresh()
            ready = perf_counter()
    return {
        "source_root": str(Path(ouro.__file__).resolve().parent.parent),
        **phases,
        "import_ms": (imported - started) * 1000,
        "mount_to_interactive_ms": (interactive - constructed) * 1000,
        "interactive_ms": (interactive - started) * 1000,
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
        sample = json.loads(result.stdout)
        if Path(sample["source_root"]).resolve() != args.root.resolve():
            raise RuntimeError(f"startup imported a different checkout: {sample['source_root']}")
        print(
            json.dumps(
                {
                    "sample": index + 1,
                    **sample,
                    "process_and_shutdown_ms": (perf_counter() - started) * 1000,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
