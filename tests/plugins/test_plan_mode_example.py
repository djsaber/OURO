from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import MessageRecord
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

EXAMPLE = Path(__file__).parents[2] / "examples" / "plugins" / "plan-mode"
PLUGIN_ID = "dev.ouro.examples.plan_mode"


class PlanBuildProvider:
    provider_id = "plan-build"

    def __init__(self) -> None:
        self.stream_count = 0
        self.plan_path: Path | None = None
        self.requests: list[list[MessageRecord]] = []

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
                if message.metadata.get("kind") == "context_provider"
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
        self.messages: list[MessageRecord] = []

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


class CommandPauseProvider:
    provider_id = "plan-command"

    def __init__(self) -> None:
        self.messages: list[MessageRecord] = []

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
        yield ProviderStreamEvent(
            type="tool_call",
            tool_call=ToolCallRequest(
                id="call_question",
                name="AskUser",
                arguments={"question": "Which component matters most?"},
            ),
        )
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class PlainTextThenQuestionProvider:
    provider_id = "plan-turn-policy"

    def __init__(self) -> None:
        self.stream_count = 0

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
            yield ProviderStreamEvent(type="text_delta", text="I have enough information.")
        else:
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call=ToolCallRequest(
                    id="call_question",
                    name="AskUser",
                    arguments={"question": "Which compatibility target is required?"},
                ),
            )
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


async def _install_and_enable(application: LocalApplication) -> None:
    assert application.plugins is not None
    application.plugins.install(EXAMPLE)
    application.plugins.authorize(PLUGIN_ID)
    application.plugins.trust_code(PLUGIN_ID)
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
    await _install_and_enable(first_application)

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
    await _install_and_enable(application)

    result = await runtime.run("Plan without changing implementation", approval_mode="auto")

    assert result.run.status == "waiting_approval"
    assert not wrong_path.exists()
    call = next(
        item for item in result.run.active_tool_batch.calls if item.request.id == "call_wrong_write"
    )
    assert call.invocation is not None
    assert call.invocation.permission_mode == "approval"
    assert call.invocation.tool_policy_ids == [
        "dev.ouro.examples.plan_mode/tool_policies/plan-tool-gate"
    ]
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
async def test_plan_command_returns_durable_attributed_scaffold_input(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = CommandPauseProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _install_and_enable(application)
    session = runtime.create_session(title="Plan command")
    assert application.plugins is not None

    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:plan",
        "Refactor the parser",
        session_id=session.session_id,
        workspace=str(workspace),
    )

    assert len(command.session_inputs) == 1
    session_input = command.session_inputs[0]
    assert session_input.plugin_id == PLUGIN_ID
    assert session_input.contribution_id == "plan"
    assert "Planning request:\nRefactor the parser" in session_input.prompt
    waiting = await runtime.run(
        session_input.prompt,
        session_id=session.session_id,
        approval_mode="auto",
        _input_attribution=session_input,
    )
    assert waiting.run.status == "waiting_user"
    snapshot = store.load_runtime_session(session.session_id)
    scaffold = next(
        message
        for message in snapshot.messages
        if message.metadata.get("session_input_source") == "plugin"
    )
    assert scaffold.role == "system_synthetic"
    assert scaffold.metadata["plugin_id"] == PLUGIN_ID
    assert "Plan mode is now active" in scaffold.text_content
    reminder = next(
        message
        for message in snapshot.messages
        if message.metadata.get("kind") == "context_provider"
    )
    assert reminder.metadata["plugin_id"] == PLUGIN_ID
    assert "Plan mode is active" in reminder.text_content
    await application.close()


@pytest.mark.asyncio
async def test_plan_mode_turn_policy_persists_feedback_and_continues_same_run(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = PlainTextThenQuestionProvider()
    runtime, store = _runtime(home, workspace, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _install_and_enable(application)

    waiting = await runtime.run("Prepare a plan", approval_mode="auto")

    assert waiting.run.status == "waiting_user"
    assert provider.stream_count == 3
    snapshot = store.load_runtime_session(waiting.session.session_id)
    attempted = next(
        message
        for message in snapshot.messages
        if message.role == "assistant" and message.text_content == "I have enough information."
    )
    feedback = next(
        message
        for message in snapshot.messages
        if message.metadata.get("session_input_source") == "turn_policy"
    )
    assert attempted.run_id == waiting.run.run_id
    assert feedback.run_id == waiting.run.run_id
    assert "plain-text answer cannot finish" in feedback.text_content
    controllers = feedback.metadata["controllers"]
    assert isinstance(controllers, list)
    assert controllers[0]["plugin_id"] == PLUGIN_ID
    assert controllers[0]["policy_id"] == "require-plan-exit"
    assert isinstance(controllers[0]["generation_id"], str)
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
    await _install_and_enable(application)
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
