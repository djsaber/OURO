"""Time accepted tool batches through the default loop or the example SDK Driver."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import statistics
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter_ns

import ouro
from ouro.application.plugins.service import PluginService
from ouro.application.runtime.service import RuntimeService
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.tool_policies import ToolPolicyDecision, ToolPolicyRegistration
from ouro.core.tools import ToolCallRequest
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.providers import FauxProvider
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolInputRequest, ToolRegistry, ToolResult

CODE = Path(ouro.__file__).resolve().parents[1]
PLUGIN = "dev.ouro.examples.review_driver"


class Sample:
    def __init__(self, store):
        self.accepted = self.first = self.last_result = self.frame = 0
        self.handlers = self.resumes = self.active = self.peak = self.frames = 0
        self.counts = Counter()
        mutation = store.session_mutation
        commit = store.commit_session
        local = threading.local()

        @contextmanager
        def observe_transaction(session_id):
            outermost = not hasattr(local, "writes")
            if outermost:
                local.writes = []
            try:
                with mutation(session_id) as lock:
                    yield lock
                if outermost and local.writes:
                    self.committed(local.writes)
            finally:
                if outermost:
                    del local.writes

        def observe_write(request):
            result = commit(request)
            facts = (
                bool(request.tool_facts.calls),
                bool(request.tool_facts.outcomes),
                any(step.message and step.message.outcome_ref for step in request.steps),
            )
            if hasattr(local, "writes"):
                local.writes.append(facts)
            else:
                self.committed([facts])
            return result

        store.session_mutation = observe_transaction
        store.commit_session = observe_write
        for name in ("load_tool_call", "load_tool_call_state", "load_tool_outcome"):
            self.count_reads(store, name)

    def committed(self, writes):
        # SQLite can nest several logical writes under the same outer transaction.
        now = perf_counter_ns()
        for accepted, outcome, frame in writes:
            if accepted:
                assert not self.accepted
                self.accepted = now
            if self.accepted and not self.frame:
                self.counts["commits"] += 1
            if outcome:
                self.last_result = now
            if frame:
                self.frames += 1
                self.frame = now

    def count_reads(self, store, name):
        read = getattr(store, name)

        def observe(*args, **kwargs):
            if self.accepted and not self.frame:
                self.counts[name] += 1
            return read(*args, **kwargs)

        setattr(store, name, observe)

    def metrics(self, size, limit):
        assert self.accepted < self.first <= self.last_result <= self.frame
        assert self.frames == 1 and self.handlers == size and 1 <= self.peak <= limit
        duration = self.frame - self.accepted
        return {
            "first_ms": (self.first - self.accepted) / 1e6,
            "publish_ms": (self.frame - self.last_result) / 1e6,
            "batch_ms": duration / 1e6,
            "batches_s": 1e9 / duration,
            "peak": self.peak,
            **self.counts,
        }


async def install_driver(home, tools, store, runtime, size):
    package = home / "source"
    shutil.copytree(CODE / "examples/plugins/review-driver", package)
    entry = package / "review_driver_plugin.py"
    source = entry.read_text(encoding="utf-8")
    start, end = source.index("async def review("), source.index("class ReviewPlugin:")
    entry.write_text(
        source[:start]
        + f"""async def review(context, resume):
    if resume is None:
        batch = await context.tools.execute(
            tuple(sdk.RunToolCall("Probe") for _ in range({size})), checkpoint="finish"
        )
        if batch.suspended:
            return sdk.DriverSuspended()
        results = batch.results
    else:
        assert resume.checkpoint == "finish"
        results = resume.results
    assert len(results) == {size} and all(not item.is_error for item in results)
    return sdk.DriverFinished("done")


