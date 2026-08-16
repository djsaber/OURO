from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.builtin_plugins.goal_mode import GOAL_MODE_PLUGIN
from ouro.builtin_plugins.goal_mode.commands import parse_goal_command
from ouro.builtin_plugins.goal_mode.goal_mode_plugin import GoalModePlugin
from ouro.builtin_plugins.goal_mode.state import GoalStateRepository, SessionGoalState
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


class _EndToEndGoalProvider:
    provider_id = "goal-e2e"

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[list[object]] = []

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
        call = self.calls
        self.calls += 1
        if call == 0:
            assert messages[-1].role == "user"
            assert messages[-1].text_content == "Ship end to end"
            assert "<goal-create>" in messages[-2].text_content
            yield ProviderStreamEvent.from_tool_call(
                ToolCallRequest(
                    id="create_goal",
                    name="CreateGoal",
                    arguments={"objective": "Ship end to end", "token_budget": 100},
                )
            )
        elif call == 1:
            yield ProviderStreamEvent(type="text_delta", text="Made verified progress.")
        elif call == 2:
            assert "<goal-continue>" in messages[-1].text_content
            yield ProviderStreamEvent.from_tool_call(
                ToolCallRequest(
                    id="complete_goal",
                    name="UpdateGoal",
                    arguments={"status": "complete", "report": "Delivered and checked."},
                )
            )
        elif call == 3:
            yield ProviderStreamEvent(type="text_delta", text="Goal complete.")
        else:
            raise AssertionError("Goal continued after completion")
        yield ProviderStreamEvent(
            type="done",
            usage=Usage(input_tokens=3, output_tokens=2),
        )

    async def check_connection(self, model) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class _MemorySessionStateStore:
    def __init__(self) -> None:
        self.values: dict[str, sdk.PluginSessionState] = {}

    def load(self, session_id: str) -> sdk.PluginSessionState | None:
        return self.values.get(session_id)

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
        self.values[session_id] = sdk.PluginSessionState(
            state_schema_version=state_schema_version,
            payload=payload,
        )

    def delete(self, session_id: str) -> None:
        self.values.pop(session_id, None)


