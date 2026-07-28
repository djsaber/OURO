from __future__ import annotations

import json
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
            yield ProviderStreamEvent(type="tool_call", tool_call=tool)
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


def _provider_tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool["name"])
    return str(tool.name)


def _runtime(
    home: Path,
    provider: ScriptedGoalProvider,
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
    path = home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))["state"]


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
    )
    plugin = GOAL_MODE_PLUGIN.factory(context)
    assert isinstance(plugin, GoalModePlugin)
    return plugin


def test_goal_builtin_manifest_is_the_public_harness_contract() -> None:
    manifest = GOAL_MODE_PLUGIN.manifest

    assert manifest.plugin_id == PLUGIN_ID
    assert manifest.engines.plugin_api == "^1.2.0"
    assert {tool.id for tool in manifest.contributes.tools} == {
        "get-goal",
        "create-goal",
        "update-goal",
    }
    assert [command.id for command in manifest.contributes.commands] == ["goal"]
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
async def test_goal_command_lifecycle_is_durable_and_does_not_create_runs(tmp_path) -> None:
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
    first_goal_id = created.data["goal_id"]
    assert _goal_state(home, session.session_id)["goal"]["status"] == "active"
    assert store.load_runtime_session(session.session_id).runs == []
    assert len(store.load_session_record(session.session_id).controls.activations) == 1

    duplicate = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Create an implicit second Goal",
        session_id=session.session_id,
    )
    assert duplicate.data["error"] == "unfinished_goal_exists"
    assert len(store.load_session_record(session.session_id).controls.activations) == 1

    paused = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "pause",
        session_id=session.session_id,
    )
    assert "paused" in paused.output
    assert _goal_state(home, session.session_id)["goal"]["status"] == "paused"
    assert store.load_session_record(session.session_id).controls.activations == []

    resumed = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "resume",
        session_id=session.session_id,
    )
    assert len(resumed.session_inputs) == 1
    assert _goal_state(home, session.session_id)["goal"]["status"] == "active"

    replaced = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "replace --token-budget 200 Ship the replacement",
        session_id=session.session_id,
    )
    replacement = _goal_state(home, session.session_id)["goal"]
    assert replacement["goal_id"] == replaced.data["goal_id"]
    assert replacement["goal_id"] != first_goal_id
    assert replacement["objective"] == "Ship the replacement"
    assert replacement["token_budget"] == 200
    assert len(store.load_session_record(session.session_id).controls.activations) == 1

    cancelled = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "cancel",
        session_id=session.session_id,
    )
    assert "cancelled" in cancelled.output.lower()
    assert not (
        home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{session.session_id}.json"
    ).exists()
    assert store.load_runtime_session(session.session_id).runs == []
    await application.close()


@pytest.mark.asyncio
async def test_goal_cli_alias_returns_attributed_input_and_visible_status(tmp_path) -> None:
    home = tmp_path / "home"
    runtime, _store = _runtime(home, ScriptedGoalProvider([]))
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal CLI")

    created = await handle_command(
        "/goal Inspect and verify the CLI path",
        runtime,
        session.session_id,
        plugins=application.plugins,
    )
    assert len(created.session_inputs) == 1
    attributed = created.session_inputs[0]
    assert attributed.plugin_id == PLUGIN_ID
    assert attributed.contribution_id == "goal"

    status = await handle_command(
        "/goal status",
        runtime,
        session.session_id,
        plugins=application.plugins,
    )
    assert "Inspect and verify the CLI path" in status.output
    assert "active" in status.output

    await handle_command(
        "/goal cancel",
        runtime,
        session.session_id,
        plugins=application.plugins,
    )
    await application.close()


