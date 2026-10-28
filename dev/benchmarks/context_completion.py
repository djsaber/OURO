"""Measure request completion separately from preparing its immutable input.

Run: uv run --frozen python -m dev.benchmarks.context_completion --repeat 7
Only disposable synthetic sessions are used; OS caches are not cleared.
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
from contextlib import closing
from pathlib import Path

from dev.benchmarks.session_resources import file_reads
from ouro.core.base import utc_now
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.request_manifest import RequestManifest
from ouro.core.session_commits import SessionCommit
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def experiment(root, backend, size, repeat):
    database = root / "sessions.db"
    store = LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(database)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    rows = []
    if backend == "files":
        database = store.sessions_dir / session.session_id / "session-data.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        for index in range(repeat):
            context = ModelRequestHeader(
                context_id=f"ctx_{index}",
                session_id=session.session_id,
                run_id="run",
                turn_id="turn",
                model_round=index + 1,
                provider_id="test",
                model_id="test",
                metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=1),
            )
            store.commit_session(
                SessionCommit(
                    before=store.load_session_record(session.session_id),
                    request_header=context,
                    request_manifest=RequestManifestRecord(
                        context_id=context.context_id,
                        manifest=RequestManifest(system_prompt="x" * size),
                    ),
                )
            )
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            with file_reads() as reads:
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    store.settle_request_header(
                        session.session_id,
                        context.context_id,
                        settled_at=utc_now(),
                        provider_done=True,
                        usage_entry_id=context.expected_usage_entry_id,
                    )
                    duration = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            wal = database.with_name(database.name + "-wal")
            rows.append(
                {
                    "seconds": duration,
                    "python_peak_bytes": peak,
                    **reads,
                    "completion_wal_bytes": wal.stat().st_size if wal.exists() else 0,
                }
            )
        result = {
            "backend": backend,
            "input_text_bytes": size,
            "samples": rows,
            "median_seconds": statistics.median(row["seconds"] for row in rows),
            "scope": "completion only; preparation and checkpoints excluded",
        }
        result["page_count"] = connection.execute("PRAGMA page_count").fetchone()[0]
        result["freelist_count"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
        result["input_rows"] = connection.execute(
            "SELECT count(*) FROM latest_contexts"
        ).fetchone()[0]
        result["header_rows"] = connection.execute(
            "SELECT count(*) FROM request_headers"
        ).fetchone()[0]
        files = [path for path in root.rglob("*") if path.is_file()]
        result["actual_files"] = len(files)
        result["logical_file_bytes_including_wal"] = sum(path.stat().st_size for path in files)
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[64, 4 * 1024 * 1024, 64 * 1024 * 1024]
    )
    args = parser.parse_args()
    if args.repeat < 1 or min(args.sizes) < 1:
        parser.error("repeat and sizes must be positive")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "repeat": args.repeat,
                "cache": "warm adapters; OS cache uncontrolled; setup excluded",
                "io": (
                    "Python Path.open reads exclude SQLite engine IO; "
                    "WAL sampled after each completion"
                ),
                "memory": "tracemalloc allocation peak, not RSS",
            }
        ),
        flush=True,
    )
    for backend in ("files", "sqlite"):
        for size in args.sizes:
            with tempfile.TemporaryDirectory(prefix="ouro-context-bench-") as temporary:
                experiment(Path(temporary), backend, size, args.repeat)


if __name__ == "__main__":
    main()
