import asyncio
import threading
from pathlib import Path

import pytest

from ouro.application.contracts.execution import ShutdownPolicy
from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.sessions.service import SessionService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import SessionBusyError
from ouro.core.provider import ModelMetadata
from ouro.core.session_controls import AttributedSessionInput, HostSessionInput
from ouro.core.state import RunRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.application.test_driver_inputs import SteeringProvider


def runtime_for(store, provider):
    return RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            provider, ModelMetadata(model_id="test", provider_id=provider.provider_id)
        ),
    )


def prepare(runtime):
    session = runtime.create_session(title="queued")
    run = runtime.prepare_run(
        RunRecord(session_id=session.session_id),
        model=runtime.current_model().key,
        reasoning=session.reasoning_request,
        workspace=str(Path.cwd()),
        approval_mode=session.approval_mode,
        interaction_mode=session.interaction_mode,
    )
    return run, HostSessionInput(prompt="prepared", host_source_id="test")


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    store = request.param(tmp_path / "home")
    store.initialize()
    return store


@pytest.mark.parametrize("entry", ["duplicate", "recovery", "runtime", "plugin", "host"])
async def test_queued_owner_is_visible_to_every_native_entry(store, entry):
    provider = SteeringProvider()
    runtime = runtime_for(store, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    # Separate Runtime and Store instances cannot consult the supervisor's maps.
    other_store = type(store)(
        store.database if isinstance(store, SQLiteSessionStore) else store.root
    )
    other = runtime_for(other_store, provider)
    try:
        occupying = await supervisor.submit("occupy")
        await provider.started.wait()
        run, session_input = prepare(runtime)
        handle = await supervisor.submit_prepared(run, session_input)
        assert other_store.is_session_locked(run.session_id)
        if entry == "duplicate":
            with pytest.raises(SessionBusyError):
                await supervisor.submit("duplicate", session_id=run.session_id)
        elif entry == "recovery":
            await supervisor.recover_session(run.session_id)
            await other.reconcile_session(run.session_id)
            SessionRecoveryService(other_store).recover(run.session_id)
            SessionService(other_store).open(run.session_id)
        elif entry == "runtime":
            result = await other.run("steer", session_id=run.session_id, queue_mode="steer")
            assert result.run.run_id == run.run_id
        elif entry == "plugin":
            result = await other.submit_plugin_input(
                AttributedSessionInput(
                    prompt="steer",
                    mode="steer",
                    plugin_id="test",
                    contribution_id="test/input",
                    generation_id="generation",
                    execution_contract_hash="contract",
                ),
                session_id=run.session_id,
            )
            assert result.run.run_id == run.run_id
        else:
            receipt = await supervisor.route_host_input(
                HostSessionInput(prompt="steer", mode="steer", host_source_id="test"),
                session_id=run.session_id,
            )
            assert receipt.message_id is None
        assert store.load_run(run.session_id, run.run_id).status == "queued"
        assert supervisor.status(handle).status == "queued"
        assert not any(
            event.event_type == "run_recovered"
            for event in store.load_session(run.session_id).events
        )
        provider.release.set()
        assert (await asyncio.wait_for(supervisor.wait(occupying), 10)).status == "completed"
        assert (await asyncio.wait_for(supervisor.wait(handle), 10)).status == "completed"
        assert not store.is_session_locked(run.session_id)
        snapshot = store.load_session(run.session_id)
        assert len(snapshot.runs) == 1
        if entry in {"runtime", "plugin", "host"}:
            assert "steer" in provider.requests[-1]
    finally:
        provider.release.set()
        await supervisor.shutdown(ShutdownPolicy(mode="abort", timeout_seconds=5))
        await other.close()


@pytest.mark.parametrize("exit_kind", ["abort", "shutdown", "before_execution_failure"])
async def test_queued_reservation_releases_when_the_job_cannot_run(store, exit_kind, monkeypatch):
    provider = SteeringProvider()
    runtime = runtime_for(store, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    try:
        await supervisor.submit("occupy")
        await provider.started.wait()
        run, session_input = prepare(runtime)
        handle = await supervisor.submit_prepared(run, session_input)
        if exit_kind == "abort":
            assert (await asyncio.wait_for(supervisor.abort(handle), 5)).status == "aborted"
            assert store.load_run(run.session_id, run.run_id).status == "aborted"
        elif exit_kind == "shutdown":
            await supervisor.shutdown(ShutdownPolicy(mode="wait", timeout_seconds=0.05))
        else:

            async def fail_before_transfer(*args, **kwargs):
                raise ValueError("execution setup failed")

            monkeypatch.setattr(runtime, "run_prepared", fail_before_transfer)
            provider.release.set()
            assert (await asyncio.wait_for(supervisor.wait(handle), 10)).status == "failed"
        assert not store.is_session_locked(run.session_id)
        recovered = SessionRecoveryService(store).recover_runtime(run.session_id)
        assert recovered.runs[0].status == ("aborted" if exit_kind == "abort" else "recoverable")
    finally:
        provider.release.set()
        await supervisor.shutdown(ShutdownPolicy(mode="abort", timeout_seconds=5))


async def test_cancelled_admission_releases_the_unpublished_reservation(store, monkeypatch):
    provider = SteeringProvider()
    runtime = runtime_for(store, provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    entered, release = threading.Event(), threading.Event()
    run, session_input = prepare(runtime)
    original = store.latest_event_sequence

    def read_boundary(session_id):
        if session_id == run.session_id:
            entered.set()
            assert release.wait(10)
        return original(session_id)

    monkeypatch.setattr(store, "latest_event_sequence", read_boundary)
    admission = asyncio.create_task(supervisor.submit_prepared(run, session_input))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert store.is_session_locked(run.session_id)
        admission.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await admission
        assert not store.is_session_locked(run.session_id)
        assert supervisor._jobs_by_session == {}
        assert (
            SessionRecoveryService(store).recover_runtime(run.session_id).runs[0].status
            == "recoverable"
        )
    finally:
        release.set()
        await supervisor.shutdown(ShutdownPolicy(mode="abort", timeout_seconds=5))
