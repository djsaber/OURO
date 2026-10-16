from __future__ import annotations

import asyncio
import json
import re
from threading import get_ident

import pytest

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agent_models import SpawnAgentRequest
from ouro.application.agents import AgentService
from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentControlRequest,
    AgentDefinition,
    AgentSpawnPolicy,
    DelegationRecord,
    ToolSelection,
)
from ouro.core.base import utc_now
from ouro.core.context import estimate_message_tokens
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolInvocation
from ouro.providers.anthropic_messages_codec import messages_to_anthropic_messages
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolInputRequest, ToolRegistry, ToolResult


class CapturingChildProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.system_prompts: list[str] = []
        self.tool_names: list[list[str]] = []
        self.messages: list[list[object]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model
        self.system_prompts.append(system_prompt)
        self.tool_names.append([item["name"] for item in (tools or [])])
        self.messages.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text="child complete")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class SyncAgentProvider:
    provider_id = "sync"

    def __init__(self) -> None:
        self.root_round = 0
        self.root_tool_schemas: list[list[dict[str, object]]] = []
        self.child_tool_names: list[str] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt
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
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "spawn_call",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "reader",
                        "task": "Inspect the architecture.",
                        "description": "Inspect architecture",
                        "execution_mode": "sync",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parent resumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class ParallelSyncAgentProvider:
    """Complete two synchronous children at the same durable parent boundary."""

    provider_id = "parallel-sync"

    def __init__(self) -> None:
        self.root_round = 0
        self.child_count = 0
        self.children_ready = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            self.child_count += 1
            if self.child_count == 2:
                self.children_ready.set()
            await self.children_ready.wait()
            yield ProviderStreamEvent(type="text_delta", text="parallel child complete")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            for index in range(2):
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": f"parallel_spawn_{index}",
                        "name": "SpawnAgent",
                        "arguments": {
                            "agent_id": "reader",
                            "task": f"Inspect partition {index}.",
                            "description": f"Parallel inspection {index}",
                            "execution_mode": "sync",
                        },
                    },
                    index=index,
                )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parallel parent resumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class SyncHitlProvider:
    provider_id = "sync-hitl"

    def __init__(self) -> None:
        self.root_round = 0
        self.child_round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            if self.child_round == 0:
                self.child_round += 1
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "sync_ask_call",
                        "name": "AskUser",
                        "arguments": {"question": "Choose A or B"},
                    },
                )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text="sync child answered")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "sync_hitl_spawn",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "general",
                        "task": "Ask one bounded question.",
                        "description": "Sync HITL child",
                        "execution_mode": "sync",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="sync parent resumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class SpawnAndChildApprovalProvider:
    provider_id = "spawn-child-approval"

    def __init__(self) -> None:
        self.root_round = 0
        self.child_round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            if self.child_round == 0:
                self.child_round += 1
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "child_danger_call",
                        "name": "Danger",
                        "arguments": {},
                    },
                )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text="child approved")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "approved_spawn_call",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "general",
                        "task": "Exercise an independently approved tool.",
                        "description": "Approval scope test",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parent spawn approved")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class AsyncFollowUpProvider:
    provider_id = "async"

    def __init__(self) -> None:
        self.root_round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            yield ProviderStreamEvent(type="text_delta", text="async child result")
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "async_spawn",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "reader",
                        "task": "Inspect later.",
                        "description": "Async inspection",
                        "execution_mode": "async",
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

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


def _delegation_id_from(messages) -> str:
    for message in messages:
        text = str(getattr(message, "text_content", ""))
        match = re.search(r"\*\*Delegation ID\*\*: `(dlg_[^`]+)`", text)
        if match:
            return match.group(1)
    raise AssertionError("delegation id missing from parent context")


class AsyncSendAfterDeliveryProvider:
    """Parent messages the just-delivered subagent from the delivery-created Run."""

    provider_id = "async-send-after-delivery"

    def __init__(self) -> None:
        self.root_round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            yield ProviderStreamEvent(type="text_delta", text="child result")
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "async_spawn",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "reader",
                        "task": "Inspect.",
                        "description": "Async inspection",
                        "execution_mode": "async",
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
        if self.root_round == 2:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "send_after_delivery",
                    "name": "SendAgentMessage",
                    "arguments": {
                        "delegation_id": _delegation_id_from(messages),
                        "message": "Follow up on the result.",
                        "mode": "follow_up",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        self.root_round += 1
        yield ProviderStreamEvent(type="text_delta", text="parent settled")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class NextBoundaryRaceProvider:
    provider_id = "next-boundary"

    def __init__(self) -> None:
        self.root_round = 0
        self.root_blocked = asyncio.Event()
        self.root_release = asyncio.Event()
        self.root_messages: list[list[object]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt
        names = [item["name"] for item in (tools or [])]
        if "SpawnAgent" not in names:
            yield ProviderStreamEvent(type="text_delta", text="boundary child result")
            yield ProviderStreamEvent(type="done")
            return
        self.root_messages.append(list(messages))
        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "next_boundary_spawn",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "reader",
                        "task": "Finish while the parent model round is active.",
                        "description": "Next-boundary race",
                        "execution_mode": "async",
                    },
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 1:
            self.root_round += 1
            self.root_blocked.set()
            await self.root_release.wait()
            yield ProviderStreamEvent(type="text_delta", text="parent reached boundary")
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="parent consumed child result")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
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

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
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

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
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

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages, tools
        if self.round == 0:
            self.round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "danger_call",
                    "name": "Danger",
                    "arguments": {"command": "x" * 500},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="approved child complete")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class AskUserChildProvider:
    provider_id = "ask-child"

    def __init__(self) -> None:
        self.round = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages, tools
        if self.round == 0:
            self.round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "ask_call",
                    "name": "AskUser",
                    "arguments": {"question": {"kind": "choice", "items": ["A", "B"]}},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="child received answer")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class ConcurrentAskUserProvider:
    provider_id = "concurrent-ask"

    def __init__(self) -> None:
        self.initial_calls = 0

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages, tools
        if self.initial_calls < 2:
            index = self.initial_calls
            self.initial_calls += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": f"concurrent_ask_{index}",
                    "name": "AskUser",
                    "arguments": {"question": f"Question {index}"},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="concurrent child answered")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


async def _handler(invocation, context):
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


def _ask_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="AskUser",
                description="Ask for structured user input.",
                parameters_schema={
                    "type": "object",
                    "properties": {"question": {}},
                    "required": ["question"],
                    "additionalProperties": False,
                },
                effect="read_only",
                execution_mode="sequential",
                batch_behavior="exclusive",
                kind="user_input",
            ),
            handler=lambda invocation, context: ToolInputRequest(
                data=invocation.arguments["question"]
            ),
        )
    )
    return registry


async def _services(
    tmp_path,
    *,
    spawn_policy: AgentSpawnPolicy | None = None,
    bind_controls: bool = False,
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    resource_store = LocalResourceStore(store.root)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=store)
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
        resources=resources,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    await supervisor.start()
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
        resources=resources,
        spawn_policy=spawn_policy,
    )
    runtime.bind_extensions(
        agent_definitions=definitions, agent_controls=service if bind_controls else None
    )

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
        agent_catalog=service.constrain_catalog(definitions.catalog_snapshot(tools)),
    )
    store.create_session(root)
    store.save_run(parent)
    return service, supervisor, store, provider


@pytest.mark.asyncio
async def test_terminal_agent_settlement_queries_store_outside_the_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    service, supervisor, store, _provider = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent").model_copy(update={"status": "completed"})
    event_loop_thread = get_ident()
    reader_threads: list[int] = []
    load_session = store.load_session_record

    def recording_load_session(session_id: str) -> SessionRecord:
        reader_threads.append(get_ident())
        return load_session(session_id)

    monkeypatch.setattr(store, "load_session_record", recording_load_session)
    try:
        await service.settle_terminal_run(parent)
    finally:
        await service.close()
        await supervisor.shutdown()

    assert reader_threads
    assert event_loop_thread not in reader_threads


