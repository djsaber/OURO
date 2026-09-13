"""Measure Session/Harness reads across control sizes using synthetic, offline data.

Run this same script against both revisions with --source. Each query is warmed up
five times, then sampled without concurrent tests or other benchmarks. Raw timings
are diagnostic evidence, not machine-independent CI assertions.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from functools import partial
from pathlib import Path
from statistics import median
from time import perf_counter_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    # Select the implementation before importing OURO, as in harness_lifecycle.py.
    sys.path.insert(0, str(args.source.resolve()))
    from ouro.application.harness import capture_session_harness
    from ouro.core.harness import HarnessState
    from ouro.core.session_commits import SessionCommit, SessionCommitStep
    from ouro.core.session_controls import SessionControlActivation, SessionControlState
    from ouro.core.state import SessionRecord
    from ouro.sessions import LocalSessionStore, SQLiteSessionStore

    with tempfile.TemporaryDirectory(prefix="ouro-harness-reads-") as directory:
        root = Path(directory)
        for backend in ("files", "sqlite"):
            store = (
                LocalSessionStore(root / backend)
                if backend == "files"
                else SQLiteSessionStore(root / "sessions.db")
            )
            store.initialize()
            for count in (0, 8, 64):
                session = SessionRecord()
                store.create_session(session)
                controls = SessionControlState(
                    activations=[
                        SessionControlActivation(
                            controller_id=f"example.c{i}/session_controllers/mode",
                            plugin_id=f"example.c{i}",
                            controller_local_id="mode",
                            plugin_class="user",
                            release_id="release",
                            generation_id="generation",
                            execution_contract_hash="contract",
                            data={"items": list(range(32))},
                        )
                        for i in range(count)
                    ]
                )
                store.commit_session(
                    SessionCommit(
                        before=session,
                        steps=(SessionCommitStep(harness=HarnessState(controls=controls)),),
                    )
                )
                operations = {
                    "record": partial(store.load_session_record, session.session_id),
                    "capture": partial(capture_session_harness, store, session.session_id),
                    "current_harness": partial(store.load_harness, session.session_id),
                }
                samples = {name: [] for name in operations}
                for iteration in range(205):
                    for name, operation in operations.items():
                        start = perf_counter_ns()
                        operation()
                        elapsed = (perf_counter_ns() - start) / 1e6
                        if iteration >= 5:
                            samples[name].append(elapsed)
                print(
                    json.dumps(
                        {
                            "label": args.label,
                            "platform": platform.platform(),
                            "python": platform.python_version(),
                            "backend": backend,
                            "controls": count,
                            "samples": samples,
                            "medians": {name: median(values) for name, values in samples.items()},
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
