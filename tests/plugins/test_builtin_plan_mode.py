from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.cli.interactive import run_interactive
from ouro.cli.interactive_commands import handle_command
from ouro.core.adapter import RuntimeAdapter
from ouro.core.interactions import AnswerResolution
from ouro.core.messages import ModelMessage
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
)
from ouro.core.tools import ToolCallRequest
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolInputRequest,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)

PLUGIN_ID = "dev.ouro.plan_mode"


class PlanBuildProvider:
    provider_id = "plan-build"

    def __init__(self) -> None:
        self.stream_count = 0
        self.plan_path: Path | None = None
        self.requests: list[list[ModelMessage]] = []
        self.tool_names: list[set[str]] = []

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt
        self.requests.append(list(messages))
        self.tool_names.append({_provider_tool_name(tool) for tool in tools or []})
        if self.stream_count == 0:
            call = ToolCallRequest(id="call_enter", name="EnterPlanMode", arguments={})
        elif self.stream_count == 1:
            scaffold = next(
                message.text_content
                for message in reversed(messages)
                if message.role == "system_synthetic" and "Plan file:" in message.text_content
            )
            match = re.search(r"Plan file: (.+)", scaffold)
            assert match is not None
            self.plan_path = Path(match.group(1))
            call = ToolCallRequest(
                id="call_write",
                name="Write",
                arguments={
                    "path": str(self.plan_path),
                    "content": "# Proposed plan\n\n1. Inspect.\n2. Implement.\n3. Verify.",
                },
            )
        elif self.stream_count == 2:
            assert self.plan_path is not None
            call = ToolCallRequest(
                id="call_edit",
                name="Edit",
                arguments={
                    "path": str(self.plan_path),
                    "old": "2. Implement.",
                    "new": "2. Implement safely.",
                },
            )
        elif self.stream_count == 3:
            call = ToolCallRequest(
                id="call_exit",
                name="ExitPlanMode",
                arguments={
                    "options": [
                        {
                            "label": "Direct implementation",
                            "description": "Apply the proposed steps directly.",
                        }
                    ]
                },
            )
        else:
            self.stream_count += 1
            yield ProviderStreamEvent(
                type="text_delta",
                text="Plan approved; ready to implement.",
            )
            yield ProviderStreamEvent(type="done")
            return
        self.stream_count += 1
        yield ProviderStreamEvent(type="tool_call", tool_call=call)
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class FinalProvider:
    # Durable Sessions retain their provider/model route across process restart.
    provider_id = "plan-build"

    def __init__(self) -> None:
        self.messages: list[ModelMessage] = []
        self.tool_names: set[str] = set()

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt
        self.messages = list(messages)
        self.tool_names = {_provider_tool_name(tool) for tool in tools or []}
        yield ProviderStreamEvent(type="text_delta", text="Plan approved; ready to implement.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class PlanToolAttemptProvider:
    provider_id = "plan-tool-attempt"

    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self.stream_count = 0
        self.tool_name = tool_name
        self.arguments = arguments

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        if self.stream_count == 0:
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="call_enter",
                    name="EnterPlanMode",
                    arguments={},
                ),
            )
        elif self.stream_count == 1:
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="call_attempt",
                    name=self.tool_name,
                    arguments=self.arguments,
                ),
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="Tool attempt handled.")
        self.stream_count += 1
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class PlainTextProvider:
    provider_id = "plan-command"

    def __init__(self) -> None:
        self.stream_count = 0
        self.messages: list[ModelMessage] = []

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, tools
        self.stream_count += 1
        self.messages = list(messages)
        yield ProviderStreamEvent(type="text_delta", text="Planning can continue later.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


def _provider_tool_name(tool: object) -> str:
    if isinstance(tool, dict):
        return str(tool["name"])
    return str(tool.name)


def _runtime(home: Path, workspace: Path, provider) -> tuple[RuntimeService, LocalSessionStore]:
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="plan-test", provider_id=provider.provider_id),
        ),
        tools=tools,
        default_workspace=workspace,
        environment=RuntimeEnvironment.local(home, store.sessions_dir),
    )
    return runtime, store


