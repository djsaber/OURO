from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_controls import SessionControlConflictError
from ouro.builtin_plugins.goal_mode import GOAL_MODE_PLUGIN
from ouro.builtin_plugins.goal_mode.goal_mode_plugin import GoalModePlugin
from ouro.cli.interactive_commands import handle_command
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
    Usage,
)
from ouro.core.tools import ToolCallRequest
from ouro.local_application import LocalApplication
from ouro.plugins.api import ScopedPluginDataStore
from ouro.sdk.plugins import v1 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry

PLUGIN_ID = "dev.ouro.goal_mode"
_SESSION_STATES: dict[Path, dict[str, sdk.PluginSessionState]] = {}


class _MemorySessionStateStore:
    def __init__(self, values: dict[str, sdk.PluginSessionState]) -> None:
        self._values = values

    def load(self, session_id: str) -> sdk.PluginSessionState | None:
        return self._values.get(session_id)

    def save(
        self,
        session_id: str,
        *,
        state_schema_version: int,
        payload: sdk.JsonValue,
        branch_policy="reset",
        projection_semantic_tags=(),
    ) -> None:
        del branch_policy, projection_semantic_tags
        self._values[session_id] = sdk.PluginSessionState(
            state_schema_version=state_schema_version,
            payload=payload,
        )

    def delete(self, session_id: str) -> None:
        self._values.pop(session_id, None)


class ScriptedGoalProvider:
    provider_id = "goal-test"

    def __init__(self, steps: list[dict[str, object]]) -> None:
        self.steps = steps
        self.calls = 0
        self.requests: list[list[Any]] = []
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
        step = self.steps[self.calls]
        self.calls += 1
        error = step.get("error")
        if isinstance(error, BaseException):
            raise error
        tool = step.get("tool")
        if isinstance(tool, ToolCallRequest):
            if tool.name == "UpdateGoal" and tool.arguments.get("goal_id") == "placeholder":
                tool = tool.model_copy(
                    update={
                        "arguments": {
                            **tool.arguments,
                            "goal_id": _created_goal_id(messages),
                        }
                    }
                )
            yield ProviderStreamEvent.from_tool_call(tool)
        else:
            yield ProviderStreamEvent(
                type="text_delta",
                text=str(step.get("text", f"step-{self.calls}")),
            )
        usage = step.get("usage")
        yield ProviderStreamEvent(
            type="done",
            usage=usage if isinstance(usage, Usage) else None,
        )

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class UserCommandAwareGoalProvider:
    """Exercise Goal creation only when the command arrives as the current user turn."""

    provider_id = "goal-command-role-test"

    def __init__(self, source_request: str) -> None:
        self.source_request = source_request
        self.calls = 0
        self.requests: list[list[Any]] = []

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
        call_index = self.calls
        self.calls += 1
        if call_index == 0:
            latest = messages[-1]
            if latest.role == "user" and self.source_request in latest.text_content:
                yield ProviderStreamEvent.from_tool_call(
                    ToolCallRequest(
                        id="call_create_goal",
                        name="CreateGoal",
                        arguments=_goal_arguments(self.source_request),
                    )
                )
            else:
                yield ProviderStreamEvent(
                    type="text_delta",
                    text="No current user Goal request was visible.",
                )
        elif call_index == 1:
            yield ProviderStreamEvent.from_tool_call(
                ToolCallRequest(
                    id="call_complete",
                    name="UpdateGoal",
                    arguments={
                        "goal_id": _created_goal_id(messages),
                        "status": "complete",
                        "report": "Verified the user-command Goal path.",
                    },
                )
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="Goal lifecycle recorded.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


def _provider_tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool["name"])
    return str(tool.name)


def _created_goal_id(messages: list[Any]) -> str:
    for message in reversed(messages):
        if (
            message.role == "tool_result"
            and message.tool_result is not None
            and message.tool_result.tool_name == "CreateGoal"
        ):
            match = re.search(r"Goal ([a-f0-9]{32}) will be activated", message.text_content)
            if match is not None:
                return match.group(1)
    raise AssertionError("CreateGoal result did not expose the created goal_id")


def _create_goal_step(
    source_request: str,
    *,
    token_budget: int | None = None,
    replace_goal_id: str | None = None,
) -> dict[str, object]:
    arguments = _goal_arguments(
        source_request,
        token_budget=token_budget,
        replace_goal_id=replace_goal_id,
    )
    return {
        "tool": ToolCallRequest(
            id="call_create_goal",
            name="CreateGoal",
            arguments=arguments,
        )
    }


