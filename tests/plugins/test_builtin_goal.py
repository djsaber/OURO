from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from ouro.application.harness import capture_harness
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_controls import SessionControlCoordinator, SessionControlError
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.builtin_plugins.goal import GOAL_PLUGIN
from ouro.builtin_plugins.goal.commands import parse_goal_command
from ouro.builtin_plugins.goal.goal_plugin import GoalPlugin
from ouro.builtin_plugins.goal.state import SessionGoalState
from ouro.core.adapter import RuntimeAdapter
from ouro.core.harness_calls import accept_state_changes
from ouro.core.messages import MessageRecord
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
    Usage,
)
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import AttributedSessionControlTransition
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest, ToolInvocation
from ouro.local_application import LocalApplication
from ouro.plugins.api import adapt_sdk_contributions
from ouro.plugins.harness_calls import invoke_plugin_callback
from ouro.plugins.resources import ScopedPluginDataStore
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sdk.plugins import v3 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult

PLUGIN_ID = "dev.ouro.goal"


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
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, tools
        self.requests.append(list(messages))
        call = self.calls
        self.calls += 1
        if call == 0:
            assert messages[-1].role == "user"
            assert messages[-1].text_content == "Ship end to end"
            assert "<goal-create>" in messages[-2].text_content
            assert "Status: active" in messages[0].text_content
            yield ProviderStreamEvent(type="text_delta", text="Made verified progress.")
        elif call == 1:
            assert "<goal-continue>" in messages[-1].text_content
            yield ProviderStreamEvent.from_tool_call(
                ToolCallRequest(
                    id="complete_goal",
                    name="UpdateGoal",
                    arguments={"status": "complete", "report": "Delivered and checked."},
                )
            )
        elif call == 2:
            yield ProviderStreamEvent(type="text_delta", text="Goal complete.")
        else:
            raise AssertionError("Goal continued after completion")
        yield ProviderStreamEvent(
            type="done",
            usage=Usage(input_tokens=3, output_tokens=2),
        )

    async def check_connection(self, model, *, request_context=None) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


class _AbortableGoalProvider:
    provider_id = "goal-abort"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        *,
        model,
        system_prompt="",
        messages,
        tools=None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        call = self.calls
        self.calls += 1
        if call == 0:
            yield ProviderStreamEvent.from_tool_call(
                ToolCallRequest(
                    id="hold_goal",
                    name="HoldGoal",
                    arguments={},
                )
            )
        else:
            raise AssertionError("aborted Goal started another model round")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model, *, request_context=None) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


def _plugin(tmp_path: Path) -> GoalPlugin:
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
        permissions=frozenset({"sessions.write", "background.run"}),
    )
    plugin = GOAL_PLUGIN.factory(context)
    assert isinstance(plugin, GoalPlugin)
    return plugin