async def _enable_plan_mode(application: LocalApplication) -> None:
    assert application.plugins is not None
    await application.start()
    assert (await application.plugins.enable(PLUGIN_ID)).status == "active"


@pytest.mark.asyncio
async def test_plan_mode_builds_with_shared_write_persists_scaffolds_and_resumes_after_restart(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_provider = PlanBuildProvider()
    first_runtime, first_store = _runtime(home, workspace, first_provider)
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    await _enable_plan_mode(first_application)

    waiting = await first_runtime.run("Design the implementation", approval_mode="auto")

    assert waiting.run.status == "waiting_user"
    assert first_provider.plan_path is not None
    expected_plan_directory = (
        home / "artifacts" / PLUGIN_ID / "sessions" / waiting.session.session_id / "plans"
    )
    assert first_provider.plan_path.parent == expected_plan_directory
    assert not first_provider.plan_path.is_relative_to(workspace)
    assert not (workspace / ".ouro").exists()
    assert first_provider.plan_path.read_text(encoding="utf-8").startswith("# Proposed plan")
    waiting_snapshot = first_store.load_runtime_session(waiting.session.session_id)
    scaffolds = [
        message
        for message in waiting_snapshot.messages
        if message.metadata.get("kind") == "context_provider"
        and message.metadata.get("plugin_id") == PLUGIN_ID
    ]
    assert len(scaffolds) == 3
    assert all(message.role == "system_synthetic" for message in scaffolds)
    assert all(str(first_provider.plan_path) in message.text_content for message in scaffolds)
    assert "EnterPlanMode" in first_provider.tool_names[0]
    assert "ExitPlanMode" not in first_provider.tool_names[0]
    assert "ExitPlanMode" in first_provider.tool_names[1]
    waiting_call = next(
        call for call in waiting.run.active_tool_batch.calls if call.request.id == "call_exit"
    )
    assert waiting_call.input_request is not None
    assert waiting_call.input_request.data["type"] == "plan_confirmation"
    assert waiting_call.input_request.data["plan"].startswith("# Proposed plan")
    await first_application.close()

    final_provider = FinalProvider()
    second_runtime, second_store = _runtime(home, workspace, final_provider)
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()

    completed = await second_runtime.answer_user(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        answer={"action": "accept", "option": "Direct implementation"},
    )

    assert completed.run.status == "completed"
    assert completed.final_text == "Plan approved; ready to implement."
    assert "ExitPlanMode" not in final_provider.tool_names
    completed_snapshot = second_store.load_runtime_session(completed.session.session_id)
    exit_result = next(
        message
        for message in completed_snapshot.messages
        if message.tool_result is not None and message.tool_result.tool_call_id == "call_exit"
    )
    assert "Exited plan mode" in exit_result.text_content
    assert "Selected option: Direct implementation" in exit_result.text_content
    assert (
        len(
            [
                message
                for message in completed_snapshot.messages
                if message.metadata.get("kind") == "context_provider"
                and message.metadata.get("plugin_id") == PLUGIN_ID
            ]
        )
        == 3
    )
    assert not any(
        message.metadata.get("kind") == "context_provider"
        and message.run_id == completed.run.run_id
        and message.sequence > exit_result.sequence
        for message in completed_snapshot.messages
    )
    state_path = (
        home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{completed.session.session_id}.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["state"]["phase"] == "inactive"
    await second_application.close()


@pytest.mark.asyncio
async def test_plan_mode_exit_uses_unattended_resolution_in_afk_mode(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlanBuildProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)

    completed = await runtime.run(
        "Design the implementation",
        approval_mode="auto",
        interaction_mode="unattended",
    )

    assert completed.run.status == "completed"
    assert completed.session.interaction_mode == "unattended"
    assert all(
        any("AFK mode is active" in message.text_content for message in request)
        for request in provider.requests
    )
    exit_result = next(
        result for result in completed.tool_results if result.tool_call_id == "call_exit"
    )
    assert exit_result.structured["phase"] == "inactive"
    assert exit_result.structured["decision"] == "proceed"
    assert exit_result.structured["resolved_by"] == "host_policy"
    assert exit_result.structured["resolver_id"] == "ouro.afk"
    assert exit_result.structured["selected_option"] == "Direct implementation"
    assert "User approval was not requested" in exit_result.content
    event_snapshot = store.load_session(completed.session.session_id)
    resolved_event = next(
        event for event in event_snapshot.events if event.event_type == "ask_user_resolved"
    )
    assert resolved_event.payload["resolution"]["kind"] == "unattended"
    state_path = (
        home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{completed.session.session_id}.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["state"]["phase"] == "inactive"
    await application.close()


@pytest.mark.asyncio
async def test_interactive_cli_collects_builtin_plan_approval_as_structured_answer(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlanBuildProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    inputs = iter(["Design the implementation", "accept", "/exit"])
    prompts: list[str] = []
    outputs: list[str] = []

    def read_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(inputs)

    code = await run_interactive(
        runtime=runtime,
        approval_mode="auto",
        input_func=read_input,
        output_func=outputs.append,
    )

    assert code == 0
    assert "Plan decision [accept/revise/reject]: " in prompts
    assert not any(prompt.startswith("Select plan option") for prompt in prompts)
    assert any(output.startswith("Plan approval requested.") for output in outputs)
    assert not any(output.startswith("Question: {") for output in outputs)
    assert "Plan approved; ready to implement." in outputs
    session = store.list_sessions()[0]
    snapshot = store.load_runtime_session(session.session_id)
    exit_result = next(
        message
        for message in snapshot.messages
        if message.tool_result is not None and message.tool_result.tool_call_id == "call_exit"
    )
    assert "Selected option: Direct implementation" in exit_result.text_content
    state_path = home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["state"]["phase"] == "inactive"
    await application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
async def test_plan_mode_denies_builtin_writer_outside_current_plan_and_continues(
    tmp_path,
    tool_name: str,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    wrong_path = workspace / "implementation.py"
    if tool_name == "Edit":
        wrong_path.write_text("old content", encoding="utf-8")
    arguments: dict[str, object] = (
        {"path": str(wrong_path), "content": "must not be written"}
        if tool_name == "Write"
        else {"path": str(wrong_path), "old": "old content", "new": "must not be edited"}
    )
    provider = PlanToolAttemptProvider(
        tool_name,
        arguments,
    )
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)

    result = await runtime.run("Plan without changing implementation", approval_mode="auto")

    assert result.run.status == "completed"
    assert result.final_text == "Tool attempt handled."
    if tool_name == "Write":
        assert not wrong_path.exists()
    else:
        assert wrong_path.read_text(encoding="utf-8") == "old content"
    denied = next(
        tool_result for tool_result in result.tool_results if tool_result.tool_name == tool_name
    )
    assert denied.is_error is True
    assert denied.structured["error_type"] == "ToolPermissionDenied"
    assert "does not exclusively target the current plan file" in denied.content
    events = store.load_session(result.session.session_id).events
    policy_event = next(
        event
        for event in events
        if event.event_type == "tool_policy_evaluated"
        and event.payload.get("tool_call_id") == "call_attempt"
    )
    assert policy_event.payload["mode"] == "deny"
    await application.close()


@pytest.mark.asyncio
async def test_plan_command_only_activates_mode_until_next_user_message(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlainTextProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    session = runtime.create_session(title="Plan command")
    assert application.plugins is not None

    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "",
        session_id=session.session_id,
        workspace=str(workspace),
    )

    assert command.session_inputs == []
    before_run = store.load_runtime_session(session.session_id)
    assert before_run.messages == []
    assert before_run.runs == []
    result = await runtime.run(
        "Refactor the parser",
        session_id=session.session_id,
        approval_mode="auto",
    )
    assert result.run.status == "completed"
    assert result.final_text == "Planning can continue later."
    assert provider.stream_count == 1
    snapshot = store.load_runtime_session(session.session_id)
    reminder = next(
        message
        for message in snapshot.messages
        if message.metadata.get("kind") == "context_provider"
    )
    assert reminder.metadata["plugin_id"] == PLUGIN_ID
    assert "Plan mode is active" in reminder.text_content
    assert not any(
        message.metadata.get("session_input_source") in {"plugin", "turn_policy"}
        for message in snapshot.messages
    )
    assert application.plugin_runtime is not None
    assert application.plugin_runtime.snapshot().turn_policies.registered() == []
    await application.close()


@pytest.mark.asyncio
async def test_plan_off_cancels_activation_bound_pending_approval(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlanBuildProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    waiting = await runtime.run("Design the implementation", approval_mode="auto")
    assert waiting.run.status == "waiting_user"
    assert application.plugins is not None

    released = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "off",
        session_id=waiting.session.session_id,
        workspace=str(workspace),
    )

    snapshot = store.load_runtime_session(waiting.session.session_id)
    cancelled = next(run for run in snapshot.runs if run.run_id == waiting.run.run_id)
    assert cancelled.status == "aborted"
    assert cancelled.active_tool_batch is not None
    pending = next(
        call
        for call in cancelled.active_tool_batch.calls
        if call.request.id == "call_exit"
    )
    assert pending.status == "aborted"
    assert pending.result is not None
    assert pending.result.structured["error_type"] == "SessionControlReleased"
    assert snapshot.session.status == "idle"
    assert snapshot.session.active_run_id is None
    assert released.data["session_controls"][0]["data"]["cancelled_run_id"] == cancelled.run_id
    with pytest.raises(ValueError, match="not waiting for user input"):
        await runtime.answer_user(
            session_id=waiting.session.session_id,
            run_id=waiting.run.run_id,
            answer={"action": "accept"},
        )
    await application.close()


@pytest.mark.asyncio
async def test_plan_off_releases_controller_without_creating_run_or_deleting_artifact(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = FinalProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    session = runtime.create_session(title="Plan off")
    assert application.plugins is not None

    activated = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "",
        session_id=session.session_id,
        workspace=str(workspace),
    )

    active_snapshot = store.load_runtime_session(session.session_id)
    assert len(active_snapshot.session.controls.activations) == 1
    activation = active_snapshot.session.controls.activations[0]
    assert activation.controller_id == f"{PLUGIN_ID}/session_controllers/mode"
    assert activation.phase == "active"
    control_result = activated.data["session_controls"][0]
    plan_path = Path(str(control_result["data"]["plan_path"]))
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Draft plan\n", encoding="utf-8")

    deactivated = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "off",
        session_id=session.session_id,
        workspace=str(workspace),
    )
    replayed = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "off",
        session_id=session.session_id,
        workspace=str(workspace),
    )

    completed = store.load_runtime_session(session.session_id)
    assert completed.session.controls.activations == []
    assert completed.messages == []
    assert completed.runs == []
    assert plan_path.read_text(encoding="utf-8") == "# Draft plan\n"
    assert "Plan mode deactivated" in deactivated.output
    assert replayed.data["session_controls"][0]["changed"] is False
    state_path = home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["state"]["phase"] == "inactive"

    result = await runtime.run(
        "Implement the parser change",
        session_id=session.session_id,
        approval_mode="auto",
    )
    assert result.run.status == "completed"
    assert not any(
        "Plan mode is active" in message.text_content for message in provider.messages
    )
    await application.close()


@pytest.mark.asyncio
async def test_host_control_command_lists_and_releases_plan_without_business_rules(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime, store = _runtime(home, workspace, FinalProvider())
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    session = runtime.create_session(title="Host control")
    assert application.plugins is not None
    await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "",
        session_id=session.session_id,
        workspace=str(workspace),
    )

    status = await handle_command(
        "/control status",
        runtime,
        session.session_id,
        plugins=application.plugins,
    )
    assert f"{PLUGIN_ID}/session_controllers/mode" in status.output
    activation_id = store.load_session_record(
        session.session_id
    ).controls.activations[0].activation_id

    released = await handle_command(
        f"/control release {activation_id}",
        runtime,
        session.session_id,
        plugins=application.plugins,
    )

    assert "Plan mode deactivated" in released.output
    assert store.load_session_record(session.session_id).controls.activations == []
    await application.close()


@pytest.mark.asyncio
async def test_disabling_plugin_revokes_its_session_controller_gate(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime, store = _runtime(home, workspace, FinalProvider())
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    session = runtime.create_session(title="Disable controller owner")
    assert application.plugins is not None
    await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "",
        session_id=session.session_id,
        workspace=str(workspace),
    )

    disabled = await application.plugins.disable(PLUGIN_ID)

    assert disabled.status == "disabled"
    assert store.load_session_record(session.session_id).controls.activations == []
    state_path = home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["state"]["phase"] == "inactive"
    await application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["state_changing", "unknown"])
async def test_plan_mode_denies_other_state_changing_tools_without_running_them(
    tmp_path,
    effect: ToolEffect,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlanToolAttemptProvider("ExternalMutation", {})
    runtime, _ = _runtime(home, workspace, provider)
    invoked: list[str] = []

    def mutate(invocation: ToolInvocation) -> ToolResult:
        invoked.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="mutated",
        )

    runtime.tools.register(
        Tool(
            definition=ToolDefinition(
                name="ExternalMutation",
                description="Mutate an external system.",
                permission_mode="auto",
                effect=effect,
            ),
            handler=mutate,
        )
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)

    completed = await runtime.run("Try another mutation", approval_mode="auto")

    assert completed.run.status == "completed"
    assert completed.final_text == "Tool attempt handled."
    assert invoked == []
    denied = next(
        result for result in completed.tool_results if result.tool_name == "ExternalMutation"
    )
    assert denied.is_error is True
    assert denied.structured["error_type"] == "ToolPermissionDenied"
    assert "Only OURO's builtin Write or Edit" in denied.content
    await application.close()


@pytest.mark.asyncio
async def test_plan_mode_allows_any_declared_read_only_tool(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlanToolAttemptProvider("ExternalRead", {})
    runtime, _ = _runtime(home, workspace, provider)
    invoked: list[str] = []

    def read(invocation: ToolInvocation) -> ToolResult:
        invoked.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="read result",
        )

    runtime.tools.register(
        Tool(
            definition=ToolDefinition(
                name="ExternalRead",
                description="Read from an external source.",
                permission_mode="auto",
                effect="read_only",
            ),
            handler=read,
        )
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)

    completed = await runtime.run("Read external information", approval_mode="auto")

    assert completed.run.status == "completed"
    assert invoked == ["call_attempt"]
    read_result = next(
        result for result in completed.tool_results if result.tool_name == "ExternalRead"
    )
    assert read_result.is_error is False
    assert read_result.content == "read result"
    await application.close()


@pytest.mark.asyncio
async def test_plan_mode_tools_cover_failures_revisions_and_idempotent_transitions(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime, _ = _runtime(home, workspace, FinalProvider())
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    assert application.plugin_runtime is not None
    catalog = application.plugin_runtime.snapshot()
    enter = catalog.tools.get("EnterPlanMode")
    exit_plan = catalog.tools.get("ExitPlanMode")
    controller = catalog.session_controllers.get(
        "dev.ouro.plan_mode/session_controllers/mode"
    )
    context = ToolExecutionContext(workspace=workspace, workspace_only=True)
    session_id = "ses_plan_contract"
    run_id = "run_plan_contract"
    enter_invocation = ToolInvocation(
        tool_call_id="call_enter",
        session_id=session_id,
        run_id=run_id,
        tool_name="EnterPlanMode",
    )

    entered = await enter.execute(enter_invocation, context=context)
    replayed_enter = await enter.execute(enter_invocation, context=context)

    assert isinstance(entered, ToolResult)
    assert isinstance(replayed_enter, ToolResult)
    assert replayed_enter.is_error is False
    assert replayed_enter.session_control_transitions == entered.session_control_transitions
    activated = await controller.activate(
        session_id=session_id,
        activation_id="activation_plan_1",
        payload={"activation_tool_call_id": "call_enter"},
        workspace=str(workspace),
    )
    plan_path = Path(str(activated.data["plan_path"]))
    missing_exit = ToolInvocation(
        tool_call_id="call_missing",
        session_id=session_id,
        run_id=run_id,
        tool_name="ExitPlanMode",
    )
    missing = await exit_plan.execute(missing_exit, context=context)
    assert isinstance(missing, ToolResult)
    assert missing.is_error is True
    assert "No plan file found" in missing.content

    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("\n", encoding="utf-8")
    empty_exit = missing_exit.model_copy(update={"tool_call_id": "call_empty"})
    empty = await exit_plan.execute(empty_exit, context=context)
    assert isinstance(empty, ToolResult)
    assert empty.is_error is True
    assert "Plan file is empty" in empty.content

    plan_path.write_text("# Plan\n\nImplement and verify.", encoding="utf-8")
    first_exit = ToolInvocation(
        tool_call_id="call_exit_1",
        session_id=session_id,
        run_id=run_id,
        tool_name="ExitPlanMode",
        arguments={
            "options": [
                {"label": "Safe", "description": "Preserve compatibility."},
            ]
        },
    )
    first_request = await exit_plan.execute(first_exit, context=context)
    replayed_request = await exit_plan.execute(first_exit, context=context)
    assert isinstance(first_request, ToolInputRequest)
    assert isinstance(replayed_request, ToolInputRequest)
    assert replayed_request.data == first_request.data

    invalid = await exit_plan.resume(
        first_exit,
        AnswerResolution(value={"action": "accept"}),
        request=first_request,
        context=context,
    )
    assert isinstance(invalid, ToolInputRequest)
    assert invalid.data["error"] == "Select one of the proposed plan options before accepting."
    rejected = await exit_plan.resume(
        first_exit,
        AnswerResolution(value={"action": "reject"}),
        request=invalid,
        context=context,
    )
    replayed_rejection = await exit_plan.resume(
        first_exit,
        AnswerResolution(value={"action": "reject"}),
        request=invalid,
        context=context,
    )
    assert isinstance(rejected, ToolResult)
    assert isinstance(replayed_rejection, ToolResult)
    assert rejected.is_error is True
    assert replayed_rejection.content == rejected.content

    second_exit = first_exit.model_copy(update={"tool_call_id": "call_exit_2", "arguments": {}})
    second_request = await exit_plan.execute(second_exit, context=context)
    assert isinstance(second_request, ToolInputRequest)
    revised = await exit_plan.resume(
        second_exit,
        AnswerResolution(value={"action": "revise", "feedback": "Add rollback steps."}),
        request=second_request,
        context=context,
    )
    assert isinstance(revised, ToolResult)
    assert revised.is_error is True
    assert "Add rollback steps" in revised.content

    third_exit = first_exit.model_copy(update={"tool_call_id": "call_exit_3", "arguments": {}})
    third_request = await exit_plan.execute(third_exit, context=context)
    assert isinstance(third_request, ToolInputRequest)
    accepted = await exit_plan.resume(
        third_exit,
        AnswerResolution(value={"action": "accept"}),
        request=third_request,
        context=context,
    )
    replayed_acceptance = await exit_plan.resume(
        third_exit,
        AnswerResolution(value={"action": "accept"}),
        request=third_request,
        context=context,
    )
    assert isinstance(accepted, ToolResult)
    assert isinstance(replayed_acceptance, ToolResult)
    assert accepted.is_error is False
    assert replayed_acceptance.content == accepted.content
    assert accepted.session_control_transitions[0].action == "release"
    await controller.deactivate(
        session_id=session_id,
        activation_id="activation_plan_1",
        reason="test_accepted",
        workspace=str(workspace),
    )

    next_enter = enter_invocation.model_copy(update={"tool_call_id": "call_enter_2"})
    next_activation = await enter.execute(next_enter, context=context)
    assert isinstance(next_activation, ToolResult)
    activated_again = await controller.activate(
        session_id=session_id,
        activation_id="activation_plan_2",
        payload={"activation_tool_call_id": "call_enter_2"},
        workspace=str(workspace),
    )
    assert activated_again.data["plan_path"] != str(plan_path)
    catalog.close()
    await application.close()