def _goal_arguments(
    source_request: str,
    *,
    token_budget: int | None = None,
    replace_goal_id: str | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "source_request": source_request,
        "outcome": source_request,
        "verification": [f"Concrete evidence confirms: {source_request}"],
        "constraints": [],
        "boundaries": [],
        "iteration_strategy": (
            "Inspect current state, take the next useful action, verify, and adapt."
        ),
        "blocked_conditions": [
            "Progress requires unavailable user input or an external state change after "
            "useful attempts."
        ],
    }
    if token_budget is not None:
        arguments["token_budget"] = token_budget
    if replace_goal_id is not None:
        arguments["replace_goal_id"] = replace_goal_id
    return arguments


def _controller_goal_payload(
    *,
    operation: str,
    goal_id: str,
    source_request: str,
    actor: str,
    **extra: object,
) -> dict[str, object]:
    arguments = _goal_arguments(source_request)
    contract = {
        key: arguments[key]
        for key in (
            "outcome",
            "verification",
            "constraints",
            "boundaries",
            "iteration_strategy",
            "blocked_conditions",
        )
    }
    return {
        "operation": operation,
        "goal_id": goal_id,
        "source_request": source_request,
        "contract": contract,
        "token_budget": None,
        "actor": actor,
        **extra,
    }


class GatedGoalProvider(ScriptedGoalProvider):
    def __init__(self, steps: list[dict[str, object]]) -> None:
        super().__init__(steps)
        self.goal_activated = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
        if self.calls == 1:
            self.goal_activated.set()
            await self.release.wait()
        async for event in super().stream(**kwargs):
            yield event


def _runtime(
    home: Path,
    provider: ScriptedGoalProvider | UserCommandAwareGoalProvider,
) -> tuple[RuntimeService, LocalSessionStore]:
    store = LocalSessionStore(home)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="goal-test", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment.local(home, store.sessions_dir),
    )
    return runtime, store


async def _enable_goal_mode(application: LocalApplication) -> None:
    assert application.plugins is not None
    await application.start()
    assert (await application.plugins.enable(PLUGIN_ID)).status == "active"


def _goal_state(home: Path, session_id: str) -> dict[str, Any]:
    records = LocalSessionStore(home).load_session_extension_states(session_id)
    state = next(record for record in records if record.owner_id == PLUGIN_ID)
    assert isinstance(state.payload, dict)
    return state.payload


def _make_goal_plugin(data_root: Path) -> GoalModePlugin:
    context = sdk.PluginContext(
        identity=sdk.PluginIdentity(
            plugin_id=PLUGIN_ID,
            version="1.0.0",
            plugin_class="builtin",
            release_id="test-release",
            package_digest=None,
            builtin_contract_hash="test-contract",
            generation_id="test-generation",
        ),
        provenance=sdk.PluginProvenanceSummary(
            source_kind="builtin",
            public_source="builtin",
        ),
        configuration={},
        data=ScopedPluginDataStore(data_root),
        credentials=None,  # type: ignore[arg-type]
        tasks=None,  # type: ignore[arg-type]
        logger=None,  # type: ignore[arg-type]
        diagnostics=None,  # type: ignore[arg-type]
        permissions=frozenset({"sessions.write", "background.run"}),
        session_state=_MemorySessionStateStore(_SESSION_STATES.setdefault(data_root.resolve(), {})),
    )
    plugin = GOAL_MODE_PLUGIN.factory(context)
    assert isinstance(plugin, GoalModePlugin)
    return plugin


def test_goal_builtin_manifest_is_the_public_harness_contract() -> None:
    manifest = GOAL_MODE_PLUGIN.manifest

    assert manifest.plugin_id == PLUGIN_ID
    assert manifest.engines.plugin_api == "^2.0.0"
    assert {tool.id for tool in manifest.contributes.tools} == {
        "get-goal",
        "create-goal",
        "update-goal",
    }
    assert [command.id for command in manifest.contributes.commands] == ["goal"]
    assert manifest.contributes.commands[0].requires_session is True
    assert [controller.id for controller in manifest.contributes.session_controllers] == ["mode"]
    assert manifest.contributes.session_controllers[0].claims[0].domain == (
        "ouro.session.primary_behavior"
    )
    assert [item.id for item in manifest.contributes.context_providers] == ["goal-scaffold"]
    assert [item.id for item in manifest.contributes.continuation_policies] == ["goal-continuation"]
    assert manifest.contributes.turn_policies == []
    assert manifest.contributes.tool_policies == []
    assert {permission.id for permission in manifest.permissions} == {
        "sessions.write",
        "background.run",
    }
    assert GOAL_MODE_PLUGIN.default_enabled is False


