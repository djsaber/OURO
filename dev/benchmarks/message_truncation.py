"""Offline suffix-cut probe: uv run python -m dev.benchmarks.message_truncation.

Fixture creation is excluded from timings. Results are diagnostic, not CI limits.
"""

from __future__ import annotations

import json
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def measure(store, *, prefix_count: int, repeat: int = 5) -> dict[str, object]:
    timings = []
    for _ in range(repeat):
        session = SessionRecord()
        messages = [
            MessageRecord.text(
                session_id=session.session_id,
                role="user" if index % 2 == 0 else "assistant",
                text="Synthetic evidence. " * 20,
                sequence=index + 1,
            )
            for index in range(prefix_count + 2)
        ]
        session = session.model_copy(
            update={
                "message_count": len(messages),
                "last_message_id": messages[-1].message_id,
            }
        )
        store.create_session(session)
        store.append_messages(messages)
        start = perf_counter()
        cut = store.truncate_messages(
            session.session_id,
            before_message_id=messages[-2].message_id,
            expected_last_message_id=messages[-1].message_id,
        )
        timings.append((perf_counter() - start) * 1000)
        assert cut.removed_count == 2
    return {
        "retained_messages": prefix_count,
        "removed_messages": 2,
        "median_ms": round(statistics.median(timings), 3),
        "max_ms": round(max(timings), 3),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ouro-truncation-") as directory:
        root = Path(directory)
        for backend, store in (
            ("files", LocalSessionStore(root / "files")),
            ("sqlite", SQLiteSessionStore(root / "sessions.sqlite")),
        ):
            store.initialize()
            for size in (100, 10000):
                print(json.dumps({"backend": backend, **measure(store, prefix_count=size)}))


if __name__ == "__main__":
    main()
