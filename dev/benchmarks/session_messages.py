"""Measure message point, Run, input and tail reads with a fixed active window.

Run: uv run --frozen python -m dev.benchmarks.session_messages --repeat 7
Synthetic setup uses the public snapshot API and is excluded from query timing.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import ouro.sessions.backends.files.inputs as file_inputs
import ouro.sessions.backends.files.messages as file_messages
import ouro.sessions.backends.sqlite.inputs as sqlite_inputs
import ouro.sessions.backends.sqlite.store as sqlite_store
from dev.benchmarks.session_resources import file_reads
from ouro.core.inputs import InputRecord, InputState, QueuedInput
from ouro.core.messages import InputMessageReference, MessageFact, MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def adapter(root, backend):
    return (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )


def sample(store, session_id, selected, operation):
    decode_module = file_messages if isinstance(store, LocalSessionStore) else sqlite_store
    input_module = file_inputs if isinstance(store, LocalSessionStore) else sqlite_inputs
    decode = decode_module.decode_versioned_record_json
    decoded = 0
    inputs = 0

    def counted(model, payload, **kwargs):
        nonlocal decoded, inputs
        if issubclass(model, MessageFact):
            decoded += 1
        elif model is InputRecord:
            inputs += 1
        return decode(model, payload, **kwargs)

    with (
        file_reads() as reads,
        patch.object(decode_module, "decode_versioned_record_json", counted),
        patch.object(input_module, "decode_versioned_record_json", counted),
    ):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            if operation == "point":
                assert store.load_message(session_id, selected[-1].message_id) == selected[-1]
            elif operation == "run":
                assert store.load_run_messages(session_id, "run_active") == selected
            elif operation == "input":
                assert store.find_queued_input_message(session_id, "queue_active") == selected[-1]
            else:
                assert store.load_messages_tail(session_id, limit=len(selected)) == selected[::-1]
            seconds = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    return {
        "seconds": seconds,
        "python_peak_bytes": peak,
        "message_decodes": decoded,
        "input_decodes": inputs,
        **reads,
    }


def experiment(root, backend, count, active, repeat):
    store = adapter(root, backend)
    store.initialize()
    session = SessionRecord(session_id="ses_bench")
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            sequence=index + 1,
            run_id="run_history" if index < count - active else "run_active",
            text="x" * 128,
        ).model_copy(update={"message_id": f"msg_{index:08d}"})
        for index in range(count)
    ]
    messages[-1].input_ref = InputMessageReference(input_id="queue_active")
    record = InputRecord(
        session_id=session.session_id,
        item=QueuedInput(
            queue_id="queue_active",
            history_epoch=session.history_epoch,
            prompt=messages[-1].text_content,
            queued_at=messages[-1].created_at,
            source="user",
            mode="follow_up",
            role="user",
            scaffold=False,
            semantic_tag=None,
        ),
    )
    state = InputState.pending(record).model_copy(
        update={"status": "consumed", "message_ids": (messages[-1].message_id,)}
    )
    setup_started = time.perf_counter()
    store.create_session_snapshot(session, messages, inputs=(record,), input_states=(state,))
    setup_seconds = time.perf_counter() - setup_started
    selected = messages[-active:]
    del messages
    files = [path for path in root.rglob("*") if path.is_file()]
    storage = {
        "actual_files": len(files),
        "logical_file_bytes": sum(path.stat().st_size for path in files),
        "setup_seconds": setup_seconds,
    }
    if backend == "sqlite":
        with store._read_connection() as connection:
            storage["page_count"] = connection.execute("PRAGMA page_count").fetchone()[0]
            storage["freelist_count"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
            storage["input_query_plan"] = connection.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM messages WHERE session_id = ? "
                "AND json_extract(payload, '$.input_ref.input_id') = ? "
                "ORDER BY row_id DESC LIMIT 1",
                (session.session_id, "queue_active"),
            ).fetchall()
    for operation in ("point", "run", "input", "tail"):
        for mode in ("warm_adapter", "fresh_adapter"):
            rows = [
                sample(
                    store if mode == "warm_adapter" else adapter(root, backend),
                    session.session_id,
                    selected,
                    operation,
                )
                for _ in range(repeat)
            ]
            print(
                json.dumps(
                    {
                        "backend": backend,
                        "messages": count,
                        "active_messages": active,
                        "pending_inputs": 0,
                        "operation": operation,
                        "cache": mode,
                        "samples": rows,
                        "median_seconds": statistics.median(row["seconds"] for row in rows),
                        **storage,
                    }
                ),
                flush=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument("--active", type=int, default=8)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--backend", choices=("files", "sqlite", "all"), default="all")
    args = parser.parse_args()
    if args.active < 1 or args.repeat < 1 or min(args.counts) < args.active:
        parser.error("repeat and active must be positive; counts must cover the active window")
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "repeat": args.repeat,
                "cache": "OS caches uncontrolled; fresh means a new adapter, not cold disk",
                "scope": "selected queries only; setup, file enumeration and SQL plans excluded",
                "memory": "tracemalloc Python peak; excludes native SQLite/OS caches and RSS",
                "io": "Python Path.open reads only; excludes SQLite engine and metadata IO",
            }
        ),
        flush=True,
    )
    for backend in ("files", "sqlite") if args.backend == "all" else (args.backend,):
        for count in args.counts:
            with tempfile.TemporaryDirectory(prefix="ouro-msg-bench-") as temporary:
                experiment(Path(temporary), backend, count, args.active, args.repeat)


if __name__ == "__main__":
    main()
