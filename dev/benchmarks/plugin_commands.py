"""Offline plugin-command acceptance and TUI responsiveness measurement.

Only disposable SDK plugins,
local stores and a synthetic streaming provider are used.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sys
import tempfile
import time
from pathlib import Path


def install_plugin(app, directory):
    source = directory / "plugin"
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 3,
        "plugin_id": "dev.benchmark.commands",
        "name": "Synthetic commands",
        "version": "1.0.0",
        "namespace": "command_probe",
        "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^3.0.0"},
        "runtime": {"kind": "python-inprocess", "entrypoint": "command_probe_plugin:create_plugin"},
        "contributes": {
            "commands": [
                {"id": "query", "description": "Read only"},
                {"id": "inspect", "description": "Required Session", "requires_session": True},
                {"id": "defer", "description": "Inputs create Session"},
            ]
        },
        "permissions": [
            {"id": permission, "reason": "Synthetic command benchmark."}
            for permission in ("sessions.write", "background.run")
        ],
        "configuration": {"schema": "./configuration.schema.json", "version": 1},
        "dependencies": {"plugins": []},
        "metadata": {},
    }
    (manifest_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "configuration.schema.json").write_text(
        '{"type":"object","properties":{},"additionalProperties":false}',
        encoding="utf-8",
    )
    (source / "command_probe_plugin.py").write_text(
        """from ouro.sdk.plugins import v3 as sdk

class Plugin:
    async def start(self): pass
    async def stop(self, reason, deadline): pass
    async def prepare(self):
        async def query(call): return sdk.CommandOutput(output='query result')
        async def inputs(call):
            return sdk.CommandOutput(output='accepted', session_input_batches=(
                sdk.SessionInputBatch(inputs=(
                    sdk.SessionInput(prompt='Synthetic scaffold', scaffold=True,
                                     semantic_tag='probe'),
                    sdk.SessionInput(prompt='Inspect evidence'),
                )),
            ))
        return sdk.ContributionSet(commands=[
            sdk.CommandContribution(id='query', description='Read only', handler=query),
            sdk.CommandContribution(id='inspect', description='Required Session', handler=inputs),
            sdk.CommandContribution(id='defer', description='Inputs create Session',
                                    handler=inputs),
        ])

def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    result = app.plugins.install(source)
    assert result.status == "disabled", result