class _GoalHarness:
    def __init__(self, store, plugin, controllers, usage):
        self.store, self.plugin, self.controllers, self.usage = store, plugin, controllers, usage
        self.session = SessionRecord()
        store.create_session(self.session)
        self.coordinator = SessionControlCoordinator(store, usage_service=usage)

    def request(self):
        current = self.store.load_session_record(self.session.session_id)
        return RunRecord(
            session_id=current.session_id,
            history_epoch=current.history_epoch,
            metadata={
                "session_control_snapshot": {
                    "activations": [
                        item.model_dump(mode="json") for item in current.controls.activations
                    ]
                }
            },
        )

    def goal(self):
        record = next(
            (
                item
                for item in self.store.load_session_extension_states(self.session.session_id)
                if item.owner_id == PLUGIN_ID
            ),
            None,
        )
        return SessionGoalState.from_json(record.payload).goal if record is not None else None

    async def call(self, handler, call, *, run=None, writable=True):
        before = self.store.load_session_record(self.session.session_id)
        snapshot = capture_harness(self.store, before.session_id, run=run, usage_service=self.usage)
        result, change = await invoke_plugin_callback(
            handler, call, snapshot=snapshot, owner_id=PLUGIN_ID, writable=writable
        )
        draft = accept_state_changes(snapshot, [change] if change else [])
        for transition in result.session_control_transitions:
            draft, _ = await self.coordinator.stage(
                AttributedSessionControlTransition(
                    action=transition.action,
                    controller_id=transition.controller_id,
                    activation_id=transition.activation_id,
                    replace_activation_ids=list(transition.replace_activation_ids),
                    payload=transition.payload,
                    reason=transition.reason,
                    plugin_id=PLUGIN_ID,
                    generation_id="generation",
                    execution_contract_hash="c" * 64,
                ),
                session=before,
                draft=draft,
                controllers=self.controllers,
            )
        self.coordinator.commit(
            SessionCommit(before=before, steps=(SessionCommitStep(harness=draft.state),))
        )
        return result

    async def tool(self, name, arguments, *, run=None):
        run = run or self.request()
        handler = {
            "CreateGoal": self.plugin.create_goal,
            "UpdateGoal": self.plugin.update_goal,
            "GetGoal": self.plugin.get_goal,
        }[name]
        return await self.call(
            handler,
            sdk.ToolCall(
                tool_call_id=f"call_{name}",
                session_id=self.session.session_id,
                run_id=run.run_id,
                tool_name=name,
                arguments=arguments,
            ),
            run=run,
            writable=name != "GetGoal",
        )

    async def command(self, argument):
        return await self.call(
            self.plugin.goal_command,
            sdk.CommandCall(
                command_id=f"{PLUGIN_ID}:goal",
                argument=argument,
                session_id=self.session.session_id,
            ),
        )

    async def continue_run(
        self,
        run,
        *,
        status="completed",
        usage=None,
        automatic_continuation=None,
        tool_call_count=None,
    ):
        return await self.call(
            self.plugin.goal_continuation,
            sdk.RunOutcome(
                session_id=self.session.session_id,
                run_id=run.run_id,
                origin="user",
                status=status,
                usage={"input_tokens": 3, "output_tokens": 2} if usage is None else usage,
                automatic_continuation=automatic_continuation,
                tool_call_count=tool_call_count,
            ),
            run=run,
        )

    def record_usage(self, run, tokens=5):
        assert self.usage is not None
        return self.usage.record_provider_usage(
            run=run,
            usage=Usage(input_tokens=tokens, output_tokens=0),
            model=ModelMetadata(model_id="goal-test", provider_id="test"),
            request_id=run.run_id,
            purpose="agent",
        )


