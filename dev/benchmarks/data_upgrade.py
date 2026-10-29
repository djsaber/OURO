"""Measure offline upgrades on synthetic history or disposable copies of an explicit home."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import cast
from unittest.mock import patch

from ouro.deployment import SessionBackend
from ouro.local_permissions import restrict_to_user
from ouro_upgrade.engine import upgrade
from ouro_upgrade.progress import UpgradeProgress

FIXTURES = Path(__file__).resolve().parents[2] / "tests/upgrade/fixtures"


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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("files", "sqlite"), default="files")
    parser.add_argument("--operations", type=int, default=1000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--check", action="store_true")
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
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "backend": args.backend,
                "source": "explicit home copy" if args.source_home else "synthetic history",
                "operations": None if args.source_home else args.operations + 1,
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
            else:
                synthetic_home(home, args.backend, args.operations)
            result = sample(home, backend=args.backend, check=args.check, profile=args.profile)
            print(json.dumps({"sample": index + 1, **result}), flush=True)


if __name__ == "__main__":
    main()
