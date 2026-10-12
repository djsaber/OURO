"""Compare one startup graph with the former repeated activate(root) algorithm."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from tests.plugins.test_startup_graph import graph_runtime


async def measure(root: Path, dependencies, *, legacy: bool):
    root.mkdir()
    runtime, events = graph_runtime(root, dependencies)
    try:
        with patch.object(runtime.store, "read_state", wraps=runtime.store.read_state) as reads:
            started = perf_counter()
            if legacy:
                # Healthy, initially empty catalog: the exact former start() loop.
                runtime._ensure_catalog()
                stamp = runtime.store.state_stamp()
                runtime.store.recover_state()
                runtime._started = True
                for key in sorted(dependencies):
                    assert await runtime.activate(f"test.graph.{key}") == "active"
                runtime._observed_state_stamp = stamp
            else:
                await runtime.start()
            elapsed = (perf_counter() - started) * 1000
            counts = reads.call_count
        assert len(runtime.catalog().generations) == len(dependencies)
        return {
            "elapsed_ms": elapsed,
            "state_reads": counts,
            "builds": dict(Counter(key for action, key in events if action == "create")),
            "service_starts": sum(action == "service_start" for action, _ in events),
        }
    finally:
        await runtime.close()


async def run(args):
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version()}))
    with tempfile.TemporaryDirectory(prefix="ouro-startup-graph-") as directory:
        sample_id = 0
        for count in args.counts:
            keys = [f"p{index:03}" for index in range(count)]
            for shape in ("chain", "shared", "independent"):
                dependencies = {
                    key: (
                        [keys[index - 1]]
                        if shape == "chain" and index
                        else [keys[0]]
                        if shape == "shared" and index
                        else []
                    )
                    for index, key in enumerate(keys)
                }
                for legacy in (True, False):
                    samples = []
                    for _ in range(args.repeat):
                        sample_id += 1
                        samples.append(
                            await measure(
                                Path(directory) / str(sample_id), dependencies, legacy=legacy
                            )
                        )
                    print(
                        json.dumps(
                            {
                                "shape": shape,
                                "count": count,
                                "legacy_loop": legacy,
                                "p50_ms": statistics.median(item["elapsed_ms"] for item in samples),
                                "samples": samples,
                            }
                        ),
                        flush=True,
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[3, 10, 30])
    parser.add_argument("--repeat", type=int, default=3)
    asyncio.run(run(parser.parse_args()))
