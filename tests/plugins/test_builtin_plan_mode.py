from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
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

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, tools
        self.requests.append(list(messages))
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
        else:
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

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, tools
        self.messages = list(messages)
        yield ProviderStreamEvent(type="text_delta", text="Plan approved; ready to implement.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class WrongWriteProvider:
    provider_id = "plan-wrong-write"

    def __init__(self, wrong_path: Path) -> None:
        self.stream_count = 0
        self.wrong_path = wrong_path

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        call = (
            ToolCallRequest(id="call_enter", name="EnterPlanMode", arguments={})
            if self.stream_count == 0
            else ToolCallRequest(
                id="call_wrong_write",
                name="Write",
                arguments={"path": str(self.wrong_path), "content": "must not be written"},
            )
        )
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


class RejectedWriteThenTextProvider:
    provider_id = "plan-rejected-write"

    def __init__(self, target: Path) -> None:
        self.stream_count = 0
        self.target = target

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
            call = ToolCallRequest(
                id="call_rejected_write",
                name="Write",
                arguments={"path": str(self.target), "content": "must not be written"},
            )
            yield ProviderStreamEvent(type="tool_call", tool_call=call)
        else:
            yield ProviderStreamEvent(type="text_delta", text="Okay, ending this response.")
        self.stream_count += 1
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


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
    assert first_provider.plan_path.is_relative_to(workspace)
    assert first_provider.plan_path.read_text(encoding="utf-8").startswith("# Proposed plan")
    waiting_snapshot = first_store.load_runtime_session(waiting.session.session_id)
    scaffolds = [
        message
        for message in waiting_snapshot.messages
        if message.metadata.get("kind") == "context_provider"
        and message.metadata.get("plugin_id") == PLUGIN_ID
    ]
    assert len(scaffolds) == 2
    assert all(message.role == "system_synthetic" for message in scaffolds)
    assert all(str(first_provider.plan_path) in message.text_content for message in scaffolds)
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
        == 2
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
async def test_plan_mode_requires_approval_for_write_outside_current_plan(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    wrong_path = workspace / "implementation.py"
    provider = WrongWriteProvider(wrong_path)
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)

    result = await runtime.run("Plan without changing implementation", approval_mode="auto")

    assert result.run.status == "waiting_approval"
    assert not wrong_path.exists()
    call = next(
        item for item in result.run.active_tool_batch.calls if item.request.id == "call_wrong_write"
    )
    assert call.invocation is not None
    assert call.invocation.permission_mode == "approval"
    assert call.invocation.tool_policy_ids == ["dev.ouro.plan_mode/tool_policies/plan-tool-gate"]
    events = store.load_session(result.session.session_id).events
    policy_event = next(
        event
        for event in events
        if event.event_type == "tool_policy_evaluated"
        and event.payload.get("tool_call_id") == "call_wrong_write"
    )
    assert policy_event.payload["mode"] == "approval"
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
async def test_plan_mode_allows_run_to_finish_after_user_rejects_tool(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "implementation.py"
    provider = RejectedWriteThenTextProvider(target)
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_plan_mode(application)
    session = runtime.create_session(title="Rejected plan tool")
    assert application.plugins is not None
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "",
        session_id=session.session_id,
        workspace=str(workspace),
    )
    assert command.session_inputs == []

    waiting = await runtime.run(
        "Change the implementation",
        session_id=session.session_id,
        approval_mode="auto",
    )

    assert waiting.run.status == "waiting_approval"
    completed = await runtime.approve_tool(
        session_id=session.session_id,
        run_id=waiting.run.run_id,
        approved=False,
        reason="结束对话",
    )

    assert completed.run.status == "completed"
    assert completed.final_text == "Okay, ending this response."
    assert provider.stream_count == 2
    assert not target.exists()
    snapshot = store.load_runtime_session(session.session_id)
    assert not any(
        message.metadata.get("session_input_source") == "turn_policy"
        for message in snapshot.messages
    )
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
    assert replayed_enter.structured["plan_path"] == entered.structured["plan_path"]
    plan_path = Path(str(entered.structured["plan_path"]))
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
        {"action": "accept"},
        request=first_request,
        context=context,
    )
    assert isinstance(invalid, ToolInputRequest)
    assert invalid.data["error"] == "Select one of the proposed plan options before accepting."
    rejected = await exit_plan.resume(
        first_exit,
        {"action": "reject"},
        request=invalid,
        context=context,
    )
    replayed_rejection = await exit_plan.resume(
        first_exit,
        {"action": "reject"},
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
        {"action": "revise", "feedback": "Add rollback steps."},
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
        {"action": "accept"},
        request=third_request,
        context=context,
    )
    replayed_acceptance = await exit_plan.resume(
        third_exit,
        {"action": "accept"},
        request=third_request,
        context=context,
    )
    assert isinstance(accepted, ToolResult)
    assert isinstance(replayed_acceptance, ToolResult)
    assert accepted.is_error is False
    assert replayed_acceptance.content == accepted.content

    next_enter = enter_invocation.model_copy(update={"tool_call_id": "call_enter_2"})
    next_activation = await enter.execute(next_enter, context=context)
    assert isinstance(next_activation, ToolResult)
    assert next_activation.structured["plan_path"] != entered.structured["plan_path"]
    catalog.close()
    await application.close()