@pytest.mark.asyncio
async def test_create_goal_exposes_the_structured_contract_and_strict_authority(
    tmp_path,
) -> None:
    plugin = _make_goal_plugin(tmp_path / "data")
    contributions = await plugin.prepare()
    create_tool = next(tool for tool in contributions.tools if tool.exposed_name == "CreateGoal")
    required = create_tool.parameters_schema["required"]

    assert isinstance(required, list)
    assert set(required) == {
        "source_request",
        "outcome",
        "verification",
        "constraints",
        "boundaries",
        "iteration_strategy",
        "blocked_conditions",
    }
    assert "NEVER call this tool autonomously" in create_tool.description
    assert "explicitly asks" in create_tool.description
    assert create_tool.permission_mode == "approval"
    assert create_tool.effect == "state_changing"

    invalid_arguments = _goal_arguments("Ship a verifiable result")
    invalid_arguments["verification"] = []
    invalid = plugin.create_goal(
        sdk.ToolCall(
            tool_call_id="call_invalid_contract",
            session_id="session_contract",
            run_id="run_contract",
            tool_name="CreateGoal",
            arguments=invalid_arguments,
        )
    )

    assert invalid.is_error is True
    assert invalid.structured["error_type"] == "invalid_goal_contract"


@pytest.mark.asyncio
async def test_goal_replace_command_delegates_to_create_goal_with_exact_identity(
    tmp_path,
) -> None:
    plugin = _make_goal_plugin(tmp_path / "data")
    session_id = "session_replace_command"
    plugin.activate_goal_controller(
        sdk.SessionControllerCall(
            session_id=session_id,
            session_kind="primary",
            activation_id="activation_old",
            controller_id="mode",
            action="activate",
            payload=_controller_goal_payload(
                operation="create",
                goal_id="goal_old",
                source_request="Old request",
                actor="model",
            ),
        )
    )
    contributions = await plugin.prepare()
    command = contributions.commands[0].handler(
        sdk.CommandCall(
            command_id="goal",
            argument="replace New explicit request",
            session_id=session_id,
        )
    )

    assert isinstance(command, sdk.CommandOutput)
    assert command.session_control_transitions == ()
    assert len(command.session_inputs) == 1
    assert command.data["replace_goal_id"] == "goal_old"
    assert '"goal_old"' in command.session_inputs[0].prompt
    before_replace = plugin.get_goal(
        sdk.ToolCall(
            tool_call_id="call_get_before_replace",
            session_id=session_id,
            run_id="run_replace",
            tool_name="GetGoal",
            arguments={},
        )
    )
    assert before_replace.structured["goal"]["goal_id"] == "goal_old"

    replacement = plugin.create_goal(
        sdk.ToolCall(
            tool_call_id="call_replace",
            session_id=session_id,
            run_id="run_replace",
            tool_name="CreateGoal",
            arguments=_goal_arguments(
                "New explicit request",
                replace_goal_id="goal_old",
            ),
        )
    )

    assert replacement.is_error is False
    assert replacement.session_control_transitions[0].action == "replace"
    assert replacement.session_control_transitions[0].replace_activation_ids == ("activation_old",)
    assert replacement.session_control_transitions[0].payload["operation"] == "replace"


@pytest.mark.asyncio
async def test_goal_command_creation_delegates_without_activating_or_creating_a_run(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider([])
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal lifecycle")

    created = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "--token-budget 100 Ship the durable feature",
        session_id=session.session_id,
    )

    assert len(created.session_inputs) == 1
    assert created.output == "Requesting Goal creation through the Agent."
    assert created.data == {
        "source_request": "Ship the durable feature",
        "token_budget": 100,
        "replace_goal_id": None,
    }
    assert "CreateGoal exactly once" in created.session_inputs[0].prompt
    assert "all six parts" in created.session_inputs[0].prompt
    assert '"Ship the durable feature"' in created.session_inputs[0].prompt
    assert created.session_inputs[0].role == "user"
    assert store.load_runtime_session(session.session_id).runs == []
    assert store.load_session_record(session.session_id).controls.activations == []
    assert not (
        home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    ).exists()
    await application.close()


@pytest.mark.asyncio
async def test_goal_cli_alias_returns_attributed_input_and_visible_status(tmp_path) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            _create_goal_step("Inspect and verify the CLI path"),
            {
                "tool": ToolCallRequest(
                    id="call_complete",
                    name="UpdateGoal",
                    arguments={
                        "goal_id": "placeholder",
                        "status": "complete",
                        "report": "Verified the CLI creation path.",
                    },
                )
            },
            {"text": "Goal creation and completion recorded."},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None

    created = await handle_command(
        "/goal Inspect and verify the CLI path",
        runtime,
        None,
        approval_mode="auto",
        plugins=application.plugins,
    )
    assert created.session_id is not None
    session_id = created.session_id
    assert store.load_session_record(session_id).title == "Inspect and verify the CLI path"
    assert store.load_runtime_session(session_id).runs == []
    assert len(created.session_inputs) == 1
    attributed = created.session_inputs[0]
    assert attributed.plugin_id == PLUGIN_ID
    assert attributed.contribution_id == "goal"
    assert attributed.role == "user"

    result = await runtime.submit_plugin_input(attributed, session_id=session_id)
    assert result.run.status == "completed"
    assert "CreateGoal" in provider.tool_names[0]
    assert "UpdateGoal" in provider.tool_names[1]
    snapshot = store.load_runtime_session(session_id)
    assert snapshot.messages[0].role == "user"

    status = await handle_command(
        "/goal status",
        runtime,
        session_id,
        plugins=application.plugins,
    )
    assert "Inspect and verify the CLI path" in status.output
    assert "complete" in status.output

    await handle_command(
        "/goal cancel",
        runtime,
        session_id,
        plugins=application.plugins,
    )
    await application.close()


