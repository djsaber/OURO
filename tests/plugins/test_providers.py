import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.config.providers import ProviderConfigManager
from ouro.core.billing import UsageFilter
from ouro.core.content import AudioContent, ImageContent, TextContent, VideoContent
from ouro.core.messages import (
    ModelMessage,
    ProviderContinuationArtifact,
    SkillActivationContent,
    ToolResultReference,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.core.tools import ToolCallProducer, ToolCallRequest
from ouro.local_application import LocalApplication, open_local_plugins

PLUGIN = "test.protocol.provider"
PROTOCOL = f"{PLUGIN}/providers/probe"
ROOT = Path(__file__).parents[2]


def package(tmp_path, kind):
    root = tmp_path / "package"
    (root / ".ouro-plugin").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "tests/plugins/fixtures/provider_probe.py", root / "provider_probe_plugin.py"
    )
    runtime = {"kind": kind, "entrypoint": "provider_probe_plugin:create_plugin"}
    if kind == "worker":
        runtime = {
            "kind": kind,
            "command": ["{python}", "provider_probe_plugin.py"],
            "protocol": "3.0",
        }
    (root / ".ouro-plugin/plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": PLUGIN,
                "namespace": "provider_probe",
                "name": "Provider probe",
                "version": "1.0.0",
                "engines": {"ouro": "^0.3.0", "plugin_api": "^3.0.0"},
                "runtime": runtime,
                "permissions": [
                    {"id": "models.request", "reason": "Generate"},
                    {"id": "sessions.write", "reason": "Drive"},
                ],
                "contributes": {
                    "providers": [{"id": "probe"}],
                    "run_drivers": [{"id": "review"}],
                    "commands": [{"id": "generate", "description": "generate"}],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture(params=["python-inprocess", "worker"])
async def system(tmp_path, request):
    home = tmp_path / "home"
    configs = ProviderConfigManager(home)
    provider = configs.create_provider(
        display_name="Plugin", provider_type="custom", protocol=PROTOCOL
    )
    endpoint = configs.create_endpoint(provider.provider_id, model_id="probe", make_default=True)
    configs.set_default_route(provider.provider_id, endpoint.endpoint_id)
    app = LocalApplication.build(home)
    async with app:
        app.plugins.install(package(tmp_path, request.param))
        enabled = await app.plugins.enable(PLUGIN)
        assert enabled.status == "active", app.plugins.diagnostics(PLUGIN)
        yield app, home


async def test_configured_plugin_default_routes_native_driver_and_independent_generation(system):
    app, home = system
    check = await app.runtime.adapter.provider.check_connection(app.runtime.adapter.model)
    assert check.ok and check.message == "probe ready"
    native = await app.runtime.run("answer")
    assert native.run.status == "completed", native.run.error
    assert native.final_text == "provider answer"
    custom = await app.runtime.run("review", driver_id=f"{PLUGIN}/run_drivers/review")
    assert custom.run.status == "completed", custom.run.error
    assert custom.final_text == native.final_text
    result = await app.plugin_runtime.catalog().commands.execute(f"{PLUGIN}:generate", "")
    assert result.output == native.final_text
    entries = app.runtime.usage.list_entries(UsageFilter()).entries
    assert len(entries) == 3 and all(item.total_tokens == 10 for item in entries)
    assert app.plugin_runtime._active[PLUGIN].lease_count == 0
    assert app.plugin_runtime.catalog().lease_count == 0
    async with open_local_plugins(home) as plugins:
        result = await plugins.runtime.catalog().commands.execute(f"{PLUGIN}:generate", "")
        assert result.output == native.final_text


async def test_provider_request_preserves_model_visible_semantics_and_multimodal_order(system):
    app, _ = system
    model = app.runtime.adapter.model.model_copy(update={"model_id": "inspect"})
    content = [
        TextContent(text="first"),
        ImageContent(uri="data:image/png;base64,AA=="),
        AudioContent(uri="data:audio/wav;base64,AA==", media_type="audio/wav"),
        VideoContent(uri="data:video/mp4;base64,AA==", media_type="video/mp4"),
        SkillActivationContent(
            name="skill",
            source="plugin",
            location="skills/demo/SKILL.md",
            base_directory="skills/demo",
            instructions="Be precise.",
            content_digest="sha256:" + "a" * 64,
        ),
    ]
    messages = [
        ModelMessage(role="user", content=content, semantic_tag="context"),
        ModelMessage(
            role="assistant",
            tool_calls=[ToolCallRequest(id="t1", name="read")],
            provider_continuation=ProviderContinuationArtifact(
                provider_id="native", protocol="opaque", model_id="m", blocks=({"state": "s"},)
            ),
        ),
        ModelMessage(
            role="tool_result",
            content=[TextContent(text="result")],
            tool_result=ToolResultReference(
                tool_call_id="t1",
                tool_name="read",
                producer=ToolCallProducer(kind="driver", driver_id="d"),
            ),
        ),
    ]
    events = [
        event
        async for event in app.runtime.adapter.provider.stream(
            model=model,
            messages=messages,
            tools=[{"name": "read", "parameters": {"type": "object"}}],
            request_context=ProviderRequestContext({"request.id": "req"}),
        )
    ]
    raw = json.loads(events[0].text)
    expected_messages = [item.model_dump(mode="json") for item in messages]
    # Editing coordinates stay in canonical history, outside the Provider contract.
    expected_messages[0]["content"][4] = content[4].model_dump(
        mode="json", exclude={"text_offset", "order"}
    )
    assert raw["messages"] == expected_messages
    assert raw["context"]["request.id"] == "req"
    assert raw["tools"][0]["name"] == "read"
    assert events[-1].usage.input_tokens == 7


async def test_disable_stops_stream_and_cached_route_cannot_call_removed_provider(system):
    app, _ = system
    generation = app.plugin_runtime._active[PLUGIN]
    provider = app.runtime.adapter.provider
    model = app.runtime.adapter.model.model_copy(update={"model_id": "wait"})
    stream = provider.stream(model=model, messages=[])
    assert (await anext(stream)).text == "provider answer"
    pending = asyncio.create_task(anext(stream))
    await app.plugins.disable(PLUGIN)
    with pytest.raises(RuntimeError, match="unavailable"):
        await asyncio.wait_for(pending, 5)
    await stream.aclose()
    await app.plugin_runtime.wait_stopped(PLUGIN)
    assert generation.lease_count == 0 and generation.gate.in_flight == 0
    await app.plugins.remove(PLUGIN)
    with pytest.raises(ValueError, match="unavailable"):
        await anext(provider.stream(model=model, messages=[]))


async def test_upgrade_keeps_inflight_generation_until_stream_close_and_rebinds_cached_route(
    system,
):
    app, home = system
    provider = app.runtime.adapter.provider
    generation = app.plugin_runtime._active[PLUGIN]
    model = app.runtime.adapter.model
    stream = provider.stream(model=model.model_copy(update={"model_id": "wait"}), messages=[])
    assert (await anext(stream)).text == "provider answer"
    source = home.parent / "package"
    path = source / "provider_probe_plugin.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace("provider answer", "new answer"), encoding="utf-8"
    )
    manifest_path = source / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    installed = app.plugins.install(source)
    await app.plugins.select(PLUGIN, installed.package_digest)
    assert not generation.stopped and generation.lease_count == 1
    events = [event async for event in provider.stream(model=model, messages=[])]
    assert events[0].text == "new answer"
    await asyncio.wait_for(stream.aclose(), 5)
    await asyncio.wait_for(generation.retirement_ready.wait(), 5)
    assert generation.lease_count == 0 and generation.gate.in_flight == 0


async def test_restart_loads_provider_default_and_keeps_route_and_usage(system):
    app, home = system
    await app.close()
    async with LocalApplication.build(home) as restarted:
        outcome = await restarted.runtime.run("after restart")
        assert outcome.run.status == "completed", outcome.run.error
        assert outcome.final_text == "provider answer"
        assert restarted.runtime.adapter.model.provider_id == app.runtime.adapter.model.provider_id
        assert restarted.runtime.usage.list_entries(UsageFilter()).entries[0].total_tokens == 10


async def test_provider_error_redacts_route_credential(system):
    app, home = system
    configs = ProviderConfigManager(home)
    model = app.runtime.adapter.model
    configs.update_provider(model.provider_id, api_key="unique-provider-secret")
    configs.update_endpoint(
        model.provider_id, model.endpoint_id, model_id="error", display_name="Failure"
    )
    outcome = await app.runtime.run("fail")
    assert outcome.run.status == "failed"
    assert "unique-provider-secret" not in outcome.run.error
    assert "REDACTED" in outcome.run.error


async def test_provider_retry_and_error_diagnostics_redact_resolved_credentials(system):
    app, home = system
    model = app.runtime.adapter.model
    configs = ProviderConfigManager(home)
    secret = "opaque-diagnostic-credential"
    configs.update_provider(model.provider_id, api_key=secret)
    configs.update_endpoint(
        model.provider_id, model.endpoint_id, model_id="diagnostics", display_name="Diagnostics"
    )
    result = await app.runtime.run("diagnostic failure")
    assert result.run.status == "failed"
    retry = next(event for event in result.events if event.event_type == "run_retry_scheduled")
    assert "REDACTED" in retry.model_dump_json()
    assert secret not in result.run.model_dump_json()
    assert all(secret not in event.model_dump_json() for event in result.events)
    adapter = app.runtime.models.adapter_for(f"{model.provider_id}/{model.endpoint_id}")
    check = await adapter.provider.check_connection(adapter.model)
    assert not check.ok
    assert secret not in check.model_dump_json()
    assert "REDACTED" in check.message
    assert check.error_code == "[REDACTED]"


async def test_provider_tool_stream_executes_native_tool_and_replays_result(system):
    app, home = system
    path = home / "input.txt"
    path.write_text("native tool result", encoding="utf-8")
    model = app.runtime.adapter.model
    configs = ProviderConfigManager(home)
    configs.update_endpoint(
        model.provider_id,
        model.endpoint_id,
        model_id="tools",
        display_name="Tool model",
        request_options={"path": str(path)},
    )
    outcome = await app.runtime.run("Read the file", workspace=home)
    assert outcome.run.status == "completed", outcome.run.error
    assert "native tool result" in outcome.final_text
    assert outcome.run.model_round_count == 2
    entries = app.runtime.usage.list_entries(UsageFilter()).entries
    assert len(entries) == 2 and sum(item.total_tokens for item in entries) == 20


async def test_provider_request_options_do_not_mutate_cached_model_route(system):
    app, home = system
    model = app.runtime.adapter.model
    configs = ProviderConfigManager(home)
    configs.update_endpoint(
        model.provider_id,
        model.endpoint_id,
        model_id="mutate",
        display_name="Local copy",
        request_options={"nested": {"value": "original"}},
    )
    for _ in range(2):
        outcome = await app.runtime.run("request")
        assert outcome.run.status == "completed", outcome.run.error
        assert outcome.final_text == "original"


async def test_provider_does_not_inherit_driver_control_or_bypass_active_run(system):
    app, home = system
    model = app.runtime.adapter.model
    ProviderConfigManager(home).update_endpoint(
        model.provider_id,
        model.endpoint_id,
        model_id="scope",
        display_name="Scope",
    )
    for driver in ("ouro.default", f"{PLUGIN}/run_drivers/review"):
        outcome = await app.runtime.run("check scope", driver_id=driver)
        assert outcome.run.status == "completed", outcome.run.error
        failures = json.loads(outcome.final_text)
        assert len(failures) == 2
        assert "live parent Run invocation" in failures[0]
        assert "active Run" in failures[1]


async def test_registered_existing_adapter_preserves_native_events():
    from pydantic import TypeAdapter

    from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta, Usage
    from ouro.plugins.catalog import ContributionBinding
    from ouro.plugins.providers import PluginProvider, adapt_provider
    from ouro.providers import FauxProvider
    from ouro.sdk.plugins.providers import (
        ProviderCheck,
        ProviderConnection,
        ProviderContribution,
        ProviderEvent,
    )

    native = FauxProvider(
        [
            ProviderStreamEvent(type="reasoning_delta", reasoning_content="think"),
            ProviderStreamEvent(
                type="tool_call_delta", tool_call_delta=ToolCallDelta(index=0, name_delta="read")
            ),
            ProviderStreamEvent(type="done", usage=Usage(input_tokens=3, output_tokens=5)),
        ]
    )
    model = ModelMetadata(provider_id="faux", model_id="faux")

    async def stream(request, output):
        async for event in native.stream(model=model, messages=[]):
            await output(TypeAdapter(ProviderEvent).validate_python(event.model_dump()))

    async def check(request):
        result = await native.check_connection(model)
        return ProviderCheck(result.ok, result.message)

    registration = adapt_provider(ProviderContribution("faux", stream, check), owner_id=PLUGIN)

    async def bind(identity):
        return ContributionBinding(registration, ())

    provider = PluginProvider("faux", PROTOCOL, bind, lambda context: ProviderConnection(PROTOCOL))
    actual = [event async for event in provider.stream(model=model, messages=[])]
    expected = [event async for event in native.stream(model=model, messages=[])]
    assert actual == expected
    assert (await provider.check_connection(model)).ok


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_public_provider_example_runs_through_cli_check_and_native_route(
    tmp_path, kind, capsys
):
    from ouro.cli.provider_commands import check_provider_connection

    home = tmp_path / "home"
    source = tmp_path / "faux-provider"
    shutil.copytree(ROOT / "examples/plugins/faux-provider", source)
    plugin_id = "dev.ouro.examples.faux_provider"
    if kind == "worker":
        manifest_path = source / ".ouro-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime"] = {
            "kind": "worker",
            "command": ["{python}", "worker.py"],
            "protocol": "3.0",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    async with LocalApplication.build(home) as app:
        app.plugins.install(source)
        assert (await app.plugins.enable(plugin_id)).status == "active"
    configs = ProviderConfigManager(home)
    provider = configs.create_provider(
        display_name="Plugin Faux", provider_type="custom", protocol=f"{plugin_id}/providers/faux"
    )
    endpoint = configs.create_endpoint(provider.provider_id, model_id="faux", make_default=True)
    configs.set_default_route(provider.provider_id, endpoint.endpoint_id)
    assert await check_provider_connection(home=home, json_mode=True) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    async with LocalApplication.build(home) as app:
        outcome = await app.runtime.run("example")
        assert outcome.final_text == "Hello from OURO."
        await app.plugins.disable(plugin_id)
        await app.plugins.remove(plugin_id)
    assert await check_provider_connection(home=home, json_mode=True) != 0
