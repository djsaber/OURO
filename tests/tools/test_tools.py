import asyncio

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.tools import (
    Tool,
    ToolBatchExecutor,
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
async def test_relative_file_path_resolves_against_workspace(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("workspace content", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Read",
            arguments={"path": "note.txt"},
        ),
        context=ToolExecutionContext(workspace=tmp_path, workspace_only=True),
    )

    assert outcome.result is not None
    assert outcome.result.content == "workspace content"
    assert outcome.result.structured["path"] == str(target.resolve())


@pytest.mark.asyncio
async def test_workspace_policy_rejects_parent_traversal(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Read",
            arguments={"path": "../outside.txt"},
        ),
        context=ToolExecutionContext(workspace=workspace, workspace_only=True),
    )

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "ToolPolicyError"
    assert "outside the configured workspace" in outcome.result.content


@pytest.mark.asyncio
async def test_workspace_policy_rejects_symlink_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available on this platform")

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Read",
            arguments={"path": "link.txt"},
        ),
        context=ToolExecutionContext(workspace=workspace, workspace_only=True),
    )

    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolPolicyError"


@pytest.mark.asyncio
async def test_tool_output_is_truncated_at_executor_boundary() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="Verbose", description="verbose"),
            handler=lambda invocation: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="x" * 200,
            ),
        )
    )

    outcome = await ToolExecutor(registry).execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Verbose"),
        context=ToolExecutionContext(max_output_chars=80),
    )

    assert outcome.result is not None
    assert len(outcome.result.content) == 80
    assert "tool output truncated" in outcome.result.content
    assert outcome.result.metadata["output_truncated"] is True


@pytest.mark.asyncio
async def test_bash_rejects_working_directory_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Bash",
            arguments={"command": "echo unreachable", "cwd": str(tmp_path)},
        ),
        approved=True,
        context=ToolExecutionContext(workspace=workspace, workspace_only=True),
    )

    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolPolicyError"


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


@pytest.mark.asyncio
async def test_async_tool_exception_returns_error_result() -> None:
    registry = ToolRegistry()

    async def failing_handler(invocation: ToolInvocation) -> ToolResult:
        raise RuntimeError("async handler failure")

    registry.register(
        Tool(
            definition=ToolDefinition(name="AsyncFailing", description="Fails asynchronously."),
            handler=failing_handler,
        )
    )
    executor = ToolExecutor(registry)

    outcome = await executor.execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="AsyncFailing")
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert "async handler failure" in outcome.result.content
    assert outcome.result.structured.get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_sync_tool_exception_returns_error_result() -> None:
    registry = ToolRegistry()

    def failing_handler(invocation: ToolInvocation) -> ToolResult:
        raise ValueError("sync handler failure")

    registry.register(
        Tool(
            definition=ToolDefinition(name="SyncFailing", description="Fails synchronously."),
            handler=failing_handler,
        )
    )
    executor = ToolExecutor(registry)

    outcome = await executor.execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="SyncFailing")
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert "sync handler failure" in outcome.result.content
    assert outcome.result.structured.get("error_type") == "ValueError"


@pytest.mark.asyncio
async def test_tool_execution_aborted_still_returns_aborted_outcome() -> None:
    from ouro.tools.base import ToolExecutionAborted

    registry = ToolRegistry()

    async def aborting_handler(invocation: ToolInvocation) -> ToolResult:
        raise ToolExecutionAborted("cancelled by signal")

    registry.register(
        Tool(
            definition=ToolDefinition(name="Aborting", description="Aborts during execution."),
            handler=aborting_handler,
        )
    )
    executor = ToolExecutor(registry)

    outcome = await executor.execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Aborting")
    )

    assert outcome.status == "aborted"
    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert "cancelled by signal" in outcome.result.content


@pytest.mark.asyncio
async def test_aborted_signal_prevents_tool_handler_from_starting() -> None:
    registry = ToolRegistry()
    started = False

    async def handler(invocation: ToolInvocation) -> ToolResult:
        nonlocal started
        started = True
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        )

    registry.register(
        Tool(
            definition=ToolDefinition(name="SideEffect", description="side effect"),
            handler=handler,
        )
    )
    signal = AbortSignal()
    signal.abort("stopped before execution")

    outcome = await ToolExecutor(registry).execute_prepared(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="SideEffect"),
        signal=signal,
    )

    assert outcome.status == "aborted"
    assert started is False


@pytest.mark.asyncio
async def test_parallel_batch_waits_for_siblings_when_checkpoint_fails() -> None:
    registry = ToolRegistry()
    slow_finished = asyncio.Event()

    async def fast(invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        )

    async def slow(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(0.02)
        slow_finished.set()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        )

    registry.register(
        Tool(definition=ToolDefinition(name="Fast", description="fast"), handler=fast)
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Slow", description="slow"), handler=slow)
    )
    batch = ToolBatchExecutor(ToolExecutor(registry))

    async def broken_checkpoint(outcome) -> None:
        if outcome.invocation.tool_name == "Fast":
            raise RuntimeError("checkpoint failed")

    with pytest.raises(RuntimeError, match="checkpoint failed"):
        await batch.execute(
            [
                ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Fast"),
                ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Slow"),
            ],
            mode="parallel",
            max_concurrency=2,
            on_outcome=broken_checkpoint,
        )

    assert slow_finished.is_set()


@pytest.mark.asyncio
async def test_unknown_tool_returns_model_readable_error() -> None:
    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Missing")
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "UnknownTool"
    assert "unknown tool: Missing" in outcome.result.content


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_rejected_before_approval() -> None:
    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Write",
            arguments={"path": "note.txt"},
        )
    )

    assert outcome.status == "completed"
    assert outcome.approval is None
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolArgumentValidationError"
    assert "'content' is a required property" in outcome.result.content


def test_registry_rejects_invalid_tool_schema() -> None:
    registry = ToolRegistry()
    tool = Tool(
        definition=ToolDefinition(
            name="Broken",
            description="Invalid schema.",
            parameters_schema={"type": "not-a-json-schema-type"},
        ),
        handler=lambda invocation: ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        ),
    )

    with pytest.raises(ValueError, match="invalid parameters_schema for tool Broken"):
        registry.register(tool)


def test_registry_replacement_is_atomic_when_new_contribution_conflicts() -> None:
    registry = ToolRegistry()

    def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        )

    registry.register(
        Tool(definition=ToolDefinition(name="owned", description="Owned"), handler=handler)
    )
    registry.register(
        Tool(definition=ToolDefinition(name="other", description="Other"), handler=handler)
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.replace(
            ("owned",),
            (
                Tool(
                    definition=ToolDefinition(name="other", description="Conflict"),
                    handler=handler,
                ),
            ),
        )

    assert registry.names() == ["other", "owned"]