@pytest.mark.asyncio
async def test_goal_command_is_a_user_turn_with_plugin_provenance(tmp_path) -> None:
    home = tmp_path / "home"
    source_request = "Ship the request through Goal mode"
    provider = UserCommandAwareGoalProvider(source_request)
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal command role", approval_mode="auto")

    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        source_request,
        session_id=session.session_id,
    )
    attributed = command.session_inputs[0]
    result = await runtime.submit_plugin_input(attributed, session_id=session.session_id)

    assert result.run.status == "completed"
    assert provider.calls == 3
    assert provider.requests[0][-1].role == "user"
    snapshot = store.load_runtime_session(session.session_id)
    assert snapshot.messages[0].role == "user"
    assert snapshot.messages[0].metadata["plugin_id"] == PLUGIN_ID
    goal = _goal_state(home, session.session_id)["goal"]
    assert goal["source_request"] == source_request
    assert goal["status"] == "complete"
    await application.close()


@pytest.mark.asyncio
async def test_active_goal_replacement_supersedes_continuation_as_user_intent(tmp_path) -> None:
    home = tmp_path / "home"
    old_request = "Keep working on the old request"
    new_request = "Replace it with the new request"
    provider = GatedGoalProvider(
        [
            _create_goal_step(old_request),
            {"text": "The old Goal made bounded progress."},
            {"text": "replacement step is filled after the old Goal activates"},
            {
                "tool": ToolCallRequest(
                    id="call_complete_replacement",
                    name="UpdateGoal",
                    arguments={
                        "goal_id": "placeholder",
                        "status": "complete",
                        "report": "Verified the replacement Goal.",
                    },
                )
            },
            {"text": "Replacement Goal completion recorded."},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Replace active Goal", approval_mode="auto")
    original = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        old_request,
        session_id=session.session_id,
    )
    original_run = asyncio.create_task(
        runtime.submit_plugin_input(
            original.session_inputs[0],
            session_id=session.session_id,
        )
    )
    await asyncio.wait_for(provider.goal_activated.wait(), timeout=2)
    old_goal_id = _goal_state(home, session.session_id)["goal"]["goal_id"]

    replacement = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        f"replace {new_request}",
        session_id=session.session_id,
    )
    assert replacement.data["replace_goal_id"] == old_goal_id
    assert replacement.session_inputs[0].role == "user"
    provider.steps[2] = _create_goal_step(new_request, replace_goal_id=old_goal_id)
    queued = await runtime.submit_plugin_input(
        replacement.session_inputs[0],
        session_id=session.session_id,
    )
    assert queued.run.status == "running"

    provider.release.set()
    await original_run

    goal = _goal_state(home, session.session_id)["goal"]
    assert goal["goal_id"] != old_goal_id
    assert goal["source_request"] == new_request
    assert goal["status"] == "complete"
    snapshot = store.load_runtime_session(session.session_id)
    replacement_message = next(
        message
        for message in snapshot.messages
        if message.role == "user" and new_request in message.text_content
    )
    assert replacement_message.metadata["plugin_id"] == PLUGIN_ID
    latest_user_message = next(
        message for message in reversed(provider.requests[2]) if message.role == "user"
    )
    assert new_request in latest_user_message.text_content
    assert any(
        "sole exception is a current user-role message" in message.text_content
        for message in provider.requests[2]
    )
    assert snapshot.runs[0].continuation is not None
    assert snapshot.runs[0].continuation.action == "pause"
    assert snapshot.runs[0].continuation.reason == "superseded_by_queued_input"
    assert store.load_session_record(session.session_id).controls.activations == []
    await application.close()


