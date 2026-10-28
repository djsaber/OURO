"""Measure selected resource reads using disposable target-format sessions.

Run: uv run --frozen python -m dev.benchmarks.session_resources --repeat 7
No OS cache eviction; fresh adapters distinguish process-local indexing from warm reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ouro.core.resources import ResourceSource, SessionResourceReference
from ouro.core.state import SessionRecord
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def metadata(name, content):
    return SessionResourceReference(
        filename=name,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="application/octet-stream",
        size=len(content),
        source=ResourceSource(name=name),
    )


def adapter(root, backend):
    return LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "ouro.db")


@contextmanager
def file_reads():
    counts = Counter()
    original = Path.open

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)

        def read(self, size=-1):
            data = self.stream.read(size)
            return self._count(data)

        def readline(self, size=-1):
            return self._count(self.stream.readline(size))

        def __iter__(self):
            return self

        def __next__(self):
            return self._count(next(self.stream))

        def _count(self, data):
            counts["python_read_bytes"] += len(
                data.encode("utf-8") if isinstance(data, str) else data
            )
            return data

    def opened(path, *args, **kwargs):
        counts["path_opens"] += 1
        return Reader(original(path, *args, **kwargs))

    with patch.object(Path, "open", opened):
        yield counts


def sample(store, blobs, session_id, resource_id, size):
    with file_reads() as counts:
        tracemalloc.start()
        started = time.perf_counter()
        try:
            record = store.load_resource_reference(session_id, resource_id)
            parts = blobs.read_ranges(session_id, record, ((0, 16), (size - 32, 32)))
            assert parts == (b"x" * 16, b"x" * 32)
            duration = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return {"seconds": duration, "python_peak_bytes": peak, **counts}


def experiment(root, backend, old_count, size, repeat):
    store = adapter(root, backend)
    store.initialize()
    session = SessionRecord()
    content = b"x" * size
    selected = metadata("selected.bin", content)
    historical = [metadata(f"old-{index}.bin", b"history") for index in range(old_count)]

    def contents():
        yield selected, content
        for record in historical:
            yield record, b"history"

    store.create_session_snapshot(
        session, [], [selected, *historical], resource_contents=contents()
    )
    blobs = LocalResourceStore(root)
    for mode in ("warm_adapter", "fresh_adapter"):
        rows = [
            sample(
                store if mode == "warm_adapter" else adapter(root, backend),
                blobs,
                session.session_id,
                selected.resource_id,
                size,
            )
            for _ in range(repeat)
        ]
        result = {
            "backend": backend,
            "cache": mode,
            "old_resources": old_count,
            "selected_bytes": size,
            "window_bytes": 48,
            "samples": rows,
            "median_seconds": statistics.median(row["seconds"] for row in rows),
            "scope": "point metadata lookup and two windows; Python reads exclude SQLite engine IO",
        }
        if backend == "sqlite":
            with store._read_connection() as connection:
                result["query_plan"] = connection.execute(
                    "EXPLAIN QUERY PLAN SELECT payload FROM resource_references "
                    "WHERE session_id = ? AND resource_id = ?",
                    (session.session_id, selected.resource_id),
                ).fetchall()
                result["page_count"] = connection.execute("PRAGMA page_count").fetchone()[0]
                result["freelist_count"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
            wal = root / "ouro.db-wal"
            result["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        files = [path for path in root.rglob("*") if path.is_file()]
        result["actual_files"] = len(files)
        result["logical_file_bytes"] = sum(path.stat().st_size for path in files)
        print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--backend", choices=("files", "sqlite", "all"), default="all")
    parser.add_argument("--old-counts", type=int, nargs="+", default=[0, 1000, 10000])
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[4096, 4 * 1024 * 1024, 64 * 1024 * 1024]
    )
    args = parser.parse_args()
    if args.repeat < 1 or min(args.old_counts) < 0 or min(args.sizes) < 48:
        parser.error("repeat must be positive, counts non-negative and sizes at least 48")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "repeat": args.repeat,
                "cache": "OS cache uncontrolled; synthetic setup excluded from timing",
                "memory": "tracemalloc allocation peak; excludes native SQLite and OS caches",
            }
        ),
        flush=True,
    )
    backends = ("files", "sqlite") if args.backend == "all" else (args.backend,)
    for backend in backends:
        # Vary old resources and file size independently, keeping the selected windows fixed.
        scenarios = {(count, args.sizes[0]) for count in args.old_counts}
        scenarios.update((args.old_counts[0], size) for size in args.sizes)
        for count, size in sorted(scenarios):
            with tempfile.TemporaryDirectory(prefix="ouro-resource-bench-") as temporary:
                experiment(Path(temporary), backend, count, size, args.repeat)


if __name__ == "__main__":
    main()
