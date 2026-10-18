"""Measure three running Sessions, two observers, and repeated visible-session changes."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import tempfile
from collections import Counter
from functools import wraps
from pathlib import Path
from time import perf_counter

from dev.benchmarks.session_observation import distribution, process_resources
from dev.benchmarks.session_pipeline import Probe
from ouro.application.agent_tree import AgentTreeProjector
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from ouro.tui import conversation_history as history_module
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory


class Provider:
    provider_id = "faux"

    def __init__(self):
        self.queues = {}
        self.started = asyncio.Queue()

    async def stream(self, *, request_context, **kwargs):
        session_id = request_context.values["session.id"]
        queue = self.queues[session_id] = asyncio.Queue()
        yield ProviderStreamEvent(type="text_delta", text="Initial shared content.\n\n")
        self.started.put_nowait(session_id)
        while True:
            text = await queue.get()
            if text is None:
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text=text)


def application(home, backend, provider):
    store = LocalSessionStore(home) if backend == "files" else SQLiteSessionStore(home / "db")
    store.initialize()
    return LocalApplication.from_runtime(
        RuntimeService(
            store=store,
            adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
            tools=ToolRegistry(),
            default_workspace=home,
            environment=RuntimeEnvironment.local(home),
        )
    )


async def ready(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete([w for w in app.workers if w.name == "tui-start"])


async def measure(home, backend, cycles):
    provider = Provider()
    owner, viewer = (application(home, backend, provider) for _ in range(2))
    first = OuroTuiApp(application=owner, close_application=False)
    second = OuroTuiApp(application=viewer, close_application=False)
    renders, trees = Counter(), []
    original_render = history_module._render_entry_requests
    original_write = ConversationHistory._write_entries
    original_tree = AgentTreeProjector.snapshot

    def render(requests, **kwargs):
        renders.update(item.entry.run_id for item in requests)
        return original_render(requests, **kwargs)

    def write(history, start, stop):
        renders.update(entry.run_id for entry in history.entries[start:stop])
        return original_write(history, start, stop)

    def tree(projector, session_id):
        start = perf_counter()
        result = original_tree(projector, session_id)
        trees.append((perf_counter() - start) * 1000)
        return result

    history_module._render_entry_requests = render
    ConversationHistory._write_entries = write
    AgentTreeProjector.snapshot = tree
    producer = None
    probe = None
    probe_tasks = []
    try:
        async with owner, viewer:
            sessions = [
                owner.runtime.sessions.create(title=f"Background {i}").session for i in range(3)
            ]
            handles = [
                await owner.supervisor.submit("Observe", session_id=session.session_id)
                for session in sessions
            ]
            for _ in sessions:
                await asyncio.wait_for(provider.started.get(), 10)
            ids = [item.session_id for item in sessions]
            runs = [owner.supervisor.status(identity).run_id for identity in ids]
            async with (
                first.run_test(size=(120, 40)) as first_pilot,
                second.run_test(size=(120, 40)) as second_pilot,
            ):
                await ready(first, first_pilot)
                await ready(second, second_pilot)
                await first._open_session(ids[0])
                await second._open_session(ids[0])
                await first_pilot.pause()
                await second_pilot.pause()
                renders.clear()
                tree_start = len(trees)
                for index in range(20):
                    for queue in provider.queues.values():
                        queue.put_nowait(f"Shared **delta** {index}.\n\n")
                    await asyncio.sleep(0.02)
                async with asyncio.timeout(10):
                    while "delta** 19" not in second._projection.assistant_text:
                        await asyncio.sleep(0.005)
                for app in (first, second):
                    await app.query_one(ConversationHistory).wait_until_rendered()
                hidden_renders = {identity: renders[identity] for identity in runs[1:]}
                assert not any(hidden_renders.values())
                text_tree_queries = len(trees) - tree_start
                probe = Probe(owner.runtime.store, 0)
                probe.phase = "run"
                probe_tasks = [
                    asyncio.create_task(probe.tick()),
                    asyncio.create_task(probe.keys(first)),
                ]
                stages = Counter()

                def timed(name, operation):
                    @wraps(operation)
                    async def call(*args, **kwargs):
                        start = perf_counter()
                        try:
                            return await operation(*args, **kwargs)
                        finally:
                            stages[name] += (perf_counter() - start) * 1000

                    return call

                for target, names in (
                    (first._controller, ("prepare_session_selection", "observe_session")),
                    (
                        first,
                        ("_install_session_surface", "_restore_projection", "_replace_agent_tree"),
                    ),
                ):
                    for name in names:
                        setattr(target, name, timed(name, getattr(target, name)))

                async def produce():
                    index = 20
                    while True:
                        for queue in provider.queues.values():
                            queue.put_nowait(f"Continuing {index}. ")
                        index += 1
                        await asyncio.sleep(0.02)

                producer = asyncio.create_task(produce())
                samples = []
                for cycle in range(cycles):
                    latency = []
                    phase_samples = []
                    for session_id in (*ids[1:], ids[0]):
                        stages.clear()
                        started = perf_counter()
                        await first._open_session(session_id)
                        await first.query_one(ConversationHistory).wait_until_rendered()
                        await first.screen.wait_for_refresh()
                        latency.append((perf_counter() - started) * 1000)
                        phase_samples.append(
                            {name: round(value, 3) for name, value in stages.items()}
                        )
                    await asyncio.sleep(0.05)
                    samples.append(
                        {
                            "cycle": cycle + 1,
                            "switch_ms": [round(value, 3) for value in latency],
                            "stages_ms": phase_samples,
                            "tasks": len(asyncio.all_tasks()),
                            "sources": [
                                len(app.supervisor._event_streams) for app in (owner, viewer)
                            ],
                            "subscriptions": [
                                sum(len(items) for items in app.supervisor._subscriptions.values())
                                for app in (owner, viewer)
                            ],
                            "history_caches": [
                                len(app._session_history_states) for app in (first, second)
                            ],
                            "process": process_resources(),
                        }
                    )
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                for queue in provider.queues.values():
                    queue.put_nowait(None)
                await asyncio.gather(*(owner.supervisor.wait(handle) for handle in handles))
                async with asyncio.timeout(15):
                    while second._projection.activity.status != "completed":
                        await asyncio.sleep(0.005)
                assert not viewer.supervisor._jobs_by_session
                probe.running = False
                await asyncio.gather(*probe_tasks)
                probe.restore()
            await asyncio.gather(
                *owner.supervisor._event_stream_tasks, *viewer.supervisor._event_stream_tasks
            )
            return {
                "backend": backend,
                "cycles": samples,
                "hidden_run_render_counts": hidden_renders,
                "tree_queries_during_text_phase": text_tree_queries,
                "tree_query_count": len(trees),
                "tree_query_duration": distribution(trees),
                "responsiveness": probe.report(),
                "sources_after_detach": [
                    len(app.supervisor._event_streams) for app in (owner, viewer)
                ],
                "subscriptions_after_detach": [
                    len(app.supervisor._subscriptions) for app in (owner, viewer)
                ],
            }
    finally:
        if probe is not None:
            probe.running = False
            await asyncio.gather(*probe_tasks, return_exceptions=True)
            probe.restore()
        if producer is not None:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        for queue in provider.queues.values():
            queue.put_nowait(None)
        await owner.close()
        await viewer.close()
        history_module._render_entry_requests = original_render
        ConversationHistory._write_entries = original_write
        AgentTreeProjector.snapshot = original_tree


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--backend", choices=("files", "sqlite", "all"), default="all")
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error("--cycles must be positive")
    print(
        json.dumps(
            {"platform": platform.platform(), "python": platform.python_version(), **vars(args)}
        ),
        flush=True,
    )
    for backend in ("files", "sqlite") if args.backend == "all" else (args.backend,):
        with tempfile.TemporaryDirectory(prefix="ouro-collaboration-") as directory:
            print(
                json.dumps(asyncio.run(measure(Path(directory), backend, args.cycles))), flush=True
            )


if __name__ == "__main__":
    main()
