"""Offline M5 probe; run the same file against M4 and M5 using --source.

Fixture creation, index warmup and checkpoint counts are outside measured sections.
Process I/O includes SQLite's native calls; Python fsync counts do NOT include SQLite
VFS synchronization. This is diagnostic evidence, not a machine-independent CI limit.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from time import perf_counter
from unittest.mock import patch


def process_io():
    if sys.platform == "win32":

        class Counters(ctypes.Structure):
            _fields_ = [
                (key, ctypes.c_ulonglong)
                for key in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessIoCounters.argtypes = (ctypes.c_void_p, ctypes.POINTER(Counters))
        values = Counters()
        if not kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            key: getattr(values, key)
            for key in ("read_bytes", "write_bytes", "read_ops", "write_ops")
        }
    values = dict(line.split(": ") for line in Path("/proc/self/io").read_text().splitlines())
    return {
        "read_bytes": int(values["rchar"]),
        "write_bytes": int(values["wchar"]),
        "read_ops": int(values["syscr"]),
        "write_ops": int(values["syscw"]),
    }


def measure(operation):
    syncs = 0
    fsync = os.fsync

    def counted(fd):
        nonlocal syncs
        syncs += 1
        return fsync(fd)

    with patch("os.fsync", counted):
        before = process_io()
        start = perf_counter()
        result = operation()
        elapsed = (perf_counter() - start) * 1000
        after = process_io()
    return result, {
        "elapsed_ms": round(elapsed, 3),
        "python_fsync_calls": syncs,
        **{key: after[key] - before[key] for key in before},
    }


def checkpoint_count(store, session_id, backend):
    if backend == "files":
        return len(list((store.sessions_dir / session_id / "harness").glob("*.json")))
    with closing(sqlite3.connect(store.database)) as connection:
        return connection.execute(
            "SELECT count(*) FROM harness_versions WHERE session_id=?", (session_id,)
        ).fetchone()[0]


def probe(store, backend, prefix, owners, owner_bytes):
    # Import after --source selection so both revisions run this exact workload.
    from ouro.core.harness import HarnessState
    from ouro.core.harness_calls import HarnessCallSnapshot, HarnessInvocation
    from ouro.core.messages import MessageRecord
    from ouro.core.session_commits import SessionCommit, SessionCommitStep
    from ouro.core.session_extensions import SessionExtensionStateRecord
    from ouro.core.state import SessionRecord

    session = SessionRecord()
    store.create_session(session)
    state = HarnessState(
        states=tuple(
            SessionExtensionStateRecord(
                session_id=session.session_id,
                owner_id=f"example.owner{index}",
                state_schema_version=1,
                payload={"value": "x" * owner_bytes},
            )
            for index in range(owners)
        )
    )
    baseline_count = checkpoint_count(store, session.session_id, backend)
    _, write = measure(
        lambda: store.commit_session(
            SessionCommit(before=session, steps=(SessionCommitStep(harness=state),))
        )
    )
    count = checkpoint_count(store, session.session_id, backend)
    state_change_versions = count - baseline_count
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text="data " * 100)
        for _ in range(prefix + 2)
    ]
    store.append_messages(messages)
    session = store.load_session_record(session.session_id)
    replacement = state.model_copy(
        update={
            "states": tuple(
                owner.model_copy(update={"payload": {"value": "y" * owner_bytes}})
                for owner in state.states
            )
        }
    )
    session = store.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(harness=replacement),))
    ).session
    count = checkpoint_count(store, session.session_id, backend)
    store.load_message(session.session_id, messages[-2].message_id)
    cut, withdrawal = measure(
        lambda: store.commit_session(
            SessionCommit(before=session, before_message_id=messages[-2].message_id)
        )
    )
    assert cut.truncation is not None and cut.truncation.removed_count == 2
    assert checkpoint_count(store, session.session_id, backend) == count
    assert store.load_harness(session.session_id).state == state

    # One phase captures one state; all owner views are served from that snapshot.
    def capture():
        with store.session_mutation(session.session_id):
            current = store.load_session_record(session.session_id)
            return HarnessCallSnapshot(
                session_id=session.session_id,
                harness_version=current.harness_version,
                history_epoch=current.history_epoch,
                last_message_id=current.last_message_id,
                state=store.load_harness(session.session_id).state,
            )

    snapshot, read = measure(capture)

    def callback(owner, writable):
        for _ in range(20):
            invocation = HarnessInvocation(snapshot, owner.owner_id, writable=writable)
            value = invocation.read()
            assert value == owner
            if writable:
                invocation.replace(value)
            assert invocation.finish() is None

    with ThreadPoolExecutor(max_workers=8) as executor:
        _, readonly = measure(
            lambda: list(executor.map(lambda owner: callback(owner, False), snapshot.state.states))
        )
        _, noop = measure(
            lambda: list(executor.map(lambda owner: callback(owner, True), snapshot.state.states))
        )
    assert checkpoint_count(store, session.session_id, backend) == count
    return {
        "backend": backend,
        "prefix": prefix,
        "suffix": 2,
        "owners": owners,
        "owner_bytes": owner_bytes,
        "harness_bytes": len(state.model_dump_json().encode()),
        "state_change": write,
        "cut": withdrawal,
        "capture": read,
        "readonly_callbacks": readonly,
        "noop_callbacks": noop,
        "state_change_versions": state_change_versions,
        "cut_versions": 0,
        "callback_versions": 0,
        "native_sqlite_fsync_calls": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from ouro.sessions import LocalSessionStore, SQLiteSessionStore

    with tempfile.TemporaryDirectory(prefix="ouro-harness-") as directory:
        root = Path(directory)
        for backend in ("files", "sqlite"):
            store = (
                LocalSessionStore(root / backend)
                if backend == "files"
                else SQLiteSessionStore(root / backend / "sessions.db")
            )
            store.initialize()
            for prefix in (100, 10000):
                for owners, size in ((1, 1024), (8, 8192), (8, 65536)):
                    for repeat in range(args.repeat):
                        print(
                            json.dumps(
                                {
                                    "label": args.label,
                                    "platform": platform.platform(),
                                    "repeat": repeat,
                                    **probe(store, backend, prefix, owners, size),
                                }
                            ),
                            flush=True,
                        )


if __name__ == "__main__":
    main()
