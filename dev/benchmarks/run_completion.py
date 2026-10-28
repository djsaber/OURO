"""Measure a Run completion commit against unrelated canonical history.

Run: uv run --frozen python -m dev.benchmarks.run_completion --repeat 5
This measures the synchronous commit and catalog refresh, not a full model turn.
"""

from __future__ import annotations

import argparse
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
from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.core.base import utc_now
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def experiment(root, backend, history_size, old_runs, repeat):
    database = root / "sessions.db"
    store = LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(database)
    store.initialize()
    session = SessionRecord()
    store.create_session_snapshot(
        session,
        [
            MessageRecord.text(
                session_id=session.session_id,
                role="user" if index % 2 == 0 else "assistant",
                text="x" * 64,
            )
            for index in range(history_size)
        ],
    )
    for _ in range(old_runs):
        store.save_run(
            RunRecord(session_id=session.session_id, status="completed", ended_at=utc_now())
        )
    runs = RunLifecycle(
        store=store,
        events=RuntimeEventRecorder(store=store, observers=ObserverRegistry()),
    )
    observer = closing(sqlite3.connect(database)) if backend == "sqlite" else nullcontext(None)
    samples = []
    with observer as connection:
        if connection is not None:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        for index in range(repeat):
            session = store.load_session_record(session.session_id)
            run = RunRecord(session_id=session.session_id, status="running")
            session = store.commit_session(
                SessionCommit(
                    before=session,
                    after=session.model_copy(
                        update={"status": "running", "active_run_id": run.run_id}
                    ),
                    runs=(run,),
                )
            ).session
            with file_reads() as reads:
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    completed, _, _ = runs._complete(
                        session=session,
                        run=run,
                        final_text="answer",
                        reasoning_content="",
                        model_response=None,
                        provider_continuation=None,
                        turn_id=f"turn_{index}",
                        had_tool_error=False,
                        assistant_already_committed=False,
                    )
                    duration = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            assert completed.message_count == history_size + index + 1
            samples.append({"seconds": duration, "python_peak_bytes": peak, **reads})
        files = [path for path in root.rglob("*") if path.is_file()]
        wal = database.with_name(database.name + "-wal")
        result = {
            "backend": backend,
            "old_messages": history_size,
            "old_runs": old_runs,
            "samples": samples,
            "median_seconds": statistics.median(item["seconds"] for item in samples),
            "actual_files": len(files),
            "logical_file_bytes_including_wal": sum(path.stat().st_size for path in files),
            "wal_bytes_after_samples": wal.stat().st_size if wal.exists() else 0,
        }
        if connection is not None:
            result["page_count"] = connection.execute("PRAGMA page_count").fetchone()[0]
            result["freelist_count"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "repeat": args.repeat,
                "scope": "Run completion commit with one response and catalog refresh",
                "cache": "initialized adapters; OS cache uncontrolled; setup excluded",
                "io": "Path.open counts include writes; read bytes exclude SQLite engine IO",
                "memory": "tracemalloc allocations inside completion only; not process RSS",
                "history_text_bytes_per_message": 64,
            }
        ),
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="ouro-run-completion-") as directory:
        for backend in ("files", "sqlite"):
            for history_size, old_runs in ((64, 1), (4096, 1), (64, 512)):
                experiment(
                    Path(directory) / f"{backend}-{history_size}-{old_runs}",
                    backend,
                    history_size,
                    old_runs,
                    args.repeat,
                )


if __name__ == "__main__":
    main()