@pytest.mark.asyncio
async def test_user_can_pause_resume_and_cancel_goal_without_model_inference(tmp_path) -> None:
    home = tmp_path / "home"
    provider = GatedGoalProvider(
        [
            _create_goal_step("Exercise the user lifecycle controls"),
            {"text": "The bounded Run stopped after the user paused the Goal."},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal lifecycle controls", approval_mode="auto")
    creation = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Exercise the user lifecycle controls",
        session_id=session.session_id,
    )
    run_task = asyncio.create_task(
        runtime.submit_plugin_input(
            creation.session_inputs[0],
            session_id=session.session_id,
        )
    )
    await asyncio.wait_for(provider.goal_activated.wait(), timeout=2)

    paused = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "pause",
        session_id=session.session_id,
    )
    assert paused.data["status"] == "paused"
    assert _goal_state(home, session.session_id)["goal"]["status"] == "paused"
    assert store.load_session_record(session.session_id).controls.activations == []
    provider.release.set()
    await run_task
    assert provider.calls == 2
    assert _goal_state(home, session.session_id)["goal"]["status"] == "paused"

    resumed = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "resume",
        session_id=session.session_id,
    )
    assert resumed.data["status"] == "active"
    assert resumed.session_inputs[0].role == "system_synthetic"
    assert _goal_state(home, session.session_id)["goal"]["status"] == "active"
    assert len(store.load_session_record(session.session_id).controls.activations) == 1

    cancelled = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "cancel",
        session_id=session.session_id,
    )
    assert cancelled.data["cancelled"] is True
    assert store.load_session_record(session.session_id).controls.activations == []
    status = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "status",
        session_id=session.session_id,
    )
    assert status.data["goal"] is None
    await application.close()


@pytest.mark.asyncio
async def test_goal_naturally_continues_then_update_goal_completes_and_accounts_final_run(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            _create_goal_step("Complete and verify the result"),
            {"text": "Made partial progress.", "usage": Usage(input_tokens=3, output_tokens=2)},
            {
                "tool": ToolCallRequest(
                    id="call_complete",
                    name="UpdateGoal",
                    arguments={
                        "goal_id": "placeholder",
                        "status": "complete",
                        "report": "Verified the requested result.",
                    },
                ),
                "usage": Usage(input_tokens=4, output_tokens=1),
            },
            {"text": "Goal completion recorded.", "usage": Usage(input_tokens=2, output_tokens=1)},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal completion", approval_mode="auto")
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Complete and verify the result",
        session_id=session.session_id,
    )

    result = await runtime.submit_plugin_input(
        command.session_inputs[0],
        session_id=session.session_id,
    )

    assert result.run.status == "completed"
    assert provider.calls == 4
    assert any(
        "previous bounded Run ended" in message.text_content for message in provider.requests[2]
    )
    snapshot = store.load_runtime_session(session.session_id)
    assert [run.origin for run in snapshot.runs] == ["plugin", "plugin"]
    assert all(run.continuation is not None for run in snapshot.runs)
    assert [run.continuation.action for run in snapshot.runs if run.continuation] == [
        "continue",
        "complete",
    ]
    goal = _goal_state(home, session.session_id)["goal"]
    assert goal["status"] == "complete"
    assert goal["actor"] == "model"
    assert goal["runs_used"] == 2
    assert goal["tokens_used"] == 13
    assert goal["last_accounted_run_id"] == snapshot.runs[-1].run_id
    assert store.load_session_record(session.session_id).controls.activations == []
    scaffolds = [
        message
        for message in snapshot.messages
        if message.metadata.get("kind") == "context_provider"
        and message.metadata.get("plugin_id") == PLUGIN_ID
    ]
    assert scaffolds
    assert all(message.semantic_tag == "goal-contract" for message in scaffolds)
    assert all("source_request_json" in message.text_content for message in scaffolds)
    assert all("goal_contract_json" in message.text_content for message in scaffolds)

    await runtime.reconcile_pending_run_continuations()
    replayed = _goal_state(home, session.session_id)["goal"]
    assert replayed["runs_used"] == 2
    assert replayed["tokens_used"] == 13
    await application.close()


