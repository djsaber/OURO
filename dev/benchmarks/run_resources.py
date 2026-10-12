"""Offline Run resource lifetime probe.

Resource scenarios use real Runtime/coordinators/Catalog leases with a synthetic
Catalog publisher and current Session control reads. Prepared runs use real files/SQLite stores and
FauxProvider. Timings and allocation measurements run separately. No CI time limits.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import platform
import sys
import tempfile
import tracemalloc
import weakref
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch


def make_runtime(root, backend="files"):
    from ouro.application.runtime import RuntimeService
    from ouro.core.adapter import RuntimeAdapter
    from ouro.core.provider import ModelMetadata
    from ouro.plugins.catalog import ExtensionCatalogBase, build_extension_catalog
    from ouro.providers import FauxProvider
    from ouro.sessions import LocalSessionStore, SQLiteSessionStore
    from ouro.tools import ToolRegistry

    store = (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )
    store.initialize()
    app = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")),
    )
    app.create_session(_session_id="synthetic", title="Resource probe")
    catalog = build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=app.tools,
            observers=app.observers,
            commands=None,
            compaction=None,
            continuations=None,
            tool_policies=None,
        ),
        (),
    )

    async def synchronize():
        pass

    app.bind_extensions(
        catalog=SimpleNamespace(
            snapshot=catalog.acquire, catalog=lambda: catalog, synchronize=synchronize
        )
    )
    return app, catalog


def make_run(session_id="synthetic"):
    from ouro.core.agents import AgentCatalogSnapshot
    from ouro.core.state import RunRecord

    return RunRecord(
        session_id=session_id,
        status="waiting_approval",
        agent_catalog=AgentCatalogSnapshot(),
        metadata={
            "effective_tool_names": [],
            "effective_tool_refs": [],
            "delegated_constraints": {"allow_external_paths": False},
            "extension_generations": [],
        },
    )


async def resource_probe(root, count, scenario, *, memory):
    app, catalog = make_runtime(root)
    runs = [make_run() for _ in range(count)]
    references = []
    lease_peak = 0
    if memory:
        gc.collect()
        tracemalloc.start()
    started = perf_counter()
    try:
        for run in runs:
            await app._ensure_extension_snapshot(run)
            coordinator = app._tool_calls_for_run(run.run_id)
            references.append(weakref.ref(coordinator))
            if scenario == "wait_resume":
                app._finish_run_observers(run)
                await app._ensure_extension_snapshot(run)
                assert app._tool_calls_for_run(run.run_id) is not coordinator
            del coordinator
            lease_peak = max(lease_peak, catalog.lease_count)
            if scenario == "lease_transfer":
                lease = app._detach_extension_snapshot(run.run_id)
                lease.close()
                del lease
            elif scenario != "close_waiting":
                terminal = run.model_copy(update={"status": "completed"})
                app._finish_run_observers(terminal)
                app._finish_run_observers(terminal)
        before_close = catalog.lease_count
        await app.close()
        elapsed = (perf_counter() - started) * 1000
        gc.collect()
        retained = sum(reference() is not None for reference in references)
        current, peak = tracemalloc.get_traced_memory() if memory else (None, None)
        assert catalog.lease_count == 0
        return {
            "scenario": scenario,
            "count": count,
            "memory_probe": memory,
            "elapsed_ms": None if memory else elapsed,
            "lease_peak": lease_peak,
            "leases_before_close": before_close,
            "leases_after_close": catalog.lease_count,
            "coordinators_after_close": retained,
            "traced_current_bytes": current,
            "traced_peak_bytes": peak,
        }
    finally:
        if memory:
            tracemalloc.stop()
        await app.close()


async def prepared_probe(root, backend):
    from ouro.application.runs import reserve_run
    from ouro.core.agents import DelegatedConstraints
    from ouro.core.session_controls import HostSessionInput

    app, catalog = make_runtime(root, backend)
    try:
        # Independent sessions avoid conflating growing conversation history with
        # resource ownership. Session creation/reservation are outside the timer.
        samples = []
        for index in range(11):
            session = app.create_session(title="prepared timing")
            run = make_run(session.session_id).model_copy(update={"status": "queued"})
            reserve_run(app.store, run)
            started = perf_counter()
            result = await app.run_prepared(
                run, HostSessionInput(prompt="hello", host_source_id="benchmark")
            )
            await app.drain_events()
            elapsed = (perf_counter() - started) * 1000
            assert result.run.status == "completed"
            assert catalog.lease_count == 0
            if index:
                samples.append(elapsed)
        # Count decoding separately from the uninstrumented pipeline timings.
        session = app.create_session(title="prepared")
        run = make_run(session.session_id).model_copy(update={"status": "queued"})
        reserve_run(app.store, run)
        with patch.object(
            DelegatedConstraints, "model_validate", wraps=DelegatedConstraints.model_validate
        ) as decode:
            result = await app.run_prepared(
                run, HostSessionInput(prompt="hello", host_source_id="benchmark")
            )
        await app.drain_events()
        snapshot = app.store.load_runtime_session(session.session_id)
        content = [
            {key: message.model_dump(mode="json")[key] for key in ("role", "content")}
            for message in snapshot.messages
        ]
        assert result.run.status == "completed"
        assert catalog.lease_count == 0
        return {
            "scenario": "prepared_decode",
            "backend": backend,
            "constraint_decodes": decode.call_count,
            "run_ms": samples,
            "status": result.run.status,
            "messages_digest": hashlib.sha256(
                json.dumps(content, sort_keys=True).encode()
            ).hexdigest(),
            "leases_after_run": catalog.lease_count,
        }
    finally:
        await app.close()


async def measure(args):
    sys.path.insert(0, str(args.source.resolve()))
    print(
        json.dumps(
            {
                "label": args.label,
                "platform": platform.platform(),
                "python": platform.python_version(),
            }
        ),
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="ouro-run-resources-") as directory:
        root = Path(directory)
        for repeat in range(args.repeat):
            for count in args.counts:
                for scenario in ("terminal", "wait_resume", "lease_transfer", "close_waiting"):
                    for memory in (False, True):
                        report = await resource_probe(
                            root / f"{repeat}-{count}-{scenario}-{memory}",
                            count,
                            scenario,
                            memory=memory,
                        )
                        print(json.dumps({"repeat": repeat, **report}), flush=True)
            for backend in ("files", "sqlite"):
                report = await prepared_probe(root / f"prepared-{repeat}-{backend}", backend)
                print(json.dumps({"repeat": repeat, **report}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--counts", type=int, nargs="+", default=[100, 1000])
    args = parser.parse_args()
    if args.repeat < 1 or any(count < 1 for count in args.counts):
        parser.error("repeat and counts must be positive")
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
