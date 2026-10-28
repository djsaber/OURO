import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.provider import ProviderStreamEvent, Usage
from ouro.core.run_budget import RunBudgetPolicy
from ouro.plugins.invocation import current_invocation, plugin_invocation
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolResult
from tests.application.test_run_drivers import DRIVER, PLUGIN, host, plugin_source
from tests.application.test_runtime import GateProvider
from tests.run_facts import run_events, run_tool_results

SERVICE = "dev.ouro.examples.review_service"
ROOT = Path(__file__).parents[2]


def service_packages(tmp_path, kind):
    driver = plugin_source(tmp_path, kind)
    for name, target in (
        ("native_service_driver.py", "review_driver_plugin.py"),
        ("native_service_worker.py", "worker.py"),
    ):
        shutil.copyfile(ROOT / "tests/plugins/fixtures" / name, driver / target)
    manifest_path = driver / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contributes"].pop("commands")
    manifest["dependencies"] = {"plugins": [{"plugin_id": SERVICE, "version": "^0.1.0"}]}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service = tmp_path / "service"
    shutil.copytree(ROOT / "examples/plugins/review-service", service)
    if kind == "worker":
        manifest_path = service / ".ouro-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime"] = {
            "kind": "worker",
            "command": ["{python}", "worker.py"],
            "protocol": "3.0",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return driver, service


@pytest.mark.parametrize(
    ("store_type", "kind"),
    [(LocalSessionStore, "python-inprocess"), (SQLiteSessionStore, "worker")],
)
async def test_dependency_service_uses_callers_model_tool_and_waiting_contract(
    tmp_path, store_type, kind
):
    store = store_type(tmp_path / ("sessions.sqlite3" if kind == "worker" else "sessions"))
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    for package in reversed(service_packages(tmp_path, kind)):
        service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active", service.show(PLUGIN)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        assert pending.run.status == "waiting_approval", pending.run.error
        assert pending.run.model_round_count == 2
        assert pending.run.driver_checkpoint.payload == {"step": "summarize", "review": "reviewed"}
        assert pending.run.driver.dependencies[0].plugin_id == SERVICE
        assert calls == []
        assert all(item.gate.in_flight == 0 for item in plugins._active.values())
        await runtime.close()
        await plugins.close()
        runtime, plugins, service = host(tmp_path, store, calls)
        await plugins.start()
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "deployed"
        assert result.run.model_round_count == 2
        assert result.run.driver_checkpoint.state == "consumed"
        assert len(calls) == 1
        assert len(store.load_runs(pending.session.session_id)) == 1
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_nested_model_service_preserves_caller_usage_and_budget(tmp_path, kind):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(
        tmp_path, store, [], usage_service=UsageService(LocalUsageLedger(tmp_path / "billing"))
    )
    runtime.run_budget = RunBudgetPolicy(max_model_rounds=1)
    runtime.adapter.provider = FauxProvider(
        [
            ProviderStreamEvent(type="text_delta", text="reviewed"),
            ProviderStreamEvent(type="done", usage=Usage(input_tokens=11, output_tokens=7)),
        ]
    )
    for package in reversed(service_packages(tmp_path, kind)):
        service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        result = await runtime.run("deploy", driver_id=DRIVER)
        assert result.run.status == "paused", result.run.error
        assert any(
            event.payload.get("reason") == "model_rounds" for event in run_events(store, result)
        )
        assert result.run.model_round_count == 1
        assert result.usage["input_tokens"] == 11
        assert result.usage["output_tokens"] == 7
        assert (
            runtime.usage.run_usage(result.session.session_id, result.run.run_id).request_count == 1
        )
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("restriction", ["permission", "read_only"])
async def test_service_cannot_relax_native_run_permissions(tmp_path, kind, restriction):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    packages = service_packages(tmp_path, kind)
    manifest_path = packages[1] / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if restriction == "permission":
        manifest["permissions"] = []
    else:
        manifest["exports"][0]["methods"]["inspect"]["effect"] = "read_only"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for package in reversed(packages):
        service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        result = await runtime.run("deploy", driver_id=DRIVER)
        assert result.run.status == "failed"
        assert (
            "sessions.write" if restriction == "permission" else "read-only"
        ) in result.run.error
        assert result.run.model_round_count == 0
        assert result.usage is None
    finally:
        await runtime.close()
        await plugins.close()


async def test_worker_service_model_call_uses_native_lifetime_instead_of_rpc_default(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    provider = GateProvider()
    runtime.adapter.provider = provider
    packages = service_packages(tmp_path, "worker")
    for package in packages:
        source = package / "worker.py"
        text = source.read_text(encoding="utf-8").replace(
            "max_requests=1", "max_requests=1, request_timeout=0.05"
        )
        source.write_text(text, encoding="utf-8")
    for package in reversed(packages):
        service.install(package)
    task = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        for generation in plugins._active.values():
            generation.instance.client.request_timeout = 0.05
        task = asyncio.create_task(runtime.run("review", driver_id=DRIVER))
        async with asyncio.timeout(5):
            await provider.first_started.wait()
        done, _ = await asyncio.wait({task}, timeout=0.15)
        assert not done, task.result().run.error
        provider.release_first.set()
        result = await task
        assert result.run.status == "completed", result.run.error
        assert result.run.model_round_count == 2
    finally:
        provider.release_first.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize(("kind", "stop"), [("python-inprocess", "abort"), ("worker", "disable")])
async def test_nested_native_call_settles_before_run_or_plugin_stops(
    tmp_path, monkeypatch, kind, stop
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    for package in reversed(service_packages(tmp_path, kind)):
        service.install(package)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def streaming(**kwargs):
        entered.set()
        try:
            yield ProviderStreamEvent(type="text_delta", text="partial")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(runtime.adapter.provider, "stream", streaming)
    task = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        generations = tuple(plugins._active.values())
        session = runtime.create_session()
        task = asyncio.create_task(
            runtime.run("review", session_id=session.session_id, driver_id=DRIVER)
        )
        async with asyncio.timeout(10):
            await entered.wait()
            if stop == "abort":
                await runtime.abort_run(session_id=session.session_id)
            else:
                await service.disable(PLUGIN)
            result = await task
        assert result.run.status == ("aborted" if stop == "abort" else "failed"), result.run.error
        assert closed.is_set()
        assert all(item.gate.in_flight == 0 for item in generations)
        assert not store.is_session_locked(session.session_id)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        await plugins.close()


async def test_tool_service_model_reentry_fails_without_starting_another_request(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()

    async def deploy(invocation, context):
        await plugins._active[SERVICE].services.call(SERVICE, "review", "inspect")
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name="Deploy", content="unused"
        )

    runtime, plugins, service = host(tmp_path, store, [], mode="auto", handler=deploy)
    for package in reversed(service_packages(tmp_path, "python-inprocess")):
        service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        async with asyncio.timeout(5):
            result = await runtime.run("deploy", driver_id=DRIVER)
        assert result.run.status == "completed", result.run.error
        assert result.run.model_round_count == 2
        assert run_tool_results(store, result)[0].is_error
        assert "sequentially" in run_tool_results(store, result)[0].content
    finally:
        await runtime.close()
        await plugins.close()


async def test_default_driver_establishes_its_accepted_run_scope(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    observed = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            invocation = current_invocation.get()
            observed.append((invocation.session_id, invocation.run_id, invocation.workspace))
            yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

    runtime.adapter.provider = Provider()
    try:
        with plugin_invocation(
            "parent", writable=True, session_id="parent", run_id="parent"
        ) as parent:
            parent.close()
            result = await runtime.run("task", workspace=str(tmp_path))
            assert current_invocation.get() is parent
        assert result.run.status == "completed", result.run.error
        assert observed == [(result.session.session_id, result.run.run_id, str(tmp_path))]
    finally:
        await runtime.close()
        await plugins.close()