def _plugin(tmp_path: Path) -> tuple[GoalModePlugin, _MemorySessionStateStore]:
    state = _MemorySessionStateStore()
    context = sdk.PluginContext(
        identity=sdk.PluginIdentity(
            plugin_id=PLUGIN_ID,
            version="2.0.0",
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
        data=ScopedPluginDataStore(tmp_path),
        credentials=None,  # type: ignore[arg-type]
        tasks=None,  # type: ignore[arg-type]
        logger=None,  # type: ignore[arg-type]
        diagnostics=None,  # type: ignore[arg-type]
        permissions=frozenset({"sessions.write", "background.run", "sessions.prompt.inspect"}),
        session_state=state,
    )
    plugin = GOAL_MODE_PLUGIN.factory(context)
    assert isinstance(plugin, GoalModePlugin)
    return plugin, state


def _tool_call(
    name: str,
    arguments: dict[str, object],
    *,
    run_id: str = "run_1",
) -> sdk.ToolCall:
    return sdk.ToolCall(
        tool_call_id=f"call_{name}",
        session_id="session_1",
        run_id=run_id,
        tool_name=name,
        arguments=arguments,
    )


def _activate(
    plugin: GoalModePlugin,
    *,
    activation_id: str,
    payload: sdk.JsonValue,
) -> sdk.SessionControllerOutput:
    return plugin.activate_goal_controller(
        sdk.SessionControllerCall(
            session_id="session_1",
            session_kind="primary",
            activation_id=activation_id,
            controller_id=f"{PLUGIN_ID}/session_controllers/mode",
            action="activate",
            payload=payload,
        )
    )


def _deactivate(
    plugin: GoalModePlugin,
    *,
    activation_id: str,
    payload: sdk.JsonValue,
) -> sdk.SessionControllerOutput:
    return plugin.deactivate_goal_controller(
        sdk.SessionControllerCall(
            session_id="session_1",
            session_kind="primary",
            activation_id=activation_id,
            controller_id=f"{PLUGIN_ID}/session_controllers/mode",
            action="deactivate",
            payload=payload,
        )
    )


def _create(
    plugin: GoalModePlugin,
    *,
    objective: str = "Ship the feature",
    token_budget: int | None = None,
    run_id: str = "run_1",
    activation_id: str = "activation_1",
) -> None:
    arguments: dict[str, object] = {"objective": objective}
    if token_budget is not None:
        arguments["token_budget"] = token_budget
    result = plugin.create_goal(_tool_call("CreateGoal", arguments, run_id=run_id))
    assert result.is_error is False
    transition = result.session_control_transitions[0]
    assert transition.action == "acquire"
    _activate(plugin, activation_id=activation_id, payload=transition.payload)


def _goal(state: _MemorySessionStateStore):
    return GoalStateRepository(state).load("session_1").goal


def test_manifest_and_tool_contract_are_minimal_and_generic() -> None:
    manifest = GOAL_MODE_PLUGIN.manifest

    assert manifest.version == "2.0.0"
    assert {item.id for item in manifest.contributes.tools} == {
        "get-goal",
        "create-goal",
        "update-goal",
    }
    assert [item.id for item in manifest.contributes.context_providers] == ["goal-run-binding"]
    assert [item.id for item in manifest.contributes.prompt_policies] == ["goal-input-gate"]
    assert {item.active_when for item in manifest.contributes.tools} == {None}
    assert {item.id for item in manifest.permissions} == {
        "sessions.write",
        "background.run",
        "sessions.prompt.inspect",
    }
    descriptions = "\n".join(item.description for item in manifest.contributes.tools)
    forbidden = {
        "ouro",
        "session",
        "run",
        "host",
        "plugin",
        "controller",
        "activation",
        "continuation",
        "checkpoint",
        "transaction",
        "generation",
    }
    assert not {word for word in forbidden if word in descriptions.casefold()}


@pytest.mark.asyncio
async def test_tool_schemas_use_objective_and_implicit_update_identity(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path)
    contributions = await plugin.prepare()
    tools = {item.exposed_name: item for item in contributions.tools}

    assert tools["CreateGoal"].parameters_schema["required"] == ["objective"]
    assert set(tools["CreateGoal"].parameters_schema["properties"]) == {
        "objective",
        "token_budget",
    }
    assert tools["GetGoal"].parameters_schema["properties"] == {}
    assert tools["UpdateGoal"].parameters_schema["required"] == ["status", "report"]
    assert set(tools["UpdateGoal"].parameters_schema["properties"]) == {
        "status",
        "report",
    }
    assert {item.active_when for item in tools.values()} == {None}


def test_create_command_returns_one_atomic_user_message_pair(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path)

    result = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="--token-budget 1200 Preserve this objective exactly",
            session_id="session_1",
        )
    )

    assert result.session_inputs == ()
    assert len(result.session_input_batches) == 1
    batch = result.session_input_batches[0]
    assert batch.coalesce_key is None
    assert [item.role for item in batch.inputs] == ["user", "user"]
    assert [item.semantic_tag for item in batch.inputs] == ["goal-create", None]
    assert batch.inputs[1].prompt == "Preserve this objective exactly"
    assert "1200" in batch.inputs[0].prompt


@pytest.mark.asyncio
async def test_goal_runs_end_to_end_and_retains_final_usage(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = _EndToEndGoalProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="goal-e2e", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment.local(home, store.sessions_dir),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    await application.start()
    assert application.plugins is not None
    await application.plugins.enable(PLUGIN_ID)
    session = runtime.create_session(title="Goal e2e", approval_mode="auto")

    try:
        command = await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal",
            "--token-budget 100 Ship end to end",
            session_id=session.session_id,
        )
        assert len(command.session_input_batches) == 1

        await runtime.submit_plugin_input_batch(
            command.session_input_batches[0],
            session_id=session.session_id,
        )

        assert provider.calls == 4
        record = next(
            item
            for item in store.load_session_extension_states(session.session_id)
            if item.owner_id == PLUGIN_ID
        )
        goal = SessionGoalState.from_json(record.payload).goal
        assert goal is not None
        assert goal.status == "complete"
        assert goal.tokens_used == 20
        assert goal.report == "Delivered and checked."
        assert runtime.store.load_session(session.session_id).runs[-1].status == "completed"

        messages = store.load_session(session.session_id).messages
        by_tag = {message.semantic_tag: message for message in messages if message.semantic_tag}
        assert set(by_tag) == {"goal-create", "goal-continue"}
        for scaffold in by_tag.values():
            assert scaffold.role == "user"
            assert scaffold.scaffold is True
        objective = next(
            message for message in messages if message.text_content == "Ship end to end"
        )
        assert objective.role == "user"
        assert objective.semantic_tag is None
        assert objective.scaffold is False
    finally:
        await application.close()