@pytest.mark.asyncio
async def test_goal_naturally_continues_then_update_goal_completes_and_accounts_final_run(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    provider = ScriptedGoalProvider(
        [
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
    session = runtime.create_session(title="Goal completion")
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Complete and verify the result",
        session_id=session.session_id,
    )
    goal_id = str(command.data["goal_id"])
    complete_call = provider.steps[1]["tool"]
    assert isinstance(complete_call, ToolCallRequest)
    provider.steps[1]["tool"] = complete_call.model_copy(
        update={"arguments": {**complete_call.arguments, "goal_id": goal_id}}
    )

    result = await runtime.submit_plugin_input(
        command.session_inputs[0],
        session_id=session.session_id,
    )

    assert result.run.status == "completed"
    assert provider.calls == 3
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
    assert all("objective_json" in message.text_content for message in scaffolds)

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
    session = runtime.create_session(title="Goal blocked")
    command = await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Retry and diagnose the external operation",
        session_id=session.session_id,
    )
    blocked_call = provider.steps[3]["tool"]
    assert isinstance(blocked_call, ToolCallRequest)
    provider.steps[3]["tool"] = blocked_call.model_copy(
        update={
            "arguments": {
                **blocked_call.arguments,
                "goal_id": str(command.data["goal_id"]),
            }
        }
    )

    await runtime.submit_plugin_input(command.session_inputs[0], session_id=session.session_id)

    snapshot = store.load_runtime_session(session.session_id)
    assert provider.calls == 5
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
    provider = ScriptedGoalProvider([step])
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal budget")
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
    assert provider.calls == 1
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
    provider = ScriptedGoalProvider([{"error": RuntimeError("provider unavailable")}])
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)
    assert application.plugins is not None
    session = runtime.create_session(title="Goal failure")
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
                    arguments={"objective": "Explicitly requested durable work"},
                ),
                "usage": Usage(input_tokens=2, output_tokens=1),
            }
        ]
    )
    runtime, store = _runtime(home, provider)
    application = LocalApplication.from_runtime(runtime, home=home)
    await _enable_goal_mode(application)

    waiting = await runtime.run(
        "Create a Goal for the explicitly requested durable work",
        approval_mode="approval",
    )

    assert waiting.run.status == "waiting_approval"
    snapshot = store.load_runtime_session(waiting.session.session_id)
    assert len(snapshot.runs) == 1
    assert provider.calls == 1
    assert snapshot.session.controls.activations == []
    state_path = (
        home / "plugins" / "data" / PLUGIN_ID / "sessions" / f"{waiting.session.session_id}.json"
    )
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
            payload={
                "operation": "create",
                "goal_id": old_goal_id,
                "objective": "Old objective",
                "token_budget": None,
                "actor": "user",
            },
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
            payload={
                "operation": "replace",
                "goal_id": new_goal_id,
                "objective": "New objective",
                "token_budget": None,
                "actor": "user",
                "expected_goal_id": old_goal_id,
                "expected_revision": 1,
            },
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
                payload={
                    "operation": "create",
                    "goal_id": "goal_subagent",
                    "objective": "Nested durable control",
                    "token_budget": None,
                    "actor": "model",
                },
            )
        )

    plugin.activate_goal_controller(
        sdk.SessionControllerCall(
            session_id="primary_session",
            session_kind="primary",
            activation_id="activation_primary",
            controller_id="mode",
            action="activate",
            payload={
                "operation": "create",
                "goal_id": "goal_primary",
                "objective": "Account one Run once",
                "token_budget": None,
                "actor": "user",
            },
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
    runtime, _store = _runtime(home, ScriptedGoalProvider([]))
    application = LocalApplication.from_runtime(runtime, home=home)
    assert application.plugins is not None
    await application.start()
    assert (await application.plugins.enable(PLUGIN_ID)).status == "active"
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    session = runtime.create_session(title="Mode conflict")
    await application.plugins.execute_command(
        "dev.ouro.plan_mode:plan",
        "on",
        session_id=session.session_id,
    )

    with pytest.raises(SessionControlConflictError, match="primary_behavior"):
        await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal",
            "Conflicting objective",
            session_id=session.session_id,
        )

    await application.close()


@pytest.mark.asyncio
async def test_plan_cannot_partially_activate_while_goal_is_active(tmp_path) -> None:
    home = tmp_path / "home"
    runtime, store = _runtime(home, ScriptedGoalProvider([]))
    application = LocalApplication.from_runtime(runtime, home=home)
    assert application.plugins is not None
    await application.start()
    assert (await application.plugins.enable(PLUGIN_ID)).status == "active"
    assert (await application.plugins.enable("dev.ouro.plan_mode")).status == "active"
    session = runtime.create_session(title="Goal owns primary behavior")
    await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Keep the Goal controller authoritative",
        session_id=session.session_id,
    )
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
    await application.plugins.execute_command(
        f"{PLUGIN_ID}:goal", "cancel", session_id=session.session_id
    )
    await application.close()


@pytest.mark.asyncio
async def test_active_goal_recovers_after_restart_without_boot_time_autorun(tmp_path) -> None:
    home = tmp_path / "home"
    first_runtime, _first_store = _runtime(home, ScriptedGoalProvider([]))
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    await _enable_goal_mode(first_application)
    assert first_application.plugins is not None
    session = first_runtime.create_session(title="Recover Goal")
    created = await first_application.plugins.execute_command(
        f"{PLUGIN_ID}:goal",
        "Survive a Host restart",
        session_id=session.session_id,
    )
    goal_id = created.data["goal_id"]
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

    result = await second_runtime.run(
        "Continue after restart",
        session_id=session.session_id,
    )

    assert result.run.status == "failed"
    recovered = _goal_state(home, session.session_id)["goal"]
    assert recovered["goal_id"] == goal_id
    assert recovered["objective"] == "Survive a Host restart"
    assert recovered["status"] == "paused"
    assert recovered["runs_used"] == 1
    assert second_store.load_session_record(session.session_id).controls.activations == []
    await second_application.close()
