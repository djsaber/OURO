"""Measure offline upgrades on synthetic history or disposable copies of an explicit home."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import shutil
import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import cast
from unittest.mock import patch

from dev.benchmarks.harness_lifecycle import process_io
from ouro.deployment import SessionBackend
from ouro.local_permissions import restrict_to_user
from ouro_upgrade.engine import upgrade
from ouro_upgrade.progress import UpgradeProgress

FIXTURES = Path(__file__).resolve().parents[2] / "tests/upgrade/fixtures"


def native_home(home: Path, backend: str, messages: int) -> None:
    """Extend frozen native writer output without using the current storage writer."""
    shutil.copytree(FIXTURES / "native" / backend, home)
    source = home / "session.sql"
    directory = home / "sessions/ses_fixture"
    database = (
        directory / "session-data.sqlite3" if backend == "files" else home / "sessions.sqlite3"
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(source.read_text("utf-8"))
        source.unlink()
        first = (
            (directory / "messages.jsonl").read_text("utf-8").splitlines()[0]
            if backend == "files"
            else connection.execute("SELECT payload FROM messages WHERE row_id=1").fetchone()[0]
        )
        message = json.loads(first)
        message["content"] = [{"type": "text", "text": "x" * 4096}]
        with (
            (directory / "messages.jsonl").open("ab")
            if backend == "files"
            else nullcontext() as stream
        ):
            for index in range(messages):
                identity, sequence = f"msg_bench_{index}", index + 5
                message.update(message_id=identity, sequence=sequence)
                payload = json.dumps(message, separators=(",", ":"))
                if stream is not None:
                    connection.execute(
                        "INSERT INTO locations(position,message_id,offset,role,resource_refs) "
                        "VALUES(?,?,?,'user','[]')",
                        (sequence, identity, stream.tell()),
                    )
                    stream.write((payload + "\n").encode("utf-8"))
                else:
                    connection.execute(
                        "INSERT INTO messages(session_id,message_id,sequence,role,"
                        "resource_refs,payload) "
                        "VALUES('ses_fixture',?,?,'user','[]',?)",
                        (identity, sequence, payload),
                    )
            if messages:
                connection.execute(
                    "UPDATE sessions SET payload=json_set(payload,'$.message_count',?,"
                    "'$.last_message_id',?)",
                    (messages + 4, identity),
                )
                if backend == "sqlite":
                    connection.execute(
                        "UPDATE session_catalog SET payload=json_set(payload,'$.message_count',?)",
                        (messages + 4,),
                    )
        connection.commit()
    if backend == "files":
        index = home / "sessions/index.json"
        catalog = json.loads(index.read_text("utf-8"))
        catalog["sessions"][0]["message_count"] = messages + 4
        index.write_text(json.dumps(catalog), "utf-8")


def synthetic_home(home: Path, backend: str, operations: int) -> None:
    shutil.copytree(FIXTURES / backend, home)
    for source in home.rglob("*.sql"):
        with closing(sqlite3.connect(source.with_suffix(".sqlite3"))) as connection:
            connection.executescript(source.read_text("utf-8"))
        source.unlink()
    raw = json.loads(
        (FIXTURES / "files/sessions/ses_fixture/operations/op_fixture.json").read_text("utf-8")
    )
    if backend == "files":
        directory = home / "sessions/ses_fixture/operations"
        for index in range(operations):
            raw["operation_id"] = f"op_bench_{index}"
            (directory / f"{raw['operation_id']}.json").write_text(
                json.dumps(raw), encoding="utf-8"
            )
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            for index in range(operations):
                raw["operation_id"] = f"op_bench_{index}"
                connection.execute(
                    "INSERT INTO session_commits(session_id,operation_id,payload) VALUES (?,?,?)",
                    ("ses_fixture", raw["operation_id"], json.dumps(raw)),
                )
            connection.commit()


def sample(
    home: Path, *, backend: SessionBackend, check: bool, profile: Path | None
) -> dict[str, object]:
    phases: dict[str, float] = {}
    phase: str | None = None
    phase_started = perf_counter()
    sync_count = 0
    sync_seconds = 0.0
    original_sync = os.fsync

    def progress(event: UpgradeProgress) -> None:
        nonlocal phase, phase_started
        if event.phase != phase:
            now = perf_counter()
            if phase is not None:
                phases[phase] = phases.get(phase, 0.0) + now - phase_started
            phase, phase_started = event.phase, now

    def sync(descriptor: int) -> None:
        nonlocal sync_count, sync_seconds
        started = perf_counter()
        try:
            original_sync(descriptor)
        finally:
            sync_count += 1
            sync_seconds += perf_counter() - started

    profiler = cProfile.Profile() if profile else None
    before_io = process_io()
    started = perf_counter()
    try:
        with patch("os.fsync", sync):
            if profiler:
                profiler.enable()
            report = upgrade(
                home,
                session_backend=backend,
                task_backend=backend,
                check=check,
                progress=progress,
            )
    finally:
        elapsed = perf_counter() - started
        after_io = process_io()
        if profiler:
            profiler.disable()
            assert profile is not None
            profiler.dump_stats(str(profile))
    return {
        "seconds": elapsed,
        "phase_seconds": dict(phases),
        "python_fsync_calls": sync_count,
        "python_fsync_seconds": sync_seconds,
        "status": report["status"],
        "changed_files": len(cast(list[str], report["files"])),
        "process_io": {name: after_io[name] - value for name, value in before_io.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("files", "sqlite"), default="files")
    parser.add_argument("--operations", type=int, default=1000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--native-messages",
        type=int,
        help="Upgrade layout 41/schema 26 with this many additional 4 KiB messages",
    )
    parser.add_argument(
        "--profile", type=Path, help="Save cProfile statistics (requires --repeat 1)"
    )
    parser.add_argument(
        "--source-home", type=Path, help="Copy this stopped home instead of building synthetic data"
    )
    args = parser.parse_args()
    if args.operations < 0 or args.repeat < 1:
        parser.error("operations must be nonnegative and repeat must be positive")
    if args.profile and args.repeat != 1:
        parser.error("--profile requires --repeat 1")
    if args.native_messages is not None and (args.native_messages < 0 or args.source_home):
        parser.error("--native-messages requires a nonnegative count and no --source-home")
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "backend": args.backend,
                "source": "explicit home copy" if args.source_home else "synthetic history",
                "operations": None
                if args.source_home or args.native_messages is not None
                else args.operations + 1,
                "native_messages": args.native_messages,
                "profiled": bool(args.profile),
            }
        ),
        flush=True,
    )
    for index in range(args.repeat):
        with TemporaryDirectory(prefix="ouro-upgrade-benchmark-") as directory:
            root = Path(directory)
            restrict_to_user(root, directory=True)
            home = root / "home"
            if args.source_home:
                shutil.copytree(args.source_home, home, ignore=shutil.ignore_patterns(".upgrade"))
            elif args.native_messages is not None:
                native_home(home, args.backend, args.native_messages)
            else:
                synthetic_home(home, args.backend, args.operations)
            result = sample(home, backend=args.backend, check=args.check, profile=args.profile)
            if args.native_messages is not None:
                assert result["status"] == ("upgrade_required" if args.check else "upgraded")
            print(json.dumps({"sample": index + 1, **result}), flush=True)


if __name__ == "__main__":
    main()
