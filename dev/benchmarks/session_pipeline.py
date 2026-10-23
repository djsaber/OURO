"""Offline session/TUI responsiveness probe using only disposable synthetic data.

Run: uv run python -m dev.benchmarks.session_pipeline --repeat 3
Timing distributions are diagnostic, not machine-independent CI assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import inspect
import json
import platform
import statistics
import tempfile
import threading
import time
import traceback
from collections import Counter
from functools import wraps
from pathlib import Path

from textual import events

from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.application.observations.activity import queued_runtime_activity, runtime_activity_changed
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.widgets.activity import _BUSY_ACTIVITIES, ActivityIndicator


class ScriptedProvider:
    provider_id = "faux"
    deltas_per_round = 40
    delta_interval_ms = 5.0

    def __init__(self) -> None:
        self.round = 0

    async def stream(self, **_):
        self.round += 1
        for index in range(self.deltas_per_round):
            await asyncio.sleep(self.delta_interval_ms / 1000)
            yield ProviderStreamEvent(type="text_delta", text=f"Evidence {index}. " * 8 + "\n\n")
        if self.round % 2:
            yield ProviderStreamEvent.from_tool_call(
                {"id": f"call_{self.round}", "name": "Evidence", "arguments": {}}
            )
        yield ProviderStreamEvent(type="done")


def distribution(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[int((len(ordered) - 1) * 0.95)], 3),
        "p99_ms": round(ordered[int((len(ordered) - 1) * 0.99)], 3),
        "max_ms": round(max(ordered), 3),
    }


class Probe:
    def __init__(self, store, delay_ms):
        self.loop_thread = threading.get_ident()
        self.store_calls = Counter()
        self.loop_store_ms = []
        self.loop_lag_ms = []
        self.key_ms = []
        self.spinner_ms = []
        self.spinner_last = None
        self.phase = "open"
        self.phase_lag = {"open": [], "run": []}
        self.render_ms = []
        self.slow_render_stacks = []
        self.slow_callbacks = []
        self.gc_ms = []
        self.gc_started = 0.0
        self.gc_stack = []
        self.gc_collections = []
        self.running = True
        self.originals = {}
        for name in dir(store):
            if name.startswith("_") or not callable(operation := getattr(store, name)):
                continue
            self.originals[name] = operation
            setattr(store, name, self.wrap(name, operation, delay_ms))
        self.store = store
        gc.callbacks.append(self.gc_callback)
        self.handle_run = asyncio.Handle._run

        def timed_callback(handle):
            callback = handle._callback
            start = time.perf_counter()
            try:
                return self.handle_run(handle)
            finally:
                duration = (time.perf_counter() - start) * 1000
                if duration > 16:
                    # Inspect only slow callbacks: materializing frame locals
                    # on every tick would itself distort allocation and GC.
                    owner = getattr(callback, "__self__", None)
                    name = (
                        owner.get_coro().__qualname__
                        if isinstance(owner, asyncio.Task)
                        else getattr(callback, "__qualname__", type(callback).__name__)
                    )
                    self.slow_callbacks.append({"callback": name, "ms": round(duration, 3)})

        asyncio.Handle._run = timed_callback

    def wrap(self, name, operation, delay_ms):
        @wraps(operation)
        def call(*args, **kwargs):
            on_loop = threading.get_ident() == self.loop_thread
            started = time.perf_counter()
            if delay_ms and name.startswith(("load_", "save_", "append_", "latest_")):
                time.sleep(delay_ms / 1000)
            try:
                return operation(*args, **kwargs)
            finally:
                if on_loop:
                    self.store_calls[name] += 1
                    self.loop_store_ms.append((time.perf_counter() - started) * 1000)

        return call

    async def tick(self):
        while self.running:
            start = time.perf_counter()
            await asyncio.sleep(0.001)
            lag = max(0, (time.perf_counter() - start) * 1000 - 1)
            self.loop_lag_ms.append(lag)
            self.phase_lag[self.phase].append(lag)

    async def keys(self, app):
        composer = app.query_one(Composer)
        while self.running:
            await asyncio.sleep(0.03)
            if composer.disabled:
                continue
            composer.focus()
            previous = composer.text
            start = time.perf_counter()
            app.post_message(events.Key("x", "x"))
            async with asyncio.timeout(5):
                while composer.text == previous and self.running:
                    await asyncio.sleep(0.001)
            if composer.text != previous:
                self.key_ms.append((time.perf_counter() - start) * 1000)

    def gc_callback(self, phase, info):
        if phase == "start":
            self.gc_started = time.perf_counter()
            self.gc_stack = (
                [f"{frame.name}:{frame.lineno}" for frame in traceback.extract_stack(limit=12)]
                if info["generation"] == 2
                else []
            )
        else:
            elapsed = (time.perf_counter() - self.gc_started) * 1000
            self.gc_ms.append(elapsed)
            if elapsed > 16:
                self.gc_collections.append(
                    {**info, "ms": round(elapsed, 3), "stack": self.gc_stack}
                )

    def restore(self):
        if self.gc_callback in gc.callbacks:
            gc.callbacks.remove(self.gc_callback)
        asyncio.Handle._run = self.handle_run
        for name, original in self.originals.items():
            setattr(self.store, name, original)

    def report(self):
        return {
            "loop_lag": distribution(self.loop_lag_ms),
            "key_to_composer": distribution(self.key_ms),
            "spinner_tick_gap": distribution(self.spinner_ms),
            "store_on_loop": dict(self.store_calls),
            "store_on_loop_duration": distribution(self.loop_store_ms),
            "phase_loop_lag": {
                phase: distribution(values) for phase, values in self.phase_lag.items()
            },
            "sync_history_render": distribution(self.render_ms),
            "slow_render_stacks": self.slow_render_stacks,
            "slow_callbacks": self.slow_callbacks,
            "gc": distribution(self.gc_ms),
            "slow_gc_collections": self.gc_collections,
        }


def application(home, backend):
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    store.initialize()
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Evidence", description="Synthetic evidence", effect="read_only"
            ),
            handler=lambda invocation, context: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="evidence\n" * 4096,
            ),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            ScriptedProvider(), ModelMetadata(model_id="faux", provider_id="faux")
        ),
        tools=registry,
        default_workspace=home,
        environment=RuntimeEnvironment.local(home),
    )
    return LocalApplication.from_runtime(runtime)


async def run_one(backend, mode, delay_ms):
    with tempfile.TemporaryDirectory(prefix="ouro-performance-") as tmp:
        home = Path(tmp)
        app = await asyncio.to_thread(application, home, backend)
        session = SessionRecord(title="Synthetic history", current_model="faux/faux")
        messages = [
            MessageRecord.text(
                session_id=session.session_id,
                sequence=index + 1,
                role="user" if index % 2 == 0 else "assistant",
                text="Historical evidence. " * 40,
            )
            for index in range(120)
        ]
        await asyncio.to_thread(app.runtime.store.create_session_snapshot, session, messages)
        probe = None
        tasks = []
        render = ConversationHistory._render_entries
        spinner_tick = ActivityIndicator._tick
        spinner_activity = ActivityIndicator.set_activity
        try:
            if mode == "tui":

                def timed_spinner(indicator):
                    if probe is not None and probe.running:
                        now = time.perf_counter()
                        if probe.spinner_last is not None:
                            probe.spinner_ms.append((now - probe.spinner_last) * 1000)
                        probe.spinner_last = now
                    return spinner_tick(indicator)

                def set_spinner_activity(indicator, activity, **kwargs):
                    if probe is not None and activity not in _BUSY_ACTIVITIES:
                        probe.spinner_last = None
                    return spinner_activity(indicator, activity, **kwargs)

                ActivityIndicator._tick = timed_spinner
                ActivityIndicator.set_activity = set_spinner_activity
                tui = OuroTuiApp(application=app, close_application=False)
                async with tui.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await tui.workers.wait_for_complete(
                        [worker for worker in tui.workers if worker.name == "tui-start"]
                    )
                    probe = Probe(app.runtime.store, delay_ms)
                    tasks = [asyncio.create_task(probe.tick())]

                    def timed_render(history):
                        start = time.perf_counter()
                        try:
                            return render(history)
                        finally:
                            duration = (time.perf_counter() - start) * 1000
                            probe.render_ms.append(duration)
                            if duration > 8:
                                probe.slow_render_stacks.append(
                                    [
                                        f"{frame.name}:{frame.lineno}"
                                        for frame in traceback.extract_stack(limit=8)
                                    ]
                                )

                    ConversationHistory._render_entries = timed_render
                    await asyncio.sleep(0.01)
                    await tui._open_session(session.session_id)
                    probe.phase = "run"
                    previous_workers = set(tui.workers)
                    composer = tui.query_one(Composer)
                    composer.focus()
                    composer.load_text("Inspect evidence")
                    await pilot.press("enter")
                    worker = next(
                        worker
                        for worker in tui.workers
                        if worker.group == "run" and worker not in previous_workers
                    )
                    tasks.append(asyncio.create_task(probe.keys(tui)))
                    async with asyncio.timeout(60):
                        await worker.wait()
                    await tui._flush_pending_runtime_events()
                    await asyncio.sleep(0.2)
                    assert tui._projection.activity.status == "completed"
                    probe.running = False
                    await asyncio.gather(*tasks)
                    await pilot.pause()
                    probe.restore()
                    ConversationHistory._render_entries = render
            else:
                await app.start()
                probe = Probe(app.runtime.store, delay_ms)
                tasks = [asyncio.create_task(probe.tick())]
                await asyncio.sleep(0.01)
                subscription = app.supervisor.subscribe(session.session_id)
                if inspect.isawaitable(subscription):
                    subscription = await subscription
                handle = await app.supervisor.submit(
                    "Inspect evidence", session_id=session.session_id
                )
                probe.phase = "run"
                result = await app.supervisor.wait(handle)
                assert result.status == "completed"
                subscription.close()
                await app.runtime.drain_events()
                probe.running = False
                await asyncio.gather(*tasks)
                probe.restore()
            return {"backend": backend, "mode": mode, **probe.report()}
        finally:
            ConversationHistory._render_entries = render
            ActivityIndicator._tick = spinner_tick
            ActivityIndicator.set_activity = spinner_activity
            if probe is not None:
                probe.running = False
                await asyncio.gather(*tasks, return_exceptions=True)
                probe.restore()
            await app.close()


def activity_probe():
    tools = tuple(
        RuntimeToolCallActivity(
            tool_call_id=f"call_{index}",
            tool_name="Evidence",
            status="completed",
            source_index=index,
            result_data={"rows": [{"line": row, "text": "content " * 8} for row in range(5000)]},
        )
        for index in range(4)
    )
    before = queued_runtime_activity("synthetic").model_copy(update={"tool_calls": tools})
    after = before.model_copy(update={"sequence": 2})
    durations = []
    for _ in range(30):
        start = time.perf_counter()
        assert not runtime_activity_changed(before, after)
        durations.append((time.perf_counter() - start) * 1000)
    return {"mode": "activity", "comparison": distribution(durations)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--mode", choices=["runtime", "tui", "activity", "all"], default="all")
    parser.add_argument("--backend", choices=["files", "sqlite", "all"], default="all")
    parser.add_argument("--store-delay-ms", type=float, default=0)
    parser.add_argument("--deltas-per-round", type=int, default=40)
    parser.add_argument("--delta-interval-ms", type=float, default=5)
    args = parser.parse_args()
    if args.deltas_per_round < 1:
        parser.error("--deltas-per-round must be positive")
    ScriptedProvider.deltas_per_round = args.deltas_per_round
    if args.delta_interval_ms < 0:
        parser.error("--delta-interval-ms must be nonnegative")
    ScriptedProvider.delta_interval_ms = args.delta_interval_ms
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "configuration": vars(args),
            }
        ),
        flush=True,
    )
    for _ in range(args.repeat):
        if args.mode in {"activity", "all"}:
            print(json.dumps(activity_probe()), flush=True)
        if args.mode == "activity":
            continue
        for mode in ("runtime", "tui") if args.mode == "all" else (args.mode,):
            for backend in ("files", "sqlite") if args.backend == "all" else (args.backend,):
                print(json.dumps(await run_one(backend, mode, args.store_delay_ms)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