@pytest.fixture(params=["files", "sqlite"])
async def goal_session(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    store.initialize()
    plugin = _plugin(tmp_path / "plugin")
    contributed = adapt_sdk_contributions(await plugin.prepare(), owner_id=PLUGIN_ID)
    controllers = PluginSessionControllerRegistry()
    controllers.replace_owner(
        "instance",
        [
            replace(
                item,
                plugin_id=PLUGIN_ID,
                release_id="release",
                generation_id="generation",
                execution_contract_hash="c" * 64,
            )
            for item in contributed.session_controllers
        ],
    )
    ledger = LocalUsageLedger(tmp_path / "home")
    ledger.initialize()
    return _GoalHarness(store, plugin, controllers, UsageService(ledger))


def test_manifest_and_tool_contract_are_minimal_and_generic() -> None:
    manifest = GOAL_PLUGIN.manifest

    assert manifest.name == "Goal"
    assert manifest.version == "2.0.0"
    assert {item.id for item in manifest.contributes.tools} == {
        "get-goal",
        "create-goal",
        "update-goal",
    }
    assert [item.id for item in manifest.contributes.context_providers] == ["goal-context"]
    assert not manifest.contributes.tool_policies
    assert "clear" not in {
        option.value for option in manifest.contributes.commands[0].argument_options
    }
    assert not manifest.contributes.prompt_policies
    assert {item.active_when for item in manifest.contributes.tools} == {None}
    assert {item.permission_control for item in manifest.contributes.tools} == {"tool"}
    assert {item.permission_mode for item in manifest.contributes.tools} == {"auto"}
    assert {item.id for item in manifest.permissions} == {
        "sessions.write",
        "background.run",
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
    plugin = _plugin(tmp_path)
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
    assert {item.permission_control for item in tools.values()} == {"tool"}
    assert {item.permission_mode for item in tools.values()} == {"auto"}


async def test_create_command_returns_one_atomic_user_message_pair(goal_session) -> None:
    result = await goal_session.command("--token-budget 1200 Preserve this objective exactly")

    goal = goal_session.goal()
    assert goal is not None
    assert goal.status == "active"
    assert goal.objective == "Preserve this objective exactly"
    assert goal.token_budget == 1200
    assert result.session_inputs == ()
    assert len(result.session_input_batches) == 1
    batch = result.session_input_batches[0]
    assert batch.coalesce_key == "goal-lifecycle"
    assert [item.role for item in batch.inputs] == ["user", "user"]
    assert [item.semantic_tag for item in batch.inputs] == ["goal-create", None]
    assert batch.inputs[1].prompt == "Preserve this objective exactly"
    assert "1200" in batch.inputs[0].prompt


async def test_resume_preserves_budget_until_explicitly_removed(goal_session) -> None:
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship", "token_budget": 30})
    await g.command("pause")
    await g.command("resume")
    assert g.goal().token_budget == 30
    await g.command("replace Updated objective")
    assert g.goal().token_budget == 30
    await g.command("pause")
    await g.command("resume --no-token-budget")
    assert g.goal().token_budget is None


async def test_model_resume_rejects_stale_work_and_preserves_budget(goal_session):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship", "token_budget": 10})
    old_request = g.request()
    g.record_usage(old_request, 5)
    await g.command("pause")
    stale = await g.tool("UpdateGoal", {"status": "active", "report": "Resume"}, run=old_request)
    assert stale.structured["error_type"] == "stale_goal_update"
    result = await g.tool(
        "UpdateGoal", {"status": "active", "report": "New evidence allows progress"}
    )
    assert not result.is_error
    assert g.goal().status == "active"
    assert g.goal().token_budget == 10
    g.record_usage(g.request(), 5)
    await g.command("pause")
    rejected = await g.tool("UpdateGoal", {"status": "active", "report": "Try again"})
    assert rejected.structured["error_type"] == "invalid_token_budget"
    assert g.goal().status == "paused"


async def test_cancel_removes_goal_without_creating_a_new_objective(goal_session) -> None:
    await goal_session.tool("CreateGoal", {"objective": "Ship"})
    await goal_session.command("cancel")
    assert goal_session.goal() is None


@pytest.mark.asyncio
async def test_goal_runs_end_to_end_and_retains_final_usage(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    ledger = LocalUsageLedger(home)
    ledger.initialize()
    provider = _EndToEndGoalProvider()
    runtime = RuntimeService(
        store=store,
        usage_service=UsageService(ledger),
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
        assert len(command.accepted_inputs) == 1
        pending = runtime.queued_inputs.eligible_session(
            store.load_session_record(session.session_id)
        )
        await runtime.run(pending[0].prompt, session_id=session.session_id, _queued_inputs=pending)

        assert provider.calls == 3
        record = next(
            item
            for item in store.load_session_extension_states(session.session_id)
            if item.owner_id == PLUGIN_ID
        )
        goal = SessionGoalState.from_json(record.payload).goal
        assert goal is not None
        assert goal.status == "complete"
        assert runtime.usage.session_scope_tokens(session.session_id) == {
            PLUGIN_ID: {goal.goal_id: 10}
        }
        # The first work request is scoped; the acknowledgement follows release.
        assert runtime.usage.summarize().totals.total_tokens == 15
        status = await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal", "status", session_id=session.session_id
        )
        assert status.data["goal"]["tokens_used"] == 10
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


@pytest.mark.asyncio
async def test_cold_abort_restores_goal_snapshot_and_pauses_goal(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = _AbortableGoalProvider()
    tools = ToolRegistry()

    def hold_goal(invocation: ToolInvocation, context) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="not reached",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="HoldGoal",
                description="Keep the Goal Run at an approval boundary.",
                permission_mode="approval",
            ),
            handler=hold_goal,
        )
    )
    runtime = RuntimeService(
        store=store,
        usage_service=UsageService(LocalUsageLedger(home)),
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="goal-abort", provider_id=provider.provider_id),
        ),
        tools=tools,
        environment=RuntimeEnvironment.local(home, store.sessions_dir),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    await application.start()
    assert application.plugins is not None
    await application.plugins.enable(PLUGIN_ID)
    session = runtime.create_session(title="Goal abort")

    try:
        command = await application.plugins.execute_command(
            f"{PLUGIN_ID}:goal",
            "Recover after interruption",
            session_id=session.session_id,
        )
        assert "Goal: active" in command.output
        pending = runtime.queued_inputs.eligible_session(
            store.load_session_record(session.session_id)
        )
        pending_hold = await runtime.run(
            pending[0].prompt, session_id=session.session_id, _queued_inputs=pending
        )
        assert pending_hold.run.status == "waiting_approval"

        before = store.load_session_record(session.session_id)
        assert len(before.controls.activations) == 1
        previous_activation_id = before.controls.activations[0].activation_id
        runtime._release_run_resources(pending_hold.run.run_id)

        aborted = await runtime.abort_run(
            session_id=session.session_id,
            run_id=pending_hold.run.run_id,
        )

        record = next(
            item
            for item in store.load_session_extension_states(session.session_id)
            if item.owner_id == PLUGIN_ID
        )
        goal = SessionGoalState.from_json(record.payload).goal
        controls = store.load_session_record(session.session_id).controls.activations
        settled = store.load_run(session.session_id, aborted.run.run_id).continuation
        assert aborted.run.status == "aborted"
        assert goal is not None
        assert goal.status == "paused"
        assert goal.report is not None
        assert "interrupted" in goal.report
        assert len(controls) == 1
        assert controls[0].phase == "active"
        assert controls[0].activation_id != previous_activation_id
        assert settled is not None
        assert settled.action == "pause"
        assert settled.reason == "run_aborted"
        assert provider.calls == 1
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
        ("--token-budgeting analysis", "create", None, "--token-budgeting analysis"),
        (
            "--token-budget 10 --token-budgeting analysis",
            "create",
            10,
            "--token-budgeting analysis",
        ),
    ],
)
def test_goal_command_parser_preserves_optional_budget(
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


async def test_lifecycle_uses_native_controls_and_keeps_one_business_identity(goal_session):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship", "token_budget": 30})
    first = g.goal()
    run = g.request()
    g.record_usage(run, 12)
    for command, status in [
        ("pause", "paused"),
        ("resume --token-budget 40", "active"),
        ("replace Updated objective", "active"),
    ]:
        result = await g.command(command)
        assert not result.data.get("error")
        assert "goal" not in result.data  # Lifecycle intent is not a final state read.
        goal = g.goal()
        assert goal.status == status
        assert goal.goal_id == first.goal_id
        assert goal.created_at == first.created_at
        assert g.usage.session_scope_tokens(g.session.session_id) == {PLUGIN_ID: {goal.goal_id: 12}}
    assert g.goal().objective == "Updated objective"
    assert g.goal().token_budget == 40
    stale = await g.tool("UpdateGoal", {"status": "complete", "report": "old work"}, run=run)
    assert stale.structured["error_type"] == "stale_goal_update"
    assert g.goal().status == "active"
    await g.command("cancel")
    assert g.goal() is None
    assert not g.store.load_session_record(g.session.session_id).controls.activations
    await g.tool("CreateGoal", {"objective": "New work"})
    assert g.goal().goal_id != first.goal_id
    result = await g.tool("GetGoal", {})
    assert result.structured["goal"]["tokens_used"] == 0


@pytest.mark.parametrize(
    "argument",
    [
        "--token-budget 0 Ship",
        "--token-budget -1 Ship",
        "--token-budget many Ship",
        "--token-budget",
        "--no-token-budget=10 Ship",
        "--token-budget 10 --no-token-budget Ship",
        "resume --no-token-budget --token-budget 10",
        "replace --token-budget 10 --token-budget 20 Ship",
        "replace --no-token-budget",
        "cancel Ship",
        "status extra",
    ],
)
def test_invalid_goal_commands_are_rejected(argument):
    assert parse_goal_command(argument).error is not None


@pytest.mark.parametrize("action", ["resume", "replace Revised objective"])
async def test_inherited_budget_cannot_restart_exhausted_goal(goal_session, action):
    g = goal_session
    await g.command("--token-budget 10 Ship")
    run = g.request()
    g.record_usage(run, 10)
    await g.continue_run(run)
    assert (await g.command(action)).data["error"] == "invalid_token_budget"
    assert g.goal().status == "budget_exhausted"
    assert g.goal().objective == "Ship"
    explicit = (
        "resume --token-budget 11"
        if action == "resume"
        else ("replace --token-budget 11 Revised objective")
    )
    assert not (await g.command(explicit)).data.get("error")
    assert g.goal().token_budget == 11
    assert g.goal().status == "active"


@pytest.mark.parametrize(
    ("automatic", "tools", "status", "reason"),
    [
        (True, 0, "completed", "goal_no_tool_calls"),
        (True, 1, "completed", "goal_active"),
        (False, 0, "completed", "goal_active"),
        (None, 0, "completed", "goal_active"),
        (True, None, "completed", "goal_active"),
        (True, 0, "failed", "run_failed"),
        (True, 0, "waiting_user", None),
        (True, 0, "waiting_approval", None),
        (True, 0, "waiting_child", None),
    ],
)
async def test_empty_work_guard_requires_known_automatic_terminal_execution(
    goal_session, automatic, tools, status, reason
):
    g = goal_session
    await g.command("Ship")
    decision = await g.continue_run(
        g.request(), status=status, automatic_continuation=automatic, tool_call_count=tools
    )
    assert decision.reason == reason
    assert g.goal().status == (
        "paused" if reason in {"goal_no_tool_calls", "run_failed"} else "active"
    )


async def test_budget_stop_precedes_empty_work_guard(goal_session):
    g = goal_session
    await g.command("--token-budget 10 Ship")
    run = g.request()
    g.record_usage(run, 10)
    decision = await g.continue_run(run, automatic_continuation=True, tool_call_count=0)
    assert decision.reason == "goal_budget_exhausted"
    assert g.goal().status == "budget_exhausted"


async def test_create_result_does_not_bind_the_request_that_created_it(goal_session):
    g = goal_session
    request = g.request()
    await g.tool("CreateGoal", {"objective": "Ship"}, run=request)
    result = await g.tool("UpdateGoal", {"status": "complete", "report": "premature"}, run=request)
    assert result.structured["error_type"] == "stale_goal_update"
    complete = await g.tool("UpdateGoal", {"status": "complete", "report": "checked"})
    assert not complete.is_error
    assert g.goal().status == "complete"
    assert g.goal().report == "checked"
    assert not g.store.load_session_record(g.session.session_id).controls.activations
    await g.command("cancel")
    assert g.goal() is None


@pytest.mark.parametrize("status", ["aborted", "failed", "paused", "recoverable"])
async def test_abnormal_termination_pauses_without_copying_execution_data(goal_session, status):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship"})
    run = g.request()
    outcome = await g.continue_run(run, status=status)
    assert outcome.action == "pause"
    assert outcome.reason == f"run_{status}"
    assert g.goal().status == "paused"
    control = g.store.load_session_record(g.session.session_id).controls.activations[0]
    assert control.activation_id != run.control_bindings[0].activation_id
    assert control.usage_scope is None


async def test_budget_uses_native_totals_and_does_not_write_on_ordinary_continuation(goal_session):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship", "token_budget": 10})
    run = g.request()
    g.record_usage(run, 7)
    before = g.store.load_harness(g.session.session_id)
    for _ in range(2):
        decision = await g.continue_run(run)
        assert decision.action == "continue"
        assert "Tokens used: 7" in decision.prompt
    assert g.store.load_harness(g.session.session_id) == before
    next_run = g.request()
    g.record_usage(next_run, 5)
    decision = await g.continue_run(next_run)
    assert decision.action == "pause"
    assert g.goal().status == "budget_exhausted"
    assert g.usage.session_scope_tokens(g.session.session_id)[PLUGIN_ID][g.goal().goal_id] == 12
    invalid = await g.command("resume --token-budget 12")
    assert invalid.data["error"] == "invalid_token_budget"
    invalid = await g.command("resume")
    assert invalid.data["error"] == "invalid_token_budget"
    assert g.goal().status == "budget_exhausted"
    await g.command("resume --no-token-budget")
    assert g.goal().status == "active"
    assert g.goal().token_budget is None


@pytest.mark.parametrize("missing", ["ledger", "provider"])
async def test_unavailable_usage_pauses_budgeted_goal_without_inventing_zero(goal_session, missing):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship", "token_budget": 10})
    run = g.request()
    if missing == "ledger":
        g.usage = None
    outcome = await g.continue_run(run, usage={} if missing == "provider" else None)
    assert outcome.reason == "usage_unavailable"
    assert g.goal().status == "paused"
    if missing == "ledger":
        result = await g.tool("GetGoal", {})
        assert result.structured["goal"]["tokens_used"] is None
        assert "unavailable" in result.content


async def test_blocked_goal_retains_claim_but_stops_usage_scope(goal_session):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship"})
    run = g.request()
    g.record_usage(run, 8)
    await g.tool(
        "UpdateGoal", {"status": "blocked", "report": "Three attempts; needs input"}, run=run
    )
    assert g.goal().status == "blocked"
    control = g.store.load_session_record(g.session.session_id).controls.activations[0]
    assert control.usage_scope is None
    g.record_usage(g.request(), 3)
    assert g.usage.session_scope_tokens(g.session.session_id)[PLUGIN_ID][g.goal().goal_id] == 8


async def test_goal_undo_restores_business_state_but_keeps_real_usage(goal_session):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship", "token_budget": 50})
    goal = g.goal()
    boundary = MessageRecord(
        session_id=g.session.session_id,
        role="user",
        content=[{"type": "text", "text": "Change goal"}],
    )
    g.store.commit_session(
        SessionCommit(
            before=g.store.load_session_record(g.session.session_id),
            steps=(SessionCommitStep(message=boundary),),
        )
    )
    run = g.request()
    g.record_usage(run, 20)
    await g.command("replace Revised")
    assert g.goal().objective == "Revised"
    g.store.commit_session(
        SessionCommit(
            before=g.store.load_session_record(g.session.session_id),
            before_message_id=boundary.message_id,
        )
    )
    assert g.goal() == goal
    result = await g.tool("GetGoal", {})
    assert result.structured["goal"]["tokens_used"] == 20
    assert result.structured["goal"]["tokens_remaining"] == 30
    payload = g.store.load_session_extension_states(g.session.session_id)[0].payload
    assert set(payload) == {"goal"}
    assert set(payload["goal"]) == {
        "goal_id",
        "objective",
        "status",
        "token_budget",
        "created_at",
        "updated_at",
        "report",
    }


async def test_failed_self_replacement_keeps_goal_and_different_controller_releases_it(
    goal_session,
):
    g = goal_session
    await g.tool("CreateGoal", {"objective": "Ship"})
    before = g.store.load_harness(g.session.session_id)
    activation = before.state.controls.activations[0]
    with pytest.raises(SessionControlError, match="unsupported Goal transition"):
        await g.coordinator.apply(
            AttributedSessionControlTransition(
                action="replace",
                controller_id="mode",
                plugin_id=PLUGIN_ID,
                generation_id="generation",
                execution_contract_hash="c" * 64,
                replace_activation_ids=[activation.activation_id],
                payload={"operation": "invalid"},
                reason="arbitrary user explanation",
            ),
            session_id=g.session.session_id,
            controllers=g.controllers,
        )
    assert g.store.load_harness(g.session.session_id) == before

    def activate(call):
        return sdk.SessionControllerOutput()

    alternative = adapt_sdk_contributions(
        sdk.ContributionSet(
            session_controllers=[
                sdk.SessionControllerContribution(
                    id="alternative",
                    description="another mode",
                    claims=(sdk.SessionControlClaim("ouro.session.primary_behavior", "exclusive"),),
                    activate=activate,
                    deactivate=activate,
                )
            ]
        ),
        owner_id=PLUGIN_ID,
    ).session_controllers[0]
    g.controllers.replace_owner(
        "alternative",
        [
            replace(
                alternative,
                plugin_id=PLUGIN_ID,
                release_id="release",
                generation_id="generation",
                execution_contract_hash="c" * 64,
            )
        ],
    )
    await g.coordinator.apply(
        AttributedSessionControlTransition(
            action="replace",
            controller_id="alternative",
            plugin_id=PLUGIN_ID,
            generation_id="generation",
            execution_contract_hash="c" * 64,
            replace_activation_ids=[activation.activation_id],
            payload="opaque data owned by the replacement controller",
            reason="arbitrary user explanation",
        ),
        session_id=g.session.session_id,
        controllers=g.controllers,
    )
    assert g.goal() is None
    assert (
        g.store.load_session_record(g.session.session_id)
        .controls.activations[0]
        .controller_local_id
        == "alternative"
    )
