import asyncio
from collections.abc import Sequence

import pytest

from ouro.application.runtime import RuntimeService
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit
from ouro.core.session_controls import (
    AttributedSessionInput,
    AttributedSessionInputBatch,
    HostSessionInput,
)
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


class TurnGateProvider:
    provider_id = "turn-gate"

    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.contexts: list[list[ModelMessage]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, tools
        self.contexts.append(list(messages))
        call_number = len(self.contexts)
        if call_number == 1:
            self.first_started.set()
            await self.release_first.wait()
        yield ProviderStreamEvent(type="text_delta", text=f"answer-{call_number}")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
        )


class ToolTurnGateProvider(TurnGateProvider):
    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, tools
        self.contexts.append(list(messages))
        if len(self.contexts) == 1:
            self.first_started.set()
            await self.release_first.wait()
            yield ProviderStreamEvent.from_tool_call(
                {"id": "echo-1", "name": "Echo", "arguments": {}},
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="after-tool")
        yield ProviderStreamEvent(type="done")


def _runtime(tmp_path, *, consumption_mode: str):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = TurnGateProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="turn-gate", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        queued_input_config=QueuedInputConfig(consumption_mode=consumption_mode),
    )
    return store, provider, runtime


async def _active_session(store: LocalSessionStore):
    for _ in range(100):
        sessions = store.list_sessions()
        if sessions and sessions[0].active_run_id:
            return sessions[0]
        await asyncio.sleep(0.01)
    raise AssertionError("active session was not created")


def _user_texts(messages: Sequence[MessageRecord | ModelMessage]) -> list[str]:
    return [message.text_content for message in messages if message.role == "user"]


@pytest.mark.asyncio
async def test_one_by_one_consumption_persists_an_attributed_input_batch_together(
    tmp_path,
    monkeypatch,
) -> None:
    store, provider, runtime = _runtime(tmp_path, consumption_mode="one_by_one")
    session = runtime.create_session(title="Atomic plugin input")
    common = {
        "plugin_id": "dev.example.goal",
        "contribution_id": "goal",
        "generation_id": "generation_1",
        "execution_contract_hash": "contract_1",
    }
    batch = AttributedSessionInputBatch(
        inputs=(
            AttributedSessionInput(
                prompt="create instruction",
                role="user",
                semantic_tag="goal-create",
                **common,
            ),
            AttributedSessionInput(prompt="exact objective", role="user", **common),
        )
    )
    persisted_batch_sizes: list[int] = []
    commit_session = store.commit_session

    def record_batch(request: SessionCommit):
        result = commit_session(request)
        persisted_batch_sizes.append(len(result.messages))
        return result

    monkeypatch.setattr(store, "commit_session", record_batch)
    provider.release_first.set()

    result = await runtime.submit_plugin_input_batch(batch, session_id=session.session_id)

    assert result.run.status == "completed"
    assert len(provider.contexts) == 1
    visible = _user_texts(provider.contexts[0])
    assert "<goal-create>" in visible[-2]
    assert "create instruction" in visible[-2]
    assert visible[-1] == "exact objective"
    assert 2 in persisted_batch_sizes
    snapshot = store.load_session(session.session_id)
    inputs = [message for message in snapshot.messages if message.role == "user"]
    assert [message.semantic_tag for message in inputs] == ["goal-create", None]
    assert len({message.run_id for message in inputs}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("consumption_mode", "expected_calls", "expected_run_count", "expected_contexts"),
    [
        (
            "one_by_one",
            3,
            1,
            [["initial"], ["initial", "steer-1"], ["initial", "steer-1", "steer-2"]],
        ),
        (
            "all_at_once",
            2,
            1,
            [["initial"], ["initial", "steer-1", "steer-2"]],
        ),
    ],
)
async def test_steering_inputs_are_consumed_at_turn_boundaries_in_fifo_order(
    tmp_path,
    consumption_mode,
    expected_calls,
    expected_run_count,
    expected_contexts,
) -> None:
    store, provider, runtime = _runtime(tmp_path, consumption_mode=consumption_mode)

    first_task = asyncio.create_task(runtime.run("initial"))
    session = await _active_session(store)
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await runtime.run("steer-1", session_id=session.session_id, queue_mode="steer")
    await runtime.run("steer-2", session_id=session.session_id, queue_mode="steer")
    provider.release_first.set()
    await asyncio.wait_for(first_task, timeout=2)

    snapshot = store.load_session(session.session_id)
    assert len(provider.contexts) == expected_calls
    assert len(snapshot.runs) == expected_run_count
    assert [_user_texts(context) for context in provider.contexts] == expected_contexts
    assert "follow_up_queue" not in snapshot.session.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("consumption_mode", "expected_calls", "expected_run_count", "last_context_users"),
    [
        ("one_by_one", 3, 3, ["initial", "follow-1", "follow-2"]),
        ("all_at_once", 2, 2, ["initial", "follow-1", "follow-2"]),
    ],
)
async def test_follow_up_inputs_are_consumed_at_run_boundaries_in_fifo_order(
    tmp_path,
    consumption_mode,
    expected_calls,
    expected_run_count,
    last_context_users,
) -> None:
    store, provider, runtime = _runtime(tmp_path, consumption_mode=consumption_mode)

    first_task = asyncio.create_task(runtime.run("initial"))
    session = await _active_session(store)
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await runtime.run("follow-1", session_id=session.session_id, queue_mode="follow_up")
    await runtime.run("follow-2", session_id=session.session_id, queue_mode="follow_up")
    provider.release_first.set()
    await asyncio.wait_for(first_task, timeout=2)

    snapshot = store.load_session(session.session_id)
    assert len(provider.contexts) == expected_calls
    assert len(snapshot.runs) == expected_run_count
    assert _user_texts(provider.contexts[-1]) == last_context_users
    assert "follow_up_queue" not in snapshot.session.metadata


