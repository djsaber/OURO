from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.application.plugins import PluginService
from ouro.core.interactions import AnswerResolution, UnattendedResolution
from ouro.core.observers import ObserverRegistry
from ouro.plugins.catalog import PluginGenerationUnavailableError
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.tools import ToolInputRequest, ToolInvocation, ToolRegistry

PLUGIN = "dev.ouro.examples.dynamic_records"
EXAMPLE = Path(__file__).parents[2] / "examples/plugins/dynamic-records"
PROBE = Path(__file__).parent / "fixtures/dynamic_tool_worker.py"


def package(tmp_path, kind="worker", *, probe=False):
    source = tmp_path / "package"
    shutil.copytree(EXAMPLE, source)
    manifest = source / ".ouro-plugin/plugin.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    if kind == "python-inprocess":
        raw["runtime"] = {"kind": kind, "entrypoint": "dynamic_records_plugin:create_plugin"}
    elif probe:
        shutil.copyfile(PROBE, source / PROBE.name)
        raw["runtime"]["command"] = ["{python}", PROBE.name]
        raw["tool_groups"][0]["resumable"] = True
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    return source


@pytest.fixture
async def worker(tmp_path):
    runtime = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=ToolRegistry(), observers=ObserverRegistry()),
    )
    service = PluginService(runtime.store, runtime=runtime)
    service.install(package(tmp_path, probe=True))
    try:
        assert (await service.enable(PLUGIN)).status == "active", service.diagnostics()
        instance = runtime._active[PLUGIN].instance
        instance.client.max_incoming_requests = 1
        yield runtime, instance
    finally:
        await runtime.close()


async def handlers(client, expected):
    async with asyncio.timeout(5):
        while True:
            identities = await client.request("test.inspect")
            if len(identities) == expected:
                return identities
            await asyncio.sleep(0.01)


async def test_worker_replacement_and_withdrawal_keep_accepted_handlers_until_lease_release(worker):
    runtime, instance = worker
    client = instance.client
    original = runtime.snapshot()
    try:
        first_id = (await handlers(client, 1))[0]
        first = await client.request("test.publish", {"fields": ["topic"]})
        accepted = runtime.snapshot()
        try:
            assert len(await handlers(client, 2)) == 2
            second = await client.request("test.publish", {"fields": ["category"]})
            await handlers(client, 3)
            await client.request("test.close", {"index": first})
            with runtime.snapshot() as current:
                assert current.tools.get("RecordData").definition.parameters_schema["required"] == [
                    "category"
                ]
            await client.request("test.close", {"index": second})
            await client.request("test.close", {"index": second})
            await handlers(client, 2)
            with runtime.snapshot() as current:
                assert current.tools.names() == []
            result = await client.request(
                "tool_groups.call",
                {
                    "registration_id": first_id,
                    "contribution_id": "record",
                    "call": {
                        "tool_call_id": "old",
                        "session_id": "s",
                        "run_id": "r",
                        "tool_name": "RecordData",
                        "arguments": {"title": "kept"},
                    },
                    "session_state": {
                        "writable": True,
                        "value": None,
                        "controls": [],
                        "run_controls": [],
                        "usage_tokens": None,
                    },
                },
            )
            assert result["structured"]["record"] == {"title": "kept"}
        finally:
            accepted.close()
        await handlers(client, 1)
    finally:
        original.close()
    await handlers(client, 0)
    assert not instance.tool_group_bridge._owned


async def test_worker_rejected_publication_releases_only_unpublished_handlers(worker):
    runtime, instance = worker
    before = runtime.catalog().catalog_id
    with pytest.raises(RuntimeError, match="contract does not match"):
        await instance.client.request("test.publish", {"invalid": True})
    assert runtime.catalog().catalog_id == before
    await handlers(instance.client, 1)
    assert len(instance.tool_group_bridge._owned) == 1


async def test_worker_read_only_call_cannot_publish_or_withdraw_group(worker):
    runtime, instance = worker
    index = await instance.client.request("test.publish", {"fields": ["topic"]})
    await handlers(instance.client, 1)
    with plugin_invocation(PLUGIN, writable=False):
        with pytest.raises(RuntimeError, match="read-only"):
            await instance.client.request("test.publish", {"fields": ["title"]})
        with pytest.raises(RuntimeError, match="read-only"):
            await instance.client.request("test.close", {"index": index})
    await handlers(instance.client, 1)
    with runtime.snapshot() as catalog:
        assert catalog.tools.get("RecordData").definition.parameters_schema["required"] == ["topic"]


