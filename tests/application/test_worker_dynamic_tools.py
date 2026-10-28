from __future__ import annotations

import asyncio

import pytest

from ouro.application.plugins.service import PluginService
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.plugins.contributions.preparation import ContributionCoordinator
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.application.helpers import wait_for_runtime_event
from tests.plugins.test_worker_tool_groups import PLUGIN, package


def host(tmp_path, store, provider, *, approval=False):
    tools = ToolRegistry()
    plugins = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=tools, observers=ObserverRegistry()),
    )
    service = PluginService(plugins.store, runtime=plugins, session_store=store)
    runtime = RuntimeService(
        store=store,
        tools=tools,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
        permission_overrides={"RecordData": "approval"} if approval else {},
    )
    runtime.bind_extensions(catalog=plugins)
    return runtime, plugins, service


def events(identity, arguments):
    return [
        ProviderStreamEvent.from_tool_call(
            ToolCallRequest(id=identity, name="RecordData", arguments=arguments)
        ),
        ProviderStreamEvent(type="done"),
    ]


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_public_dynamic_record_plugin_keeps_decisions_and_commits_each_schema(
    tmp_path, kind, store_type
):
    store = store_type(
        tmp_path / "sessions.db" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    requested, release = asyncio.Event(), asyncio.Event()
    schemas = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            schemas.append(next(item for item in kwargs["tools"] if item["name"] == "RecordData"))
            if len(schemas) == 1:
                requested.set()
                await release.wait()
            async for event in super().stream(**kwargs):
                yield event

    provider = Provider(
        event_batches=[
            events("first", {"title": "original"}),
            events("second", {"topic": "current"}),
            [ProviderStreamEvent(type="text_delta", text="done"), ProviderStreamEvent(type="done")],
        ]
    )
    runtime, plugins, service = host(tmp_path, store, provider)
    service.install(package(tmp_path, kind))
    task = None
    try:
        assert (await service.enable(PLUGIN)).status == "active", service.diagnostics()
        task = asyncio.create_task(runtime.run("save two records"))
        await wait_for_runtime_event(requested, task, description="initial RecordData schema")
        await service.execute_command(f"{PLUGIN}:record-fields", "topic")
        release.set()
        result = await task
        assert result.run.status == "completed", result.run.error
        assert schemas[0]["parameters_schema"]["required"] == ["title"]
        assert schemas[1]["parameters_schema"]["required"] == ["topic"]
        values = {
            value.owner_id: value
            for value in store.load_session_extension_states(result.session.session_id)
        }
        assert values[PLUGIN].payload == {"records": [{"title": "original"}, {"topic": "current"}]}
        history = store.load_session(result.session.session_id)
        assert [message.text_content for message in history.messages if message.tool_result] == [
            "Record saved.",
            "Record saved.",
        ]
        assert plugins.catalog().lease_count == 0
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_worker_group_approval_restart_checks_revision_before_session_mutation(
    tmp_path, changed, store_type
):
    store = store_type(
        tmp_path / "sessions.db" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    arguments = {"topic": "pending"} if changed else {"title": "pending"}
    runtime, plugins, service = host(
        tmp_path, store, FauxProvider(events("pending", arguments)), approval=True
    )
    service.install(package(tmp_path))
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        if changed:
            await service.execute_command(f"{PLUGIN}:record-fields", "topic")
        pending = await runtime.run("record")
        assert pending.run.status == "waiting_approval", pending.run.error
        assert pending.run.driver_checkpoint.state == "pending"
        assert plugins.catalog().lease_count == 0
    finally:
        await runtime.close()
        await plugins.close()

    runtime, plugins, service = host(
        tmp_path,
        store,
        FauxProvider(
            [ProviderStreamEvent(type="text_delta", text="done"), ProviderStreamEvent(type="done")]
        ),
        approval=True,
    )
    try:
        await plugins.start()
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.run.driver_checkpoint.state == "consumed"
        values = {
            value.owner_id: value
            for value in store.load_session_extension_states(result.session.session_id)
        }
        history = store.load_session(result.session.session_id)
        outputs = [message for message in history.messages if message.tool_result]
        assert len(outputs) == 1
        if changed:
            assert PLUGIN not in values
            assert "ToolExecutionContractMismatch" in outputs[0].text_content
        else:
            assert values[PLUGIN].payload == {"records": [{"title": "pending"}]}
            assert outputs[0].text_content == "Record saved."
    finally:
        await runtime.close()
        await plugins.close()