async def _active_subagent_services(
    tmp_path,
    *,
    cascade_timeout_seconds: float = 5.0,
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = SteeredChildProvider()
    tools = _tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    definitions.create(
        agent_id="reader",
        name="Reader",
        description="Read while active.",
        system_prompt="READ ONLY",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=tools,
        expected_revision=0,
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="steered", provider_id="steered"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    await supervisor.start()
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
        cascade_timeout_seconds=cascade_timeout_seconds,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
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
    return service, supervisor, store, provider, root, parent


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
    child_run = store.load_run(child.session_id, handle.child_run_id)
    assert child_run.origin == "delegation"
    assert child_run.initiator_id == f"ouro.subagents/{handle.delegation_id}"
    messages = store.load_runtime_session(child.session_id).messages
    assert messages[0].role == "user"
    assert messages[0].metadata == {
        "session_input_source": "delegation",
        "session_input_mode": "follow_up",
        "delegation_id": handle.delegation_id,
    }
    assert provider.messages[0][0].role == "user"
    assert provider.tool_names == [["Read"]]
    assert provider.system_prompts[0].startswith("BASE INSTRUCTIONS")
    assert provider.system_prompts[0].endswith("CHILD READER INSTRUCTIONS")
    anthropic_system, anthropic_messages = messages_to_anthropic_messages(
        provider.messages[0],
        system_prompt=provider.system_prompts[0],
        supports_image_input=False,
        provider_id="capture",
        model_id="capture",
        include_continuation=False,
    )
    assert anthropic_system.endswith("CHILD READER INSTRUCTIONS")
    assert [message["role"] for message in anthropic_messages] == ["user"]
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.initial_message_id == messages[0].message_id
    tree = service.agent_tree_snapshot("ses_root")
    assert tree.terminal_count == 1
    assert tree.pending_interaction_count == 0
    assert tree.edges[0].delegation_id == handle.delegation_id
    assert tree.nodes[0].result_ready is True
    assert tree.nodes[0].delivery_status == "skipped"
    serialized_tree = tree.model_dump_json()
    assert "Inspect the repository architecture." not in serialized_tree
    assert "CHILD READER INSTRUCTIONS" not in serialized_tree
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "paused"])
async def test_continue_reuses_interrupted_child_run_and_refreshes_its_result(
    tmp_path, monkeypatch, status
):
    service, supervisor, store, provider = await _services(tmp_path)
    successful_stream = provider.stream
    attempts = 0

    async def interrupted_stream(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield ProviderStreamEvent(type="text_delta", text="unfinished evidence")
            raise RuntimeError("child connection interrupted")
        async for event in successful_stream(**kwargs):
            yield event

    monkeypatch.setattr(provider, "stream", interrupted_stream)
    try:
        child = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/continue-child",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                agent_id="reader",
                task="Inspect the repository.",
                description="Inspect repository",
            )
        )
        await service.wait_agents((child.delegation_id,))
        before = store.load_runtime_session(child.child_session_id)
        assert before.runs[0].status == "failed"
        store.save_run(before.runs[0].model_copy(update={"status": status}))
        parent = store.load_run("ses_root", "run_parent")
        await service.continue_children(parent)
        async with asyncio.timeout(10):
            await service.wait_agents((child.delegation_id,))
        after = store.load_runtime_session(child.child_session_id)
        relation = store.load_delegation("ses_root", child.delegation_id)
        assert len(after.runs) == 1
        assert after.runs[0].run_id == before.runs[0].run_id
        assert after.runs[0].status == "completed"
        assert after.messages[0] == before.messages[0]
        assert relation.result.terminal_status == "completed"
        assert [m.text_content for m in after.messages if m.role == "user"] == [
            "Inspect the repository."
        ]
        assert attempts == 2
        await service.continue_children(parent)
        assert attempts == 2
    finally:
        await service.close()
        await supervisor.shutdown()


@pytest.mark.asyncio
async def test_root_continue_resumes_its_interrupted_child_without_new_runs(tmp_path, monkeypatch):
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    root_input = MessageRecord.text(
        session_id=root.session_id,
        run_id=parent.run_id,
        role="user",
        text="parent task",
        sequence=1,
    )
    store.append_message(root_input)
    store.save_session(
        root.model_copy(update={"message_count": 1, "last_message_id": root_input.message_id})
    )
    child_attempts = 0

    async def stream(**kwargs):
        nonlocal child_attempts
        if kwargs["messages"][0].text_content == "child task":
            child_attempts += 1
            if child_attempts == 1:
                yield ProviderStreamEvent(type="text_delta", text="incomplete")
                raise RuntimeError("child disconnected")
        yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done")

    monkeypatch.setattr(provider, "stream", stream)
    try:
        child = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="root/continue-child",
                parent_session_id=root.session_id,
                parent_run_id=parent.run_id,
                agent_id="reader",
                task="child task",
                description="Continue child",
            )
        )
        await service.wait_agents((child.delegation_id,))
        store.save_run(parent.model_copy(update={"status": "failed", "ended_at": utc_now()}))
        store.save_session(
            store.load_session_record(root.session_id).model_copy(
                update={"status": "failed", "active_run_id": None}
            )
        )
        resumed = await supervisor.runtime.continue_run(session_id=root.session_id)
        async with asyncio.timeout(10):
            await service.wait_agents((child.delegation_id,))
        assert resumed.run.status == "completed"
        assert resumed.run.run_id == parent.run_id
        assert len(store.load_runs(root.session_id)) == 1
        child_runs = store.load_runs(child.child_session_id)
        assert len(child_runs) == 1
        assert child_runs[0].status == "completed"
        assert child_runs[0].run_id == child.child_run_id
        assert child_attempts == 2
    finally:
        await service.close()
        await supervisor.shutdown()


async def test_child_history_continuation_keeps_frozen_contract_and_new_run_ownership(tmp_path):
    service, supervisor, store, provider = await _services(tmp_path, bind_controls=True)
    try:
        child = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/undo-child",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                agent_id="reader",
                task="inspect",
                description="Inspect repository",
            )
        )
        await service.wait_agents((child.delegation_id,))
        before = store.load_runtime_session(child.child_session_id)
        store.commit_session(
            SessionCommit(before=before.session, before_message_id=before.messages[-1].message_id)
        )
        result = await supervisor.runtime.continue_run(session_id=child.child_session_id)
        assert result.run.status == "completed"
        assert result.run.run_id != child.child_run_id
        assert result.run.history_epoch == 1
        relation = store.load_delegation("ses_root", child.delegation_id)
        assert relation.current_child_run_id == result.run.run_id
        assert relation.result.child_run_id == result.run.run_id
        assert store.load_run(child.child_session_id, child.child_run_id) == before.runs[0]
        assert store.load_runtime_session(child.child_session_id).messages[0] == before.messages[0]
        assert provider.tool_names[-1] == ["Read"]
        assert "CHILD READER INSTRUCTIONS" in provider.system_prompts[-1]
        for field in ("effective_tool_refs", "delegated_constraints", "run_budget"):
            assert result.run.metadata[field] == before.runs[0].metadata[field]
    finally:
        await service.close()
        await supervisor.shutdown()


