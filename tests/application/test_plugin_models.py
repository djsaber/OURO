import asyncio
import json
import shutil
from contextvars import copy_context
from pathlib import Path

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.billing import UsageFilter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, Usage
from ouro.local_application import LocalApplication
from ouro.plugins.invocation import plugin_invocation
from ouro.providers import FauxProvider
from ouro.sdk.plugins.models import ModelInput, ModelRequest
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry

PLUGIN = "dev.ouro.examples.model_review"
ROOT = Path(__file__).parents[2]


def package(tmp_path, kind, *, probe=False, restriction=None):
    path = tmp_path / "model-review"
    shutil.copytree(ROOT / "examples/plugins/model-review", path)
    manifest_path = path / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if kind == "worker":
        manifest["runtime"] = {
            "kind": "worker",
            "command": ["{python}", "worker.py"],
            "protocol": "3.0",
        }
    if probe:
        shutil.copyfile(
            ROOT / "tests/plugins/fixtures/model_probe.py", path / "model_review_plugin.py"
        )
        if kind == "worker":
            manifest["runtime"]["command"][-1] = "model_review_plugin.py"
        manifest.pop("contributes")
        manifest["exports"] = [
            {
                "id": "probe",
                "methods": {
                    "generate": {
                        "effect": "read_only" if restriction == "read_only" else "state_changing",
                        "input_schema": {"type": "object"},
                    }
                },
            }
        ]
    if restriction == "permission":
        manifest["permissions"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture(params=["python-inprocess", "worker"])
def kind(request):
    return request.param


@pytest.fixture
async def system(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(provider_id="faux", model_id="faux")),
        tools=ToolRegistry(),
    )
    # The product composition supplies the ledger and independent model port.
    app = LocalApplication.from_runtime(runtime, home=tmp_path / "home")
    try:
        yield runtime, app.plugin_runtime, app.plugins, app
    finally:
        await app.close()


async def call(plugins, **arguments):
    return await plugins._active[PLUGIN].services.call(PLUGIN, "probe", "generate", arguments)


async def test_public_model_review_command_needs_no_run_or_session(tmp_path, kind, system):
    runtime, plugins, service, app = system
    runtime.adapter.provider = FauxProvider()
    service.install(package(tmp_path, kind))
    assert (await service.enable(PLUGIN)).status == "active", service.show(PLUGIN)
    result = await plugins.catalog().commands.execute(
        f"{PLUGIN}:model-review", "faux/faux Review this"
    )
    assert result.output == "Hello from OURO."
    assert not runtime.store.list_sessions()
    (entry,) = plugins._model_generation.usage.list_entries(UsageFilter()).entries
    assert entry.plugin_id == PLUGIN and entry.session_id is None and entry.run_id is None


async def test_worker_and_inprocess_generation_deliver_ordered_stream_with_no_rpc_deadline(
    tmp_path, kind, system
):
    runtime, plugins, service, _ = system

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            await asyncio.sleep(0.03)
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="think")
            yield ProviderStreamEvent(type="text_delta", text="answer")
            yield ProviderStreamEvent(type="done", usage=Usage(input_tokens=4, output_tokens=2))

    runtime.adapter.provider = Provider()
    service.install(package(tmp_path, kind, probe=True))
    assert (await service.enable(PLUGIN)).status == "active"
    result = await call(plugins)
    assert result["output"] == [
        {"sequence": 1, "kind": "reasoning", "text": "think"},
        {"sequence": 2, "kind": "text", "text": "answer"},
    ]
    assert result["result"]["text"] == "answer"
    assert plugins._active[PLUGIN].gate.in_flight == 0


@pytest.mark.parametrize("restriction", ["permission", "read_only", "run"])
async def test_independent_model_cannot_bypass_permission_or_run_contract(
    tmp_path, kind, system, restriction
):
    runtime, plugins, service, _ = system
    provider = FauxProvider()
    runtime.adapter.provider = provider
    service.install(package(tmp_path, kind, probe=True, restriction=restriction))
    assert (await service.enable(PLUGIN)).status == "active"
    expected = {"permission": "models.request", "read_only": "read-only", "run": "active Run"}[
        restriction
    ]
    with plugin_invocation(
        "caller", writable=True, run_id="run_active" if restriction == "run" else None
    ):
        with pytest.raises((PermissionError, RuntimeError), match=expected):
            await call(plugins)
    assert provider._stream_count == 0
    assert not plugins._model_generation.usage.list_entries(UsageFilter()).entries


@pytest.mark.parametrize("ending", ["disable", "callback_error", "timeout"])
async def test_plugin_model_cancellation_settles_owned_provider(tmp_path, kind, system, ending):
    runtime, plugins, service, _ = system
    started, closed = asyncio.Event(), asyncio.Event()

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            try:
                started.set()
                yield ProviderStreamEvent(type="text_delta", text="partial")
                await asyncio.Event().wait()
            finally:
                closed.set()

    runtime.adapter.provider = Provider()
    service.install(package(tmp_path, kind, probe=True))
    assert (await service.enable(PLUGIN)).status == "active"
    active = plugins._active[PLUGIN]
    task = asyncio.create_task(
        call(
            plugins,
            callback_error=ending == "callback_error",
            timeout_seconds=0.1 if ending == "timeout" else 5,
        )
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
        if ending == "disable":
            await service.disable(PLUGIN)
        with pytest.raises((RuntimeError, TimeoutError, asyncio.CancelledError)):
            await task
        assert closed.is_set()
        assert active.gate.in_flight == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_late_or_revoked_model_handle_cannot_start_request(tmp_path, kind, system):
    runtime, plugins, service, _ = system
    provider = FauxProvider()
    runtime.adapter.provider = provider
    service.install(package(tmp_path, kind))
    assert (await service.enable(PLUGIN)).status == "active"
    instance = plugins._active[PLUGIN].instance
    models = instance.models if kind == "python-inprocess" else instance.model_bridge.models
    request = ModelRequest((ModelInput("user", "Review"),), model="faux/faux")
    with plugin_invocation(PLUGIN, writable=True):
        context = copy_context()
    with pytest.raises(RuntimeError, match="invocation has finished"):
        await asyncio.create_task(models.generate(request), context=context)
    await service.disable(PLUGIN)
    with pytest.raises(RuntimeError, match="generation is unavailable"):
        await models.generate(request)
    assert provider._stream_count == 0


async def test_candidate_cannot_make_model_request_before_publication(tmp_path, kind, system):
    runtime, plugins, service, _ = system
    provider = FauxProvider()
    runtime.adapter.provider = provider
    source = package(tmp_path, kind, probe=True)
    entry = source / "model_review_plugin.py"
    entry.write_text(
        entry.read_text(encoding="utf-8").replace(
            "PREPARE_REQUEST = False", "PREPARE_REQUEST = True"
        ),
        encoding="utf-8",
    )
    service.install(source)
    assert (await service.enable(PLUGIN)).status == "failed"
    assert PLUGIN not in plugins._active
    assert provider._stream_count == 0
