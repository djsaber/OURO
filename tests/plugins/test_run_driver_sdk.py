from types import SimpleNamespace

import pytest

from ouro.core import run_drivers as core
from ouro.plugins.catalog import GenerationGate
from ouro.plugins.invocation import current_invocation, plugin_invocation
from ouro.plugins.native_ports import PluginNativePorts
from ouro.plugins.run_drivers import adapt_run_driver
from ouro.plugins.worker_drivers import WorkerDriverBridge
from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.worker_drivers import WorkerRunContext


async def test_driver_invocation_inherits_accepted_run_workspace_constraints():
    async def handler(context, resume):
        invocation = current_invocation.get()
        assert invocation.session_id == "session"
        assert invocation.run_id == "run"
        assert invocation.workspace == "workspace"
        assert invocation.workspace_only
        return sdk.DriverFinished("done")

    driver = adapt_run_driver(sdk.RunDriverContribution("review", handler), owner_id="plugin")
    result = await driver.handler(
        SimpleNamespace(
            session_id="session", run_id="run", workspace="workspace", workspace_only=True
        ),
        None,
    )
    assert result == core.DriverFinished("done")
    assert current_invocation.get() is None


@pytest.mark.parametrize("method", ["run.models.request", "run.inputs.consume"])
async def test_worker_native_ports_reject_requests_without_a_live_parent(method):
    ports = PluginNativePorts(
        GenerationGate("generation"), frozenset({"sessions.write"}), plugin_id="dev.ouro.test"
    )
    bridge = WorkerDriverBridge(SimpleNamespace(), models=ports, tools=ports, inputs=ports)
    with pytest.raises(RuntimeError, match="live parent"):
        await bridge.dispatch(method, {"run_id": "forged"})


async def test_accepted_child_driver_gets_its_own_scope_after_parent_callback_has_ended():
    async def handler(context, resume):
        invocation = current_invocation.get()
        assert invocation.session_id == "child-session"
        assert invocation.run_id == "child-run"
        assert invocation.workspace == "child-workspace"
        assert invocation.native_run is context
        return sdk.DriverFinished("done")

    driver = adapt_run_driver(sdk.RunDriverContribution("child", handler), owner_id="child")
    with plugin_invocation("parent", writable=True, session_id="parent", run_id="parent") as parent:
        parent.close()
        result = await driver.handler(
            SimpleNamespace(
                session_id="child-session",
                run_id="child-run",
                workspace="child-workspace",
                workspace_only=True,
            ),
            None,
        )
        assert current_invocation.get() is parent
    assert result == core.DriverFinished("done")


async def test_sdk_context_cannot_be_reused_inside_another_run_invocation():
    contexts = []

    async def handler(context, resume):
        if contexts:
            with pytest.raises(RuntimeError, match="another invocation"):
                await contexts[0].models.request()
        contexts.append(context)
        return sdk.DriverFinished("done")

    driver = adapt_run_driver(sdk.RunDriverContribution("review", handler), owner_id="plugin")
    for identity in ("first", "second"):
        await driver.handler(
            SimpleNamespace(
                session_id=identity, run_id=identity, workspace=None, workspace_only=False
            ),
            None,
        )
    with pytest.raises(RuntimeError, match="live parent"):
        await contexts[0].models.request()


async def test_copied_callback_context_cannot_call_native_ports_after_it_ends():
    ports = PluginNativePorts(
        GenerationGate("generation"), frozenset({"sessions.write"}), plugin_id="dev.ouro.test"
    )
    with plugin_invocation("plugin", writable=True, native_run=SimpleNamespace()) as invocation:
        invocation.close()
        with pytest.raises(RuntimeError, match="finished"):
            await ports.request()


async def test_worker_driver_preserves_typed_media_and_resource_references():
    class Peer:
        async def request(self, method, params, *, timeout):
            assert method == "run.tools.execute_model_response"
            assert params["checkpoint"] == {"step": "review"}
            return {
                "suspended": False,
                "results": [
                    {
                        "tool_call_id": "call",
                        "tool_name": "Render",
                        "is_error": False,
                        "content": [
                            {"type": "text", "text": "rendered"},
                            {
                                "type": "image",
                                "uri": "ouro-resource:image",
                                "media_type": "image/png",
                            },
                        ],
                        "structured": {"width": 42},
                        "resource_refs": ["ouro-resource:image"],
                    }
                ],
            }

    context = WorkerRunContext(Peer(), "session", "run")
    result = await context.tools.execute_model_response("response", checkpoint={"step": "review"})
    assert result.results[0].content == (
        sdk.ToolTextContent("rendered"),
        sdk.ToolImageContent("ouro-resource:image", "image/png"),
    )
    assert result.results[0].structured == {"width": 42}
    assert result.results[0].resource_refs == ("ouro-resource:image",)