async def run_one(backend, mode, scenario, delay_ms):
    from dev.benchmarks.session_pipeline import Probe, application
    from ouro.application.plugin_commands import consume_plugin_command, submit_plugin_command
    from ouro.core.messages import MessageRecord
    from ouro.core.state import SessionRecord
    from ouro.tui.app import OuroTuiApp
    from ouro.tui.widgets import _BUSY_ACTIVITIES, ActivityIndicator

    with tempfile.TemporaryDirectory(prefix="ouro-command-probe-") as tmp:
        root = Path(tmp)
        app = await asyncio.to_thread(application, root / "home", backend)
        await asyncio.to_thread(install_plugin, app, root)
        session_id = None
        if scenario == "existing":
            session = SessionRecord(title="History", current_model="faux/faux")
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
            session_id = session.session_id
        selector = {
            "query": "query",
            "required": "inspect",
            "deferred": "defer",
            "existing": "inspect",
        }[scenario]
        probe = None
        tasks = []
        first_event_ms = None
        submitted_ms = None
        started = 0.0
        original_tick, original_activity = ActivityIndicator._tick, ActivityIndicator.set_activity

        def timed_tick(indicator):
            if probe is not None and probe.running:
                now = time.perf_counter()
                if probe.spinner_last is not None:
                    probe.spinner_ms.append((now - probe.spinner_last) * 1000)
                probe.spinner_last = now
            return original_tick(indicator)

        def activity(indicator, value, **kwargs):
            if probe is not None and value not in _BUSY_ACTIVITIES:
                probe.spinner_last = None
            return original_activity(indicator, value, **kwargs)

        async def event_seen(event):
            nonlocal first_event_ms
            if first_event_ms is None and event.event_type == "model_text_delta":
                first_event_ms = (time.perf_counter() - started) * 1000

        app.runtime.add_event_sink(event_seen)
        try:
            if mode == "tui":
                ActivityIndicator._tick = timed_tick
                ActivityIndicator.set_activity = activity
                tui = OuroTuiApp(application=app, close_application=False)
                async with tui.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await tui.workers.wait_for_complete(
                        [worker for worker in tui.workers if worker.name == "tui-start"]
                    )
                    enabled = await app.plugins.enable("dev.benchmark.commands")
                    assert enabled.status == "active", (enabled, app.plugins.diagnostics())
                    if session_id is not None:
                        await tui._open_session(session_id)
                    probe = Probe(app.runtime.store, delay_ms)
                    probe.phase = "run"
                    tasks = [
                        asyncio.create_task(probe.tick()),
                        asyncio.create_task(probe.keys(tui)),
                    ]
                    await asyncio.sleep(0.01)
                    started = time.perf_counter()
                    await tui._run_plugin_command(f"/{selector} command title").wait()
                    await app.runtime.drain_events()
                    await tui._flush_pending_runtime_events()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    session_id = tui._controller.session_id
                    await asyncio.sleep(0.2)
                    probe.running = False
                    await asyncio.gather(*tasks)
                    probe.restore()
                    await pilot.pause()
            else:
                await app.start()
                enabled = await app.plugins.enable("dev.benchmark.commands")
                assert enabled.status == "active", (enabled, app.plugins.diagnostics())
                probe = Probe(app.runtime.store, delay_ms)
                probe.phase = "run"
                tasks = [asyncio.create_task(probe.tick())]
                await asyncio.sleep(0.01)
                started = time.perf_counter()
                submission = await submit_plugin_command(
                    app.plugins,
                    app.runtime,
                    selector,
                    "command title",
                    session_id=session_id,
                )
                assert submission is not None
                session_id = submission.session_id
                submitted_ms = (time.perf_counter() - started) * 1000
                async for _result in consume_plugin_command(app.runtime, submission):
                    pass
                await app.runtime.drain_events()
                elapsed_ms = (time.perf_counter() - started) * 1000
                probe.running = False
                await asyncio.gather(*tasks)
                probe.restore()
            if session_id is None:
                assert scenario == "query"
                assert not app.runtime.sessions.catalog().entries
                canonical = []
                statuses = []
            else:
                snapshot = await asyncio.to_thread(app.runtime.store.load_session, session_id)
                canonical = [
                    (m.role, m.text_content, m.scaffold, m.semantic_tag) for m in snapshot.messages
                ]
                runs = await asyncio.to_thread(app.runtime.store.load_runs, session_id)
                statuses = [run.status for run in runs]
                assert statuses and all(status == "completed" for status in statuses), statuses
                assert [m.text_content for m in snapshot.messages if m.semantic_tag == "probe"] == [
                    "Synthetic scaffold"
                ]
            return {
                "backend": backend,
                "mode": mode,
                "scenario": scenario,
                "elapsed_ms": round(elapsed_ms, 3),
                "submission_ms": round(submitted_ms, 3) if submitted_ms is not None else None,
                "first_text_ms": round(first_event_ms, 3) if first_event_ms is not None else None,
                "message_count": len(canonical),
                "statuses": statuses,
                "digest": hashlib.sha256(
                    json.dumps(canonical, ensure_ascii=False).encode()
                ).hexdigest(),
                **probe.report(),
            }
        finally:
            ActivityIndicator._tick, ActivityIndicator.set_activity = (
                original_tick,
                original_activity,
            )
            if probe is not None:
                probe.running = False
                await asyncio.gather(*tasks, return_exceptions=True)
                probe.restore()
            await app.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--label", default="working")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--backend", choices=["files", "sqlite", "all"], default="all")
    parser.add_argument("--mode", choices=["application", "tui", "all"], default="all")
    parser.add_argument(
        "--scenario", choices=["query", "required", "deferred", "existing", "all"], default="all"
    )
    parser.add_argument("--store-delay-ms", type=float, default=0)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    print(
        json.dumps(
            {
                "label": args.label,
                "source": str(args.source.resolve()),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "delay_ms": args.store_delay_ms,
            }
        ),
        flush=True,
    )
    for _ in range(args.repeat):
        for backend in ("files", "sqlite") if args.backend == "all" else (args.backend,):
            for mode in ("application", "tui") if args.mode == "all" else (args.mode,):
                for scenario in (
                    ("query", "required", "deferred", "existing")
                    if args.scenario == "all"
                    else (args.scenario,)
                ):
                    print(
                        json.dumps(await run_one(backend, mode, scenario, args.store_delay_ms)),
                        flush=True,
                    )


if __name__ == "__main__":
    asyncio.run(main())
