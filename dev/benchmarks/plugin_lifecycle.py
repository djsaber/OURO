"""Measure real plugin enable/disable cycles and retained generation resources offline."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import tracemalloc
import weakref
from ctypes import wintypes
from pathlib import Path
from time import perf_counter

from ouro.local_application import open_local_plugins

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "plugins"


def process_handles():
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return count.value
    descriptors = Path("/proc/self/fd")
    return len(tuple(descriptors.iterdir())) if descriptors.exists() else None


async def measure(home, example, *, count, memory):
    manifest = json.loads(
        (EXAMPLES / example / ".ouro-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    plugin_id = manifest["plugin_id"]
    async with open_local_plugins(home) as plugins:
        plugins.install(EXAMPLES / example)
        # Imports, worker launch machinery and thread pools are warmed separately.
        assert (await plugins.enable(plugin_id)).status == "active"
        assert (await plugins.disable(plugin_id)).status == "disabled"
        gc.collect()
        before_handles = process_handles()
        modules_before = {name for name in sys.modules if name.startswith("_ouro_plugin_")}
        generations, workers, samples = [], [], []
        if memory:
            tracemalloc.start()
        try:
            for _ in range(count):
                started = perf_counter()
                assert (await plugins.enable(plugin_id)).status == "active"
                generation = plugins.runtime._active[plugin_id]
                generations.append(weakref.ref(generation))
                if manifest["runtime"]["kind"] == "worker":
                    workers.append(weakref.ref(generation.instance.client))
                del generation
                assert (await plugins.disable(plugin_id)).status == "disabled"
                assert not plugins.runtime.has_resources(plugin_id)
                if not memory:
                    samples.append((perf_counter() - started) * 1000)
            gc.collect()
            allocations = tracemalloc.get_traced_memory() if memory else (None, None)
        finally:
            if memory:
                tracemalloc.stop()
        retained_generations = sum(reference() is not None for reference in generations)
        retained_workers = sum(reference() is not None for reference in workers)
        retained_modules = {
            name for name in sys.modules if name.startswith("_ouro_plugin_")
        } - modules_before
        assert not retained_generations and not retained_workers and not retained_modules
        ordered = sorted(samples)
        return {
            "example": example,
            "cycles": count,
            "memory_probe": memory,
            "handles_before": before_handles,
            "handles_after": process_handles(),
            "retained_generations": retained_generations,
            "retained_workers": retained_workers,
            "retained_modules": len(retained_modules),
            "allocated_bytes_after_gc": allocations[0],
            "allocated_bytes_peak": allocations[1],
            "elapsed_ms": samples,
            "p50_ms": statistics.median(samples) if samples else None,
            "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
        }


async def run(args):
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version()}))
    with tempfile.TemporaryDirectory(prefix="ouro-plugin-lifecycle-") as directory:
        for example in ("inprocess-command", "worker-tool"):
            for memory in (False, True):
                report = await measure(
                    Path(directory) / f"{example}-{memory}",
                    example,
                    count=args.cycles,
                    memory=memory,
                )
                print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=100)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("cycles must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
