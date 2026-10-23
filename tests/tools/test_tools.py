import asyncio
import threading

import pytest

from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.tools import (
    Tool,
    ToolBatchExecutor,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolInputRequest,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)
from ouro.tools.builtin.shell import detect_command_shell


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return registry


def _command_tool_name() -> str:
    shell = detect_command_shell()
    assert shell is not None
    return shell.tool_name


def test_scoped_registry_reads_one_fresh_controller_snapshot_per_listing() -> None:
    registry = ToolRegistry()
    for name, controller_id in (("plain", None), ("first", "a"), ("second", "a")):
        registry.register(
            Tool(
                definition=ToolDefinition(
                    name=name, description=name, active_when_controller_id=controller_id
                ),
                handler=lambda invocation, context: "done",
            )
        )
    calls = 0
    active = {"a"}

    def controllers():
        nonlocal calls
        calls += 1
        return set(active)

    scoped = registry.scoped(controllers)
    assert scoped.names() == ["first", "plain", "second"]
    assert calls == 1
    active.clear()
    assert [item.name for item in scoped.definitions()] == ["plain"]
    assert calls == 2
    with pytest.raises(KeyError, match="inactive"):
        scoped.get("first")
    active.add("a")
    assert scoped.get("first").definition.name == "first"
    plain = scoped.selected(["plain"])
    previous_calls = calls
    assert plain.names() == ["plain"]
    assert calls == previous_calls


def test_terminal_tool_result_without_start_has_zero_duration() -> None:
    ended_at = utc_now()

    result = ToolResult(
        tool_call_id="call_terminal",
        tool_name="GuardedTool",
        ended_at=ended_at,
    )

    assert result.started_at == ended_at
    assert result.ended_at == ended_at


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
    assert outcome.result.content == "1\thello"


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
    assert outcome.result.content == "1\tworkspace content"
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
async def test_workspace_policy_allows_host_artifact_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_root = tmp_path / "home" / "artifacts"
    target = artifact_root / "dev.example.plugin" / "plan.md"

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Write",
            arguments={"path": str(target), "content": "# Plan"},
        ),
        approved=True,
        context=ToolExecutionContext(
            workspace=workspace,
            artifact_root=artifact_root,
            workspace_only=True,
        ),
    )

    assert outcome.result is not None
    assert outcome.result.is_error is False
    assert target.read_text(encoding="utf-8") == "# Plan"


@pytest.mark.asyncio
async def test_workspace_policy_does_not_allow_other_user_data(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    target = home / "config" / "config.json"

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="Write",
            arguments={"path": str(target), "content": "{}"},
        ),
        approved=True,
        context=ToolExecutionContext(
            workspace=workspace,
            artifact_root=home / "artifacts",
            workspace_only=True,
        ),
    )

    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "ToolPolicyError"
    assert not target.exists()


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
            handler=lambda invocation, context: ToolResult(
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
async def test_command_shell_rejects_working_directory_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = await ToolExecutor(_registry()).execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name=_command_tool_name(),
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


@pytest.mark.asyncio
async def test_ask_user_returns_waiting_state() -> None:
    executor = ToolExecutor(_registry())
    arguments = {
        "questions": [
            {
                "id": "file",
                "header": "File",
                "question": "Which file?",
                "options": [
                    {
                        "label": "notes.md (Recommended)",
                        "description": "Use the existing notes file.",
                    },
                    {
                        "label": "README.md",
                        "description": "Use the project readme instead.",
                    },
                ],
            }
        ]
    }

    outcome = await executor.execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="AskUser",
            arguments=arguments,
        ),
        approval_mode="approval",
    )

    assert outcome.status == "waiting_user"
    assert outcome.input_request is not None
    assert outcome.input_request.data == {
        "type": "ask_user",
        "questions": [{**arguments["questions"][0], "multi_select": False}],
    }


@pytest.mark.asyncio
async def test_any_tool_can_request_structured_user_input() -> None:
    request = {
        "type": "cards",
        "items": [
            {"id": "fast", "title": "Fast plan", "tags": ["cheap", "reversible"]},
            {"id": "safe", "title": "Safe plan", "tags": ["reviewed"]},
        ],
        "multiple": True,
    }
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="ConfirmPlan", description="Confirm a plan."),
            handler=lambda _invocation, context: ToolInputRequest(data=request),
        )
    )

    outcome = await ToolExecutor(registry).execute(
        ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="ConfirmPlan")
    )

    assert outcome.status == "waiting_user"
    assert outcome.result is None
    assert outcome.input_request is not None
    assert outcome.input_request.data == request


@pytest.mark.asyncio
async def test_plugin_style_tool_receives_execution_context() -> None:
    registry = ToolRegistry()
    seen_contexts: list[ToolExecutionContext] = []

    async def handler(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
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

    async def failing_handler(invocation: ToolInvocation, context) -> ToolResult:
        raise RuntimeError("async handler failure token=tool-secret")

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
    assert "tool-secret" not in outcome.result.content
    assert outcome.result.structured.get("error_message") == ("async handler failure token=***")
    assert outcome.result.structured.get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_sync_tool_exception_returns_error_result() -> None:
    registry = ToolRegistry()

    def failing_handler(invocation: ToolInvocation, context) -> ToolResult:
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
async def test_tool_result_identity_mismatch_becomes_current_call_error() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="BrokenIdentity", description="Returns the wrong id."),
            handler=lambda _invocation, context: ToolResult(
                tool_call_id="different_call",
                tool_name="DifferentTool",
                content="invalid",
            ),
        )
    )
    invocation = ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_call_id="expected_call",
        tool_name="BrokenIdentity",
    )

    outcome = await ToolExecutor(registry).execute(invocation)

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.tool_call_id == "expected_call"
    assert outcome.result.tool_name == "BrokenIdentity"
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "ToolResultContractError"


