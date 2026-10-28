"""Measure cold adapter recovery and one Runtime turn against unrelated history.

Run: uv run --frozen python -m dev.benchmarks.runtime_history --repeat 3
Synthetic isolated data; FauxProvider excludes network and provider encoding costs.
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
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.request_manifest import RequestManifest
from ouro.core.response_drafts import ResponseDraftChunk
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


def adapter(root, backend):
    return (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )


def seed(root, backend, covered, old_runs, summaries):
    store = adapter(root, backend)
    store.initialize()
    session = SessionRecord()
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user" if index % 2 == 0 else "assistant",
            text="x" * 64,
        )
        for index in range(covered + 2)
    ]
    records = [
        CompactionRecord(
            session_id=session.session_id,
            compaction_id=f"cmp_{index:08}",
            previous_compaction_id=f"cmp_{index - 1:08}" if index else None,
            strategy_id="synthetic",
            trigger="manual",
            summary="s" * 64,
            covered_message_count=covered,
            covered_end_message_id=messages[covered - 1].message_id,
            history_boundary_message_id=messages[-1].message_id,
        )
        for index in range(summaries)
    ]
    run = RunRecord(session_id=session.session_id, status="running")
    session.active_run_id = session.last_run_id = run.run_id
    session.status = "running"
    session.context_selection.compaction_id = records[-1].compaction_id
    store.create_session_snapshot(session, messages, compactions=records)
    for _ in range(old_runs):
        store.save_run(
            RunRecord(session_id=session.session_id, status="completed", ended_at=utc_now())
        )
    store.save_run(run)
    request = ModelRequestHeader(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn_interrupted",
        model_round=1,
        provider_id="test",
        model_id="test",
        started_at=utc_now(),
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
    )
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            runs=(run.model_copy(update={"current_request_id": request.context_id}),),
            request_header=request,
            request_manifest=RequestManifestRecord(
                context_id=request.context_id, manifest=RequestManifest()
            ),
        )
    )
    store.append_response_draft(
        session.session_id,
        request.context_id,
        ResponseDraftChunk(channel="text", content="partial response"),
    )
    return session.session_id


async def sample(root, backend, covered, old_runs, summaries):
    session_id = seed(root, backend, covered, old_runs, summaries)
    database = root / "sessions.db"
    observer = closing(sqlite3.connect(database)) if backend == "sqlite" else nullcontext(None)
    runtime = None
    try:
        with observer as connection:
            if connection is not None:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            with file_reads() as reads:
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    cold = adapter(root, backend)
                    cold.initialize()
                    recovered = SessionRecoveryService(cold).recover_runtime(session_id)
                    assert recovered.session.message_count == covered + 3
                    recovery_seconds = time.perf_counter() - started
                    _, recovery_peak = tracemalloc.get_traced_memory()
                    recovery_reads = dict(reads)
                    del recovered
                    tracemalloc.reset_peak()
                    turn_started = time.perf_counter()
                    runtime = RuntimeService(
                        store=cold,
                        tools=ToolRegistry(),
                        adapter=RuntimeAdapter(
                            FauxProvider(), ModelMetadata(model_id="test", provider_id="test")
                        ),
                    )
                    result = await runtime.run("new task", session_id=session_id)
                    assert result.run.status == "completed"
                    assert result.final_text == "Hello from OURO."
                    turn_seconds = time.perf_counter() - turn_started
                    _, turn_peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            files = [path for path in root.rglob("*") if path.is_file()]
            wal = database.with_name(database.name + "-wal")
            row = {
                "recovery_seconds": recovery_seconds,
                "recovery_python_peak_bytes": recovery_peak,
                "recovery_reads": recovery_reads,
                "turn_seconds": turn_seconds,
                "turn_python_peak_bytes": turn_peak,
                "turn_reads": {
                    key: value - recovery_reads.get(key, 0) for key, value in reads.items()
                },
                "actual_files": len(files),
                "logical_file_bytes_including_wal": sum(path.stat().st_size for path in files),
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            }
            if connection is not None:
                row["page_count"] = connection.execute("PRAGMA page_count").fetchone()[0]
                row["freelist_count"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
            return row
    finally:
        if runtime is not None:
            await runtime.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "repeat": args.repeat,
                "scope": "fresh adapter initialize/recover, then Runtime creation and one turn",
                "cache": "new adapters and synthetic stores per sample; OS cache uncontrolled",
                "setup": "excluded; 64-character bodies, two active messages, one current Draft",
                "provider": "FauxProvider; no network or provider-specific encoding",
                "io": "Path.open includes writes; Python bytes exclude SQLite native IO",
                "memory": "tracemalloc, not RSS; turn peak includes retained recovery allocations",
            }
        ),
        flush=True,
    )
    for backend in ("files", "sqlite"):
        for covered, old_runs, summaries in ((64, 1, 1), (4096, 1, 1), (64, 512, 1), (64, 1, 512)):
            samples = []
            for _ in range(args.repeat):
                with tempfile.TemporaryDirectory(prefix="ouro-entry-") as temporary:
                    samples.append(
                        await sample(Path(temporary), backend, covered, old_runs, summaries)
                    )
            print(
                json.dumps(
                    {
                        "backend": backend,
                        "covered_messages": covered,
                        "old_runs": old_runs,
                        "summaries": summaries,
                        "samples": samples,
                        "median_recovery_seconds": statistics.median(
                            row["recovery_seconds"] for row in samples
                        ),
                        "median_turn_seconds": statistics.median(
                            row["turn_seconds"] for row in samples
                        ),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
