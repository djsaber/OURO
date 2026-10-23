from __future__ import annotations

import asyncio

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.plugins.test_tool_groups import PLUGIN, group, make_runtime


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_model_decision_keeps_original_tools_and_next_decision_sees_new_group(
    tmp_path, store_type
):
    instances = []
    plugins = make_runtime(tmp_path, instances)
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    requested = asyncio.Event()
    release = asyncio.Event()
    observed = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            observed.append(kwargs["tools"])
            if len(observed) == 1:
                requested.set()
                await release.wait()
            async for event in super().stream(**kwargs):
                yield event

    provider = Provider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    ToolCallRequest(id="original", name="discovered", arguments={"query": "term"})
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent.from_tool_call(
                    ToolCallRequest(id="updated", name="discovered", arguments={"topic": "new"})
                ),
                ProviderStreamEvent(type="done"),
            ],
            [ProviderStreamEvent(type="text_delta", text="done"), ProviderStreamEvent(type="done")],
        ]
    )
    runtime = RuntimeService(
        store=store,
        tools=plugins.coordinator.tools,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
    )
    runtime.bind_extensions(catalog=plugins)
    task = None
    try:
        assert await plugins.enable(PLUGIN) == "active"
        task = asyncio.create_task(runtime.run("search twice"))
        async with asyncio.timeout(5):
            await requested.wait()
        await instances[-1].context.tool_groups.publish(group("two", argument="topic"))
        release.set()
        result = await task
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "done"
        assert len(observed) == 3
        assert observed[0][0]["parameters_schema"]["required"] == ["query"]
        assert observed[1][0]["parameters_schema"]["required"] == ["topic"]
        history = store.load_runtime_session(result.session.session_id)
        results = [message for message in history.messages if message.tool_result]
        assert [item.text_content for item in results] == ["one:term", "two:new"]
        assert plugins.catalog().lease_count == 0
        assert plugins._active[PLUGIN].lease_count == 0
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_approval_restart_requires_original_dynamic_tool_revision(
    tmp_path, store_type, changed
):
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()

    def host(provider):
        instances = []
        plugins = make_runtime(tmp_path, instances)
        runtime = RuntimeService(
            store=store,
            tools=plugins.coordinator.tools,
            adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
            permission_overrides={"discovered": "approval"},
        )
        runtime.bind_extensions(catalog=plugins)
        return runtime, plugins, instances

    provider = FauxProvider(
        [
            ProviderStreamEvent.from_tool_call(
                ToolCallRequest(id="pending", name="discovered", arguments={"query": "term"})
            ),
            ProviderStreamEvent(type="done"),
        ]
    )
    runtime, plugins, instances = host(provider)
    try:
        assert await plugins.enable(PLUGIN) == "active"
        if changed:
            await instances[-1].context.tool_groups.publish(group("two"))
        pending = await runtime.run("search")
        assert pending.run.status == "waiting_approval", pending.run.error
        assert pending.run.driver_checkpoint.state == "pending"
        assert plugins.catalog().lease_count == 0
    finally:
        await runtime.close()
        await plugins.close()

    runtime, plugins, _ = host(
        FauxProvider(
            [ProviderStreamEvent(type="text_delta", text="done"), ProviderStreamEvent(type="done")]
        )
    )
    try:
        await plugins.start()
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        history = store.load_runtime_session(result.session.session_id)
        results = [message for message in history.messages if message.tool_result]
        if changed:
            assert not any(item.text_content == "one:term" for item in results)
            assert any("ToolExecutionContractMismatch" in item.text_content for item in results)
        else:
            assert [item.text_content for item in results] == ["one:term"]
        assert result.run.status == "completed", result.run.error
        assert result.run.driver_checkpoint.state == "consumed"
    finally:
        await runtime.close()
        await plugins.close()