@pytest.mark.parametrize(
    ("argument", "action", "budget", "objective"),
    [
        ("status", "status", None, None),
        ("New goal", "create", None, "New goal"),
        ("--token-budget 30 New goal", "create", 30, "New goal"),
        ("resume --token-budget 25", "resume", 25, None),
        ("resume", "resume", None, None),
        ("replace --token-budget=40 Updated", "replace", 40, "Updated"),
        ("replace Updated", "replace", None, "Updated"),
    ],
)
def test_goal_command_parser_treats_missing_budget_as_unlimited(
    argument: str,
    action: str,
    budget: int | None,
    objective: str | None,
) -> None:
    parsed = parse_goal_command(argument)

    assert parsed.error is None
    assert parsed.action == action
    assert parsed.token_budget == budget
    assert parsed.objective == objective


def test_latest_goal_wrapper_is_retained_after_completion(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin)
    update = plugin.update_goal(
        _tool_call(
            "UpdateGoal",
            {"status": "complete", "report": "Delivered and verified."},
        )
    )
    transition = update.session_control_transitions[0]
    assert transition.action == "release"

    _deactivate(plugin, activation_id="activation_1", payload=transition.payload)
    decision = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id="session_1",
            run_id="run_1",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 8, "output_tokens": 5},
        )
    )

    goal = _goal(state)
    assert goal is not None
    assert goal.status == "complete"
    assert goal.tokens_used == 13
    assert goal.report == "Delivered and verified."
    assert goal.activation_id is None
    assert decision.action == "complete"
    public = plugin.get_goal(_tool_call("GetGoal", {})).structured["goal"]
    assert public == goal.to_public()
    assert set(public) == {
        "objective",
        "status",
        "token_budget",
        "tokens_used",
        "tokens_remaining",
        "created_at",
        "updated_at",
        "report",
    }


def test_old_execution_cannot_update_replaced_goal(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin, objective="Old objective", run_id="old_run")
    current = _goal(state)
    assert current is not None

    _activate(
        plugin,
        activation_id="activation_2",
        payload={
            "operation": "replace",
            "expected_activation_id": current.activation_id,
            "expected_revision": current.revision,
            "objective": "New objective",
            "token_budget": None,
        },
    )
    stale = plugin.update_goal(
        _tool_call(
            "UpdateGoal",
            {"status": "complete", "report": "Old work finished."},
            run_id="old_run",
        )
    )

    assert stale.is_error is True
    assert stale.structured["error_type"] == "stale_goal_update"
    decision = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id="session_1",
            run_id="old_run",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 6, "output_tokens": 4},
        )
    )
    goal = _goal(state)
    assert goal is not None
    assert goal.objective == "New objective"
    assert goal.status == "active"
    assert goal.tokens_used == 10
    assert decision.action == "ignore"


def test_blocked_update_rotates_activation_and_retains_final_usage(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin)
    update = plugin.update_goal(
        _tool_call(
            "UpdateGoal",
            {"status": "blocked", "report": "Same external outage after three attempts."},
        )
    )
    transition = update.session_control_transitions[0]
    assert transition.action == "replace"
    _activate(plugin, activation_id="activation_2", payload=transition.payload)
    assert (
        plugin.bind_goal_run(
            sdk.ContextCall(
                session_id="session_1",
                run_id="run_1",
                message_count=1,
                model_round=2,
            )
        ).fragments
        == ()
    )

    decision = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id="session_1",
            run_id="run_1",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 10, "output_tokens": 7},
        )
    )

    goal = _goal(state)
    assert goal is not None
    assert goal.status == "blocked"
    assert goal.activation_id == "activation_2"
    assert goal.tokens_used == 17
    assert decision.action == "pause"


def test_budget_is_settled_after_the_full_execution_and_then_stops(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin, token_budget=10)

    decision = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id="session_1",
            run_id="run_1",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 8, "output_tokens": 5},
        )
    )

    assert decision.action == "pause"
    transition = decision.session_control_transitions[0]
    assert transition.action == "replace"
    _activate(plugin, activation_id="activation_2", payload=transition.payload)
    goal = _goal(state)
    assert goal is not None
    assert goal.status == "budget_exhausted"
    assert goal.tokens_used == 13
    assert goal.tokens_used > goal.token_budget  # type: ignore[operator]


