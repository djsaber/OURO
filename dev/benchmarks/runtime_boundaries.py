"""Offline cost of cancellation reads, decision tool views and tool estimates."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    EffectiveToolRef,
)
from ouro.core.context import ContextBuilder
from ouro.core.provider import ModelMetadata
from ouro.core.state import RunRecord
from ouro.core.tools import ToolDefinition
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import ToolRegistry


def catalog(agents: int, tools: int) -> AgentCatalogSnapshot:
    references = tuple(
        EffectiveToolRef(
            public_name=f"Tool{index}",
            canonical_tool_id=f"ouro.host/tools/Tool{index}",
        )
        for index in range(tools)
    )
    return AgentCatalogSnapshot(
        entries=tuple(
            AgentCatalogEntry(
                definition=AgentDefinition(
                    agent_id=f"agent{index}", name=f"Agent {index}", description="Probe"
                ),
                effective_tools=references,
            )
            for index in range(agents)
        )
    )


def measure(call, repeat: int) -> dict[str, object]:
    samples = []
    for _ in range(repeat):
        started = perf_counter()
        call()
        samples.append((perf_counter() - started) * 1000)
    return {"samples_ms": samples, "median_ms": statistics.median(samples)}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=30)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version()}))
    with tempfile.TemporaryDirectory(prefix="ouro-runtime-boundaries-") as directory:
        for backend in ("files", "sqlite"):
            root = Path(directory) / backend
            store = (
                LocalSessionStore(root)
                if backend == "files"
                else SQLiteSessionStore(root / "sessions.db")
            )
            store.initialize()
            runtime = RuntimeService(
                store=store,
                tools=ToolRegistry(),
                adapter=RuntimeAdapter(
                    FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")
                ),
            )
            runtime.create_session(_session_id="synthetic")
            async with LocalApplication.from_runtime(runtime, home=root):
                for agent_count, tool_count in ((1, 20), (10, 100), (20, 200)):
                    run = RunRecord(
                        session_id="synthetic",
                        status="running",
                        agent_catalog=catalog(agent_count, tool_count),
                    )
                    runtime.store.save_run(run)
                    lock = await runtime.run_ownership.acquire(run.session_id, run.run_id)
                    assert lock is not None
                    try:

                        def poll(runtime=runtime, run=run):
                            assert (
                                runtime.run_ownership.abort_state(run.session_id, run.run_id)
                                is None
                            )

                        print(
                            json.dumps(
                                {
                                    "scenario": "abort_read",
                                    "backend": backend,
                                    "agents": agent_count,
                                    "tools_per_agent": tool_count,
                                    "payload_bytes": len(run.model_dump_json().encode()),
                                    **measure(poll, args.repeat),
                                }
                            ),
                            flush=True,
                        )
                    finally:
                        await runtime.run_ownership.release(run.run_id, lock)
                run = RunRecord(session_id="synthetic", agent_catalog=catalog(1, 20))
                await runtime._ensure_extension_snapshot(run)

                def tool_view(runtime=runtime, run=run):
                    assert len(runtime._tools_for_run(run.run_id).definitions()) >= 3

                tool_view()
                print(
                    json.dumps(
                        {
                            "scenario": "bound_tool_view",
                            "backend": backend,
                            **measure(tool_view, args.repeat),
                        }
                    ),
                    flush=True,
                )
        builder = ContextBuilder()
        for metadata_bytes in (0, 32768):
            tool = ToolDefinition(
                name="Echo",
                description="Echo input",
                parameters_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                metadata={"audit_detail": "x" * metadata_bytes},
            )

            def context(tool=tool):
                return builder.build(messages=[], tools=[tool])

            print(
                json.dumps(
                    {
                        "scenario": "tool_context",
                        "metadata_bytes": metadata_bytes,
                        "estimated_tokens": context().metrics.estimated_input_tokens,
                        **measure(context, args.repeat),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