@pytest.mark.asyncio
async def test_primary_fork_owns_agent_tree_independently_from_lineage_root(tmp_path) -> None:
    service, supervisor, store, _provider = await _services(tmp_path)
    branch = store.load_session_record("ses_root").model_copy(
        update={
            "root_session_id": "ses_deleted_lineage_root",
            "parent_session_id": "ses_deleted_lineage_root",
        }
    )
    store.save_session(branch)

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/branch-request",
            parent_session_id=branch.session_id,
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect the surviving branch.",
            description="Inspect branch",
        )
    )
    await service.wait_agents((handle.delegation_id,))

    relation = store.load_delegation(branch.session_id, handle.delegation_id)
    child = store.load_session_record(handle.child_session_id)
    assert branch.agent_root_session_id == branch.session_id
    assert relation.root_session_id == branch.session_id
    assert child.root_session_id == branch.session_id
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_child_context_contains_only_task_and_counts_child_input(
    tmp_path,
) -> None:
    service, supervisor, store, provider = await _services(tmp_path)
    store.append_message(
        MessageRecord.text(
            session_id="ses_root",
            role="user",
            text="parent secret must stay isolated",
            sequence=1,
        )
    )
    store.append_message(
        MessageRecord.text(
            session_id="ses_root",
            role="system",
            text="private plugin context",
            sequence=2,
        )
    )

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/task-only-context",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect only the requested target.",
            description="Isolated context",
        )
    )
    await service.wait_agents((handle.delegation_id,))

    initial = provider.messages[0][0]
    assert initial.text_content == "Inspect only the requested target."
    assert "parent secret" not in initial.text_content
    assert "private plugin context" not in initial.text_content
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.task_resource_refs == ()
    context = store.load_latest_context(handle.child_session_id)
    assert context is not None
    assert context.metrics.estimated_input_tokens >= estimate_message_tokens(
        context.source_messages[0]
    )
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_child_task_is_visible_only_in_child_canonical_context(tmp_path) -> None:
    service, supervisor, store, provider = await _services(tmp_path)
    sensitive_task = "Inspect the target with api_key=child-task-secret"
    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/sensitive-child-task",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task=sensitive_task,
            description="Inspect a bounded target",
        )
    )
    await service.wait_agents((handle.delegation_id,))

    child = store.load_runtime_session(handle.child_session_id)
    assert sensitive_task in child.messages[0].text_content
    assert sensitive_task in provider.messages[0][0].text_content
    relation = store.load_delegation("ses_root", handle.delegation_id)
    root = store.load_session("ses_root")
    public_projection = "\n".join(
        (
            handle.model_dump_json(),
            service.agent_tree_snapshot("ses_root").model_dump_json(),
            relation.model_dump_json(),
            *(event.model_dump_json() for event in root.events),
            *(
                event.model_dump_json()
                for event in store.load_session(handle.child_session_id).events
            ),
        )
    )
    assert sensitive_task not in public_projection
    assert "child-task-secret" not in public_projection
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_agent_task_resources_are_explicitly_copied(
    tmp_path,
) -> None:
    service, supervisor, store, provider = await _services(tmp_path)
    resources = service.resources
    assert isinstance(resources, ResourceService)
    source = tmp_path / "parent-evidence.txt"
    source.write_text("delegated resource contents", encoding="utf-8")
    imported = resources.import_resource("ses_root", source)

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/resource-context",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect the delegated resource.",
            description="Resource context",
            resource_refs=(imported.resource.resource_id,),
        )
    )
    await service.wait_agents((handle.delegation_id,))

    child_snapshot = store.load_runtime_session(handle.child_session_id)
    assert child_snapshot.messages[0].resource_refs == [imported.resource.resource_id]
    assert imported.resource.resource_id in resources.session_resource_ids(handle.child_session_id)
    assert "delegated resource contents" in provider.messages[0][0].text_content
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.result is not None
    assert relation.result.resource_refs == ()
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_spawn_uses_run_frozen_agent_mode_and_tool_capability_policy(tmp_path) -> None:
    policy = AgentSpawnPolicy(
        allowed_agent_id_patterns=("reader",),
        allowed_execution_modes=("sync",),
        allow_write=False,
        allow_network=False,
        allow_mcp=False,
        allow_external_paths=False,
    )
    service, supervisor, store, _ = await _services(tmp_path, spawn_policy=policy)
    parent = store.load_run("ses_root", "run_parent")
    assert parent.agent_catalog is not None
    assert [entry.definition.agent_id for entry in parent.agent_catalog.entries] == ["reader"]
    assert [
        tool.public_name for tool in parent.agent_catalog.resolve("reader").effective_tools
    ] == ["Read"]

    with pytest.raises(ValueError, match="execution mode is not allowed"):
        await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/disallowed-async",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                agent_id="reader",
                task="Do not start.",
                description="Disallowed mode",
                execution_mode="async",
            )
        )

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/allowed-sync",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect within the workspace.",
            description="Allowed mode",
            execution_mode="sync",
        )
    )
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.constraints.allow_external_paths is False
    assert relation.child_session_id is not None
    assert relation.current_child_run_id is not None
    child_run = store.load_run(
        relation.child_session_id,
        relation.current_child_run_id,
    )
    assert child_run.metadata["delegated_constraints"] == relation.constraints.model_dump(
        mode="json"
    )

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
async def test_spawn_retry_resumes_reserved_child_identity_after_create_failure(
    tmp_path,
    monkeypatch,
) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    request = SpawnAgentRequest(
        request_id="host/replay-child-create",
        parent_session_id="ses_root",
        parent_run_id="run_parent",
        agent_id="reader",
        task="Inspect after a storage retry.",
        description="Replay child creation",
    )
    original_create_session = store.create_session
    failed = False

    def fail_first_child_create(session: SessionRecord) -> None:
        nonlocal failed
        if session.session_kind == "subagent" and not failed:
            failed = True
            raise PersistenceError("injected child create failure")
        original_create_session(session)

    monkeypatch.setattr(store, "create_session", fail_first_child_create)
    with pytest.raises(PersistenceError, match="injected child create failure"):
        await service.spawn_agent(request)

    reserved = store.list_delegations("ses_root")[0]
    assert reserved.spawn_state == "requested"
    assert reserved.child_session_id is not None
    assert reserved.current_child_run_id is not None
    monkeypatch.setattr(store, "create_session", original_create_session)

    handle = await service.spawn_agent(request)
    waited = await service.wait_agents((handle.delegation_id,))

    replayed = store.load_delegation("ses_root", handle.delegation_id)
    assert handle.delegation_id == reserved.delegation_id
    assert replayed.child_session_id == reserved.child_session_id
    assert replayed.current_child_run_id == reserved.current_child_run_id
    assert waited.completed is True
    assert len(store.list_delegations("ses_root")) == 1
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_spawn_retry_resubmits_same_queued_run_after_scheduler_failure(
    tmp_path,
    monkeypatch,
) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    request = SpawnAgentRequest(
        request_id="host/replay-submit",
        parent_session_id="ses_root",
        parent_run_id="run_parent",
        agent_id="reader",
        task="Inspect after a scheduler retry.",
        description="Replay scheduler submission",
    )
    original_submit = supervisor.submit_prepared
    failed = False

    async def fail_first_submit(run: RunRecord, session_input) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected scheduler failure")
        return await original_submit(run, session_input)

    monkeypatch.setattr(supervisor, "submit_prepared", fail_first_submit)
    with pytest.raises(RuntimeError, match="injected scheduler failure"):
        await service.spawn_agent(request)

    failed_relation = store.list_delegations("ses_root")[0]
    assert failed_relation.spawn_state == "failed"
    assert failed_relation.child_session_id is not None
    assert failed_relation.current_child_run_id is not None
    assert (
        store.load_run(
            failed_relation.child_session_id,
            failed_relation.current_child_run_id,
        ).status
        == "queued"
    )
    monkeypatch.setattr(supervisor, "submit_prepared", original_submit)

    handle = await service.spawn_agent(request)
    waited = await service.wait_agents((handle.delegation_id,))

    replayed = store.load_delegation("ses_root", handle.delegation_id)
    assert replayed.child_session_id == failed_relation.child_session_id
    assert replayed.current_child_run_id == failed_relation.current_child_run_id
    assert replayed.spawn_state == "submitted"
    assert waited.completed is True
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_spawn_retry_rejects_changed_request_for_same_idempotency_key(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    request = SpawnAgentRequest(
        request_id="host/replay-mismatch",
        parent_session_id="ses_root",
        parent_run_id="run_parent",
        agent_id="reader",
        task="Inspect the original target.",
        description="Original request",
    )
    first = await service.spawn_agent(request)

    with pytest.raises(ValueError, match="different durable request"):
        await service.spawn_agent(
            request.model_copy(update={"task": "Inspect a different target."})
        )

    await service.wait_agents((first.delegation_id,))
    assert len(store.list_delegations("ses_root")) == 1
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
    assert child_snapshot.messages[-2].role == "system"
    assert child_snapshot.messages[-2].semantic_tag == "subagent-message"
    assert (
        child_snapshot.messages[-2]
        .metadata["host_source_id"]
        .endswith("/messages/host/follow-up-1")
    )
    assert child_snapshot.messages[-1].text_content == "child complete"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_boundary", ["before_input_drain", "after_run"])
async def test_idle_child_follow_up_waits_for_previous_execution_receipt(
    tmp_path, monkeypatch, completion_boundary
):
    service, supervisor, store, _ = await _services(tmp_path)
    completed, release = asyncio.Event(), asyncio.Event()
    run_prepared = supervisor.runtime.run_prepared
    drain_inputs = supervisor.runtime._drain_queued_input_queue

    async def hold_completion():
        if not completed.is_set():
            completed.set()
            await release.wait()

    async def hold_completed_run(run, session_input, **options):
        result = await run_prepared(run, session_input, **options)
        await hold_completion()
        return result

    async def hold_before_input_drain(session_id, history_epoch):
        await hold_completion()
        await drain_inputs(session_id, history_epoch)

    if completion_boundary == "before_input_drain":
        monkeypatch.setattr(
            supervisor.runtime, "_drain_queued_input_queue", hold_before_input_drain
        )
    else:
        monkeypatch.setattr(supervisor.runtime, "run_prepared", hold_completed_run)
    try:
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
        await asyncio.wait_for(completed.wait(), 5)
        await service.wait_agents((first.delegation_id,))
        submit_prepared = supervisor.submit_prepared

        async def release_predecessor_after_admission_starts(run, session_input, **options):
            asyncio.get_running_loop().call_soon(release.set)
            return await submit_prepared(run, session_input, **options)

        monkeypatch.setattr(
            supervisor, "submit_prepared", release_predecessor_after_admission_starts
        )
        resumed = await service.send_agent_message(
            first.delegation_id,
            "Inspect again.",
            request_id="host/follow-up",
            mode="follow_up",
        )
        await service.wait_agents((first.delegation_id,))
        assert resumed.child_session_id == first.child_session_id
        assert resumed.child_run_id != first.child_run_id
        assert len(store.load_runtime_session(first.child_session_id).runs) == 2
    finally:
        release.set()
        await service.close()
        await supervisor.shutdown()


@pytest.mark.asyncio
async def test_active_child_message_uses_steer_without_new_run(tmp_path) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)

    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/steered-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Wait for steering.",
            description="Steering test",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    assert child.child_session_id is not None
    assert child.child_run_id is not None
    tree_subscription = await service.subscribe_agent_tree(root.session_id)
    assert tree_subscription.initial_snapshot.active_count == 1
    tree_subscription.close()
    assert store.load_run(child.child_session_id, child.child_run_id).status == "running"
    steered = await service.send_agent_message(
        child.delegation_id,
        "Use this additional direction.",
        request_id="host/steer-1",
        mode="steer",
    )
    assert steered.child_run_id == child.child_run_id
    assert steered.revision == store.load_delegation(root.session_id, child.delegation_id).revision
    provider.release.set()
    await service.wait_agents((child.delegation_id,))

    snapshot = store.load_runtime_session(child.child_session_id)
    assert len(snapshot.runs) == 1
    steer_messages = [
        message
        for message in snapshot.messages
        if message.metadata.get("host_source_id", "").endswith("/messages/host/steer-1")
    ]
    assert len(steer_messages) == 1
    assert steer_messages[0].role == "system"
    assert snapshot.messages[-1].text_content == "steered child complete"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_send_control_result_is_markdown_without_internal_ids(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/send-format",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect once.",
            description="Send format",
        )
    )
    await service.wait_agents((handle.delegation_id,))
    assert handle.child_session_id is not None

    session = store.load_session_record("ses_root")
    run = store.load_run("ses_root", "run_parent")
    invocation = ToolInvocation(
        tool_call_id="send_format",
        session_id=session.session_id,
        run_id=run.run_id,
        tool_name="SendAgentMessage",
    )
    result = await service.execute_control(
        AgentControlRequest(
            action="send",
            request_id="send/format",
            delegation_id=handle.delegation_id,
            message="Inspect again.",
            input_mode="follow_up",
        ),
        invocation=invocation,
        session=session,
        run=run,
    )

    assert "## Message Sent to Subagent" in result.content
    assert f"**Delegation ID**: `{handle.delegation_id}`" in result.content
    assert "result will be delivered automatically" in result.content
    assert handle.child_session_id not in result.content
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.current_child_run_id is not None
    assert relation.current_child_run_id not in result.content
    assert result.structured["handle"]["child_session_id"] == handle.child_session_id
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_sync_follow_up_result_is_delivered_to_parent(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/sync-follow-up",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            parent_tool_call_id="sync_spawn_call",
            agent_id="reader",
            task="Inspect once.",
            description="Sync follow-up delivery",
            execution_mode="sync",
        )
    )
    assert handle.parent_session_id == "ses_root"
    assert handle.parent_run_id == "run_parent"
    assert handle.parent_tool_call_id == "sync_spawn_call"
    tree = service.agent_tree_snapshot("ses_root")
    assert tree.nodes[0].revision >= 0
    assert tree.nodes[0].root_session_id == "ses_root"
    assert tree.nodes[0].parent_session_id == "ses_root"
    assert tree.nodes[0].parent_run_id == "run_parent"
    assert tree.nodes[0].parent_tool_call_id == "sync_spawn_call"
    assert (
        service.resolve_agent_navigation(
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            parent_tool_call_id="sync_spawn_call",
        )
        == tree.nodes[0]
    )
    assert (
        service.resolve_agent_navigation(
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            parent_tool_call_id="missing_call",
        )
        is None
    )
    spawned = next(
        event
        for event in store.load_session("ses_root").events
        if event.event_type == "agent_spawned"
    )
    assert spawned.payload == {
        "root_session_id": "ses_root",
        "parent_session_id": "ses_root",
        "parent_run_id": "run_parent",
        "parent_tool_call_id": "sync_spawn_call",
        "delegation_id": handle.delegation_id,
        "child_session_id": handle.child_session_id,
        "child_run_id": handle.child_run_id,
        "agent_id": "reader",
        "execution_mode": "sync",
    }
    await service.wait_agents((handle.delegation_id,))
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.result is not None
    assert relation.result.delivery.policy == "resume_waiter"
    assert relation.result.delivery.status == "pending"

    await service.send_agent_message(
        handle.delegation_id,
        "Inspect again.",
        request_id="host/sync-follow-up-1",
        mode="follow_up",
    )
    await service.wait_agents((handle.delegation_id,))

    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.result is not None
    assert relation.result.delivery.policy == "next_boundary"
    assert relation.result.delivery.status == "delivered"
    async with asyncio.timeout(5):
        while not any(
            message.metadata.get("host_source_id")
            == f"ouro.subagents/results/{relation.result.delivery.delivery_id}"
            for message in store.load_runtime_session("ses_root").messages
        ):
            await asyncio.sleep(0.01)
    delivered = [
        message
        for message in store.load_runtime_session("ses_root").messages
        if message.metadata.get("host_source_id")
        == f"ouro.subagents/results/{relation.result.delivery.delivery_id}"
    ]
    assert len(delivered) == 1
    assert delivered[0].role == "system"
    assert delivered[0].semantic_tag == "subagent-result"
    assert "## Subagent Result" in delivered[0].text_content
    assert "child complete" in delivered[0].text_content
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_agent_navigation_rejects_mismatched_child_session_backlink(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/navigation-backlink",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            parent_tool_call_id="spawn_backlink",
            agent_id="reader",
            task="Inspect once.",
            description="Backlink validation",
            execution_mode="async",
        )
    )
    await service.wait_agents((handle.delegation_id,))
    assert handle.child_session_id is not None
    child = store.load_session_record(handle.child_session_id)
    store.save_session(child.model_copy(update={"parent_session_id": "ses_wrong_parent"}))

    with pytest.raises(PersistenceError, match="child Session backlink"):
        service.resolve_agent_navigation(
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            parent_tool_call_id="spawn_backlink",
        )

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_sync_follow_up_result_stays_pending_after_parent_abort(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/sync-abort-follow-up",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            parent_tool_call_id="sync_spawn_call",
            agent_id="reader",
            task="Inspect once.",
            description="Sync abort follow-up",
            execution_mode="sync",
        )
    )
    await service.wait_agents((handle.delegation_id,))
    parent = store.load_run("ses_root", "run_parent")
    store.save_run(parent.model_copy(update={"status": "aborted"}))

    await service.send_agent_message(
        handle.delegation_id,
        "Inspect again.",
        request_id="host/sync-abort-follow-up-1",
        mode="follow_up",
    )
    await service.wait_agents((handle.delegation_id,))

    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.result is not None
    assert relation.result.delivery.policy == "resume_waiter"
    assert relation.result.delivery.status == "pending"
    assert not any(
        message.role == "system" for message in store.load_runtime_session("ses_root").messages
    )
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["sync", "async"])
async def test_interrupt_racing_natural_completion_commits_one_result(
    tmp_path,
    execution_mode,
) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/interrupt-completion-race",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Race completion with an interrupt.",
            description="Interrupt completion race",
            execution_mode=execution_mode,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    interrupt = asyncio.create_task(
        service.interrupt_agent(child.delegation_id, reason="Injected race")
    )
    provider.release.set()
    interrupted, waited = await asyncio.gather(
        interrupt,
        service.wait_agents((child.delegation_id,)),
    )

    relation = store.load_delegation(root.session_id, child.delegation_id)
    assert relation.result is not None
    assert relation.result.terminal_status in {"completed", "aborted"}
    assert interrupted.child_run_id == relation.current_child_run_id
    assert waited.completed is True
    result_events = [
        event
        for event in store.load_session(root.session_id).events
        if event.event_type == "agent_result_ready"
        and event.payload.get("delegation_id") == child.delegation_id
    ]
    assert len(result_events) == 1
    assert result_events[0].payload["child_run_id"] == relation.current_child_run_id
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_close_rejects_active_child_and_is_idempotent_after_interrupt(tmp_path) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/close-active-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Remain active until explicitly interrupted.",
            description="Close active child",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    with pytest.raises(SessionBusyError, match="interrupted before close"):
        await service.close_agent(child.delegation_id)

    interrupted = await service.interrupt_agent(child.delegation_id)
    first = await service.close_agent(child.delegation_id)
    second = await service.close_agent(child.delegation_id)

    assert first.revision > interrupted.revision
    assert second == first
    assert store.load_delegation(root.session_id, child.delegation_id).control_state == "closed"
    close_events = [
        event
        for event in store.load_session(root.session_id).events
        if event.event_type == "agent_closed"
        and event.payload.get("delegation_id") == child.delegation_id
    ]
    assert len(close_events) == 1
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_interrupt_aborts_queued_child_before_model_execution(tmp_path) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    active = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/active-before-queued",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Hold the only execution permit.",
            description="Active child",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    queued = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/queued-interrupt",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Never enter the model loop.",
            description="Queued child",
        )
    )
    assert queued.child_session_id is not None
    assert queued.child_run_id is not None
    assert store.load_run(queued.child_session_id, queued.child_run_id).status == "queued"

    interrupted = await service.interrupt_agent(queued.delegation_id)

    queued_run = store.load_run(queued.child_session_id, queued.child_run_id)
    queued_relation = store.load_delegation(root.session_id, queued.delegation_id)
    assert interrupted.status == "aborted"
    assert queued_run.status == "aborted"
    assert queued_relation.result is not None
    assert queued_relation.result.terminal_status == "aborted"
    assert store.load_runtime_session(queued.child_session_id).messages == []

    provider.release.set()
    await service.wait_agents((active.delegation_id,))
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["sync", "async"])
async def test_parent_abort_cascades_bound_child_and_preserves_result(
    tmp_path,
    execution_mode,
) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/cascade-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Remain active until the parent aborts.",
            description="Cascade child",
            execution_mode=execution_mode,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    await supervisor.runtime.abort_run(
        session_id=root.session_id,
        run_id=parent.run_id,
        reason="Parent aborted during child execution",
    )

    assert child.child_session_id is not None
    assert child.child_run_id is not None
    assert store.load_run(child.child_session_id, child.child_run_id).status == "aborted"
    relation = store.load_delegation(root.session_id, child.delegation_id)
    assert relation.result is not None
    assert relation.result.terminal_status == "aborted"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_bound_cascade_timeout_emits_diagnostic_and_keeps_unknown_child(
    tmp_path,
    monkeypatch,
) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(
        tmp_path,
        cascade_timeout_seconds=0.01,
    )
    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/cascade-timeout-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="general",
            task="Expose a cascade timeout.",
            description="Cascade timeout child",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    async def blocked_abort(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor, "abort", blocked_abort)
    await supervisor.runtime.abort_run(
        session_id=root.session_id,
        run_id=parent.run_id,
        reason="Parent aborted while child cascade is blocked",
    )

    diagnostic = next(
        event
        for event in store.load_session(root.session_id).events
        if event.event_type == "diagnostic" and event.payload.get("code") == "cascade_timeout"
    )
    assert diagnostic.payload["delegation_id"] == child.delegation_id
    assert diagnostic.payload["error_type"] == "TimeoutError"
    relation = store.load_delegation(root.session_id, child.delegation_id)
    assert relation.result is None
    assert child.child_session_id is not None
    assert child.child_run_id is not None
    assert store.load_run(child.child_session_id, child.child_run_id).status == "running"

    provider.release.set()
    await service.wait_agents((child.delegation_id,))
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
    runtime.bind_extensions(agent_definitions=definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
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
            execution_mode="sync",
        )
    )

    async with asyncio.timeout(3):
        while not service.list_agent_interactions(root.session_id):
            await asyncio.sleep(0.01)
    interaction = service.list_agent_interactions(root.session_id)[0]
    assert interaction.delegation_id == child.delegation_id
    assert interaction.child_session_id == child.child_session_id
    assert interaction.tool_call_id == "danger_call"
    assert len(interaction.arguments_preview) == 240
    assert interaction.arguments_preview.startswith('{"command": "xxxx')
    assert interaction.arguments_preview.endswith("…")
    assert interaction.input_request is None
    assert store.load_run(root.session_id, parent.run_id).status == "running"
    tree = service.agent_tree_snapshot(root.session_id)
    assert tree.pending_interaction_count == 1
    assert tree.nodes[0].status == "waiting_approval"
    assert tree.nodes[0].pending_interactions[0].request_id == interaction.request_id

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
async def test_spawn_approval_does_not_approve_the_child_tool_call(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = SpawnAndChildApprovalProvider()
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Danger",
                description="Dangerous child operation.",
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
            ModelMetadata(model_id=provider.provider_id, provider_id=provider.provider_id),
        ),
        tools=tools,
        permission_overrides={"SpawnAgent": "approval"},
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=2)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Delegate with an approved spawn.")
    parent_waiting = await supervisor.wait(parent_handle)
    assert parent_waiting.status == "waiting_approval"
    assert parent_waiting.run_id is not None
    parent_run = store.load_run(parent_handle.session_id, parent_waiting.run_id)
    spawn_approval_id = parent_run.pending_approval_id
    assert spawn_approval_id is not None

    resumed_parent = await supervisor.resolve_tool_approval(
        session_id=parent_handle.session_id,
        run_id=parent_waiting.run_id,
        approval_id=spawn_approval_id,
        approved=True,
    )
    assert (await supervisor.wait(resumed_parent)).status == "waiting_child"

    async with asyncio.timeout(5):
        while not service.list_agent_interactions(parent_handle.session_id):
            await asyncio.sleep(0.01)
    child_interaction = service.list_agent_interactions(parent_handle.session_id)[0]
    assert child_interaction.tool_name == "Danger"
    assert child_interaction.request_id != spawn_approval_id
    assert child_interaction.child_session_id != parent_handle.session_id

    resumed_child = await service.resolve_agent_approval(
        child_interaction.delegation_id,
        child_session_id=child_interaction.child_session_id,
        child_run_id=child_interaction.child_run_id,
        tool_call_id=child_interaction.tool_call_id,
        approval_id=child_interaction.request_id,
        approved=True,
    )
    assert (await supervisor.wait(resumed_child)).status == "completed"
    await service.wait_agents((child_interaction.delegation_id,))
    async with asyncio.timeout(5):
        while store.load_run(parent_handle.session_id, parent_waiting.run_id).status != "completed":
            await asyncio.sleep(0.01)
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_async_child_user_input_is_streamed_and_routed_by_exact_identity(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = AskUserChildProvider()
    tools = _ask_tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="ask-child", provider_id="ask-child"),
        ),
        tools=tools,
    )
    runtime.bind_extensions(agent_definitions=definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    await supervisor.start()
    root = SessionRecord(
        session_id="ses_ask_root",
        status="running",
        active_run_id="run_ask_parent",
        last_run_id="run_ask_parent",
        current_model=runtime.current_model().key,
    )
    parent = RunRecord(
        run_id="run_ask_parent",
        session_id=root.session_id,
        status="running",
        model="ask-child",
        agent_catalog=definitions.catalog_snapshot(tools),
    )
    store.create_session(root)
    store.save_run(parent)
    subscription = await service.subscribe_agent_tree(root.session_id)

    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/ask-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="general",
            task="Ask for one bounded choice.",
            description="Structured child input",
            execution_mode="async",
        )
    )
    async with asyncio.timeout(3):
        while True:
            notification = await subscription.get()
            if (
                notification.kind == "snapshot"
                and notification.snapshot is not None
                and notification.snapshot.pending_interaction_count == 1
            ):
                break
    interaction = service.list_agent_interactions(root.session_id)[0]
    assert interaction.kind == "user_input"
    assert interaction.arguments_preview == (
        '{"question": {"kind": "choice", "items": ["A", "B"]}}'
    )
    assert interaction.input_request == {"kind": "choice", "items": ["A", "B"]}
    assert store.load_run(root.session_id, parent.run_id).status == "running"

    with pytest.raises(ValueError, match="child identity"):
        await service.resolve_agent_input(
            child.delegation_id,
            child_session_id="ses_sibling",
            child_run_id=interaction.child_run_id,
            tool_call_id=interaction.tool_call_id,
            request_id=interaction.request_id,
            answer="A",
        )
    resumed = await service.resolve_agent_input(
        child.delegation_id,
        child_session_id=interaction.child_session_id,
        child_run_id=interaction.child_run_id,
        tool_call_id=interaction.tool_call_id,
        request_id=interaction.request_id,
        answer="A",
    )
    assert (await supervisor.wait(resumed)).status == "completed"
    await service.wait_agents((child.delegation_id,))
    assert service.read_agent(child.delegation_id).final_text == "child received answer"
    subscription.close()
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_simultaneous_child_inputs_remain_isolated(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ConcurrentAskUserProvider()
    tools = _ask_tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="concurrent-ask", provider_id="concurrent-ask"),
        ),
        tools=tools,
    )
    runtime.bind_extensions(agent_definitions=definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=2)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    await supervisor.start()
    root = SessionRecord(
        session_id="ses_concurrent_ask_root",
        status="running",
        active_run_id="run_concurrent_ask_parent",
        last_run_id="run_concurrent_ask_parent",
        current_model=runtime.current_model().key,
    )
    parent = RunRecord(
        run_id="run_concurrent_ask_parent",
        session_id=root.session_id,
        status="running",
        model="concurrent-ask",
        agent_catalog=definitions.catalog_snapshot(tools),
    )
    store.create_session(root)
    store.save_run(parent)

    children = await asyncio.gather(
        *(
            service.spawn_agent(
                SpawnAgentRequest(
                    request_id=f"host/concurrent-input-{index}",
                    parent_session_id=root.session_id,
                    parent_run_id=parent.run_id,
                    agent_id="general",
                    task=f"Ask concurrent question {index}.",
                    description=f"Concurrent input child {index}",
                )
            )
            for index in range(2)
        )
    )
    async with asyncio.timeout(3):
        while len(service.list_agent_interactions(root.session_id)) != 2:
            await asyncio.sleep(0.01)
    interactions = service.list_agent_interactions(root.session_id)
    interaction_by_session = {item.child_session_id: item for item in interactions}
    assert set(interaction_by_session) == {child.child_session_id for child in children}
    first = children[0]
    second = children[1]
    assert first.child_session_id is not None
    assert second.child_session_id is not None
    first_interaction = interaction_by_session[first.child_session_id]
    second_interaction = interaction_by_session[second.child_session_id]

    with pytest.raises(ValueError, match="canonical request"):
        await service.resolve_agent_input(
            first.delegation_id,
            child_session_id=first_interaction.child_session_id,
            child_run_id=first_interaction.child_run_id,
            tool_call_id=first_interaction.tool_call_id,
            request_id=second_interaction.request_id,
            answer="wrong child",
        )
    resumed = await asyncio.gather(
        *(
            service.resolve_agent_input(
                child.delegation_id,
                child_session_id=interaction.child_session_id,
                child_run_id=interaction.child_run_id,
                tool_call_id=interaction.tool_call_id,
                request_id=interaction.request_id,
                answer=f"answer {index}",
            )
            for index, (child, interaction) in enumerate(
                (
                    (first, first_interaction),
                    (second, second_interaction),
                )
            )
        )
    )
    assert {
        status.status
        for status in await asyncio.gather(*(supervisor.wait(handle) for handle in resumed))
    } == {"completed"}
    await service.wait_agents(tuple(child.delegation_id for child in children))
    assert service.agent_tree_snapshot(root.session_id).terminal_count == 2
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_unattended_child_input_fails_deterministically_without_parking(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = AskUserChildProvider()
    tools = _ask_tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="unattended-child", provider_id="ask-child"),
        ),
        tools=tools,
    )
    runtime.bind_extensions(agent_definitions=definitions)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    await supervisor.start()
    root = SessionRecord(
        session_id="ses_unattended_root",
        status="running",
        active_run_id="run_unattended_parent",
        last_run_id="run_unattended_parent",
        current_model=runtime.current_model().key,
        interaction_mode="unattended",
    )
    parent = RunRecord(
        run_id="run_unattended_parent",
        session_id=root.session_id,
        status="running",
        model="unattended-child",
        agent_catalog=definitions.catalog_snapshot(tools),
    )
    store.create_session(root)
    store.save_run(parent)

    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/unattended-child",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="general",
            task="Do not park for input.",
            description="Unattended child",
        )
    )
    async with asyncio.timeout(3):
        result = await service.wait_agents((child.delegation_id,))

    assert result.completed is True
    assert service.list_agent_interactions(root.session_id) == ()
    assert child.child_session_id is not None
    child_snapshot = store.load_runtime_session(child.child_session_id)
    assert child_snapshot.session.interaction_mode == "unattended"
    input_error = next(
        event
        for event in store.load_session(child.child_session_id).events
        if event.event_type == "tool_call_completed" and event.payload.get("tool_name") == "AskUser"
    )
    assert input_error.payload["is_error"] is True
    assert service.read_agent(child.delegation_id).final_text == "child received answer"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_model_spawn_rejects_non_active_parent_run(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    store.save_run(parent.model_copy(update={"status": "completed"}))

    with pytest.raises(SessionBusyError, match="must be active for model delegation"):
        await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/rejected",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                parent_tool_call_id="call_rejected",
                agent_id="reader",
                task="Do not run.",
                description="Rejected",
            )
        )

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_host_spawn_can_use_completed_parent_frozen_catalog(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    store.save_run(parent.model_copy(update={"status": "completed"}))

    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/completed-parent",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Use the completed parent contract.",
            description="Host delegation",
            unattended=True,
        )
    )
    waited = await service.wait_agents((child.delegation_id,))

    assert waited.completed is True
    assert waited.handles[0].status == "completed"
    assert child.child_session_id is not None
    assert store.load_session_record(child.child_session_id).interaction_mode == "unattended"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_tree_projects_missing_child_run_as_recoverable(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    assert parent.agent_catalog is not None
    reader = parent.agent_catalog.resolve("reader")
    relation = store.create_delegation(
        DelegationRecord(
            idempotency_key="host/missing-child-run",
            root_session_id="ses_root",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            child_session_id="ses_missing_child",
            current_child_run_id="run_missing_child",
            agent_id="reader",
            task_description="Missing child evidence",
            prompt_digest="sha256:" + "c" * 64,
            spawn_state="submitted",
            definition=reader.definition,
            effective_tools=reader.effective_tools,
        )
    )

    tree = service.agent_tree_snapshot("ses_root")

    assert tree.nodes[0].delegation_id == relation.delegation_id
    assert tree.nodes[0].status == "recoverable"
    assert tree.nodes[0].activity is None
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_concurrent_spawns_are_scheduled_and_subagent_cannot_spawn_nested_agent(
    tmp_path,
) -> None:
    service, supervisor, _, provider, root, parent = await _active_subagent_services(tmp_path)
    handles = await asyncio.gather(
        *(
            service.spawn_agent(
                SpawnAgentRequest(
                    request_id=f"host/concurrent-{index}",
                    parent_session_id=root.session_id,
                    parent_run_id=parent.run_id,
                    agent_id="general",
                    task=f"Run concurrent task {index}.",
                    description=f"Concurrent subagent {index}",
                )
            )
            for index in range(2)
        ),
    )
    assert len(handles) == 2
    assert len({handle.delegation_id for handle in handles}) == 2
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    with pytest.raises(ValueError, match="maximum subagent depth is 1"):
        await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/nested-subagent",
                parent_session_id=handles[0].child_session_id,
                parent_run_id=handles[0].child_run_id,
                agent_id="reader",
                task="Attempt nested delegation.",
                description="Nested subagent",
            )
        )

    provider.release.set()
    await service.wait_agents(tuple(handle.delegation_id for handle in handles))
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_shared_root_budget_pauses_queued_child_and_aggregates_each_run_once(
    tmp_path,
) -> None:
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    parent = parent.model_copy(
        update={
            "metadata": {
                **parent.metadata,
                "run_budget": {"max_input_tokens": 10},
            }
        }
    )
    store.save_run(parent)
    first = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/root-budget-first",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Complete the already-started work.",
            description="First budget child",
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    second = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/root-budget-second",
            parent_session_id=root.session_id,
            parent_run_id=parent.run_id,
            agent_id="reader",
            task="Pause before starting when the root budget is gone.",
            description="Second budget child",
        )
    )
    first_run = store.load_run(first.child_session_id, first.child_run_id)
    store.save_run(first_run.model_copy(update={"usage": {"input_tokens": 10}}))

    provider.release.set()
    await service.wait_agents((first.delegation_id, second.delegation_id))

    assert store.load_run(first.child_session_id, first.child_run_id).status == "completed"
    assert store.load_run(second.child_session_id, second.child_run_id).status == "paused"
    first_relation = store.load_delegation(root.session_id, first.delegation_id)
    second_relation = store.load_delegation(root.session_id, second.delegation_id)
    assert first_relation.result is not None
    assert first_relation.result.terminal_status == "completed"
    assert second_relation.result is not None
    assert second_relation.result.terminal_status == "paused"
    assert provider.round == 1

    follow_up = RunRecord(
        session_id=first.child_session_id,
        parent_run_id=parent.run_id,
        trigger="subagent_follow_up",
        status="completed",
        usage={"input_tokens": 3},
    )
    store.save_run(follow_up)
    tree = service.agent_tree_snapshot(root.session_id)
    assert tree.root_usage == {"input_tokens": 13}
    first_node = next(node for node in tree.nodes if node.delegation_id == first.delegation_id)
    assert first_node.usage == {"input_tokens": 13}

    with pytest.raises(SessionBusyError, match="root input token budget is exhausted"):
        await service.spawn_agent(
            SpawnAgentRequest(
                request_id="host/root-budget-exhausted",
                parent_session_id=root.session_id,
                parent_run_id=parent.run_id,
                agent_id="reader",
                task="Do not start after exhaustion.",
                description="Exhausted budget child",
            )
        )
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
    tree = service.agent_tree_snapshot("ses_root")
    assert tree.root_usage == {"input_tokens": 25, "output_tokens": 10}
    assert tree.root_budget is not None
    assert tree.root_budget.max_input_tokens == 100
    assert tree.nodes[0].budget.max_input_tokens == 75
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_root_model_round_budget_uses_durable_run_counters(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    parent = parent.model_copy(
        update={
            "model_round_count": 2,
            "metadata": {
                **parent.metadata,
                "run_budget": {"max_model_rounds": 3},
            },
        }
    )
    store.save_run(parent)

    handle = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/round-budget",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Use the final shared model round.",
            description="Final model round",
        )
    )
    relation = store.load_delegation("ses_root", handle.delegation_id)
    assert relation.budget.max_model_rounds == 1

    await service.wait_agents((handle.delegation_id,))
    child = store.load_run(handle.child_session_id, handle.child_run_id)
    assert child.model_round_count == 1
    assert service.budget_reason(parent, 1) == "model_rounds"

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_unlimited_shared_budget_does_not_scan_child_usage(tmp_path, monkeypatch) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    parent = store.load_run("ses_root", "run_parent")
    parent = parent.model_copy(
        update={
            "metadata": {
                "run_budget": {
                    "max_model_rounds": None,
                    "max_input_tokens": None,
                    "max_output_tokens": None,
                    "max_elapsed_seconds": None,
                }
            }
        }
    )
    store.save_run(parent)

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("unlimited budgets must not enumerate child history")

    monkeypatch.setattr(store, "list_delegations", unexpected_scan)
    try:
        assert service.budget_reason(parent, 1) is None
    finally:
        await service.close()
        await supervisor.shutdown()


