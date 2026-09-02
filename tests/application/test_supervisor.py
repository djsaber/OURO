from __future__ import annotations

import asyncio
import json

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.sessions import SessionService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.supervisor_models import ShutdownPolicy
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentCatalogSnapshot
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
    ToolCallDelta,
)
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


class BlockingToolStreamProvider:
    provider_id = "blocking-tool-stream"

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0,
                id_delta="call_stream",
                name_delta="Inspect",
            ),
        )
        yield ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0,
                arguments_delta='{\n\t"path":',
            ),
        )
        await self.release.wait()
        yield ProviderStreamEvent(type="done")


class RecordingManagedResource:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


class BlockingStartResource:
    def __init__(self) -> None:
        self.start_count = 0
        self.close_count = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> None:
        self.start_count += 1
        self.entered.set()
        await self.release.wait()

    async def close(self) -> None:
        self.close_count += 1


class ClosableFauxProvider(FauxProvider):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

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
async def test_supervisor_defers_incompatible_runtime_to_target_without_blocking_startup(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = SessionRecord(title="Old runtime")
    run = RunRecord(
        session_id=session.session_id,
        run_id="run_old_contract",
        status="completed",
        agent_catalog=AgentCatalogSnapshot(),
    )
    runtime.store.create_session(session)
    runtime.store.save_run(run)
    payload = json.loads(run.model_dump_json())
    payload["agent_catalog"]["contract_hash"] = "sha256:" + "0" * 64
    run_path = runtime.store.sessions_dir / session.session_id / "runs" / f"{run.run_id}.json"
    run_path.write_text(json.dumps(payload), encoding="utf-8")
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)

    started = await supervisor.start()

    assert started.diagnostics == ()
    with pytest.raises(PersistenceError, match="agent catalog contract hash"):
        await supervisor.submit("must remain strict", session_id=session.session_id)
    handle = await supervisor.submit("new session still works")
    assert (await supervisor.wait(handle)).status == "completed"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_supervisor_closes_runtime_provider_connections(tmp_path) -> None:
    provider = ClosableFauxProvider()
    supervisor = RuntimeSupervisor(_runtime(tmp_path, provider))

    await supervisor.start()
    await supervisor.shutdown()

    assert provider.closed is True


@pytest.mark.asyncio
async def test_concurrent_supervisor_start_is_single_flight(tmp_path) -> None:
    resource = BlockingStartResource()
    supervisor = RuntimeSupervisor(
        _runtime(tmp_path, FauxProvider()),
        max_concurrency=2,
        managed_resources=(resource,),
    )

    starts = [asyncio.create_task(supervisor.start()) for _ in range(2)]
    await resource.entered.wait()
    await asyncio.sleep(0)
    assert resource.start_count == 1
    resource.release.set()
    await asyncio.gather(*starts)

    assert len(supervisor._workers) == 2
    await supervisor.shutdown()
    assert resource.close_count == 1


