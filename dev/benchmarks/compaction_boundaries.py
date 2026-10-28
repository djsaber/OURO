"""Measure context preparation with unrelated covered prefixes and summaries.

Run: uv run --frozen python -m dev.benchmarks.compaction_boundaries --repeat 5
Setup uses disposable synthetic snapshots. This is not an end-to-end request benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from contextlib import closing, nullcontext
from pathlib import Path

from dev.benchmarks.session_resources import file_reads
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


async def experiment(root, backend, history_size, summary_count, repeat):
    database = root / "sessions.db"
    store = LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(database)
    store.initialize()
    session = SessionRecord()
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user" if index % 2 == 0 else "assistant",
            text="x" * 64,
            sequence=index + 1,
        ).model_copy(update={"message_id": f"msg_{index:08}"})
        for index in range(history_size + 2)
    ]
    records = [
        CompactionRecord(
            session_id=session.session_id,
            compaction_id=f"cmp_{index:08}",
            previous_compaction_id=f"cmp_{index - 1:08}" if index else None,
            strategy_id="synthetic",
            trigger="manual",
            summary="s" * 64,
            covered_message_count=history_size,
            covered_end_message_id=messages[history_size - 1].message_id,
            history_boundary_message_id=messages[-1].message_id,
        )
        for index in range(summary_count)
    ]
    session.context_selection.compaction_id = records[-1].compaction_id
    store.create_session_snapshot(session, messages, compactions=records)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="test")),
    )
    observer = closing(sqlite3.connect(database)) if backend == "sqlite" else nullcontext(None)
    samples = []
    try:
        with observer as connection:
            if connection is not None:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            for sample in range(repeat):
                with file_reads() as reads:
                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        prepared, _ = await runtime.context_preparation.prepare(
                            [],
                            0,
                            session=session,
                            run=run,
                            tools=[],
                            model_round=1,
                            turn_id=f"turn_{sample}",
                            abort_signal=AbortSignal(),
                        )
                        elapsed = time.perf_counter() - started
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                assert len(prepared.provider_context.messages) == 3
                samples.append({"seconds": elapsed, "python_peak_bytes": peak, **reads})
            files = [path for path in root.rglob("*") if path.is_file()]
            wal = database.with_name(database.name + "-wal")
            result = {
                "backend": backend,
                "covered_messages": history_size,
                "retained_tail_messages": 2,
                "retained_summaries": summary_count,
                "active_record_bytes": len(records[-1].model_dump_json().encode()),
                "samples": samples,
                "median_seconds": statistics.median(item["seconds"] for item in samples),
                "actual_files": len(files),
                "logical_file_bytes_including_wal": sum(path.stat().st_size for path in files),
                "wal_bytes_after_preparations": wal.stat().st_size if wal.exists() else 0,
            }
            if connection is not None:
                result["page_count"] = connection.execute("PRAGMA page_count").fetchone()[0]
                result["freelist_count"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
            print(json.dumps(result), flush=True)
    finally:
        await runtime.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--history-sizes", type=int, nargs="+", default=[64, 4096])
    parser.add_argument("--summary-counts", type=int, nargs="+", default=[1, 512])
    args = parser.parse_args()
    if min(args.repeat, *args.history_sizes, *args.summary_counts) < 1:
        parser.error("repeat and sizes must be positive")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "repeat": args.repeat,
                "scope": "selected history read, context projection and latest request replacement",
                "cache": "initialized adapters; OS cache uncontrolled; snapshot setup excluded",
                "io": "Path.open includes writes; Python read bytes exclude SQLite engine IO",
                "memory": "tracemalloc allocation peak, not RSS; snapshot setup excluded",
            }
        ),
        flush=True,
    )
    cases = dict.fromkeys(
        [(size, 1) for size in args.history_sizes]
        + [(min(args.history_sizes), count) for count in args.summary_counts]
    )
    for backend in ("files", "sqlite"):
        for history_size, summary_count in cases:
            with tempfile.TemporaryDirectory(prefix="ouro-summary-bench-") as temporary:
                await experiment(Path(temporary), backend, history_size, summary_count, args.repeat)


if __name__ == "__main__":
    asyncio.run(main())
