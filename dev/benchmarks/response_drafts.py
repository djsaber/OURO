"""Measure equal output split into different Draft fragment counts in disposable sessions.

Run: uv run --frozen python -m dev.benchmarks.response_drafts --repeat 3
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import tempfile
import time
import tracemalloc
from contextlib import closing
from pathlib import Path

from ouro.core.base import utc_now
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.messages import MessageRecord
from ouro.core.request_manifest import RequestManifest
from ouro.core.response_drafts import ResponseDraftChunk
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def storage(root: Path, backend: str) -> dict[str, int]:
    files = [path for path in root.rglob("*") if path.is_file()]
    result = {
        "files": len(files),
        "file_bytes": sum(path.stat().st_size for path in files),
    }
    databases = (
        [root / "sessions.db"]
        if backend == "sqlite"
        else list(root.glob("sessions/*/session-data.sqlite3"))
    )
    result.update(pages=0, free_pages=0, draft_rows=0, draft_payload_bytes=0, wal_bytes=0)
    for database in databases:
        with closing(sqlite3.connect(database)) as connection:
            result["pages"] += connection.execute("PRAGMA page_count").fetchone()[0]
            result["free_pages"] += connection.execute("PRAGMA freelist_count").fetchone()[0]
            result["page_size"] = connection.execute("PRAGMA page_size").fetchone()[0]
            rows, content_bytes = connection.execute(
                "SELECT count(*), coalesce(sum(length(cast(content AS blob))), 0) "
                "FROM response_drafts"
            ).fetchone()
            result["draft_rows"] += rows
            result["draft_payload_bytes"] += content_bytes
        wal = database.with_name(database.name + "-wal")
        result["wal_bytes"] += wal.stat().st_size if wal.exists() else 0
    return result


def experiment(root: Path, backend: str, fragments: int, output_bytes: int):
    store = (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session.active_run_id = run.run_id
    session.status = "running"
    store.create_session(session)
    header = ModelRequestHeader(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        started_at=utc_now(),
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
    )
    run.current_request_id = header.context_id
    store.commit_session(
        SessionCommit(
            before=session,
            runs=(run,),
            request_header=header,
            request_manifest=RequestManifestRecord(
                context_id=header.context_id, manifest=RequestManifest()
            ),
        )
    )
    chunk = ResponseDraftChunk(channel="text", content="x" * (output_bytes // fragments))
    tracemalloc.start()
    started = time.perf_counter()
    for _ in range(fragments):
        store.append_response_draft(session.session_id, header.context_id, chunk)
    append_seconds = time.perf_counter() - started
    _, append_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    active = storage(root, backend)
    response = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn",
        role="assistant",
        text="x" * output_bytes,
    )
    tracemalloc.start()
    started = time.perf_counter()
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            steps=(SessionCommitStep(message=response),),
        )
    )
    completion_seconds = time.perf_counter() - started
    _, completion_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    terminal = storage(root, backend)
    assert store.load_response_draft(session.session_id, header.context_id) is None
    assert (
        store.load_message(session.session_id, response.message_id).text_content
        == "x" * output_bytes
    )
    return {
        "backend": backend,
        "fragments": fragments,
        "output_bytes": output_bytes,
        "append_seconds": append_seconds,
        "completion_seconds": completion_seconds,
        "append_python_peak": append_peak,
        "completion_python_peak": completion_peak,
        "active": active,
        "terminal": terminal,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--fragments", type=int, nargs="+", default=[10, 1000, 100000])
    parser.add_argument("--output-bytes", type=int, default=1000000)
    args = parser.parse_args()
    if args.repeat < 1 or any(n < 1 or args.output_bytes % n for n in args.fragments):
        parser.error("repeat/counts must be positive; output bytes must be divisible by counts")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "repeat": args.repeat,
                "cache": "warm adapters; OS caches uncontrolled",
                "scope": "synchronous Draft port and response commit; no provider or tool time",
                "memory": "tracemalloc Python peak; not process RSS or SQLite native memory",
                "disk": "actual file lengths; SQLite allocated/free pages and WAL separately",
            }
        ),
        flush=True,
    )
    for backend in ("files", "sqlite"):
        for fragments in args.fragments:
            for sample in range(args.repeat):
                with tempfile.TemporaryDirectory(prefix="ouro-draft-bench-") as temporary:
                    result = experiment(Path(temporary), backend, fragments, args.output_bytes)
                    print(json.dumps({"sample": sample, **result}), flush=True)


if __name__ == "__main__":
    main()