@pytest.mark.asyncio
async def test_primary_budget_check_does_not_scan_unrelated_delegations(tmp_path) -> None:
    service, supervisor, store, _ = await _services(tmp_path)
    unrelated = SessionRecord(session_id="ses_corrupt_history")
    store.create_session(unrelated)
    relation = store.create_delegation(
        DelegationRecord(
            delegation_id="dlg_corrupt_history",
            idempotency_key="old/background",
            root_session_id=unrelated.session_id,
            parent_session_id=unrelated.session_id,
            parent_run_id="run_old",
            agent_id="reader",
            task_description="Old incompatible delegation",
            prompt_digest="sha256:old",
            definition=AgentDefinition(
                agent_id="reader",
                name="Reader",
                description="Old reader",
            ),
        )
    )
    relation_path = (
        store.sessions_dir / unrelated.session_id / "delegations" / f"{relation.delegation_id}.json"
    )
    payload = relation.model_dump(mode="json")
    payload["execution_mode"] = "background"
    relation_path.write_text(json.dumps(payload), encoding="utf-8")
    parent = store.load_run("ses_root", "run_parent")

    assert service.budget_reason(parent, 1) is None
    with pytest.raises(PersistenceError, match="execution_mode"):
        store.list_delegations(unrelated.session_id)

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_root_tree_subscription_adds_children_and_reconnects_from_store(tmp_path) -> None:
    service, supervisor, _, _ = await _services(tmp_path)
    subscription = await service.subscribe_agent_tree("ses_root")

    assert subscription.initial_snapshot.nodes == ()
    child = await service.spawn_agent(
        SpawnAgentRequest(
            request_id="host/tree-child",
            parent_session_id="ses_root",
            parent_run_id="run_parent",
            agent_id="reader",
            task="Inspect the tree projection.",
            description="Tree projection child",
        )
    )

    observed = None
    async with asyncio.timeout(3):
        while observed is None:
            notification = await subscription.get()
            if (
                notification.kind == "snapshot"
                and notification.snapshot is not None
                and any(
                    node.delegation_id == child.delegation_id
                    for node in notification.snapshot.nodes
                )
            ):
                observed = notification.snapshot
    assert len(observed.edges) == 1
    assert observed.edges[0].delegation_id == child.delegation_id
    assert observed.edges[0].parent_session_id == "ses_root"
    assert observed.edges[0].child_session_id == child.child_session_id
    assert observed.revision > subscription.initial_snapshot.revision

    subscription.close()
    await service.wait_agents((child.delegation_id,))
    assert child.child_session_id is not None
    child_stream = await supervisor.subscribe(child.child_session_id, after_sequence=0)
    async with asyncio.timeout(3):
        while True:
            child_notification = await child_stream.get()
            if (
                child_notification.kind == "event"
                and child_notification.event is not None
                and child_notification.event.event_type == "model_text_delta"
            ):
                assert child_notification.event.session_id == child.child_session_id
                break
    child_stream.close()
    reconnected = await service.subscribe_agent_tree("ses_root")
    assert reconnected.initial_snapshot == service.agent_tree_snapshot("ses_root")
    assert reconnected.initial_snapshot.terminal_count == 1
    initial_revision = reconnected.initial_snapshot.revision
    await service.send_agent_message(
        child.delegation_id,
        "Inspect one follow-up for the live tree.",
        request_id="host/tree-follow-up",
        mode="follow_up",
    )
    async with asyncio.timeout(3):
        while True:
            notification = await reconnected.get()
            if (
                notification.kind == "snapshot"
                and notification.snapshot is not None
                and notification.snapshot.revision > initial_revision
            ):
                break
    await service.wait_agents((child.delegation_id,))
    await service.close_agent(child.delegation_id)
    async with asyncio.timeout(3):
        while True:
            notification = await reconnected.get()
            if notification.kind == "snapshot" and notification.snapshot is not None:
                if notification.snapshot.nodes[0].control_state == "closed":
                    break
    reconnected.close()
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_slow_root_tree_subscriber_gets_lag_diagnostic(tmp_path) -> None:
    service, supervisor, _, _ = await _services(tmp_path)
    subscription = await service.subscribe_agent_tree("ses_root", max_buffer=1)

    children = await asyncio.gather(
        *(
            service.spawn_agent(
                SpawnAgentRequest(
                    request_id=f"host/lag-child-{index}",
                    parent_session_id="ses_root",
                    parent_run_id="run_parent",
                    agent_id="reader",
                    task=f"Inspect lag path {index}.",
                    description=f"Lag child {index}",
                )
            )
            for index in range(2)
        )
    )
    await service.wait_agents(tuple(child.delegation_id for child in children))
    await asyncio.sleep(0)

    notification = await subscription.get()
    assert notification.kind == "diagnostic"
    assert notification.diagnostic is not None
    assert notification.diagnostic.code == "agent_tree_subscription_lagged"
    tree = service.agent_tree_snapshot("ses_root")
    assert tree.terminal_count == 2
    assert {node.delegation_id for node in tree.nodes} == {
        child.delegation_id for child in children
    }
    assert len({node.session_id for node in tree.nodes}) == 2
    subscription.close()
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_concurrency", [1, 2])
async def test_sync_spawn_releases_parent_worker_and_resumes_original_tool_call(
    tmp_path,
    max_concurrency: int,
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
    provider = SyncAgentProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="sync", provider_id="sync"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=max_concurrency)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
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
    spawn_result_messages = [
        message
        for message in snapshot.messages
        if message.role == "tool_result"
        and message.tool_result is not None
        and message.tool_result.tool_name == "SpawnAgent"
    ]
    assert parent_run.status == "completed"
    assert parent_run.run_id == parent_run_id
    assert len(spawn_result_messages) == 1
    assert spawn_result_messages[0].tool_result is not None
    assert spawn_result_messages[0].tool_result.tool_call_id == "spawn_call"
    spawn_content = spawn_result_messages[0].text_content
    delegation = store.list_delegations(root_session_id)[0]
    assert "## Subagent Result" in spawn_content
    assert f"**Delegation ID**: `{delegation.delegation_id}`" in spawn_content
    assert "**Status**: completed" in spawn_content
    assert "child evidence" in spawn_content
    assert delegation.child_session_id not in spawn_content
    assert delegation.current_child_run_id not in spawn_content
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
            "description": "A general-purpose subagent for a bounded delegated task.",
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
    assert [
        event.payload["tool_call_id"]
        for event in events
        if event.event_type == "tool_call_completed"
        and event.payload.get("tool_name") == "SpawnAgent"
    ] == ["spawn_call"]
    relation = store.list_delegations(root_session_id)[0]
    assert relation.result is not None
    assert relation.result.delivery.status == "delivered"
    assert relation.result.delivery.target_run_id == parent_run_id

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_parallel_sync_spawns_serialize_parent_resumption(tmp_path) -> None:
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
    provider = ParallelSyncAgentProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="parallel-sync", provider_id="parallel-sync"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=4)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Delegate two synchronous tasks.")
    parked = await supervisor.wait(parent_handle)
    assert parked.status == "waiting_child"
    assert parked.run_id is not None

    async with asyncio.timeout(5):
        while store.load_run(parent_handle.session_id, parked.run_id).status != "completed":
            await asyncio.sleep(0.01)

    events = store.load_session(parent_handle.session_id).events
    completed_spawn_ids = {
        event.payload["tool_call_id"]
        for event in events
        if event.event_type == "tool_call_completed"
        and event.payload.get("tool_name") == "SpawnAgent"
    }
    assert completed_spawn_ids == {"parallel_spawn_0", "parallel_spawn_1"}
    snapshot = store.load_runtime_session(parent_handle.session_id)
    assert snapshot.messages[-1].text_content == "parallel parent resumed"
    relations_by_call = {
        relation.parent_tool_call_id: relation
        for relation in store.list_delegations(parent_handle.session_id)
    }
    spawn_results = [
        message
        for message in snapshot.messages
        if message.tool_result is not None and message.tool_result.tool_name == "SpawnAgent"
    ]
    assert {message.tool_result.tool_call_id for message in spawn_results} == {
        "parallel_spawn_0",
        "parallel_spawn_1",
    }
    for message in spawn_results:
        assert message.tool_result is not None
        assert message.tool_execution is not None
        relation = relations_by_call[message.tool_result.tool_call_id]
        assert relation.result is not None
        assert message.tool_execution.started_at == relation.created_at
        assert message.tool_execution.ended_at == relation.result.result_ready_at
        assert message.tool_execution.ended_at > message.tool_execution.started_at

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_sync_child_hitl_keeps_parent_parked_and_single_worker_progresses(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = _ask_tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    provider = SyncHitlProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="sync-hitl", provider_id="sync-hitl"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Delegate a synchronous question.")
    parked = await supervisor.wait(parent_handle)
    assert parked.status == "waiting_child"
    assert parked.run_id is not None
    root_session_id = parent_handle.session_id

    async with asyncio.timeout(3):
        while not service.list_agent_interactions(root_session_id):
            await asyncio.sleep(0.01)
    interaction = service.list_agent_interactions(root_session_id)[0]
    assert interaction.kind == "user_input"
    assert store.load_run(root_session_id, parked.run_id).status == "waiting_child"
    tree = service.agent_tree_snapshot(root_session_id)
    assert tree.nodes[0].status == "waiting_user"
    assert tree.pending_interaction_count == 1
    assert tree.root_activity is not None
    assert tree.root_activity.status == "waiting_child"
    assert tree.waiting_on_delegation_ids == (tree.nodes[0].delegation_id,)

    child_resume = await service.resolve_agent_input(
        tree.nodes[0].delegation_id,
        child_session_id=interaction.child_session_id,
        child_run_id=interaction.child_run_id,
        tool_call_id=interaction.tool_call_id,
        request_id=interaction.request_id,
        answer="A",
    )
    assert (await supervisor.wait(child_resume)).status == "completed"
    async with asyncio.timeout(5):
        while True:
            relation = store.list_delegations(root_session_id)[0]
            if (
                store.load_run(root_session_id, parked.run_id).status == "completed"
                and relation.result is not None
                and relation.result.delivery.status == "delivered"
            ):
                break
            await asyncio.sleep(0.01)

    root_snapshot = store.load_runtime_session(root_session_id)
    assert root_snapshot.messages[-1].text_content == "sync parent resumed"
    relation = store.list_delegations(root_session_id)[0]
    assert relation.result is not None
    assert relation.result.delivery.status == "delivered"
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_parent_abort_resolves_sync_wait_and_cascades_waiting_child(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = _ask_tools()
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            SyncHitlProvider(),
            ModelMetadata(model_id="sync-hitl", provider_id="sync-hitl"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Delegate a synchronous question.")
    parked = await supervisor.wait(parent_handle)
    assert parked.status == "waiting_child"
    assert parked.run_id is not None
    async with asyncio.timeout(3):
        while not service.list_agent_interactions(parent_handle.session_id):
            await asyncio.sleep(0.01)

    await runtime.abort_run(
        session_id=parent_handle.session_id,
        run_id=parked.run_id,
        reason="Abort the parked parent",
    )

    parent = store.load_run(parent_handle.session_id, parked.run_id)
    assert parent.status == "aborted"
    assert parent.active_tool_batch is not None
    spawn_call = parent.active_tool_batch.calls[0]
    assert spawn_call.status == "aborted"
    assert spawn_call.agent_wait is not None
    assert spawn_call.agent_wait.state == "resolved"
    assert spawn_call.agent_wait.resolution is not None
    assert spawn_call.agent_wait.resolution.reason == "interrupted"
    assert spawn_call.agent_wait.resolution.resolved_delegation_id is None

    relation = store.list_delegations(parent_handle.session_id)[0]
    assert relation.child_session_id is not None
    assert relation.current_child_run_id is not None
    assert (
        store.load_run(relation.child_session_id, relation.current_child_run_id).status == "aborted"
    )
    async with asyncio.timeout(3):
        while service._armed_wait_ids:  # noqa: SLF001 - verifies waiter lifecycle cleanup.
            await asyncio.sleep(0.01)

    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_next_boundary_delivery_racing_parent_round_is_committed_once(tmp_path) -> None:
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
    provider = NextBoundaryRaceProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="next-boundary", provider_id="next-boundary"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=2)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Start a next-boundary child.")
    await asyncio.wait_for(provider.root_blocked.wait(), timeout=3)
    relations = store.list_delegations(parent_handle.session_id)
    assert len(relations) == 1
    child = relations[0]

    waited = await service.wait_agents((child.delegation_id,))

    delivered = store.load_delegation(parent_handle.session_id, child.delegation_id)
    assert waited.completed is True
    assert delivered.result is not None
    assert delivered.result.delivery.status == "delivered"
    assert delivered.result.delivery.target_queue_id == delivered.result.delivery.delivery_id
    queued = store.load_session_record(parent_handle.session_id).metadata["follow_up_queue"]
    assert [item["queue_id"] for item in queued] == [delivered.result.delivery.delivery_id]

    provider.root_release.set()
    parent = await supervisor.wait(parent_handle)
    assert parent.status == "completed"
    snapshot = store.load_runtime_session(parent_handle.session_id)
    delivery_messages = [
        message
        for message in snapshot.messages
        if message.metadata.get("host_source_id")
        == f"ouro.subagents/results/{delivered.result.delivery.delivery_id}"
    ]
    assert len(delivery_messages) == 1
    assert delivery_messages[0].role == "system"
    assert len(snapshot.runs) == 1
    assert snapshot.messages[-1].text_content == "parent consumed child result"
    await service.reconcile()
    assert len(store.load_runtime_session(parent_handle.session_id).messages) == len(
        snapshot.messages
    )
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_async_result_delivery_is_exact_once(tmp_path) -> None:
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
    provider = AsyncFollowUpProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="async", provider_id="async"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Start async work.")
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
    assert relation.execution_mode == "async"
    assert relation.result.delivery.policy == "next_boundary"
    assert relation.result.delivery.target_queue_id == relation.result.delivery.delivery_id
    async with asyncio.timeout(5):
        while True:
            snapshot = store.load_runtime_session(root_session_id)
            if (
                len(snapshot.runs) == 2
                and snapshot.runs[-1].status == "completed"
                and snapshot.messages[-1].text_content == "parent received follow-up"
            ):
                break
            await asyncio.sleep(0.01)
    snapshot = store.load_runtime_session(root_session_id)
    assert len(snapshot.runs) == 2
    delivered_messages = [
        message
        for message in snapshot.messages
        if message.metadata.get("host_source_id")
        == f"ouro.subagents/results/{relation.result.delivery.delivery_id}"
    ]
    assert len(delivered_messages) == 1
    assert delivered_messages[0].role == "system"
    delivered_content = delivered_messages[0].text_content
    assert "## Subagent Result" in delivered_content
    assert f"**Delegation ID**: `{relation.delegation_id}`" in delivered_content
    assert "**Status**: completed" in delivered_content
    assert "async child result" in delivered_content
    assert relation.child_session_id not in delivered_content
    assert relation.current_child_run_id not in delivered_content
    assert snapshot.messages[-1].text_content == "parent received follow-up"
    spawn_results = [
        message
        for message in snapshot.messages
        if message.tool_result is not None
        and message.tool_result.tool_name == "SpawnAgent"
        and message.tool_result.tool_call_id == "async_spawn"
    ]
    assert len(spawn_results) == 1
    async_content = spawn_results[0].text_content
    assert "## Subagent Dispatched (Async)" in async_content
    assert f"**Delegation ID**: `{relation.delegation_id}`" in async_content
    assert "result will be delivered automatically" in async_content
    assert relation.child_session_id not in async_content
    assert relation.current_child_run_id not in async_content
    assert not any(
        event.event_type == "agent_wait_requested"
        for event in store.load_session(root_session_id).events
    )

    assert relation.result is not None
    pending_delivery = relation.result.delivery.model_copy(
        update={
            "status": "pending",
            "target_run_id": None,
            "target_message_id": None,
            "target_queue_id": None,
            "delivered_at": None,
        }
    )
    store.save_delegation(
        relation.model_copy(
            update={"result": relation.result.model_copy(update={"delivery": pending_delivery})}
        ),
        expected_revision=relation.revision,
    )
    await service.reconcile()
    reconciled = store.load_runtime_session(root_session_id)
    repaired = store.load_delegation(root_session_id, relation.delegation_id)
    assert repaired.result is not None
    assert repaired.result.delivery.status == "delivered"
    assert repaired.result.delivery.target_message_id == delivered_messages[0].message_id
    assert len(reconciled.runs) == 2
    assert len(reconciled.messages) == len(snapshot.messages)
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_delivery_run_can_message_the_same_subagent_without_deadlock(tmp_path) -> None:
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
    provider = AsyncSendAfterDeliveryProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="async", provider_id="async"),
        ),
        tools=tools,
    )
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    await service.start()

    parent_handle = await supervisor.submit("Start async work.")
    initial = await supervisor.wait(parent_handle)
    assert initial.status == "completed"
    root_session_id = parent_handle.session_id
    async with asyncio.timeout(10):
        while True:
            snapshot = store.load_runtime_session(root_session_id)
            relations = store.list_delegations(root_session_id)
            if (
                len(snapshot.runs) == 3
                and all(run.status == "completed" for run in snapshot.runs)
                and relations
                and relations[0].result is not None
                and relations[0].result.delivery.status == "delivered"
            ):
                break
            await asyncio.sleep(0.01)

    snapshot = store.load_runtime_session(root_session_id)
    send_results = [
        message
        for message in snapshot.messages
        if message.tool_result is not None and message.tool_result.tool_name == "SendAgentMessage"
    ]
    assert len(send_results) == 1
    assert not send_results[0].tool_result.is_error
    relation = store.list_delegations(root_session_id)[0]
    assert relation.child_session_id is not None
    child_snapshot = store.load_runtime_session(relation.child_session_id)
    assert len(child_snapshot.runs) == 2
    await service.close()
    await supervisor.shutdown()


