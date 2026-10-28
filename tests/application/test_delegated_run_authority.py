from __future__ import annotations

import asyncio
from contextlib import closing

import pytest

from ouro.application.agents.definitions import AgentDefinitionService
from ouro.application.runs.lifecycle import reserve_run
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import HostSessionInput
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.delegations import record_path
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.agent_facts import seed_delegation
from tests.application.test_compaction import (
    RecordingProvider,
    RecordingStrategy,
    _append_turns,
    _runtime,
)


@pytest.fixture(params=("files", "sqlite"))
def store(request, tmp_path):
    store = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    return store


def prepared_run(store, child, relation):
    return reserve_run(
        store,
        RunRecord(
            session_id=child.session_id,
            status="queued",
            parent_run_id=relation.parent_run_id,
            initiator_id=f"ouro.subagents/{relation.delegation_id}",
            metadata={
                "root_session_id": relation.root_session_id,
                "delegation_id": relation.delegation_id,
                "agent_id": relation.agent_id,
                "agent_definition_contract_hash": relation.definition.contract_hash,
            },
        ),
    )


async def test_cold_child_binding_uses_frozen_prompt_budget_and_empty_tool_ceiling(
    store, tmp_path, monkeypatch
):
    effects = tmp_path / "effects.txt"

    def handler(invocation, context):
        effects.write_text("executed", encoding="utf-8")
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="extra", content="done")

    tools = ToolRegistry()
    tools.register(
        Tool(definition=ToolDefinition(name="extra", description="Host tool"), handler=handler)
    )
    child = SessionRecord()
    store.create_session(child)
    relation = seed_delegation(store, child, budget=RunBudgetPolicy(max_model_rounds=1))
    run = prepared_run(store, child, relation)
    # The next Runtime/Store is a cold owner, with a broader Host configuration.
    cold = (
        LocalSessionStore(store.root)
        if isinstance(store, LocalSessionStore)
        else SQLiteSessionStore(store.database)
    )
    cold.initialize()
    requests = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            requests.append(kwargs)
            yield ProviderStreamEvent.from_tool_call(
                {"id": "call_extra", "name": "extra", "arguments": {}}
            )
            yield ProviderStreamEvent(type="done")

    def no_scan(*args, **kwargs):
        raise AssertionError("Child binding must point-read its frozen Delegation")

    monkeypatch.setattr(cold, "list_delegations", no_scan)
    monkeypatch.setattr(cold, "load_delegation_by_id", no_scan)
    monkeypatch.setattr(cold, "find_delegation_by_child_session", no_scan)
    async with RuntimeService(
        store=cold,
        tools=tools,
        run_budget=RunBudgetPolicy(),
        adapter=RuntimeAdapter(Provider(), ModelMetadata(model_id="faux", provider_id="faux")),
    ) as runtime:
        result = await runtime.run_prepared(
            run, HostSessionInput(prompt="work", host_source_id="test")
        )
    assert result.run.status == "paused", result.run.error
    assert result.run.metadata["pause"]["reason"] == "model_rounds"
    assert len(requests) == 1
    assert requests[0]["tools"] == []
    assert relation.definition.system_prompt in requests[0]["system_prompt"]
    assert not effects.exists()
    assert not {
        "effective_tool_names",
        "effective_tool_refs",
        "delegated_constraints",
        "agent_system_prompt",
        "run_budget",
    }.intersection(cold.load_run(child.session_id, run.run_id).metadata)

    # Even a current broader tool ceiling cannot replace the frozen source through CAS.
    reference = AgentDefinitionService.tool_reference(tools, tools.get("extra").definition)
    with pytest.raises(PersistenceError, match="frozen contract cannot change: effective_tools"):
        cold.save_delegation(
            relation.model_copy(update={"effective_tools": (reference,)}), expected_revision=0
        )
    assert cold.load_delegation(relation.root_session_id, relation.delegation_id) == relation


@pytest.mark.parametrize("damage", ["contract", "child", "missing"])
async def test_invalid_delegation_fails_closed_before_exposing_host_tools(store, damage):
    child = SessionRecord()
    store.create_session(child)
    relation = seed_delegation(store, child)
    run = prepared_run(store, child, relation)
    if damage == "contract":
        run.metadata["agent_definition_contract_hash"] = "different-contract"
    elif damage == "child":
        other = SessionRecord()
        store.create_session(other)
        other_relation = seed_delegation(store, other)
        store.save_session(
            store.load_session_record(child.session_id).model_copy(
                update={
                    "root_session_id": other_relation.root_session_id,
                    "delegation_id": other_relation.delegation_id,
                }
            )
        )
    else:
        if isinstance(store, LocalSessionStore):
            record_path(
                store.sessions_dir / relation.root_session_id, relation.delegation_id
            ).unlink()
        else:
            with closing(store._connect()) as connection:
                connection.execute(
                    "DELETE FROM delegations WHERE delegation_id = ?", (relation.delegation_id,)
                )
                connection.commit()
    async with RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")),
    ) as runtime:
        with pytest.raises(PersistenceError, match="frozen Delegation"):
            await runtime._ensure_extension_snapshot(run)
        assert run.run_id not in runtime._run_resources


async def test_child_manual_compaction_reads_frozen_instructions_from_delegation(store):
    child = SessionRecord()
    store.create_session(child)
    relation = seed_delegation(store, child)
    _append_turns(store, child.session_id, 5)
    strategy = RecordingStrategy()
    async with _runtime(
        store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id
    ) as runtime:
        result = await runtime.compact(session_id=child.session_id)
    assert result.run.status == "completed", result.run.error
    assert len(strategy.requests) == 1
    assert relation.definition.system_prompt in strategy.requests[0].agent_system_prompt
    assert "agent_system_prompt" not in result.run.metadata


async def test_user_steering_uses_active_child_authority_for_prompt_policy_binding(store):
    child = SessionRecord()
    store.create_session(child)
    relation = seed_delegation(store, child)
    run = prepared_run(store, child, relation)
    started, release = asyncio.Event(), asyncio.Event()

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            started.set()
            await release.wait()
            yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

    async with RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(Provider(), ModelMetadata(model_id="faux", provider_id="faux")),
    ) as runtime:
        task = asyncio.create_task(
            runtime.run_prepared(run, HostSessionInput(prompt="work", host_source_id="test"))
        )
        try:
            await asyncio.wait_for(started.wait(), 10)
            queued = await runtime.run(
                "steer accepted", session_id=child.session_id, queue_mode="steer"
            )
            assert queued.run.run_id == run.run_id
            pending = runtime.queued_inputs.eligible_session(
                store.load_session_record(child.session_id)
            )
            assert [item.prompt for item in pending] == ["steer accepted"]
        finally:
            release.set()
            result = await asyncio.wait_for(task, 10)
        assert result.run.status == "completed", result.run.error
