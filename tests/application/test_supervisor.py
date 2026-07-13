from __future__ import annotations

import asyncio

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.supervisor_models import ShutdownPolicy
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import SessionBusyError
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.state import RunRecord, SessionRecord
from ouro.mcp import MCPConfigStore, MCPRuntimeResource, MCPServerConfig
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


def _runtime(tmp_path, provider) -> RuntimeService:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="supervised", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
    )


class ConcurrencyProvider:
    provider_id = "concurrency"

    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.started: list[str] = []
        self.release = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, tools
        prompt = messages[-1].text_content
        self.started.append(prompt)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await self.release.wait()
            yield ProviderStreamEvent(type="text_delta", text=prompt)
            yield ProviderStreamEvent(type="done")
        finally:
            self.active -= 1

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


class BlockingProvider:
    provider_id = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        self.started.set()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            self.closed = True
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


class RecordingManagedResource:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_supervisor_owns_long_lived_resource_shutdown(tmp_path) -> None:
    resource = RecordingManagedResource()
    supervisor = RuntimeSupervisor(
        _runtime(tmp_path, FauxProvider()),
        managed_resources=(resource,),
    )

    await supervisor.start()
    assert resource.started is True

    await supervisor.shutdown()
    assert resource.closed is True


@pytest.mark.asyncio
async def test_supervisor_accepts_configured_mcp_runtime_resource(tmp_path) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="manual",
            name="Manual MCP",
            command="not-started",
            auto_start=False,
        )
    )
    resource = MCPRuntimeResource.from_config(runtime.store.root, runtime.tools)
    supervisor = RuntimeSupervisor(runtime, managed_resources=(resource,))

    await supervisor.start()
    assert resource.manager.get("manual").status == "configured"

    await supervisor.shutdown()
    assert resource.manager.get("manual").status == "stopped"