async def test_cancelled_publication_reply_withdraws_group_but_preserves_accepted_handler(worker):
    runtime, instance = worker
    published, release = asyncio.Event(), asyncio.Event()
    dispatch = instance.tool_group_bridge.dispatch

    async def interrupted(method, params):
        result = await dispatch(method, params)
        if method == "tool_groups.publish":
            published.set()
            await release.wait()
        return result

    instance.tool_group_bridge.dispatch = interrupted
    task = asyncio.create_task(instance.client.request("test.publish", {"fields": ["topic"]}))
    accepted = None
    try:
        async with asyncio.timeout(5):
            await published.wait()
            accepted = runtime.snapshot()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            while runtime.catalog().tools.names():
                await asyncio.sleep(0.01)
        await handlers(instance.client, 1)
        assert accepted.tools.get("RecordData").definition.parameters_schema["required"] == [
            "topic"
        ]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if accepted is not None:
            accepted.close()
    await handlers(instance.client, 0)


async def test_disable_releases_worker_even_with_old_group_lease(worker):
    runtime, instance = worker
    old = runtime.snapshot()
    try:
        assert await runtime.disable(PLUGIN) == "disabled"
        assert instance.client.process is None
        assert not instance.tool_group_bridge._owned
        assert not instance.tool_group_bridge._cleanup
    finally:
        old.close()
    await asyncio.sleep(0)
    assert not instance.tool_group_bridge._cleanup


async def test_worker_tool_lifetime_belongs_to_run_not_transport_default(worker):
    runtime, instance = worker
    await instance.client.request("test.publish", {"delay": True})
    instance.client.request_timeout = 0.01
    try:
        with runtime.snapshot() as accepted:
            result = await accepted.tools.get("RecordData").execute(
                ToolInvocation(
                    session_id="s", run_id="r", tool_name="RecordData", arguments={"title": "x"}
                )
            )
            assert result.content == "delayed result"
    finally:
        instance.client.request_timeout = 30


@pytest.mark.parametrize("kind", ["answer", "unattended"])
async def test_worker_group_resume_routes_to_original_publication_after_replacement(worker, kind):
    runtime, instance = worker
    await instance.client.request("test.publish", {"wait": True, "fields": ["topic"]})
    with runtime.snapshot() as accepted:
        tool = accepted.tools.get("RecordData")
        invocation = ToolInvocation(session_id="s", run_id="r", tool_name="RecordData")
        request = await tool.execute(invocation)
        assert isinstance(request, ToolInputRequest)
        await instance.client.request("test.publish", {"fields": ["category"]})
        resolution = (
            AnswerResolution(value=True)
            if kind == "answer"
            else UnattendedResolution(resolver_id="policy", reason="background")
        )
        result = await tool.resume(invocation, resolution, request=request)
        assert result.content == f"topic:{kind}"
    await handlers(instance.client, 1)


async def test_worker_exit_clears_group_resources_before_old_catalog_is_released(worker):
    runtime, instance = worker
    accepted = runtime.snapshot()
    try:
        instance.client.process.terminate()
        async with asyncio.timeout(10):
            while runtime.has_resources(PLUGIN):
                await asyncio.sleep(0.01)
        assert runtime.status(PLUGIN) == "failed"
        assert instance.client.process is None
        assert not instance.tool_group_bridge._owned
        with pytest.raises(PluginGenerationUnavailableError):
            await accepted.tools.get("RecordData").execute(
                ToolInvocation(session_id="s", run_id="r", tool_name="RecordData")
            )
    finally:
        accepted.close()


async def test_worker_candidate_cannot_publish_before_activation(tmp_path):
    source = package(tmp_path)
    entrypoint = source / "worker.py"
    text = entrypoint.read_text(encoding="utf-8")
    entrypoint.write_text(
        text.replace(
            "async def prepare(self, params):",
            "async def prepare(self, params):\n        await self.groups.publish(record_group())",
        ),
        encoding="utf-8",
    )
    runtime = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=ToolRegistry(), observers=ObserverRegistry()),
    )
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    try:
        assert (await service.enable(PLUGIN)).status == "failed"
        assert any("active plugin generation" in item.message for item in service.diagnostics())
        assert not runtime.has_resources(PLUGIN)
        assert runtime.catalog().tools.names() == []
    finally:
        await runtime.close()
