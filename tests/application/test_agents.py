from __future__ import annotations

import asyncio

import pytest

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agent_models import SpawnAgentRequest
from ouro.application.agents import AgentService
from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentControlRequest,
    DelegatedConstraints,
    DelegationRecord,
    ToolSelection,
)
from ouro.core.errors import SessionBusyError
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolInvocation
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


class CapturingChildProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.system_prompts: list[str] = []
        self.tool_names: list[list[str]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, messages
        self.system_prompts.append(system_prompt)
        self.tool_names.append([item["name"] for item in (tools or [])])
        yield ProviderStreamEvent(type="text_delta", text="child complete")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class ForegroundAgentProvider:
    provider_id = "foreground"

    def __init__(self, store: LocalSessionStore) -> None:
        self.store = store
        self.root_round = 0
        self.root_tool_schemas: list[list[dict[str, object]]] = []
        self.child_tool_names: list[str] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages
        definitions = list(tools or [])
        names = [item["name"] for item in definitions]
        if "SpawnAgent" not in names:
            self.child_tool_names = names
            yield ProviderStreamEvent(type="text_delta", text="child evidence")
            yield ProviderStreamEvent(type="done")
            return

        self.root_tool_schemas.append(definitions)
        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "spawn_call",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "reader",
                        "task": "Inspect the architecture.",
                        "description": "Inspect architecture",
                        "execution_mode": "foreground",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 1:
            self.root_round += 1
            root = next(
                session
                for session in self.store.list_sessions()
                if session.session_kind == "primary"
            )
            delegation = self.store.list_delegations(root.session_id)[0]
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "wait_call",
                    "name": "WaitAgents",
                    "arguments": {"delegation_ids": [delegation.delegation_id]},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parent resumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class BackgroundFollowUpProvider:
    provider_id = "background"

    def __init__(self) -> None:
        self.root_round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            yield ProviderStreamEvent(type="text_delta", text="background child result")
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "background_spawn",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "reader",
                        "task": "Inspect later.",
                        "description": "Background inspection",
                        "execution_mode": "background",
                        "delivery_policy": "follow_up",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 1:
            self.root_round += 1
            yield ProviderStreamEvent(type="text_delta", text="parent initial complete")
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parent received follow-up")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class SteeredChildProvider:
    provider_id = "steered"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        if self.round == 0:
            self.round += 1
            self.started.set()
            await self.release.wait()
            yield ProviderStreamEvent(type="text_delta", text="initial child work")
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="steered child complete")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class ApprovalChildProvider:
    provider_id = "approval"

    def __init__(self) -> None:
        self.round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        if self.round == 0:
            self.round += 1
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "danger_call",
                    "name": "Danger",
                    "arguments": {},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="approved child complete")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


async def _handler(invocation):
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content="ok",
    )


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    for name, effect in (("Read", "read_only"), ("Write", "state_changing")):
        registry.register(
            Tool(
                definition=ToolDefinition(
                    name=name,
                    description=f"{name} data.",
                    effect=effect,
                ),
                handler=_handler,
            )
        )
    return registry