@pytest.mark.asyncio
async def test_fifo_scheduler_respects_global_concurrency_limit(tmp_path) -> None:
    provider = ConcurrencyProvider()
    supervisor = RuntimeSupervisor(_runtime(tmp_path, provider), max_concurrency=2)

    handles = [await supervisor.submit(f"prompt-{index}") for index in range(4)]
    for _ in range(100):
        if len(provider.started) == 2:
            break
        await asyncio.sleep(0.01)

    assert provider.started == ["prompt-0", "prompt-1"]
    assert provider.maximum == 2
    provider.release.set()
    snapshots = await asyncio.gather(*(supervisor.wait(handle) for handle in handles))

    assert provider.started == ["prompt-0", "prompt-1", "prompt-2", "prompt-3"]
    assert all(snapshot.status == "completed" for snapshot in snapshots)
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_single_session_registry_rejects_a_second_supervised_run(tmp_path) -> None:
    provider = ConcurrencyProvider()
    supervisor = RuntimeSupervisor(_runtime(tmp_path, provider), max_concurrency=1)
    first = await supervisor.submit("first")

    with pytest.raises(SessionBusyError, match="supervised run"):
        await supervisor.submit("second", session_id=first.session_id)

    provider.release.set()
    await supervisor.wait(first)
    second = await supervisor.submit("second", session_id=first.session_id)
    assert (await supervisor.wait(second)).status == "completed"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_slow_subscription_reports_lag_without_blocking_run(tmp_path) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = runtime.sessions.create(title="Events").session
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    subscription = supervisor.subscribe(session.session_id, max_buffer=1)

    handle = await supervisor.submit("hello", session_id=session.session_id)
    terminal = await asyncio.wait_for(supervisor.wait(handle), timeout=2)
    notification = await subscription.get()

    assert terminal.status == "completed"
    assert notification.kind == "diagnostic"
    assert notification.diagnostic is not None
    assert notification.diagnostic.code == "runtime_subscription_lagged"
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_task_exception_becomes_diagnostic_and_does_not_break_worker(
    monkeypatch,
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, FauxProvider())

    async def fail_run(*_args, **_kwargs):
        raise RuntimeError("injected runtime boundary failure")

    monkeypatch.setattr(runtime, "run", fail_run)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)

    failed = await supervisor.submit("first")
    first = await supervisor.wait(failed)
    second_handle = await supervisor.submit("second")
    second = await supervisor.wait(second_handle)

    assert first.status == "failed"
    assert first.diagnostics[0].code == "supervised_run_failed"
    assert second.status == "failed"
    assert second.diagnostics[0].code == "supervised_run_failed"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_host_detach_does_not_abort_but_explicit_abort_does(tmp_path) -> None:
    provider = BlockingProvider()
    runtime = _runtime(tmp_path, provider)
    session = runtime.sessions.create(title="Detach").session
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    subscription = supervisor.subscribe(session.session_id)
    handle = await supervisor.submit("wait", session_id=session.session_id)
    await provider.started.wait()

    subscription.close()
    await asyncio.sleep(0)
    assert supervisor.status(handle).status == "running"

    aborted = await supervisor.abort(handle, reason="explicit")
    terminal = await asyncio.wait_for(supervisor.wait(handle), timeout=2)
    assert aborted.status == "aborted"
    assert terminal.status == "aborted"
    assert provider.closed is True
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_start_recovers_interrupted_durable_run_from_store(tmp_path) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = SessionRecord(status="running", active_run_id="run_interrupted")
    runtime.store.create_session(session)
    runtime.store.save_run(
        RunRecord(
            session_id=session.session_id,
            run_id="run_interrupted",
            status="running",
        )
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)

    started = await supervisor.start()

    assert [(item.run_id, item.status) for item in started.recovered] == [
        ("run_interrupted", "recoverable")
    ]
    assert runtime.store.load_run(session.session_id, "run_interrupted").status == "recoverable"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_wait_shutdown_timeout_leaves_durable_run_recoverable_for_restart(tmp_path) -> None:
    provider = BlockingProvider()
    runtime = _runtime(tmp_path, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    handle = await supervisor.submit("wait")
    await provider.started.wait()

    await supervisor.shutdown(ShutdownPolicy(mode="wait", timeout_seconds=0.01))

    durable = runtime.store.load_session(handle.session_id, recover=False)
    active = next(run for run in durable.runs if run.status == "running")
    assert supervisor.status(handle).status == "recoverable"
    recovered = runtime.store.load_session(handle.session_id, recover=True)
    assert recovered.runs[0].status == "recoverable"
    assert active.run_id is not None


@pytest.mark.asyncio
async def test_shutdown_timeout_cleans_unstarted_jobs_from_active_registry(tmp_path) -> None:
    provider = BlockingProvider()
    runtime = _runtime(tmp_path, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    running = await supervisor.submit("running")
    queued = await supervisor.submit("queued")
    await provider.started.wait()

    await supervisor.shutdown(ShutdownPolicy(mode="wait", timeout_seconds=0.01))

    assert supervisor.status(running).status == "recoverable"
    assert supervisor.status(queued).status == "aborted"
    assert supervisor.status(queued).diagnostics[0].code == ("queued_run_cancelled_on_shutdown")


@pytest.mark.asyncio
async def test_abort_shutdown_persists_abort_for_running_and_queued_jobs(tmp_path) -> None:
    provider = BlockingProvider()
    runtime = _runtime(tmp_path, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    running = await supervisor.submit("running")
    queued = await supervisor.submit("queued")
    await provider.started.wait()

    await supervisor.shutdown(ShutdownPolicy(mode="abort", timeout_seconds=2))

    assert supervisor.status(running).status == "aborted"
    assert supervisor.status(queued).status == "aborted"
    durable = runtime.store.load_session(running.session_id, recover=False)
    assert durable.runs[0].status == "aborted"
    assert provider.closed is True