async def test_late_child_delivery_is_rejected_at_parent_acceptance_after_undo(
    tmp_path, monkeypatch
):
    service, supervisor, store, _ = await _services(tmp_path)
    runtime = supervisor.runtime
    parent = store.load_run("ses_root", "run_parent")
    user = MessageRecord.text(
        session_id=parent.session_id,
        run_id=parent.run_id,
        role="user",
        text="withdraw parent",
    ).model_copy(update={"metadata": {"session_input_source": "user"}})
    store.append_message(user)
    entered, release = asyncio.Event(), asyncio.Event()
    route = supervisor.route_host_input

    async def gated(request, *, session_id):
        entered.set()
        await release.wait()
        return await route(request, session_id=session_id)

    monkeypatch.setattr(supervisor, "route_host_input", gated)
    try:
        handle = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="late-delivery",
                parent_session_id=parent.session_id,
                parent_run_id=parent.run_id,
                parent_tool_call_id="spawn",
                agent_id="reader",
                task="Return evidence",
                description="Delayed delivery",
                execution_mode="async",
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        child_before = store.load_runtime_session(handle.child_session_id)
        await runtime.abort_run(session_id=parent.session_id, run_id=parent.run_id)
        await runtime.undo(parent.session_id, message_id=user.message_id)
        release.set()
        async with asyncio.timeout(5):
            while True:
                relation = store.load_delegation(parent.session_id, handle.delegation_id)
                if relation.result.delivery.status == "skipped":
                    break
                await asyncio.sleep(0.01)
        assert relation.result.delivery.error == "parent_history_withdrawn"
        assert store.load_runtime_session(parent.session_id).messages == []
        assert (
            runtime.queued_inputs.eligible_session(store.load_session_record(parent.session_id))
            == []
        )
        child_after = store.load_runtime_session(handle.child_session_id)
        assert child_after.messages == child_before.messages
        assert child_after.runs == child_before.runs
    finally:
        release.set()
        await service.close()
        await supervisor.shutdown()