@pytest.mark.asyncio
async def test_supervisor_reports_idle_for_session_without_runs(tmp_path) -> None:
    supervisor = RuntimeSupervisor(_runtime(tmp_path, FauxProvider()))
    created = supervisor.runtime.sessions.create(title="Fresh")

    assert supervisor.status(created.session.session_id).status == "idle"

    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_supervisor_status_does_not_load_message_history(tmp_path, monkeypatch) -> None:
    supervisor = RuntimeSupervisor(_runtime(tmp_path, FauxProvider()))
    created = supervisor.runtime.sessions.create(title="History independent")

    def fail_complete_runtime_load(session_id: str):
        raise AssertionError(f"unexpected complete runtime load for {session_id}")

    monkeypatch.setattr(
        supervisor.store,
        "load_runtime_session",
        fail_complete_runtime_load,
    )

    assert supervisor.status(created.session.session_id).status == "idle"

    await supervisor.shutdown()


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
async def test_activity_subscription_exposes_initial_and_live_fine_grained_state(tmp_path) -> None:
    provider = ConcurrencyProvider()
    supervisor = RuntimeSupervisor(_runtime(tmp_path, provider), max_concurrency=1)
    handle = await supervisor.submit("hello")
    subscription = supervisor.subscribe(handle.session_id, include_activity=True)

    assert subscription.initial_activity is not None
    assert subscription.initial_activity.activity == "queued"

    observed: list[str] = []
    while "waiting_model" not in observed:
        notification = await asyncio.wait_for(subscription.get(), timeout=2)
        if notification.kind == "activity" and notification.activity is not None:
            observed.append(notification.activity.activity)

    assert "preparing_model" in observed
    assert supervisor.activity(handle).activity == "waiting_model"
    assert supervisor.activity(handle).context_status is not None
    assert supervisor.activity(handle).context_status.metrics.estimated_input_tokens > 0

    provider.release.set()
    terminal = await asyncio.wait_for(supervisor.wait(handle), timeout=2)
    while "completed" not in observed:
        notification = await asyncio.wait_for(subscription.get(), timeout=2)
        if notification.kind == "activity" and notification.activity is not None:
            observed.append(notification.activity.activity)

    assert terminal.status == "completed"
    assert "streaming_text" in observed
    assert supervisor.activity(handle).activity == "completed"
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_activity_subscription_exposes_host_neutral_streaming_tool_state(tmp_path) -> None:
    provider = BlockingToolStreamProvider()
    supervisor = RuntimeSupervisor(_runtime(tmp_path, provider), max_concurrency=1)
    handle = await supervisor.submit("inspect")
    subscription = supervisor.subscribe(handle.session_id, include_activity=True)

    while True:
        notification = await asyncio.wait_for(subscription.get(), timeout=2)
        if notification.kind != "event" or notification.event is None:
            continue
        if (
            notification.event.event_type == "model_tool_call_delta"
            and notification.event.data.get("arguments_delta")
        ):
            break

    streamed_tool = supervisor.activity(handle).tool_calls[0]

    assert streamed_tool.status == "streaming"
    assert streamed_tool.tool_name == "Inspect"
    assert streamed_tool.source_index == 0
    assert streamed_tool.argument_stream_tail == '{\n\t"path":'

    await supervisor.abort(handle, reason="test observed streaming activity")
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_activity_by_completed_handle_does_not_drift_to_newer_session_run(tmp_path) -> None:
    supervisor = RuntimeSupervisor(_runtime(tmp_path, FauxProvider()), max_concurrency=1)
    first = await supervisor.submit("first")
    first_terminal = await supervisor.wait(first)
    second = await supervisor.submit("second", session_id=first.session_id)
    second_terminal = await supervisor.wait(second)

    first_activity = supervisor.activity(first)
    session_activity = supervisor.activity(first.session_id)

    assert first_activity.run_id == first_terminal.run_id
    assert first_activity.activity == "completed"
    assert session_activity.run_id == second_terminal.run_id
    assert session_activity.activity == "completed"
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
async def test_activity_initial_snapshot_remains_available_when_event_replay_lags(tmp_path) -> None:
    supervisor = RuntimeSupervisor(_runtime(tmp_path, FauxProvider()), max_concurrency=1)
    handle = await supervisor.submit("hello")
    await supervisor.wait(handle)

    subscription = supervisor.subscribe(
        handle.session_id,
        max_buffer=1,
        include_activity=True,
    )
    notification = await subscription.get()

    assert subscription.initial_activity is not None
    assert subscription.initial_activity.activity == "completed"
    assert notification.kind == "diagnostic"
    assert notification.diagnostic is not None
    assert notification.diagnostic.code == "runtime_subscription_lagged"
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_subscription_replay_is_bounded_and_never_loads_full_history(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = runtime.sessions.create(title="Bounded replay").session
    for index in range(8):
        runtime.store.append_ordered_event(
            EventRecord(
                session_id=session.session_id,
                run_id="run_history",
                event_type="history",
                payload={"index": index},
            )
        )

    def reject_full_history(*_args, **_kwargs):
        raise AssertionError("supervisor must not load complete Session history")

    monkeypatch.setattr(runtime.store, "load_session", reject_full_history)
    supervisor = RuntimeSupervisor(runtime)
    subscription = supervisor.subscribe(session.session_id, max_buffer=2)

    notification = await subscription.get()
    assert notification.kind == "diagnostic"
    assert notification.diagnostic is not None
    assert notification.diagnostic.code == "runtime_subscription_lagged"
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_host_event_projection_is_typed_and_redacts_payload_secrets(tmp_path) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = runtime.sessions.create(title="Typed events").session
    runtime.store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id="run_typed",
            event_type="model_text_delta",
            payload={"text": "hello", "api_key": "event-secret"},
        )
    )
    supervisor = RuntimeSupervisor(runtime)
    subscription = supervisor.subscribe(session.session_id)

    notification = await subscription.get()
    assert notification.event is not None
    assert notification.event.category == "text"
    assert notification.event.text == "hello"
    assert notification.event.data["api_key"] == "***"
    assert "event-secret" not in notification.model_dump_json()
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_observation_high_water_hands_off_to_subscription_without_a_gap(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = runtime.sessions.create(title="Observation handoff").session
    first = runtime.store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id="run_live",
            event_type="model_text_delta",
            payload={"text": "before"},
        )
    )
    snapshot = SessionService(runtime.store).observation_snapshot(session.session_id)
    second = runtime.store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id="run_live",
            event_type="model_text_delta",
            payload={"text": "after"},
        )
    )
    supervisor = RuntimeSupervisor(runtime)

    subscription = supervisor.subscribe(
        session.session_id,
        after_sequence=snapshot.event_high_water,
    )
    notification = await subscription.get()

    assert snapshot.event_high_water == first.sequence
    assert notification.event is not None
    assert notification.event.sequence == second.sequence
    assert notification.event.text == "after"
    subscription.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_agent_relation_events_have_a_dedicated_category(tmp_path) -> None:
    runtime = _runtime(tmp_path, FauxProvider())
    session = runtime.sessions.create(title="Agent relation events").session
    runtime.store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id="run_parent",
            event_type="agent_spawned",
            payload={
                "root_session_id": session.session_id,
                "parent_session_id": session.session_id,
                "parent_run_id": "run_parent",
                "parent_tool_call_id": "call_spawn",
                "delegation_id": "dlg_child",
                "child_session_id": "ses_child",
                "child_run_id": "run_child",
                "agent_id": "reader",
                "execution_mode": "async",
            },
        )
    )
    supervisor = RuntimeSupervisor(runtime)
    subscription = supervisor.subscribe(session.session_id)

    notification = await subscription.get()

    assert notification.event is not None
    assert notification.event.category == "agent"
    assert notification.event.data["parent_tool_call_id"] == "call_spawn"
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
async def test_abort_waits_for_a_starting_job_to_gain_its_durable_run_identity(
    tmp_path,
    monkeypatch,
) -> None:
    provider = BlockingProvider()
    runtime = _runtime(tmp_path, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    reconcile_started = asyncio.Event()
    release_reconcile = asyncio.Event()
    original_reconcile = runtime._reconcile_session

    async def delayed_reconcile(*args, **kwargs):
        reconcile_started.set()
        await release_reconcile.wait()
        return await original_reconcile(*args, **kwargs)

    monkeypatch.setattr(runtime, "_reconcile_session", delayed_reconcile)
    handle = await supervisor.submit("starting")
    await asyncio.wait_for(reconcile_started.wait(), timeout=1)

    abort = asyncio.create_task(supervisor.abort(handle, reason="abort during startup"))
    await asyncio.sleep(0)
    assert not abort.done()

    release_reconcile.set()
    snapshot = await asyncio.wait_for(abort, timeout=2)

    assert snapshot.run_id is not None
    assert snapshot.status == "aborted"
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_target_recovery_recovers_interrupted_durable_run_from_store(tmp_path) -> None:
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

    assert started.recovered == ()
    await supervisor.recover_session(session.session_id)
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

    durable = runtime.store.load_session(handle.session_id)
    active = next(run for run in durable.runs if run.status == "running")
    assert supervisor.status(handle).status == "recoverable"
    recovered = runtime.session_recovery.recover(handle.session_id)
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
    durable = runtime.store.load_session(running.session_id)
    assert durable.runs[0].status == "aborted"
    assert provider.closed is True