@pytest.mark.asyncio
async def test_goal_can_report_blocked_after_multiple_bounded_runs(tmp_path) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            _create_goal_step("Retry and diagnose the external operation"),
            {"text": "First external attempt failed.", "usage": Usage(input_tokens=1)},
            {"text": "Second external attempt failed.", "usage": Usage(input_tokens=1)},
            {"text": "Third external attempt failed.", "usage": Usage(input_tokens=1)},
            {
                "tool": ToolCallRequest(
                    id="call_blocked",
                    name="UpdateGoal",
                    arguments={
                        "goal_id": "placeholder",
                        "status": "blocked",
                        "report": "Three verified attempts hit the same external outage.",
                    },
                ),
                "usage": Usage(input_tokens=1),
            },
            {"text": "Blocker recorded.", "usage": Usage(output_tokens=1)},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal blocked", approval_mode="auto")
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Retry and diagnose the external operation",
        session_id=session.session_id,
    )

    await runtime.submit_plugin_input(command.session_inputs[0], session_id=session.session_id)

    snapshot = store.load_runtime_session(session.session_id)
    assert provider.calls == 6
    assert len(snapshot.runs) == 4
    assert [run.continuation.action for run in snapshot.runs if run.continuation] == [
        "continue",
        "continue",
        "continue",
        "pause",
    ]
    goal = _goal_state(home, session.session_id)["goal"]
    assert goal["status"] == "blocked"
    assert goal["actor"] == "model"
    assert goal["reason_code"] == "goal_blocked"
    assert goal["runs_used"] == 4
    assert goal["tokens_used"] == 5
    assert store.load_session_record(session.session_id).controls.activations == []
    await application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step", "expected_status", "expected_reason", "expected_tokens"),
    [
        (
            {"text": "Budgeted work.", "usage": Usage(input_tokens=4, output_tokens=3)},
            "blocked",
            "token_budget_exhausted",
            7,
        ),
        (
            {"text": "Provider omitted usage."},
            "paused",
            "usage_unavailable",
            0,
        ),
    ],
)
async def test_budget_boundary_stops_without_claiming_completion(
    tmp_path,
    step,
    expected_status,
    expected_reason,
    expected_tokens,
) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            _create_goal_step("Finish within a budget", token_budget=5),
            step,
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal budget", approval_mode="auto")
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "--token-budget 5 Finish within a budget",
        session_id=session.session_id,
    )

    await runtime.submit_plugin_input(command.session_inputs[0], session_id=session.session_id)

    goal = _goal_state(home, session.session_id)["goal"]
    assert goal["status"] == expected_status
    assert goal["actor"] == "runtime"
    assert goal["reason_code"] == expected_reason
    assert goal["tokens_used"] == expected_tokens
    assert goal["runs_used"] == 1
    assert provider.calls == 2
    assert store.load_session_record(session.session_id).controls.activations == []
    if expected_reason == "token_budget_exhausted":
        rejected = await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal",
            "resume",
            session_id=session.session_id,
        )
        assert rejected.data["error"] == "token_budget_exhausted"
        resumed = await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal",
            "resume --token-budget 10",
            session_id=session.session_id,
        )
        assert len(resumed.session_inputs) == 1
        await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal",
            "cancel",
            session_id=session.session_id,
        )
    await application.close()


@pytest.mark.asyncio
async def test_runtime_failure_pauses_goal_and_releases_controller(tmp_path) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            _create_goal_step("Keep working unless execution really fails"),
            {"error": RuntimeError("provider unavailable")},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal failure", approval_mode="auto")
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Keep working unless execution really fails",
        session_id=session.session_id,
    )

    result = await runtime.submit_plugin_input(
        command.session_inputs[0], session_id=session.session_id
    )

    assert result.run.status == "failed"
    goal = _goal_state(home, session.session_id)["goal"]
    assert goal["status"] == "paused"
    assert goal["actor"] == "runtime"
    assert goal["reason_code"] == "run_failed"
    assert goal["runs_used"] == 1
    assert store.load_session_record(session.session_id).controls.activations == []
    await application.close()


@pytest.mark.asyncio
async def test_create_goal_waits_for_manual_approval_without_starting_another_run(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            {
                "tool": ToolCallRequest(
                    id="call_create_goal",
                    name="CreateGoal",
                    arguments=_goal_arguments("Explicitly requested durable work"),
                ),
                "usage": Usage(input_tokens=2, output_tokens=1),
            }
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(
        title="Approval-governed Goal creation",
        approval_mode="approval",
    )
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Explicitly requested durable work",
        session_id=session.session_id,
    )

    waiting = await runtime.submit_plugin_input(
        command.session_inputs[0],
        session_id=session.session_id,
    )

    assert waiting.run.status == "waiting_approval"
    snapshot = store.load_runtime_session(session.session_id)
    assert len(snapshot.runs) == 1
    assert snapshot.runs[0].origin == "plugin"
    assert provider.calls == 1
    assert snapshot.session.controls.activations == []
    state_path = home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    assert not state_path.exists()
    await application.close()