async def _services(tmp_path):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingChildProvider()
    tools = _tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    definitions.create(
        agent_id="reader",
        name="Reader",
        description="Read bounded evidence.",
        system_prompt="CHILD READER INSTRUCTIONS",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=tools,
        expected_revision=0,
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=tools,
        system_prompt="BASE INSTRUCTIONS",
    )
    runtime.attach_agent_definitions(definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    await supervisor.start()
    service = AgentService(store=store, definitions=definitions, scheduler=supervisor)

    root = SessionRecord(
        session_id="ses_root",
        status="running",
        active_run_id="run_parent",
        last_run_id="run_parent",
        current_model=runtime.current_model().key,
    )
    parent = RunRecord(
        run_id="run_parent",
        session_id=root.session_id,
        status="running",
        model="capture",
        agent_catalog=definitions.catalog_snapshot(tools),
    )
    store.create_session(root)
    store.save_run(parent)
    return service, supervisor, store, provider


@pytest.mark.asyncio
async def test_spawn_runs_child_as_independent_session_with_frozen_tools(tmp_path) -> None:
    service, supervisor, store, provider = await _services(tmp_path)

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/request-1",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect the repository architecture.",
            description="Inspect architecture",
        )
    )
    waited = await service.wait_agents((handle.delegation_id,))
    detail = service.read_agent(handle.delegation_id)

    assert waited.completed is True
    assert detail.final_text == "child complete"
    child = store.load_session_record(handle.child_session_id)
    assert child.session_kind == "subagent"
    assert child.root_session_id == "ses_root"
    assert child.parent_session_id == "ses_root"
    messages = store.load_runtime_session(child.session_id).messages
    assert messages[0].role == "system_synthetic"
    assert messages[0].metadata["host_source_id"].startswith("ouro.subagents/")
    assert provider.tool_names == [["Read"]]
    assert provider.system_prompts[0].startswith("BASE INSTRUCTIONS")
    assert provider.system_prompts[0].endswith("CHILD READER INSTRUCTIONS")
    assert service.agent_tree_snapshot("ses_root").terminal_count == 1
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_duplicate_spawn_request_returns_same_delegation(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    request = SpawnAgentRequest(
        request_id="host/stable-request",
        parent_session_id="ses_root",
        parent_run_id="run_parent",
        agent_id="reader",
        task="Inspect once.",
        description="Inspect once",
    )

    first = await service.spawn_agent(request)
    second = await service.spawn_agent(request)
    await service.wait_agents((first.delegation_id,))

    assert second.delegation_id == first.delegation_id
    assert len(service.list_agents("ses_root")) == 1
    assert (
        len([session for session in store.list_sessions() if session.session_kind == "subagent"])
        == 1
    )
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_wait_control_supports_any_and_immediate_timeout(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    completed = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/completed-for-any",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Complete first.",
            description="Completed child",
        )
    )
    await service.wait_agents((completed.delegation_id,))
    completed_relation = store.list_delegations("ses_root")[0]
    pending = DelegationRecord(
        idempotency_key="host/pending-for-any",
        root_session_id="ses_root",
        parent_session_id="ses_root",
        parent_run_id="run_parent",
        agent_id=completed_relation.agent_id,
        task_description="Pending child",
        prompt_digest="sha256:" + "b" * 64,
        definition=completed_relation.definition,
        effective_tools=completed_relation.effective_tools,
    )
    pending = store.create_delegation(pending)
    session = store.load_session_record("ses_root")
    run = store.load_run("ses_root", "run_parent")
    invocation = ToolInvocation(
        tool_call_id="wait_semantics",
        session_id=session.session_id,
        run_id=run.run_id,
        tool_name="WaitAgents",
    )

    any_result = await service.execute_control(
        AgentControlRequest(
            action="wait",
            request_id="wait/any",
            delegation_ids=(completed.delegation_id, pending.delegation_id),
            completion_condition="any",
        ),
        invocation=invocation,
        session=session,
        run=run,
    )
    timeout_result = await service.execute_control(
        AgentControlRequest(
            action="wait",
            request_id="wait/timeout",
            delegation_ids=(completed.delegation_id, pending.delegation_id),
            completion_condition="all",
            timeout_seconds=0,
        ),
        invocation=invocation,
        session=session,
        run=run,
    )

    assert any_result.status == "completed"
    assert any_result.structured["reason"] == "condition_met"
    assert timeout_result.status == "completed"
    assert timeout_result.structured["reason"] == "timeout"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_idle_child_follow_up_reuses_session_and_creates_new_run(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    first = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/initial-child",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect once.",
            description="Initial inspection",
        )
    )
    await service.wait_agents((first.delegation_id,))
    assert first.child_session_id is not None
    assert first.child_run_id is not None

    resumed = await service.send_agent_message(
        first.delegation_id,
        "Inspect the follow-up.",
        request_id="host/follow-up-1",
        mode="follow_up",
    )
    await service.wait_agents((first.delegation_id,))

    assert resumed.child_session_id == first.child_session_id
    assert resumed.child_run_id != first.child_run_id
    child_snapshot = store.load_runtime_session(first.child_session_id)
    assert len(child_snapshot.runs) == 2
    assert child_snapshot.messages[-2].role == "system_synthetic"
    assert (
        child_snapshot.messages[-2]
        .metadata["host_source_id"]
        .endswith("/messages/host/follow-up-1")
    )
    assert child_snapshot.messages[-1].text_content == "child complete"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_active_child_message_uses_steer_without_new_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = SteeredChildProvider()
    tools = _tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="steered", provider_id="steered"),
        ),
        tools=tools,
    )
    runtime.attach_agent_definitions(definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(store=store, definitions=definitions, scheduler=supervisor)
    await supervisor.start()
    root = SessionRecord(
        session_id="ses_steer_root",
        status="running",
        active_run_id="run_steer_parent",
        last_run_id="run_steer_parent",
        current_model=runtime.current_model().key,
    )
    parent = RunRecord(
        run_id="run_steer_parent",
        session_id=root.session_id,
        status="running",
        model="steered",
        agent_catalog=definitions.catalog_snapshot(tools),
    )
    store.create_session(root)
    store.save_run(parent)
    await service.start()

    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/steered-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="general",
            task="Wait for steering.",
            description="Steering test",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    steered = await service.send_agent_message(
        child.delegation_id,
        "Use this additional direction.",
        request_id="host/steer-1",
        mode="steer",
    )
    assert steered.child_run_id == child.child_run_id
    provider.release.set()
    await service.wait_agents((child.delegation_id,))

    assert child.child_session_id is not None
    snapshot = store.load_runtime_session(child.child_session_id)
    assert len(snapshot.runs) == 1
    steer_messages = [
        message
        for message in snapshot.messages
        if message.metadata.get("host_source_id", "").endswith("/messages/host/steer-1")
    ]
    assert len(steer_messages) == 1
    assert steer_messages[0].role == "system_synthetic"
    assert snapshot.messages[-1].text_content == "steered child complete"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_child_hitl_is_projected_at_root_and_resolved_by_exact_identity(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ApprovalChildProvider()
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Danger",
                description="Dangerous operation.",
                permission_mode="approval",
                effect="state_changing",
            ),
            handler=_handler,
        )
    )
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="approval", provider_id="approval"),
        ),
        tools=tools,
    )
    runtime.attach_agent_definitions(definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(store=store, definitions=definitions, scheduler=supervisor)
    await supervisor.start()
    root = SessionRecord(
        session_id="ses_hitl_root",
        status="running",
        active_run_id="run_hitl_parent",
        last_run_id="run_hitl_parent",
        current_model=runtime.current_model().key,
    )
    parent = RunRecord(
        run_id="run_hitl_parent",
        session_id=root.session_id,
        status="running",
        model="approval",
        agent_catalog=definitions.catalog_snapshot(tools),
    )
    store.create_session(root)
    store.save_run(parent)
    await service.start()
    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/hitl-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="general",
            task="Request approval.",
            description="HITL test",
            execution_mode="foreground",
            delivery_policy="resume_waiter",
        )
    )

    async with asyncio.timeout(3):
        while not service.list_agent_interactions(root.session_id):
            await asyncio.sleep(0.01)
    interaction = service.list_agent_interactions(root.session_id)[0]
    assert interaction.delegation_id == child.delegation_id
    assert interaction.child_session_id == child.child_session_id
    assert interaction.tool_call_id == "danger_call"
    assert store.load_run(root.session_id, parent.run_id).status == "running"

    with pytest.raises(ValueError, match="canonical request"):
        await service.resolve_agent_approval(
            child.delegation_id,
            child_session_id=interaction.child_session_id,
            child_run_id=interaction.child_run_id,
            tool_call_id="wrong_call",
            approval_id=interaction.request_id,
            approved=True,
        )
    resumed = await service.resolve_agent_approval(
        child.delegation_id,
        child_session_id=interaction.child_session_id,
        child_run_id=interaction.child_run_id,
        tool_call_id=interaction.tool_call_id,
        approval_id=interaction.request_id,
        approved=True,
    )
    assert (await supervisor.wait(resumed)).status == "completed"
    await service.wait_agents((child.delegation_id,))
    assert service.read_agent(child.delegation_id).final_text == "approved child complete"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_spawn_rejects_non_active_parent_run(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    store.save_run(parent.model_copy(update={"status": "completed"}))

    with pytest.raises(SessionBusyError, match="parent run is not active"):
        await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/rejected",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                agent_id="reader",
                task="Do not run.",
                description="Rejected",
            )
        )

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_spawn_enforces_single_write_capable_child_per_root(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    tools = _tools()
    service.definitions.create(
        agent_id="writer",
        name="Writer",
        description="Modify bounded workspace state.",
        system_prompt="WRITE ONLY WHEN REQUIRED",
        tool_selection=ToolSelection(mode="selected", names=("Write",)),
        tools=tools,
        expected_revision=1,
    )
    parent = store.load_run("ses_root", "run_parent")
    parent = parent.model_copy(
        update={"agent_catalog": service.definitions.catalog_snapshot(tools)}
    )
    store.save_run(parent)
    reader = parent.agent_catalog.resolve("reader")
    store.create_delegation(
        DelegationRecord(
            idempotency_key="host/existing-writer",
            root_session_id="ses_root",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task_description="Existing write-capable work.",
            prompt_digest="sha256:" + "a" * 64,
            definition=reader.definition,
            effective_tools=reader.effective_tools,
            constraints=DelegatedConstraints(allow_write=True),
        )
    )

    with pytest.raises(SessionBusyError, match="write-capable child quota"):
        await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/second-writer",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                agent_id="writer",
                task="Write concurrently.",
                description="Second writer",
            )
        )

    assert len(store.list_delegations("ses_root")) == 1
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_spawn_budget_is_bounded_by_root_remaining_usage(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    store.save_run(
        parent.model_copy(
            update={
                "usage": {"input_tokens": 25, "output_tokens": 10},
                "metadata": {
                    "run_budget": {
                        "max_input_tokens": 100,
                        "max_output_tokens": 40,
                    }
                },
            }
        )
    )

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/bounded-budget",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Stay within the remaining budget.",
            description="Bounded child",
        )
    )
    relation = next(
        item
        for item in store.list_delegations("ses_root")
        if item.delegation_id == handle.delegation_id
    )

    assert relation.budget.max_input_tokens == 75
    assert relation.budget.max_output_tokens == 30
    await service.wait_agents((handle.delegation_id,))
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_foreground_join_releases_single_worker_and_resumes_original_run(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = _tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    definitions.create(
        agent_id="reader",
        name="Reader",
        description="Read bounded evidence.",
        system_prompt="CHILD ONLY",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=tools,
        expected_revision=0,
    )
    provider = ForegroundAgentProvider(store)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="foreground", provider_id="foreground"),
        ),
        tools=tools,
    )
    runtime.attach_agent_definitions(definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(store=store, definitions=definitions, scheduler=supervisor)
    runtime.attach_agent_controls(service)
    await service.start()

    parent_handle = await supervisor.submit("Delegate this task.")
    parked = await supervisor.wait(parent_handle)
    assert parked.status == "waiting_child"
    root_session_id = parent_handle.session_id
    parent_run_id = parked.run_id
    assert parent_run_id is not None

    async with asyncio.timeout(5):
        while store.load_run(root_session_id, parent_run_id).status != "completed":
            await asyncio.sleep(0.01)
        while (
            store.list_delegations(root_session_id)[0].result is None
            or store.list_delegations(root_session_id)[0].result.delivery.status != "delivered"
        ):
            await asyncio.sleep(0.01)

    parent_run = store.load_run(root_session_id, parent_run_id)
    snapshot = store.load_runtime_session(root_session_id)
    wait_messages = [
        message
        for message in snapshot.messages
        if message.role == "tool_result"
        and message.tool_result is not None
        and message.tool_result.tool_name == "WaitAgents"
    ]
    assert parent_run.status == "completed"
    assert parent_run.run_id == parent_run_id
    assert wait_messages[0].tool_result is not None
    assert wait_messages[0].tool_result.tool_call_id == "wait_call"
    assert snapshot.messages[-1].text_content == "parent resumed"
    assert provider.child_tool_names == ["Read"]
    spawn_definition = next(
        item for item in provider.root_tool_schemas[0] if item["name"] == "SpawnAgent"
    )
    agent_options = spawn_definition["parameters_schema"]["properties"]["agent_id"]["oneOf"]
    assert agent_options == [
        {
            "const": "general",
            "title": "General",
            "description": "A general-purpose child agent for a bounded delegated task.",
        },
        {
            "const": "reader",
            "title": "Reader",
            "description": "Read bounded evidence.",
        },
    ]
    events = store.load_session(root_session_id).events
    assert [event.event_type for event in events].count("agent_wait_requested") == 1
    assert [event.event_type for event in events].count("agent_wait_resolved") == 1
    relation = store.list_delegations(root_session_id)[0]
    assert relation.result is not None
    assert relation.result.delivery.status == "delivered"
    assert relation.result.delivery.target_run_id == parent_run_id

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_background_follow_up_delivery_is_exact_once(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = _tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    definitions.create(
        agent_id="reader",
        name="Reader",
        description="Read bounded evidence.",
        system_prompt="",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=tools,
        expected_revision=0,
    )
    provider = BackgroundFollowUpProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="background", provider_id="background"),
        ),
        tools=tools,
    )
    runtime.attach_agent_definitions(definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(store=store, definitions=definitions, scheduler=supervisor)
    runtime.attach_agent_controls(service)
    await service.start()

    parent_handle = await supervisor.submit("Start background work.")
    initial = await supervisor.wait(parent_handle)
    assert initial.status == "completed"
    root_session_id = parent_handle.session_id
    async with asyncio.timeout(5):
        while True:
            relations = store.list_delegations(root_session_id)
            if (
                relations
                and relations[0].result is not None
                and relations[0].result.delivery.status == "delivered"
            ):
                break
            await asyncio.sleep(0.01)

    relation = store.list_delegations(root_session_id)[0]
    assert relation.result is not None
    assert relation.result.delivery.policy == "follow_up"
    assert relation.result.delivery.target_run_id is not None
    snapshot = store.load_runtime_session(root_session_id)
    assert len(snapshot.runs) == 2
    delivered_messages = [
        message
        for message in snapshot.messages
        if message.metadata.get("host_source_id")
        == f"ouro.subagents/results/{relation.result.delivery.delivery_id}"
    ]
    assert len(delivered_messages) == 1
    assert delivered_messages[0].role == "system_synthetic"
    assert snapshot.messages[-1].text_content == "parent received follow-up"

    await service.reconcile()
    assert len(store.load_runs(root_session_id)) == 2
    await service.close()
    await supervisor.shutdown()