@pytest.mark.asyncio
async def test_invalid_tool_return_becomes_model_readable_error() -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="InvalidReturn", description="Returns no result."),
            handler=lambda _invocation, context: None,
        )
    )
    invocation = ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_call_id="expected_call",
        tool_name="InvalidReturn",
    )

    outcome = await ToolExecutor(registry).execute(invocation)

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.tool_call_id == "expected_call"
    assert outcome.result.tool_name == "InvalidReturn"
    assert outcome.result.is_error is True
    assert outcome.result.structured["error_type"] == "ToolResultContractError"


@pytest.mark.asyncio
async def test_sync_tool_handler_does_not_block_the_event_loop() -> None:
    registry = ToolRegistry()
    entered = threading.Event()
    release = threading.Event()
    handler_threads: list[int] = []

    def blocking_handler(invocation: ToolInvocation, context) -> ToolResult:
        handler_threads.append(threading.get_ident())
        entered.set()
        release.wait()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="done",
        )

    registry.register(
        Tool(
            definition=ToolDefinition(name="Blocking", description="Blocks synchronously."),
            handler=blocking_handler,
        )
    )
    watchdog = threading.Timer(1, release.set)
    watchdog.start()
    event_loop_thread = threading.get_ident()
    execution = asyncio.create_task(
        ToolExecutor(registry).execute(
            ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="Blocking")
        )
    )

    try:
        handler_entered = await asyncio.to_thread(entered.wait, 2)
        event_loop_remained_responsive = not execution.done()
    finally:
        release.set()
        watchdog.cancel()
    outcome = await execution

    assert handler_entered is True
    assert event_loop_remained_responsive is True
    assert handler_threads != [event_loop_thread]
    assert outcome.result is not None
    assert outcome.result.content == "done"


@pytest.mark.asyncio
async def test_tool_execution_aborted_still_returns_aborted_outcome() -> None:
    from ouro.tools.base import ToolExecutionAborted

    registry = ToolRegistry()

    async def aborting_handler(invocation: ToolInvocation, context) -> ToolResult:
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

    async def handler(invocation: ToolInvocation, context) -> ToolResult:
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
async def test_executor_cancels_async_tool_without_handler_polling() -> None:
    registry = ToolRegistry()
    signal = AbortSignal()
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def handler(invocation: ToolInvocation, context) -> ToolResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        )

    registry.register(
        Tool(
            definition=ToolDefinition(name="AsyncWait", description="Waits asynchronously."),
            handler=handler,
        )
    )
    execution = asyncio.create_task(
        ToolExecutor(registry).execute(
            ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="AsyncWait"),
            signal=signal,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    signal.abort("stop async tool")
    outcome = await asyncio.wait_for(execution, timeout=1)

    assert finalized.is_set()
    assert outcome.status == "aborted"
    assert outcome.result is not None
    assert outcome.result.structured == {"aborted": True, "reason": "stop async tool"}


@pytest.mark.asyncio
async def test_executor_owns_sync_tool_until_cancel_outcome_is_known() -> None:
    registry = ToolRegistry()
    signal = AbortSignal()
    entered = threading.Event()
    release = threading.Event()

    def handler(invocation: ToolInvocation, context) -> ToolResult:
        entered.set()
        release.wait(timeout=2)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="finished after cancellation",
        )

    registry.register(
        Tool(
            definition=ToolDefinition(name="SyncWait", description="Waits synchronously."),
            handler=handler,
        )
    )
    execution = asyncio.create_task(
        ToolExecutor(registry).execute(
            ToolInvocation(session_id="ses_1", run_id="run_1", tool_name="SyncWait"),
            signal=signal,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)

    signal.abort("stop sync tool")
    await asyncio.sleep(0.05)
    assert execution.done() is False

    release.set()
    outcome = await asyncio.wait_for(execution, timeout=1)

    assert outcome.status == "unknown_outcome"
    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.result.structured == {
        "unknown_outcome": True,
        "reason": "stop sync tool",
    }


@pytest.mark.asyncio
async def test_executor_accepts_cooperative_cancellation_from_sync_tool() -> None:
    registry = ToolRegistry()
    signal = AbortSignal()
    entered = threading.Event()
    checkpoint = threading.Event()

    def handler(
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult:
        entered.set()
        while True:
            context.raise_if_aborted()
            checkpoint.wait(timeout=0.01)

    registry.register(
        Tool(
            definition=ToolDefinition(name="SyncCooperative", description="Checks context."),
            handler=handler,
        )
    )
    execution = asyncio.create_task(
        ToolExecutor(registry).execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_1",
                tool_name="SyncCooperative",
            ),
            signal=signal,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)

    signal.abort("stop cooperative tool")
    outcome = await asyncio.wait_for(execution, timeout=1)

    assert outcome.status == "aborted"
    assert outcome.result is not None
    assert outcome.result.structured == {
        "aborted": True,
        "reason": "stop cooperative tool",
    }


@pytest.mark.asyncio
async def test_parallel_batch_waits_for_siblings_when_checkpoint_fails() -> None:
    registry = ToolRegistry()
    slow_finished = asyncio.Event()

    async def fast(invocation: ToolInvocation, context) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        )

    async def slow(invocation: ToolInvocation, context) -> ToolResult:
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
        handler=lambda invocation, context: ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        ),
    )

    with pytest.raises(ValueError, match="invalid parameters_schema for tool Broken"):
        registry.register(tool)


def test_registry_replacement_is_atomic_when_new_contribution_conflicts() -> None:
    registry = ToolRegistry()

    def handler(invocation: ToolInvocation, context) -> ToolResult:
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