def test_replaced_goal_rejects_stale_tool_and_ignores_old_run_settlement(tmp_path) -> None:
    plugin = _make_goal_plugin(tmp_path / "data")
    session_id = "session_stale"
    old_goal_id = "goal_old"
    new_goal_id = "goal_new"
    old_activation = "activation_old"
    new_activation = "activation_new"
    plugin.activate_goal_controller(
        sdk.SessionControllerCall(
            session_id=session_id,
            session_kind="primary",
            activation_id=old_activation,
            controller_id="mode",
            action="activate",
            payload=_controller_goal_payload(
                operation="create",
                goal_id=old_goal_id,
                source_request="Old objective",
                actor="user",
            ),
        )
    )
    plugin.goal_context(
        sdk.ContextCall(
            session_id=session_id,
            run_id="run_old",
            message_count=1,
            model_round=0,
        )
    )
    blank_report = plugin.update_goal(
        sdk.ToolCall(
            tool_call_id="call_blank_report",
            session_id=session_id,
            run_id="run_old",
            tool_name="UpdateGoal",
            arguments={
                "goal_id": old_goal_id,
                "status": "complete",
                "report": "   ",
            },
        )
    )
    invalid_status = plugin.update_goal(
        sdk.ToolCall(
            tool_call_id="call_invalid_status",
            session_id=session_id,
            run_id="run_old",
            tool_name="UpdateGoal",
            arguments={
                "goal_id": old_goal_id,
                "status": "paused",
                "report": "The model cannot pause a Goal.",
            },
        )
    )
    assert blank_report.structured["error_type"] == "invalid_report"
    assert invalid_status.structured["error_type"] == "invalid_status"
    plugin.activate_goal_controller(
        sdk.SessionControllerCall(
            session_id=session_id,
            session_kind="primary",
            activation_id=new_activation,
            controller_id="mode",
            action="activate",
            payload=_controller_goal_payload(
                operation="replace",
                goal_id=new_goal_id,
                source_request="New objective",
                actor="user",
                expected_goal_id=old_goal_id,
                expected_revision=1,
            ),
        )
    )
    plugin.deactivate_goal_controller(
        sdk.SessionControllerCall(
            session_id=session_id,
            session_kind="primary",
            activation_id=old_activation,
            controller_id="mode",
            action="deactivate",
            reason="replaced",
        )
    )

    stale_tool = plugin.update_goal(
        sdk.ToolCall(
            tool_call_id="call_old",
            session_id=session_id,
            run_id="run_old",
            tool_name="UpdateGoal",
            arguments={
                "goal_id": old_goal_id,
                "status": "complete",
                "report": "Old Run tried to finish.",
            },
        )
    )
    stale_continuation = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id=session_id,
            run_id="run_old",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 5, "output_tokens": 2},
        )
    )
    current = plugin.get_goal(
        sdk.ToolCall(
            tool_call_id="call_get",
            session_id=session_id,
            run_id="run_old",
            tool_name="GetGoal",
            arguments={},
        )
    )

    assert stale_tool.is_error is True
    assert stale_tool.structured["error_type"] == "stale_goal_id"
    assert stale_continuation.action == "ignore"
    public_goal = current.structured["goal"]
    assert isinstance(public_goal, Mapping)
    assert public_goal["goal_id"] == new_goal_id
    assert public_goal["status"] == "active"
    assert public_goal["runs_used"] == 0


def test_goal_accounting_is_idempotent_and_subagents_cannot_activate(tmp_path) -> None:
    plugin = _make_goal_plugin(tmp_path / "data")
    with pytest.raises(RuntimeError, match="primary Sessions"):
        plugin.activate_goal_controller(
            sdk.SessionControllerCall(
                session_id="subagent_session",
                session_kind="subagent",
                activation_id="activation_subagent",
                controller_id="mode",
                action="activate",
                payload=_controller_goal_payload(
                    operation="create",
                    goal_id="goal_subagent",
                    source_request="Nested durable control",
                    actor="model",
                ),
            )
        )

    plugin.activate_goal_controller(
        sdk.SessionControllerCall(
            session_id="primary_session",
            session_kind="primary",
            activation_id="activation_primary",
            controller_id="mode",
            action="activate",
            payload=_controller_goal_payload(
                operation="create",
                goal_id="goal_primary",
                source_request="Account one Run once",
                actor="user",
            ),
        )
    )
    plugin.goal_context(
        sdk.ContextCall(
            session_id="primary_session",
            run_id="run_primary",
            message_count=1,
            model_round=0,
        )
    )
    outcome = sdk.RunOutcome(
        session_id="primary_session",
        run_id="run_primary",
        status="completed",
        origin="plugin",
        usage={"input_tokens": 4, "output_tokens": 3},
    )

    first = plugin.goal_continuation(outcome)
    second = plugin.goal_continuation(outcome)
    current = plugin.get_goal(
        sdk.ToolCall(
            tool_call_id="call_get",
            session_id="primary_session",
            run_id="run_primary",
            tool_name="GetGoal",
            arguments={},
        )
    )

    assert first.action == second.action == "continue"
    public_goal = current.structured["goal"]
    assert isinstance(public_goal, Mapping)
    assert public_goal["runs_used"] == 1
    assert public_goal["tokens_used"] == 7


