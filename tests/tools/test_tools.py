import pytest

from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return registry


@pytest.mark.asyncio
async def test_read_runs_automatically(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    executor = ToolExecutor(_registry())

    outcome = await executor.execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Read",
            arguments={"path": str(target)},
        )
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.content == "hello"


@pytest.mark.asyncio
async def test_write_requires_approval_then_runs(tmp_path) -> None:
    target = tmp_path / "created.txt"
    executor = ToolExecutor(_registry())
    invocation = ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_name="Write",
        arguments={"path": str(target), "content": "saved"},
    )

    pending = await executor.execute(invocation)

    assert pending.status == "approval_required"
    assert pending.approval is not None
    assert not target.exists()

    completed = await executor.execute(invocation, approved=True)

    assert completed.status == "completed"
    assert target.read_text(encoding="utf-8") == "saved"


def test_rejected_result_is_tool_error() -> None:
    executor = ToolExecutor(_registry())
    invocation = ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Bash")

    result = executor.rejected_result(invocation, "nope")

    assert result.is_error is True
    assert result.structured["rejected"] is True
    assert result.content == "nope"


@pytest.mark.asyncio
async def test_ask_user_returns_waiting_state() -> None:
    executor = ToolExecutor(_registry())

    outcome = await executor.execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="AskUser",
            arguments={"question": "Which file?"},
        )
    )

    assert outcome.status == "waiting_user"
    assert outcome.question == "Which file?"


@pytest.mark.asyncio
async def test_plugin_style_tool_receives_execution_context() -> None:
    registry = ToolRegistry()
    seen_contexts: list[ToolExecutionContext] = []

    async def handler(
        invocation: ToolInvocation,
        *,
        context: ToolExecutionContext,
    ) -> ToolResult:
        seen_contexts.append(context)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="ok",
        )

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="PluginEcho",
                description="Plugin-style echo.",
                source="third_party_plugin",
            ),
            handler=handler,
        )
    )
    executor = ToolExecutor(registry)

    outcome = await executor.execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="PluginEcho")
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.content == "ok"
    assert len(seen_contexts) == 1