@pytest.mark.asyncio
async def test_host_follow_up_preserves_explicit_scaffold_attribution(tmp_path) -> None:
    store, provider, runtime = _runtime(tmp_path, consumption_mode="one_by_one")

    first_task = asyncio.create_task(runtime.run("initial"))
    session = await _active_session(store)
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    receipt = await runtime.submit_host_input(
        HostSessionInput(
            prompt="Recover from the durable checkpoint.",
            mode="follow_up",
            title="Recovery",
            host_source_id="ouro.runtime/recovery",
            semantic_tag="runtime-recovery",
        ),
        session_id=session.session_id,
    )

    queued_event = store.load_session(session.session_id).events[-1]
    assert receipt.session_id == session.session_id
    assert queued_event.payload["queue_id"] == receipt.queue_id
    assert queued_event.payload["source"] == "host"
    assert queued_event.payload["host_source_id"] == "ouro.runtime/recovery"
    provider.release_first.set()
    await asyncio.wait_for(first_task, timeout=2)

    snapshot = store.load_session(session.session_id)
    host_message = next(
        message
        for message in snapshot.messages
        if message.metadata.get("session_input_source") == "host"
    )
    host_run = snapshot.runs[-1]

    assert host_message.role == "system"
    assert host_message.semantic_tag == "runtime-recovery"
    assert host_message.text_content == "Recover from the durable checkpoint."
    assert host_message.metadata["host_source_id"] == "ouro.runtime/recovery"
    assert provider.contexts[-1][-1].text_content == (
        "<runtime-recovery>\nRecover from the durable checkpoint.\n</runtime-recovery>"
    )
    assert not hasattr(provider.contexts[-1][-1], "message_id")
    assert host_run.origin == "host"
    assert host_run.trigger == "host"
    assert host_run.initiator_id == "ouro.runtime/recovery"
    assert host_run.parent_run_id == snapshot.runs[0].run_id
    assert "follow_up_queue" not in snapshot.session.metadata


@pytest.mark.asyncio
async def test_steering_waits_for_tool_results_before_entering_model_context(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ToolTurnGateProvider()
    tools = ToolRegistry()

    def echo(invocation):
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="echo-result",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Echo",
                description="Echo a result.",
                permission_mode="auto",
            ),
            handler=echo,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="tool-gate", provider_id=provider.provider_id),
        ),
        tools=tools,
    )

    first_task = asyncio.create_task(runtime.run("initial"))
    session = await _active_session(store)
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await runtime.run("steer", session_id=session.session_id, queue_mode="steer")
    provider.release_first.set()
    await asyncio.wait_for(first_task, timeout=2)

    assert [message.role for message in provider.contexts[1]] == [
        "user",
        "assistant",
        "tool_result",
        "user",
    ]
    assert provider.contexts[1][-1].text_content == "steer"


@pytest.mark.asyncio
async def test_steering_is_consumed_before_model_resume_after_approval(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ToolTurnGateProvider()
    provider.release_first.set()
    tools = ToolRegistry()

    def echo(invocation):
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="approved-result",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Echo",
                description="Echo an approved result.",
                permission_mode="approval",
            ),
            handler=echo,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="approval-gate", provider_id=provider.provider_id),
        ),
        tools=tools,
    )

    waiting = await runtime.run("initial")
    assert waiting.run.status == "waiting_approval"
    await runtime.run(
        "steer",
        session_id=waiting.session.session_id,
        queue_mode="steer",
    )
    completed = await runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approved=True,
    )

    assert completed.run.status == "completed"
    assert [message.role for message in provider.contexts[1]] == [
        "user",
        "assistant",
        "tool_result",
        "user",
    ]
    assert provider.contexts[1][-1].text_content == "steer"


@pytest.mark.asyncio
async def test_delivery_modes_keep_independent_boundaries_without_losing_queue_order(
    tmp_path,
) -> None:
    store, provider, runtime = _runtime(tmp_path, consumption_mode="one_by_one")

    first_task = asyncio.create_task(runtime.run("initial"))
    session = await _active_session(store)
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)
    await runtime.run("follow", session_id=session.session_id, queue_mode="follow_up")
    await runtime.run("steer", session_id=session.session_id, queue_mode="steer")
    provider.release_first.set()
    await asyncio.wait_for(first_task, timeout=2)

    snapshot = store.load_session(session.session_id)
    assert [_user_texts(context) for context in provider.contexts] == [
        ["initial"],
        ["initial", "steer"],
        ["initial", "steer", "follow"],
    ]
    assert [run.status for run in snapshot.runs] == ["completed", "completed"]