async def test_undo_completed_parent_cancels_bound_child_without_rewriting_parent_audit(tmp_path):
    service, supervisor, store, provider, root, parent = await _active_subagent_services(tmp_path)
    user = MessageRecord.text(
        session_id=root.session_id,
        run_id=parent.run_id,
        role="user",
        text="withdraw parent",
    ).model_copy(update={"metadata": {"session_input_source": "user"}})
    store.append_message(user)
    try:
        handle = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="withdraw-bound-child",
                parent_session_id=root.session_id,
                parent_run_id=parent.run_id,
                agent_id="reader",
                task="Remain active",
                description="Bound child",
                execution_mode="async",
            )
        )
        await asyncio.wait_for(provider.started.wait(), 5)
        completed = parent.model_copy(update={"status": "completed", "ended_at": utc_now()})
        current = store.load_session_record(root.session_id)
        store.commit_session(
            SessionCommit(
                before=current,
                after=current.model_copy(update={"status": "idle", "active_run_id": None}),
                runs=(completed,),
            )
        )
        child_before = store.load_runtime_session(handle.child_session_id)
        result = await supervisor.runtime.undo(root.session_id, message_id=user.message_id)
        assert result.session.history_epoch == 1
        assert store.load_run(root.session_id, parent.run_id) == completed
        child_after = store.load_runtime_session(handle.child_session_id)
        assert child_after.messages[: len(child_before.messages)] == child_before.messages
        assert child_after.runs[-1].status == "aborted"
        relation = store.load_delegation(root.session_id, handle.delegation_id)
        assert relation.result is not None
        assert relation.result.terminal_status == "aborted"
    finally:
        await service.close()
        await supervisor.shutdown()