@pytest.mark.asyncio
async def test_goal_and_plan_mode_conflict_through_the_shared_framework_claim(tmp_path) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
            _create_goal_step("Conflicting objective"),
            {"text": "Goal creation conflict reported."},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    assert application.plugins is not None
    await application.start()
    assert (await application.plugins.enable(PLUGIN_ID)).status == "active"
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    session = runtime.create_session(title="Mode conflict", approval_mode="auto")
    await application.plugins.execute_command(
        "dev.ouro.plan_mode:plan",
        "on",
        session_id=session.session_id,
    )

    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Conflicting objective",
        session_id=session.session_id,
    )
    result = await runtime.submit_plugin_input(
        command.session_inputs[0],
        session_id=session.session_id,
    )

    create_result = next(item for item in result.tool_results if item.tool_name == "CreateGoal")
    assert create_result.is_error is True
    assert create_result.structured["error_type"] == "ToolPermissionDenied"
    assert "Plan mode denied" in create_result.content
    assert len(store.load_session_record(session.session_id).controls.activations) == 1
    assert not (
        home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    ).exists()

    await application.close()


@pytest.mark.asyncio
async def test_plan_cannot_partially_activate_while_goal_is_active(tmp_path) -> None:
    home = tmp_path / "home"
    provider = GatedGoalProvider(
        [
            _create_goal_step("Keep the Goal controller authoritative"),
            {
                "tool": ToolCallRequest(
                    id="call_complete",
                    name="UpdateGoal",
                    arguments={
                        "goal_id": "placeholder",
                        "status": "complete",
                        "report": "Goal arbitration verified.",
                    },
                )
            },
            {"text": "Goal arbitration recorded."},
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    assert application.plugins is not None
    await application.start()
    assert (await application.plugins.enable(PLUGIN_ID)).status == "active"
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    session = runtime.create_session(
        title="Goal owns primary behavior",
        approval_mode="auto",
    )
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Keep the Goal controller authoritative",
        session_id=session.session_id,
    )
    run_task = asyncio.create_task(
        runtime.submit_plugin_input(
            command.session_inputs[0],
            session_id=session.session_id,
        )
    )
    await asyncio.wait_for(provider.goal_activated.wait(), timeout=2)
    before = store.load_session_record(session.session_id).controls.activations

    with pytest.raises(SessionControlConflictError, match="primary_behavior"):
        await application.plugins.execute_command(
            "dev.ouro.plan_mode:plan",
            "on",
            session_id=session.session_id,
        )

    after = store.load_session_record(session.session_id).controls.activations
    assert [item.activation_id for item in after] == [item.activation_id for item in before]
    assert after[0].controller_id == f"{PLUGIN_ID}/session_controllers/mode"
    assert _goal_state(home, session.session_id)["goal"]["status"] == "active"
    provider.release.set()
    await run_task
    assert _goal_state(home, session.session_id)["goal"]["status"] == "complete"
    await application.close()


@pytest.mark.asyncio
async def test_active_goal_recovers_after_restart_without_boot_time_autorun(tmp_path) -> None:
    home = tmp_path / "home"
    first_provider = ScriptedGoalProvider(
        [
            _create_goal_step("Survive a Host restart"),
            _create_goal_step("A second Goal must remain pending"),
        ]
    )
    first_runtime, _first_store = _runtime(home, first_provider)
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    await _enable_goal_mode(first_application)
    assert first_application.plugins is not None
    session = first_runtime.create_session(title="Recover Goal", approval_mode="approval")
    created = await first_application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Survive a Host restart",
        session_id=session.session_id,
    )
    waiting = await first_runtime.submit_plugin_input(
        created.session_inputs[0],
        session_id=session.session_id,
    )
    assert waiting.run.pending_approval_id is not None
    active_waiting = await first_runtime.approve_tool(
        session_id=session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=True,
    )
    assert active_waiting.run.status == "waiting_approval"
    assert active_waiting.run.pending_approval_id is not None
    goal_id = _goal_state(home, session.session_id)["goal"]["goal_id"]
    await first_application.close()

    second_provider = ScriptedGoalProvider([{"error": RuntimeError("recovered provider failure")}])
    second_runtime, second_store = _runtime(home, second_provider)
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()
    assert second_provider.calls == 0
    assert second_application.plugins is not None
    status = await second_application.plugins.execute_command(
        f"{PLUGIN_ID}:goal", "status", session_id=session.session_id
    )
    status_goal = status.data["goal"]
    assert isinstance(status_goal, Mapping)
    assert status_goal["goal_id"] == goal_id
    assert status_goal["status"] == "active"

    assert second_store.load_session_record(session.session_id).controls.activations
    cancelled = await second_application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "cancel",
        session_id=session.session_id,
    )
    assert "cancelled" in cancelled.output.lower()
    assert second_store.load_session_record(session.session_id).controls.activations == []
    await second_application.close()