def test_complete_wins_over_budget_exhaustion_in_the_same_execution(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin, token_budget=10)
    update = plugin.update_goal(
        _tool_call(
            "UpdateGoal",
            {"status": "complete", "report": "Verified complete."},
        )
    )
    transition = update.session_control_transitions[0]
    _deactivate(plugin, activation_id="activation_1", payload=transition.payload)

    decision = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id="session_1",
            run_id="run_1",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 20, "output_tokens": 3},
        )
    )

    goal = _goal(state)
    assert goal is not None
    assert goal.status == "complete"
    assert goal.tokens_used == 23
    assert decision.action == "complete"


def test_resume_exhausted_goal_without_budget_removes_the_budget(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin, token_budget=10)
    decision = plugin.goal_continuation(
        sdk.RunOutcome(
            session_id="session_1",
            run_id="run_1",
            status="completed",
            origin="plugin",
            usage={"input_tokens": 10, "output_tokens": 1},
        )
    )
    transition = decision.session_control_transitions[0]
    _activate(plugin, activation_id="activation_2", payload=transition.payload)

    too_small = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="resume --token-budget 11",
            session_id="session_1",
        )
    )
    resumed = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="resume",
            session_id="session_1",
        )
    )

    assert too_small.data["error"] == "invalid_token_budget"
    assert len(resumed.session_input_batches) == 1
    assert resumed.session_input_batches[0].coalesce_key == "goal-lifecycle"
    resume_transition = resumed.session_control_transitions[0]
    _activate(plugin, activation_id="activation_3", payload=resume_transition.payload)
    goal = _goal(state)
    assert goal is not None
    assert goal.status == "active"
    assert goal.token_budget is None
    assert goal.tokens_used == 11


def test_pause_resume_replace_and_cancel_are_user_owned_transitions(tmp_path) -> None:
    plugin, state = _plugin(tmp_path)
    _create(plugin, objective="Initial", token_budget=100)
    initial = _goal(state)
    assert initial is not None
    created_at = initial.created_at

    pause = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="pause",
            session_id="session_1",
        )
    )
    assert pause.session_control_transitions[0].action == "replace"
    assert pause.session_input_batches[0].inputs[0].semantic_tag == "goal-paused"
    _activate(
        plugin,
        activation_id="activation_2",
        payload=pause.session_control_transitions[0].payload,
    )

    replace_output = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="replace Updated objective",
            session_id="session_1",
        )
    )
    assert replace_output.session_input_batches[0].inputs[0].semantic_tag == "goal-updated"
    _activate(
        plugin,
        activation_id="activation_3",
        payload=replace_output.session_control_transitions[0].payload,
    )
    updated = _goal(state)
    assert updated is not None
    assert updated.objective == "Updated objective"
    assert updated.created_at == created_at
    assert updated.token_budget is None

    cancel = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="cancel",
            session_id="session_1",
        )
    )
    assert cancel.session_input_batches[0].inputs[0].semantic_tag == "goal-cancelled"
    _deactivate(
        plugin,
        activation_id="activation_3",
        payload=cancel.session_control_transitions[0].payload,
    )
    assert _goal(state) is None


def test_prompt_gate_rejects_ordinary_input_while_goal_is_stopped(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path)
    _create(plugin)
    pause = plugin._commands.handle(  # noqa: SLF001 - builtin contract test
        sdk.CommandCall(
            command_id=f"{PLUGIN_ID}:goal",
            argument="pause",
            session_id="session_1",
        )
    )
    _activate(
        plugin,
        activation_id="activation_2",
        payload=pause.session_control_transitions[0].payload,
    )

    decision = plugin.goal_input_gate(
        sdk.PromptPolicyCall(
            session_id="session_1",
            prospective_run_id="run_2",
            prompt="Continue anyway",
        )
    )

    assert decision.action == "deny"
    assert "/goal resume" in (decision.reason or "")


def test_old_state_schema_is_rejected_instead_of_silently_converted(tmp_path) -> None:
    _, state = _plugin(tmp_path)
    state.values["session_1"] = sdk.PluginSessionState(
        state_schema_version=2,
        payload={"goal": None, "run_binding": None},
    )

    with pytest.raises(ValueError, match="unsupported Goal state schema"):
        GoalStateRepository(state).load("session_1")
