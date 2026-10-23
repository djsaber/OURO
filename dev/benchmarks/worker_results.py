"""Measure worker result decoding with synthetic responses and real SDK handlers.

Use the same script with --source for each revision. The fake transport measures
host processing only: decoded mode starts after JSON parsing; json mode includes
parsing. Neither mode measures process startup, IPC, plugin work or terminal paint.
Run sequentially without concurrent tests; timings are not CI thresholds.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sys
import tempfile
import tracemalloc
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from unittest.mock import patch


async def measure(args):
    sys.path.insert(0, str(args.source.resolve()))
    from ouro.plugins.manifest import PluginManifest
    from ouro.plugins.worker.instance import WorkerPluginInstance
    from ouro.sdk.plugins import v5 as sdk

    package = args.source / "examples/plugins/worker-tool"
    manifest = PluginManifest.model_validate_json(
        (package / ".ouro-plugin/plugin.json").read_text(encoding="utf-8")
    )
    calls = {
        "context": sdk.ContextCall("session", "run", 0, 0),
        "command": sdk.CommandCall("command", session_id="session"),
        "tool": sdk.ToolCall("call", "session", "run", "echo", {}),
        "continuation": sdk.RunOutcome("session", "run", "completed", "user"),
        "compaction": sdk.CompactionCall("session", "run", "", (), 100, 20),
    }
    with tempfile.TemporaryDirectory(prefix="ouro-worker-results-") as directory:
        instance = WorkerPluginInstance(
            manifest,
            package_root=package,
            package_digest="synthetic",
            permissions=frozenset(),
            configuration={},
            data_directory=Path(directory),
            credentials={},
            diagnostic=lambda *_: None,
        )
        for count in (1, 64, 512):
            inputs = [{"prompt": f"input {i}", "semantic_tag": "context"} for i in range(count)]
            transitions = [{"action": "acquire", "controller_id": f"mode{i}"} for i in range(count)]
            payloads = {
                "context": {
                    "fragments": [
                        {"text": f"context {i}", "semantic_tag": "context"} for i in range(count)
                    ]
                },
                "command": {
                    "session_inputs": inputs,
                    "session_input_batches": [{"inputs": inputs, "coalesce_key": "batch"}],
                    "session_control_transitions": transitions,
                },
                "tool": {"content": "done", "session_control_transitions": transitions},
                "continuation": {
                    "action": "continue",
                    "prompt": "next",
                    "session_control_transitions": transitions,
                },
                "compaction": {
                    "summary": "summary",
                    "usage": {f"tokens{i}": i for i in range(count)},
                },
            }
            for kind, payload in payloads.items():
                encoded = json.dumps(payload)
                handler = getattr(instance, f"_{kind}_handler")("example")
                call = calls[kind]
                for transport in ("decoded", "json"):

                    async def respond(
                        _method,
                        _params,
                        encoded=encoded,
                        transport=transport,
                        payload=payload,
                        *,
                        timeout=None,
                    ):
                        return json.loads(encoded) if transport == "json" else dict(payload)

                    instance.client.request = respond
                    for _ in range(10):
                        await handler(call)
                    samples = []
                    for _ in range(args.samples):
                        start = perf_counter_ns()
                        for _ in range(5):
                            await handler(call)
                        samples.append((perf_counter_ns() - start) / 5 / 1e6)

                    peaks = []
                    for _ in range(3):
                        tracemalloc.start()
                        await handler(call)
                        peaks.append(tracemalloc.get_traced_memory()[1])
                        tracemalloc.stop()

                    # Counts are measured separately so spies do not distort timings.
                    with ExitStack() as stack:
                        constructors = {
                            name: stack.enter_context(
                                patch.object(sdk, name, wraps=getattr(sdk, name))
                            )
                            for name in (
                                "ContextFragment",
                                "SessionInput",
                                "SessionInputBatch",
                                "SessionControlTransition",
                            )
                        }
                        output = await handler(call)
                    digest = hashlib.sha256(
                        json.dumps(asdict(output), sort_keys=True).encode()
                    ).hexdigest()
                    print(
                        json.dumps(
                            {
                                "label": args.label,
                                "platform": platform.platform(),
                                "python": platform.python_version(),
                                "kind": kind,
                                "items": count,
                                "transport": transport,
                                "wire_bytes": len(encoded.encode()),
                                "median_ms": median(samples),
                                "samples_ms": samples,
                                "peak_bytes": peaks,
                                "constructors": {
                                    name: spy.call_count for name, spy in constructors.items()
                                },
                                "output_digest": digest,
                            }
                        ),
                        flush=True,
                    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--label", required=True)
    parser.add_argument("--samples", type=int, default=60)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
