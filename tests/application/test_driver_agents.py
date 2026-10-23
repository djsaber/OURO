from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.application.agents.definitions import AgentDefinitionService
from ouro.application.agents.service import AgentService
from ouro.application.plugins.service import PluginService
from ouro.application.runtime.service import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import ToolSelection
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.plugins.contributions.preparation import ContributionCoordinator
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult

PLUGIN = "dev.ouro.examples.delegating_driver"
DRIVER = f"{PLUGIN}/run_drivers/delegate"
EXAMPLE = Path(__file__).parents[2] / "examples/plugins/delegating-driver"


class DelegationProvider(FauxProvider):
    def __init__(self, act: bool):
        super().__init__()
        self.act = act
        self.root_calls = 0
        self.child_calls = 0
        self.children_ready = asyncio.Event()
        self.release_children = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        if "CHILD ONLY" in system_prompt:
            self.child_calls += 1
            child = self.child_calls
            if child == 2:
                self.children_ready.set()
            await self.release_children.wait()
            yield ProviderStreamEvent(type="text_delta", text=f"reader evidence {child}")
        else:
            self.root_calls += 1
            if self.root_calls == 1:
                assert tools == []
                yield ProviderStreamEvent(type="text_delta", text="Investigate the user request")
            else:
                assert self.root_calls == 2
                receipts = [item for item in messages if item.tool_result is not None]
                assert len(receipts) == 2
                assert "reader evidence 1" in "\n".join(item.text_content for item in receipts)
                assert "reader evidence 2" in "\n".join(item.text_content for item in receipts)
                assert all(item.tool_result.producer.driver_id == DRIVER for item in receipts)
                assert not any(item.tool_calls for item in messages)
                if self.act:
                    yield ProviderStreamEvent(
                        type="tool_call_delta",
                        tool_call_delta=ToolCallDelta(
                            index=0, id_delta="deploy", name_delta="Deploy", arguments_delta="{}"
                        ),
                    )
                else:
                    yield ProviderStreamEvent(type="text_delta", text="review complete")
        yield ProviderStreamEvent(type="done")


def assemble(tmp_path, store, provider, calls):
    tools = ToolRegistry()

    def deploy(invocation, context):
        calls.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name="Deploy", content="deployed"
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Deploy", description="Deploy", permission_mode="approval"
            ),
            handler=deploy,
        )
    )
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path / "home"))
    if definitions.list(tools=tools).revision == 0:
        definitions.create(
            agent_id="reader",
            name="Reader",
            description="Read bounded evidence",
            system_prompt="CHILD ONLY",
            tool_selection=ToolSelection(mode="selected", names=()),
            tools=tools,
            expected_revision=0,
        )
    plugins = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=tools, observers=ObserverRegistry()),
    )
    plugin_service = PluginService(plugins.store, runtime=plugins, session_store=store)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
        tools=tools,
        tool_execution_config=ToolExecutionConfig(mode="parallel"),
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=2)
    agents = AgentService(
        store=store, definitions=definitions, scheduler=supervisor, events=runtime.event_recorder
    )
    runtime.bind_extensions(catalog=plugins, agent_definitions=definitions, agent_controls=agents)
    return runtime, supervisor, agents, plugins, plugin_service


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("act", [False, True])
@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("restart", [False, True])
async def test_driver_parallel_agents_review_and_conditional_approved_action(
    tmp_path, kind, act, store_type, restart
):
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    provider = DelegationProvider(act)
    calls = []
    runtime, supervisor, agents, plugins, service = assemble(tmp_path, store, provider, calls)
    package = tmp_path / "package"
    shutil.copytree(EXAMPLE, package)
    manifest = package / ".ouro-plugin/plugin.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if kind == "worker":
        value["runtime"] = {
            "kind": "worker",
            "command": ["{python}", "worker.py"],
            "protocol": "3.0",
        }
    manifest.write_text(json.dumps(value), encoding="utf-8")
    service.install(package)
    assert (await service.enable(PLUGIN)).status == "active"
    await agents.start()
    try:
        pending = await runtime.run("Investigate and act", driver_id=DRIVER)
        assert pending.run.status == "waiting_child", pending.run.error
        async with asyncio.timeout(5):
            await provider.children_ready.wait()
        assert provider.child_calls == 2
        assert pending.run.driver_checkpoint.payload == "review"
        assert pending.run.active_tool_batch.producer.driver_id == DRIVER
        relations = store.list_delegations(pending.session.session_id)
        assert len(relations) == 2
        assert all(relation.parent_run_id == pending.run.run_id for relation in relations)
        if restart:
            # Leave native child results durable without a live parent waiter.
            await agents.close()
        provider.release_children.set()
        if restart:
            # Durable child writes and their journal notifications may lag under
            # parallel coverage. This guards hangs, not settlement latency.
            async with asyncio.timeout(30):
                completed_children = await asyncio.gather(
                    *(
                        supervisor.wait_run(
                            relation.child_session_id, relation.current_child_run_id
                        )
                        for relation in relations
                    )
                )
            assert all(child.status == "completed" for child in completed_children)
            await supervisor.shutdown()
            await plugins.close()
            parked = store.load_run(pending.session.session_id, pending.run.run_id)
            assert parked.status == "waiting_child"
            assert parked.driver_checkpoint.state == "pending"
            runtime, supervisor, agents, plugins, _ = assemble(tmp_path, store, provider, calls)
            await plugins.start()
            await agents.start()
            await runtime.reconcile_session(pending.session.session_id)
        async with asyncio.timeout(60):
            result = await supervisor.wait_run(
                pending.session.session_id,
                pending.run.run_id,
                until=frozenset({"waiting_approval"}),
            )
        assert result.status == ("waiting_approval" if act else "completed"), result.error
        async with asyncio.timeout(5):
            while plugins.catalog().lease_count:
                await asyncio.sleep(0.01)
        assert result.model_round_count == 2
        assert provider.root_calls == 2
        assert calls == []
        if act:
            assert result.driver_checkpoint.payload == "finish"
            handle = await supervisor.resolve_tool_approval(
                session_id=result.session_id,
                run_id=result.run_id,
                approval_id=result.pending_approval_id,
                approved=True,
            )
            resumed = await supervisor.wait(handle)
            assert resumed.status == "completed", resumed
            assert (
                store.load_runtime_session(result.session_id).messages[-1].text_content
                == "deployed"
            )
            assert calls == ["deploy"]
        snapshot = store.load_runtime_session(pending.session.session_id)
        spawns = [
            item
            for item in snapshot.messages
            if item.tool_result is not None and item.tool_result.tool_name == "SpawnAgent"
        ]
        assert len(spawns) == 2
        assert all(item.tool_result.producer.driver_id == DRIVER for item in spawns)
        assert {item.tool_result.tool_call_id for item in spawns} == {
            relation.parent_tool_call_id for relation in relations
        }
        assert all(
            not item.tool_calls or item.tool_calls[0].name == "Deploy" for item in snapshot.messages
        )
    finally:
        provider.release_children.set()
        await agents.close()
        await supervisor.shutdown()
        await plugins.close()
