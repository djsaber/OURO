"""Measure cold/repeated/live Session selection with disposable data and a held Provider.

Use --root to run the same probe against an extracted baseline checkout.
Measurements are headless; they exclude actual terminal paint and keyboard transport.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import platform
import sys
import tempfile
from collections import Counter
from functools import wraps
from pathlib import Path
from time import perf_counter


def process_resources():
    """Windows process counters; transfer bytes include cached IO, not physical disk reads."""
    if sys.platform != "win32":
        return {}

    class IOCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("faults", ctypes.c_ulong),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working_set",
                    "working_set",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ],
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessIoCounters.argtypes = [ctypes.c_void_p, ctypes.POINTER(IOCounters)]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(MemoryCounters),
        ctypes.c_ulong,
    ]
    process = kernel.GetCurrentProcess()
    io, memory = IOCounters(), MemoryCounters()
    memory.cb = ctypes.sizeof(memory)
    if not kernel.GetProcessIoCounters(process, ctypes.byref(io)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"read_transfer_bytes": io.read_bytes, "working_set_bytes": memory.working_set}


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "p95_ms": round(ordered[int((len(ordered) - 1) * 0.95)], 3),
        "p99_ms": round(ordered[int((len(ordered) - 1) * 0.99)], 3),
        "max_ms": round(ordered[-1], 3),
    }


async def measure(backend, count, home):
    # Imports follow --root selection so the probe is identical across checkouts.
    from ouro.application.runtime import RuntimeService
    from ouro.application.runtime_environment import RuntimeEnvironment
    from ouro.core.adapter import RuntimeAdapter
    from ouro.core.messages import MessageRecord
    from ouro.core.provider import ModelMetadata, ProviderStreamEvent
    from ouro.core.state import SessionRecord
    from ouro.local_application import LocalApplication
    from ouro.sessions import LocalSessionStore, SQLiteSessionStore
    from ouro.tools import ToolRegistry
    from ouro.tui import conversation_history as history_module
    from ouro.tui.app import OuroTuiApp
    from ouro.tui.conversation_history import ConversationHistory
    from ouro.tui.state import TuiSessionProjection

    class Provider:
        provider_id = "faux"

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.purposes = []

        async def stream(self, **kwargs):
            purpose = kwargs["request_context"].values["request.purpose"]
            self.purposes.append(purpose)
            assert purpose == "chat", f"unexpected measured request: {purpose}"
            yield ProviderStreamEvent(type="text_delta", text="Uncommitted streaming content. ")
            self.started.set()
            await self.release.wait()
            yield ProviderStreamEvent(type="text_delta", text="Completed.")
            yield ProviderStreamEvent(type="done")

    store = LocalSessionStore(home) if backend == "files" else SQLiteSessionStore(home / "db")
    store.initialize()
    provider = Provider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id="faux", context_window=2_000_000),
        ),
        tools=ToolRegistry(),
        default_workspace=home,
        environment=RuntimeEnvironment.local(home),
    )
    application = LocalApplication.from_runtime(runtime)
    sessions = []
    for index in range(2):
        session = SessionRecord(title=f"Synthetic {index}", current_model="faux/faux")
        store.create_session_snapshot(
            session,
            [
                MessageRecord.text(
                    session_id=session.session_id,
                    sequence=position + 1,
                    role="user" if position % 2 == 0 else "assistant",
                    text="Synthetic **observation** evidence. " * 8,
                )
                for position in range(count)
            ],
        )
        sessions.append(session.session_id)
    calls = Counter()
    originals = {}
    history_messages_read = 0
    reduced_events = 0

    def instrument(name, operation):
        @wraps(operation)
        def read(*args, **kwargs):
            nonlocal history_messages_read
            calls[name] += 1
            result = operation(*args, **kwargs)
            if name == "load_session_observation":
                history_messages_read += len(result.messages)
            return result

        return read

    if hasattr(store, "iter_messages_before"):
        iter_messages = store.iter_messages_before
        originals["iter_messages_before"] = iter_messages

        def read_history(*args, **kwargs):
            nonlocal history_messages_read
            calls["iter_messages_before"] += 1
            records = iter_messages(*args, **kwargs)
            try:
                for item in records:
                    history_messages_read += 1
                    yield item
            finally:
                records.close()

        store.iter_messages_before = read_history

    apply_event = TuiSessionProjection.apply

    def counted_apply(*args, **kwargs):
        nonlocal reduced_events
        reduced_events += 1
        return apply_event(*args, **kwargs)

    TuiSessionProjection.apply = counted_apply

    rendered = 0
    render_ms = 0.0
    write_entries = ConversationHistory._write_entries
    render_batch = history_module._render_entry_requests

    def timed_render(operation, entry_count):
        @wraps(operation)
        def render(*args, **kwargs):
            nonlocal rendered, render_ms
            rendered += entry_count(args)
            before = perf_counter()
            try:
                return operation(*args, **kwargs)
            finally:
                render_ms += (perf_counter() - before) * 1000

        return render

    for name in dir(store):
        if name.startswith(("load_", "latest_")) and callable(operation := getattr(store, name)):
            originals[name] = operation
            setattr(store, name, instrument(name, operation))
    ConversationHistory._write_entries = timed_render(write_entries, lambda args: args[2] - args[1])
    history_module._render_entry_requests = timed_render(render_batch, lambda args: len(args[0]))
    try:
        async with application:
            app = OuroTuiApp(application=application, close_application=False)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete(
                    [worker for worker in app.workers if worker.name == "tui-start"]
                )
                stage_ms = Counter()

                def timed_stage(name, operation):
                    @wraps(operation)
                    async def call(*args, **kwargs):
                        before = perf_counter()
                        try:
                            return await operation(*args, **kwargs)
                        finally:
                            stage_ms[name] += (perf_counter() - before) * 1000

                    return call

                for owner, names in (
                    (
                        app._controller,
                        (
                            "prepare_session_selection",
                            "observe_session",
                            "load_observation_snapshot",
                        ),
                    ),
                    (
                        app,
                        (
                            "_install_session_surface",
                            "_replace_agent_tree",
                            "_restore_projection",
                            "_refresh_approval_indicator",
                            "_refresh_reasoning_indicator",
                        ),
                    ),
                ):
                    for name in names:
                        operation = getattr(owner, name, None)
                        if operation is not None:
                            setattr(owner, name, timed_stage(name, operation))

                async def select(label, target):
                    nonlocal rendered, render_ms, history_messages_read, reduced_events
                    calls.clear()
                    stage_ms.clear()
                    rendered = 0
                    render_ms = 0.0
                    history_messages_read = reduced_events = 0
                    lag = []
                    resources_before = process_resources()
                    start = perf_counter()
                    stop = asyncio.Event()

                    async def sample_loop():
                        while not stop.is_set():
                            before = perf_counter()
                            await asyncio.sleep(0.005)
                            lag.append(max(0, (perf_counter() - before) * 1000 - 5))

                    async def first_content_frame():
                        while True:
                            projection = app._projection
                            if (
                                projection is not None
                                and projection.session_id == target
                                and app.query_one(ConversationHistory).lines
                            ):
                                await app.screen.wait_for_refresh()
                                return (perf_counter() - start) * 1000
                            await asyncio.sleep(0.005)

                    sampling = asyncio.create_task(sample_loop())
                    first_frame = asyncio.create_task(first_content_frame())
                    try:
                        await app._open_session(target)
                        content_ms = (perf_counter() - start) * 1000
                        await app.query_one(ConversationHistory).wait_until_rendered()
                        await app.query_one(ConversationHistory).wait_for_theme_refresh()
                        await app.screen.wait_for_refresh()
                        first_frame_ms = await asyncio.wait_for(first_frame, 10)
                        elapsed_ms = (perf_counter() - start) * 1000
                    finally:
                        stop.set()
                        first_frame.cancel()
                        await asyncio.gather(sampling, first_frame, return_exceptions=True)
                    assert app.query_one(ConversationHistory).lines
                    resources = process_resources()
                    if resources:
                        resources["read_transfer_bytes"] -= resources_before["read_transfer_bytes"]
                    return {
                        "phase": label,
                        "elapsed_ms": round(elapsed_ms, 3),
                        "content_commit_ms": round(content_ms, 3),
                        "first_content_frame_ms": round(first_frame_ms, 3),
                        "rendered_entries": rendered,
                        "visible_entries": len(app.query_one(ConversationHistory).entries),
                        "history_render_ms": round(render_ms, 3),
                        "store_queries": dict(calls),
                        "event_sources": len(application.supervisor._event_streams),
                        "subscriptions": sum(
                            len(items) for items in application.supervisor._subscriptions.values()
                        ),
                        "async_tasks": len(asyncio.all_tasks()),
                        "history_cache_entries": len(app._session_history_states),
                        "history_messages_read": history_messages_read,
                        "reduced_events": reduced_events,
                        "loop_lag": distribution(lag),
                        "process": resources,
                        "stages_ms": {key: round(value, 3) for key, value in stage_ms.items()},
                    }

                samples = [await select("cold", sessions[0])]
                await select("other", sessions[1])
                samples.append(await select("reenter", sessions[0]))
                handle = await application.supervisor.submit("Observe", session_id=sessions[0])
                await asyncio.wait_for(provider.started.wait(), 10)
                await select("background", sessions[1])
                samples.append(await select("streaming_reenter", sessions[0]))
                assert app._projection.assistant_text == "Uncommitted streaming content. "
                provider.release.set()
                await application.supervisor.wait(handle)
                return {
                    "backend": backend,
                    "messages": count,
                    "request_purposes": provider.purposes,
                    "samples": samples,
                }
    finally:
        provider.release.set()
        await application.close()
        ConversationHistory._write_entries = write_entries
        history_module._render_entry_requests = render_batch
        TuiSessionProjection.apply = apply_event
        for name, original in originals.items():
            setattr(store, name, original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--backend", choices=("files", "sqlite", "all"), default="all")
    parser.add_argument("--messages", type=int, nargs="+", default=[120])
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve()))
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "root": str(args.root.resolve()),
                "repeat": args.repeat,
                "context_window": 2_000_000,
            }
        ),
        flush=True,
    )
    for _ in range(args.repeat):
        for backend in ("files", "sqlite") if args.backend == "all" else (args.backend,):
            for count in args.messages:
                with tempfile.TemporaryDirectory(prefix="ouro-observation-") as directory:
                    result = asyncio.run(measure(backend, count, Path(directory)))
                    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
