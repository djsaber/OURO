import asyncio

import pytest

from ouro.application.runtime import RuntimeService
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


def _result(invocation, content: str, *, is_error: bool = False) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        is_error=is_error,
    )


def _runtime(tmp_path, *, registry, first_calls, mode="sequential") -> RuntimeService:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [ProviderStreamEvent(type="done", tool_calls=first_calls)],
                    [
                        ProviderStreamEvent(type="text_delta", text="batch complete"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
        tool_execution_config=ToolExecutionConfig(mode=mode, max_concurrency=2),
    )


@pytest.mark.asyncio
async def test_parallel_batch_preserves_one_assistant_message_and_source_order(tmp_path) -> None:
    registry = ToolRegistry()
    completed: list[str] = []

    async def first(invocation) -> ToolResult:
        await asyncio.sleep(0.05)
        completed.append("first")
        return _result(invocation, "first result")

    async def second(invocation) -> ToolResult:
        await asyncio.sleep(0.01)
        completed.append("second")
        return _result(invocation, "second result")

    registry.register(
        Tool(definition=ToolDefinition(name="First", description="first"), handler=first)
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Second", description="second"), handler=second)
    )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="First", source_index=0),
            ToolCallRequest(id="call_2", name="Second", source_index=1),
        ],
    )

    result = await runtime.run("run both")
    snapshot = runtime.store.load_session(result.session.session_id)

    assert completed == ["second", "first"]
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
        "tool_result",
        "assistant",
    ]
    assert [call.id for call in snapshot.messages[1].tool_calls] == ["call_1", "call_2"]
    assert [
        message.tool_result.tool_call_id
        for message in snapshot.messages[2:4]
        if message.tool_result is not None
    ] == ["call_1", "call_2"]


@pytest.mark.asyncio
async def test_batch_waits_for_every_approval_and_marks_running_before_execution(tmp_path) -> None:
    registry = ToolRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def protected(invocation) -> ToolResult:
        executed.append(invocation.tool_call_id)
        started.set()
        await release.wait()
        return _result(invocation, invocation.tool_call_id)

    for name in ("ProtectedOne", "ProtectedTwo"):
        registry.register(
            Tool(
                definition=ToolDefinition(
                    name=name,
                    description=name,
                    permission_mode="approval",
                ),
                handler=protected,
            )
        )
    registry.register(
        Tool(
            definition=ToolDefinition(name="Automatic", description="automatic"),
            handler=protected,
        )
    )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="ProtectedOne", source_index=0),
            ToolCallRequest(id="call_2", name="ProtectedTwo", source_index=1),
            ToolCallRequest(id="call_3", name="Automatic", source_index=2),
        ],
    )

    pending = await runtime.run("run protected tools")
    batch = pending.run.active_tool_batch
    assert batch is not None
    assert [call.request.id for call in batch.calls] == ["call_1", "call_2", "call_3"]
    approval_ids = [approval.approval_id for approval in batch.pending_approvals]

    still_waiting = await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approval_id=approval_ids[0],
        approved=True,
    )
    assert still_waiting.run.status == "waiting_approval"
    assert executed == []

    resume_task = asyncio.create_task(
        runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=approval_ids[1],
            approved=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    executing = runtime.store.load_session(pending.session.session_id, recover=False)
    executing_run = next(item for item in executing.runs if item.run_id == pending.run.run_id)
    assert executing.session.status == "running"
    assert executing_run.status == "running"
    assert executing_run.phase == "tool_execution"
    release.set()

    resumed = await asyncio.wait_for(resume_task, timeout=1)
    assert resumed.run.status == "completed"
    assert sorted(executed) == ["call_1", "call_2", "call_3"]


@pytest.mark.asyncio
async def test_tool_failure_does_not_cancel_sibling_call(tmp_path) -> None:
    registry = ToolRegistry()
    completed: list[str] = []

    async def broken(invocation) -> ToolResult:
        raise RuntimeError("broken tool")

    async def healthy(invocation) -> ToolResult:
        completed.append(invocation.tool_call_id)
        return _result(invocation, "healthy")

    registry.register(
        Tool(definition=ToolDefinition(name="Broken", description="broken"), handler=broken)
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Healthy", description="healthy"), handler=healthy)
    )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="Broken", source_index=0),
            ToolCallRequest(id="call_2", name="Healthy", source_index=1),
        ],
    )

    result = await runtime.run("run both")

    assert result.run.status == "completed"
    assert completed == ["call_2"]
    assert [tool_result.is_error for tool_result in result.tool_results] == [True, False]


@pytest.mark.asyncio
async def test_tool_definition_can_force_parallel_batch_to_run_sequentially(tmp_path) -> None:
    registry = ToolRegistry()
    order: list[str] = []

    async def first(invocation) -> ToolResult:
        await asyncio.sleep(0.02)
        order.append(invocation.tool_call_id)
        return _result(invocation, "first")

    async def second(invocation) -> ToolResult:
        order.append(invocation.tool_call_id)
        return _result(invocation, "second")

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="ForcedSequential",
                description="forced",
                execution_mode="sequential",
            ),
            handler=first,
        )
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Normal", description="normal"), handler=second)
    )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="ForcedSequential", source_index=0),
            ToolCallRequest(id="call_2", name="Normal", source_index=1),
        ],
    )

    await runtime.run("run both")

    assert order == ["call_1", "call_2"]
