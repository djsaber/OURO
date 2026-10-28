"""Real Run/summary/observer growth with isolated data and no external network.

uv run --frozen python -m dev.benchmarks.long_run_growth --rounds 8 64 --repeat 3
RSS is sampled at Provider boundaries; this is not a process peak or cold-cache benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import json
import os
import platform
import sqlite3
import tempfile
from contextlib import closing, nullcontext
from pathlib import Path
from threading import Event
from time import perf_counter

from ouro.application.observations.content import SessionContentProjection
from ouro.application.runtime.service import RuntimeService
from ouro.config.compaction import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionArtifact, CompactionPolicy, CompactionStrategyRegistry
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


def rss_bytes():
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("page_faults", wintypes.DWORD),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "peak_working_set",
                        "working_set",
                        "quota_peak_paged",
                        "quota_paged",
                        "quota_peak_nonpaged",
                        "quota_nonpaged",
                        "pagefile",
                        "peak_pagefile",
                    )
                ],
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError()
        return counters.working_set
    if Path("/proc/self/statm").exists():
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return None


class Summary:
    strategy_id = "benchmark.summary"
    version = "1"

    def __init__(self):
        self.count = 0

    async def compact(self, request, *, signal=None):
        self.count += 1
        return CompactionArtifact(summary="Prior evidence retained; continue the remaining work.")


class Provider:
    provider_id = "benchmark"

    def __init__(self, rounds, chunks, sample, abort_at):
        self.rounds, self.chunks, self.sample = rounds, chunks, sample
        self.count = 0
        self.abort_at = abort_at
        self.paused = asyncio.Event()

    async def stream(self, *, messages, **kwargs):
        self.count += 1
        self.sample(self.count, messages)
        if self.count == self.abort_at:
            self.paused.set()
            await asyncio.Future()
        # Constant output size, independent of stream slicing.
        for index in range(self.chunks):
            start, end = index * 4096 // self.chunks, (index + 1) * 4096 // self.chunks
            yield ProviderStreamEvent(type="text_delta", text="x" * (end - start))
        if self.count <= self.rounds:
            yield ProviderStreamEvent.from_tool_call(
                {"id": f"evidence_{self.count}", "name": "Evidence", "arguments": {}}
            )
        yield ProviderStreamEvent(type="done")


def storage(root, database, backend):
    files = [path for path in root.rglob("*") if path.is_file()]
    wal = database.with_name(database.name + "-wal")
    result = {
        "files": len(files),
        "file_bytes": sum(path.stat().st_size for path in files),
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
    }
    if backend == "sqlite":
        with closing(sqlite3.connect(database)) as connection:
            for name in ("page_count", "page_size", "freelist_count"):
                result[name] = connection.execute(f"PRAGMA {name}").fetchone()[0]
    return result


async def sample(root, backend, rounds, chunks, slow_reader, abort_at):
    database = root / "sessions.db"
    store = LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(database)
    store.initialize()
    # An unrelated, intentionally retained read is the only slow-query injection.
    other = SessionRecord()
    store.create_session_snapshot(
        other, [MessageRecord.text(session_id=other.session_id, role="user", text="unrelated")]
    )
    read_entered, read_release = Event(), Event()

    def hold_read():
        with store.session_read(other.session_id):
            store.load_session_record(other.session_id)
            read_entered.set()
            if not read_release.wait(180):
                raise TimeoutError("benchmark read release was not signalled")

    reader = asyncio.create_task(asyncio.to_thread(hold_read)) if slow_reader else None
    if reader is not None:
        assert await asyncio.to_thread(read_entered.wait, 10)
    summary = Summary()
    strategies = CompactionStrategyRegistry()
    strategies.register(summary)
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Evidence", description="Read evidence", effect="read_only"
            ),
            handler=lambda invocation, context: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="y" * 4096,
            ),
        )
    )
    samples = []
    projection = None
    runtime = None

    def measure_round(index, messages):
        samples.append(
            {
                "round": index,
                "rss_bytes": rss_bytes(),
                "active_messages": len(messages),
                "active_text_chars": sum(len(message.text_content) for message in messages),
                "projection_chars": len(projection.assistant_text),
                "slow_queue": max(
                    (sink.queue.qsize() for sink in runtime.event_recorder._sink_observers),
                    default=0,
                ),
            }
        )

    provider = Provider(rounds, chunks, measure_round, abort_at)
    runtime = RuntimeService(
        store=store,
        tools=tools,
        system_prompt="",
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="growth",
                provider_id=provider.provider_id,
                context_window=5000,
                max_output_tokens=1000,
            ),
        ),
        compaction_strategies=strategies,
        compaction_config=CompactionConfig(
            strategy_id=summary.strategy_id,
            policy=CompactionPolicy(trigger_ratio=0.7, target_ratio=0.35),
        ),
    )
    session = runtime.create_session(title="isolated long Run")
    projection = SessionContentProjection(session.session_id)
    fast_sink = runtime.event_recorder.add_event_sink(projection.apply)
    sink_entered, sink_release = asyncio.Event(), asyncio.Event()

    async def slow_sink(event):
        sink_entered.set()
        await sink_release.wait()

    slow_sink_registration = runtime.event_recorder.add_event_sink(slow_sink)
    keeper = closing(sqlite3.connect(database)) if backend == "sqlite" else nullcontext(None)
    try:
        with keeper as connection:
            gc.collect()
            before_rss = rss_bytes()
            start = perf_counter()
            execution = asyncio.create_task(
                runtime.run("Gather evidence", session_id=session.session_id)
            )
            if abort_at:
                await asyncio.wait_for(provider.paused.wait(), 30)
                await runtime.abort_run(session_id=session.session_id)
            result = await asyncio.wait_for(execution, 180)
            elapsed = perf_counter() - start
            assert result.run.status == ("aborted" if abort_at else "completed"), (
                result.run.status,
                result.run.error,
                result.run.metadata,
                samples,
            )
            assert provider.count == (abort_at or rounds + 1)
            assert sink_entered.is_set()
            slow_sink_release_start = perf_counter()
            sink_release.set()
            await slow_sink_registration.close()
            sink_close_seconds = perf_counter() - slow_sink_release_start
            await runtime.drain_events()
            active_storage = storage(root, database, backend)
            read_release.set()
            if reader is not None:
                await reader
            checkpoint = (
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if connection
                else None
            )
            after_checkpoint = storage(root, database, backend)
            await fast_sink.close()
            await runtime.close()
            gc.collect()
            return {
                "backend": backend,
                "rounds": rounds,
                "chunks": chunks,
                "slow_reader": slow_reader,
                "abort_at": abort_at,
                "elapsed_seconds": elapsed,
                "rss_before": before_rss,
                "rss_after_close": rss_bytes(),
                "summaries": summary.count,
                "round_samples": samples,
                "active_storage": active_storage,
                "after_checkpoint": after_checkpoint,
                "checkpoint": checkpoint,
                "sink_close_seconds": sink_close_seconds,
                "remaining_sinks": len(runtime.event_recorder._sink_observers),
                "current_projection_chars": len(projection.assistant_text),
                "run_header_bytes": len(result.run.model_dump_json().encode()),
            }
    finally:
        read_release.set()
        sink_release.set()
        if reader is not None:
            await reader
        await runtime.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, nargs="+", default=[8, 64])
    parser.add_argument("--chunks", type=int, default=8)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--slow-reader", action="store_true")
    parser.add_argument("--abort-at", type=int)
    args = parser.parse_args()
    if args.repeat < 1 or args.chunks < 1 or any(rounds < 1 for rounds in args.rounds):
        parser.error("repeat, chunks, and rounds must be positive")
    if args.abort_at is not None and not 1 <= args.abort_at <= min(args.rounds) + 1:
        parser.error("abort-at must fall within every selected Run")
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "arguments": vars(args),
                "cache": "new synthetic stores; OS cache uncontrolled",
                "network": "none",
                "memory": "boundary RSS; not process peak; imports precede first sample",
            }
        ),
        flush=True,
    )
    for repeat in range(args.repeat):
        for backend in ("files", "sqlite"):
            for rounds in args.rounds:
                with tempfile.TemporaryDirectory(prefix="ouro-long-run-") as temporary:
                    report = await sample(
                        Path(temporary),
                        backend,
                        rounds,
                        args.chunks,
                        args.slow_reader,
                        args.abort_at,
                    )
                    print(json.dumps({"repeat": repeat, **report}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