"""
        + source[end:],
        encoding="utf-8",
    )
    plugins = PluginRuntimeResource(
        PluginPackageStore(home / "plugins"),
        tools=tools,
        observers=ObserverRegistry(),
        tool_policies=runtime.registries.tool_policies,
    )
    service = PluginService(plugins.store, runtime=plugins, session_store=store)
    runtime.bind_extensions(catalog=plugins)
    service.install(package)
    assert (await service.enable(PLUGIN)).status == "active"
    return plugins


async def trial(home, backend, origin, case, size, limit, delay_ms):
    store = (
        LocalSessionStore(home / "files")
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.db")
    )
    store.initialize()
    sample = Sample(store)

    async def handle(invocation, context):
        if not sample.first:
            sample.first = perf_counter_ns()
        sample.handlers += 1
        sample.active += 1
        sample.peak = max(sample.peak, sample.active)
        try:
            if case == "wait":
                return ToolInputRequest(data={"prompt": "Continue?"})
            if case == "parallel":
                await asyncio.sleep(delay_ms / 1000)
            return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="Probe", content="ok")
        finally:
            sample.active -= 1

    async def resume(invocation, resolution, request, context):
        assert resolution.value == "ok" and request.data == {"prompt": "Continue?"}
        sample.resumes += 1
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="Probe", content="ok")

    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(name="Probe", description="Read", resumable=case == "wait"),
            handler=handle,
            resume_handler=resume if case == "wait" else None,
        )
    )
    calls = [ToolCallRequest(id=f"c{i}", name="Probe", source_index=i) for i in range(size)]
    done = ProviderStreamEvent(type="done")
    provider = FauxProvider(
        event_batches=[
            [*(ProviderStreamEvent.from_tool_call(call) for call in calls), done],
            [ProviderStreamEvent(type="text_delta", text="done"), done],
        ]
    )
    runtime = RuntimeService(
        store=store,
        tools=tools,
        default_workspace=home,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
        tool_execution_config=ToolExecutionConfig(
            mode="parallel" if case in ("parallel", "policy") else "sequential",
            max_concurrency=limit,
        ),
    )
    checks = Counter()
    policy_active = policy_peak = 0
    if case == "policy":

        async def policy(call):
            nonlocal policy_active, policy_peak
            checks[call.tool_call_id] += 1
            if checks[call.tool_call_id] == 2:
                policy_active += 1
                policy_peak = max(policy_active, policy_peak)
                try:
                    await asyncio.sleep(delay_ms / 1000)
                finally:
                    policy_active -= 1
            return ToolPolicyDecision()

        runtime.registries.tool_policies.replace_owner(
            "bench", (ToolPolicyRegistration("bench", "check", policy),)
        )
    plugins = None
    try:
        if origin == "driver":
            plugins = await install_driver(home, tools, store, runtime, size)
        result = await runtime.run(
            "run tools", driver_id=f"{PLUGIN}/run_drivers/review" if plugins else None
        )
        if case == "wait":
            assert result.run.status == "waiting_user", result.run.error
            result = await runtime.answer_user(
                session_id=result.session.session_id, run_id=result.run.run_id, answer="ok"
            )
        assert result.run.status == "completed" and result.final_text == "done", result.run.error
        assert result.run.model_round_count == (0 if plugins else 2)
        assert sample.resumes == int(case == "wait")
        if plugins:
            assert result.run.driver_checkpoint.state == "consumed"
        metrics = sample.metrics(size, limit if case in ("parallel", "policy") else 1)
        if case == "policy":
            assert len(checks) == size and set(checks.values()) == {2}
            metrics["policy_peak"] = policy_peak
        return metrics
    finally:
        await runtime.close()
        if plugins:
            await plugins.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--sizes", nargs="+", type=int, default=[4, 12])
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--delay-ms", type=int, default=35)
    parser.add_argument(
        "--origins", nargs="+", default=["model", "driver"], choices=["model", "driver"]
    )
    parser.add_argument(
        "--backends", nargs="+", default=["files", "sqlite"], choices=["files", "sqlite"]
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        default=["single", "serial", "parallel", "wait"],
        choices=["single", "serial", "parallel", "wait", "policy"],
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "code": str(CODE),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "cpu": platform.processor(),
                "temp": tempfile.gettempdir(),
                "options": vars(args),
            }
        ),
        flush=True,
    )
    for backend in args.backends:
        for origin in args.origins:
            for case in args.cases:
                for size in [1] if case in ("single", "wait") else args.sizes:
                    samples = []
                    for i in range(args.warmup + args.repeat):
                        with tempfile.TemporaryDirectory(prefix="ouro-batch-bench-") as tmp:
                            sample = await trial(
                                Path(tmp), backend, origin, case, size, args.limit, args.delay_ms
                            )
                        if i >= args.warmup:
                            samples.append(sample)
                    print(
                        json.dumps(
                            {
                                "backend": backend,
                                "origin": origin,
                                "case": case,
                                "size": size,
                                "samples": samples,
                                "median": {
                                    k: statistics.median(s.get(k, 0) for s in samples)
                                    for k in samples[0]
                                },
                            }
                        ),
                        flush=True,
                    )


if __name__ == "__main__":
    asyncio.run(main())
