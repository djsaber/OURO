from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from ouro.application.contracts.plugins import PluginCommandView
from ouro.application.harness import capture_harness
from ouro.application.inputs.queue import QueuedInputQueue
from ouro.application.plugins.commands import (
    AmbiguousPluginCommandError,
    consume_plugin_command,
    submit_plugin_command,
)
from ouro.application.runs.continuations import RunContinuationService
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.application.session_controls import SessionControlCoordinator
from ouro.core.adapter import RuntimeAdapter
from ouro.core.continuations import (
    ContinuationDecision,
    ContinuationPolicyRegistration,
    RunContinuationRegistry,
)
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.messages import MessageRecord, TextContent, ToolResultReference
from ouro.core.observers import ObserverRegistration, ObserverRegistry, RunFinishedObservation
from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.session_controls import (
    SessionControlActivation,
    SessionControlClaim,
    SessionControlState,
    SessionControlTransitionRequest,
    SessionInputRequest,
)
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunContinuationSettlementPlan,
    RunOutcome,
    RunRecord,
    SessionRecord,
)
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallProducer, ToolCallRequest, ToolResult
from ouro.local_application import LocalApplication
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandResult,
)
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerOutput,
    PluginSessionControllerRegistry,
)
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.session_storage import session_connection
from tests.tool_batches import bind_tool_messages, persist_tool_batch, seed_tool_messages

TEST_GOAL_PLUGIN_ID = "dev.example.goal"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_continuation_terminal_query_runs_outside_the_event_loop(
    tmp_path,
    backend,
    monkeypatch,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="completed")
    store.create_session(session)
    store.save_run(run)
    service = RunContinuationService(
        store=store,
        registry=RunContinuationRegistry(),
        queue=QueuedInputQueue(store),
    )
    event_loop_thread = threading.get_ident()
    reader_threads: list[int] = []
    load_run = store.load_run

    def recording_load_run(session_id: str, run_id: str) -> RunRecord:
        reader_threads.append(threading.get_ident())
        return load_run(session_id, run_id)

    monkeypatch.setattr(store, "load_run", recording_load_run)

    settlement = await service.settle(session_id=session.session_id, run_id=run.run_id)

    assert settlement.settled is False
    assert reader_threads
    assert event_loop_thread not in reader_threads


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize(
    ("sources", "feedback", "automatic", "call_count"),
    [
        (["automatic"], False, True, 1),
        (["automatic"], True, True, 1),
        (["automatic"], False, True, 0),
        (["automatic"], False, True, 2),
        (["user"], False, False, 1),
        (["command"], False, False, 1),
        (["automatic", "user"], False, False, 1),
        ([], False, None, 1),
    ],
)
@pytest.mark.parametrize("producer", ["model", "driver"])
async def test_continuation_execution_facts_use_consumed_inputs_and_actual_calls(
    tmp_path, backend, sources, feedback, automatic, call_count, producer, monkeypatch
):
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(
            controllers=[_controller("dev.example.facts", "goal", "generation")]
        ),
    )
    store.save_run(run)
    messages = []
    for source in sources:
        messages.append(
            MessageRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                role="user",
                content=[TextContent(text="Continue" if source == "automatic" else "User input")],
                metadata={
                    "session_input_source": "user" if source == "user" else "plugin",
                    "session_input_mode": "follow_up",
                    "continuation_decision_id": "decision" if source == "automatic" else None,
                },
            )
        )
    if feedback:
        messages.append(
            MessageRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                role="system",
                content=[TextContent(text="Review the result")],
                metadata={"session_input_source": "turn_policy"},
            )
        )
    messages.extend(
        MessageRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=f"turn-{index}",
            role="assistant",
            tool_calls=[ToolCallRequest(id="read", name="Read", arguments={})]
            if producer == "model"
            else [],
        )
        for index in range(call_count)
    )
    messages.extend(
        MessageRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=f"turn-{index}",
            role="tool_result",
            tool_result=ToolResultReference(
                tool_call_id="read",
                tool_name="Read",
                producer=ToolCallProducer(
                    kind=producer,
                    driver_id="dev.example/run_drivers/test" if producer == "driver" else None,
                ),
            ),
        )
        for index in range(call_count)
    )
    for index in range(call_count):
        assistant = next(
            message
            for message in messages
            if message.role == "assistant" and message.turn_id == f"turn-{index}"
        )
        receipt = next(
            message
            for message in messages
            if message.role == "tool_result" and message.turn_id == f"turn-{index}"
        )
        request = ToolCallRequest(id="read", name="Read")
        batch = ToolCallBatchRecord(
            turn_id=assistant.turn_id,
            assistant_message_id=assistant.message_id if producer == "model" else None,
            producer=receipt.tool_result.producer,
            phase="completed",
            calls=[
                ToolBatchCall(
                    request=request,
                    status="completed",
                    result=ToolResult(tool_call_id="read", tool_name="Read"),
                )
            ],
        )
        persist_tool_batch(store, run, batch)
        bind_tool_messages(store, run, batch, [assistant, receipt])
    store.save_run(run)
    store.append_messages(messages)
    # Calls from other executions must not affect this outcome.
    other = MessageRecord(
        session_id=session.session_id,
        run_id="other-run",
        role="assistant",
        tool_calls=[ToolCallRequest(id="other", name="Read", arguments={})],
    )
    seed_tool_messages(store, other, [])
    store.append_message(other)
    outcomes = []

    def record(outcome):
        outcomes.append(outcome)
        return ContinuationDecision(action="continue", prompt="Continue the requested analysis.")

    policy = replace(_policy("dev.example.facts", "generation", "unused"), handler=record)
    registry = RunContinuationRegistry()
    registry.replace_owner("facts", (policy,))
    service = RunContinuationService(store=store, registry=registry, queue=QueuedInputQueue(store))

    def no_run_history(*args, **kwargs):
        raise AssertionError("continuation must use message counts and a point query")

    monkeypatch.setattr(store, "load_run_messages", no_run_history)
    settlement = await service.settle(session_id=session.session_id, run_id=run.run_id)
    assert len(outcomes) == 1
    assert outcomes[0].tool_call_count == call_count
    assert outcomes[0].automatic_continuation is automatic
    # The framework reports facts; each plugin owns its decision even with zero tool calls.
    assert settlement.queued is True
    assert settlement.run.continuation.action == "continue"


class CountingProvider:
    provider_id = "continuation-test"

    def __init__(self) -> None:
        self.calls = 0
        self.contexts = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, tools
        self.calls += 1
        self.contexts.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text=f"answer-{self.calls}")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
        )


def _write_goal_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": TEST_GOAL_PLUGIN_ID,
                "name": "Goal",
                "version": "1.0.0",
                "namespace": "goal",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "goal_plugin:create_plugin",
                },
                "contributes": {
                    "commands": [{"id": "goal", "description": "Start goal mode."}],
                    "continuation_policies": [{"id": "goal", "priority": 10, "timeout_seconds": 2}],
                },
                "permissions": [
                    {
                        "id": "sessions.write",
                        "reason": "Persist attributed goal continuation inputs.",
                    },
                    {
                        "id": "background.run",
                        "reason": "Continue an active goal after a Run completes.",
                    },
                ],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "goal_plugin.py").write_text(
        """from ouro.sdk.plugins.v5 import (
    CommandContribution,
    CommandOutput,
    ContinuationDecision,
    ContinuationPolicyContribution,
    ContributionSet,
    SessionInput,
)

class Plugin:
    async def prepare(self):
        async def goal(call):
            return CommandOutput(
                output='Goal started.',
                session_inputs=(SessionInput(prompt='Work on: ' + call.argument),),
            )

        async def continue_goal(outcome):
            if outcome.origin == 'user':
                return ContinuationDecision(
                    action='continue',
                    prompt='Check the goal and continue if it is not complete.',
                    reason='goal_still_active',
                )
            return ContinuationDecision(
                action='complete',
                reason='goal_complete',
            )

        return ContributionSet(
            commands=[CommandContribution(
                id='goal', description='Start goal mode.', handler=goal,
            )],
            continuation_policies=[ContinuationPolicyContribution(
                id='goal', handler=continue_goal,
            )],
        )

    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context):
    return Plugin()
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_plugin_continuation_creates_attributed_scaffold_follow_up(
    tmp_path,
    backend,
) -> None:
    home = tmp_path / "home"
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    store.initialize()
    provider = CountingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="continuation", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "goal-plugin"
    source.mkdir()
    _write_goal_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable(TEST_GOAL_PLUGIN_ID)).status == "active"

    result = await runtime.run("Solve the task")

    snapshot = store.load_session(result.session.session_id)
    assert provider.calls == 2
    assert [run.origin for run in snapshot.runs] == ["user", "plugin"]
    assert [run.continuation.action for run in snapshot.runs if run.continuation] == [
        "continue",
        "complete",
    ]
    assert snapshot.runs[1].parent_run_id == snapshot.runs[0].run_id
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    scaffold = snapshot.messages[2]
    assert scaffold.metadata["plugin_id"] == TEST_GOAL_PLUGIN_ID
    assert scaffold.metadata["contribution_id"] == "goal"
    assert scaffold.metadata["continuation_decision_id"]
    assert scaffold.text_content.startswith("Check the goal")
    events, _ = store.load_events_page(result.session.session_id, limit=100)
    assert any(event.event_type == "run_continuation_resolved" for event in events)

    await application.close()


@pytest.mark.asyncio
async def test_plugin_command_returns_permission_checked_session_input(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = CountingProvider()
    inspected_prompts: list[str] = []

    def inspect(call: PromptPolicyCall) -> PromptPolicyDecision:
        inspected_prompts.append(call.prompt)
        return PromptPolicyDecision(action="deny", reason="direct user prompts only")

    prompt_policies = PromptPolicyRegistry()
    prompt_policies.register(PromptPolicyRegistration("ouro.host", "direct-input-only", inspect))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="command", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        prompt_policies=prompt_policies,
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "goal-plugin"
    source.mkdir()
    _write_goal_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable(TEST_GOAL_PLUGIN_ID)).status == "active"

    alias = await submit_plugin_command(
        application.plugins,
        runtime,
        f"{TEST_GOAL_PLUGIN_ID}:goal",
        "inspect the feature",
        session_id=None,
    )
    assert alias.execution.output == "Goal started."
    assert alias.session_id is not None
    assert alias.execution.session_inputs[0].prompt == "Work on: inspect the feature"
    assert store.load_runs(alias.session_id) == []

    command = await submit_plugin_command(
        application.plugins,
        runtime,
        f"{TEST_GOAL_PLUGIN_ID}:goal",
        "ship the feature",
        session_id=None,
    )
    assert command.execution.output == "Goal started."
    assert command.session_id is not None
    assert command.session_id != alias.session_id
    assert store.load_runs(command.session_id) == []
    assert len(command.execution.session_inputs) == 1
    attributed = command.execution.session_inputs[0]
    assert attributed.plugin_id == TEST_GOAL_PLUGIN_ID
    assert attributed.contribution_id == "goal"
    assert attributed.generation_id

    (result,) = [result async for result in consume_plugin_command(runtime, command)]
    assert result.session.session_id == command.session_id
    snapshot = store.load_session(result.session.session_id)
    assert len(snapshot.runs) == 1
    assert snapshot.runs[0].origin == "plugin"
    assert snapshot.messages[0].role == "user"
    assert snapshot.messages[0].text_content == "Work on: ship the feature"
    assert inspected_prompts == []

    await application.close()


@pytest.mark.asyncio
async def test_run_finished_observer_receives_typed_terminal_outcome(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CountingProvider()
    seen = []

    def observe(observation: RunFinishedObservation) -> None:
        seen.append(observation.outcome)

    observers = ObserverRegistry()
    observers.register(
        ObserverRegistration(
            plugin_id="dev.ouro.observer",
            observer_id="runs",
            event="run_finished",
            handler=observe,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="observer", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        observers=observers,
    )

    result = await runtime.run("Observe this")
    await runtime.event_recorder.drain()

    assert len(seen) == 1
    assert seen[0].run_id == result.run.run_id
    assert seen[0].status == "completed"
    assert seen[0].final_text == "answer-1"
    await runtime.close()


@pytest.mark.asyncio
async def test_continuation_conflict_pauses_without_choosing_by_priority(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controllers = [
        _controller("dev.ouro.first", "goal", "gen_first"),
        _controller("dev.ouro.second", "goal", "gen_second"),
    ]
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=controllers),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "first",
        (_policy("dev.ouro.first", "gen_first", "first scaffold", priority=1),),
    )
    registry.replace_owner(
        "second",
        (_policy("dev.ouro.second", "gen_second", "second scaffold", priority=2),),
    )
    service = RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
    )

    settlement = await service.settle(session_id=session.session_id, run_id=run.run_id)

    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "pause"
    assert settlement.run.continuation.reason == "continuation_policy_conflict"
    assert not QueuedInputQueue(store).eligible(store.load_runtime_session(session.session_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", ["aborted", "failed", "paused", "recoverable"])
@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("decision_action", ["pause", "fail"])
async def test_abnormal_terminal_policy_can_pause_and_release_exact_control(
    tmp_path,
    run_status,
    backend,
    decision_action,
) -> None:
    store = (
        LocalSessionStore(tmp_path / run_status)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / f"{run_status}.sqlite3")
    )
    store.initialize()
    plugin_id = "dev.example.mode"
    canonical_controller_id = f"{plugin_id}/session_controllers/mode"
    activation = _activation(plugin_id, "mode")
    session = SessionRecord(controls=SessionControlState(revision=1, activations=[activation]))
    store.create_session(session)
    controller_ref = _controller(plugin_id, "boundary", "gen-mode").model_copy(
        update={
            "active_when_controller_id": canonical_controller_id,
            "activation_id": activation.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status=run_status,
        error="terminal error",
        continuation=RunContinuationCheckpoint(controllers=[controller_ref]),
    )
    store.save_run(run)
    outcomes = []
    lifecycle_calls = []
    release_payload = {"status": decision_action, "report": f"Run ended as {run_status}"}

    def decide(outcome):
        outcomes.append(outcome)
        return ContinuationDecision(
            action=decision_action,
            reason=f"terminal_run_{decision_action}",
            session_control_transitions=[
                SessionControlTransitionRequest(
                    action="release",
                    controller_id="mode",
                    payload=release_payload,
                )
            ],
        )

    registry = RunContinuationRegistry()
    registry.replace_owner(
        "mode-policy",
        (
            ContinuationPolicyRegistration(
                plugin_id=plugin_id,
                policy_id="boundary",
                handler=decide,
                generation_id="gen-mode",
                execution_contract_hash=controller_ref.execution_contract_hash,
                contract_hash=controller_ref.contract_hash,
                active_when_controller_id=canonical_controller_id,
            ),
        ),
    )
    controllers = _session_controller_registry(
        plugin_id,
        controller_ref.execution_contract_hash,
        lifecycle_calls,
    )
    service = RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
        session_controllers=controllers,
    )

    first = await service.settle(session_id=session.session_id, run_id=run.run_id)
    second = await service.settle(session_id=session.session_id, run_id=run.run_id)

    assert [outcome.status for outcome in outcomes] == [run_status]
    assert first.settled is True
    assert second.settled is False
    assert first.run.continuation is not None
    assert first.run.continuation.action == decision_action
    assert first.run.continuation.settlement_plan is None
    assert store.load_session_record(session.session_id).controls.activations == []
    assert len(lifecycle_calls) == 1
    assert lifecycle_calls[0].action == "deactivate"
    assert lifecycle_calls[0].payload == release_payload


@pytest.mark.asyncio
async def test_bound_terminal_policy_can_replace_its_exact_activation(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    plugin_id = "dev.example.mode"
    activation = _activation(plugin_id, "mode")
    session = SessionRecord(controls=SessionControlState(revision=1, activations=[activation]))
    store.create_session(session)
    controller_ref = _controller(plugin_id, "boundary", "gen-mode").model_copy(
        update={
            "active_when_controller_id": activation.controller_id,
            "activation_id": activation.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller_ref]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "mode-policy",
        (
            ContinuationPolicyRegistration(
                plugin_id=plugin_id,
                policy_id="boundary",
                handler=lambda _outcome: ContinuationDecision(
                    action="pause",
                    reason="budget_exhausted",
                    session_control_transitions=[
                        SessionControlTransitionRequest(
                            action="replace",
                            controller_id="mode",
                            replace_activation_ids=[activation.activation_id],
                            payload={"status": "budget_exhausted"},
                        )
                    ],
                ),
                generation_id="gen-mode",
                execution_contract_hash=controller_ref.execution_contract_hash,
                contract_hash=controller_ref.contract_hash,
                active_when_controller_id=activation.controller_id,
            ),
        ),
    )
    lifecycle_calls = []

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
        session_controllers=_session_controller_registry(
            plugin_id,
            controller_ref.execution_contract_hash,
            lifecycle_calls,
        ),
    ).settle(session_id=session.session_id, run_id=run.run_id)

    remaining = store.load_session_record(session.session_id).controls.activations
    assert settlement.settled is True
    assert len(remaining) == 1
    assert remaining[0].activation_id != activation.activation_id
    assert [call.activation_id for call in lifecycle_calls] == [activation.activation_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "error_type"),
    [
        ("unavailable", "ContinuationPolicyUnavailable"),
        ("exception", "RuntimeError"),
        ("timeout", "TimeoutError"),
    ],
)
async def test_bound_policy_failure_preserves_controls_until_explicit_release(
    tmp_path,
    failure_mode,
    error_type,
) -> None:
    store = LocalSessionStore(tmp_path / failure_mode)
    store.initialize()
    plugin_id = "dev.example.owner"
    activation = _activation(plugin_id, "mode")
    survivor = _activation("dev.example.survivor", "mode")
    session = SessionRecord(
        controls=SessionControlState(revision=1, activations=[activation, survivor])
    )
    store.create_session(session)
    controller_ref = _controller(plugin_id, "boundary", "gen-owner").model_copy(
        update={
            "active_when_controller_id": activation.controller_id,
            "activation_id": activation.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller_ref]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    if failure_mode != "unavailable":
        if failure_mode == "timeout":

            async def fail(_outcome):
                await asyncio.sleep(0.05)
                return ContinuationDecision()

            timeout_seconds = 0.001
        else:

            def fail(_outcome):
                raise RuntimeError("owner failed")

            timeout_seconds = 1.0
        registry.replace_owner(
            "owner",
            (
                ContinuationPolicyRegistration(
                    plugin_id=plugin_id,
                    policy_id="boundary",
                    handler=fail,
                    timeout_seconds=timeout_seconds,
                    generation_id="gen-owner",
                    execution_contract_hash=controller_ref.execution_contract_hash,
                    contract_hash=controller_ref.contract_hash,
                    active_when_controller_id=activation.controller_id,
                ),
            ),
        )

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
    ).settle(session_id=session.session_id, run_id=run.run_id)

    remaining = store.load_session_record(session.session_id).controls.activations
    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "pause"
    assert settlement.run.continuation.reason == "continuation_policy_failed"
    assert [failure.error_type for failure in settlement.failures] == [error_type]
    assert [item.activation_id for item in remaining] == [
        activation.activation_id,
        survivor.activation_id,
    ]
    assert any("explicit release is required" in item for item in settlement.diagnostics)


@pytest.mark.asyncio
async def test_invalid_continuation_transition_does_not_release_another_activation(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    plugin_id = "dev.example.owner"
    bound = _activation(plugin_id, "mode")
    other = _activation(plugin_id, "other")
    session = SessionRecord(controls=SessionControlState(revision=1, activations=[bound, other]))
    store.create_session(session)
    controller_ref = _controller(plugin_id, "boundary", "gen-owner").model_copy(
        update={
            "active_when_controller_id": bound.controller_id,
            "activation_id": bound.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller_ref]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "owner",
        (
            ContinuationPolicyRegistration(
                plugin_id=plugin_id,
                policy_id="boundary",
                handler=lambda _outcome: ContinuationDecision(
                    action="complete",
                    session_control_transitions=[
                        SessionControlTransitionRequest(
                            action="release",
                            activation_id=other.activation_id,
                        )
                    ],
                ),
                generation_id="gen-owner",
                execution_contract_hash=controller_ref.execution_contract_hash,
                contract_hash=controller_ref.contract_hash,
                active_when_controller_id=bound.controller_id,
            ),
        ),
    )

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
    ).settle(session_id=session.session_id, run_id=run.run_id)

    remaining = store.load_session_record(session.session_id).controls.activations
    assert settlement.run.continuation is not None
    assert settlement.run.continuation.reason == "continuation_decision_invalid"
    assert [item.activation_id for item in remaining] == [bound.activation_id, other.activation_id]


@pytest.mark.asyncio
async def test_staged_settlement_replay_does_not_reevaluate_or_release_new_activation(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    plugin_id = "dev.example.mode"
    old_activation = _activation(plugin_id, "mode")
    new_activation = _activation(plugin_id, "mode")
    session = SessionRecord(controls=SessionControlState(revision=2, activations=[new_activation]))
    store.create_session(session)
    controller_ref = _controller(plugin_id, "boundary", "gen-before").model_copy(
        update={
            "active_when_controller_id": old_activation.controller_id,
            "activation_id": old_activation.activation_id,
        }
    )
    plan = RunContinuationSettlementPlan(
        plugin_id=plugin_id,
        policy_id="boundary",
        execution_contract_hash=controller_ref.execution_contract_hash,
        contract_hash=controller_ref.contract_hash,
        action="complete",
        harness=capture_harness(store, session.session_id),
        session_control_transitions=[
            SessionControlTransitionRequest(
                action="release",
                controller_id="mode",
                activation_id=old_activation.activation_id,
            )
        ],
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(
            controllers=[controller_ref],
            settlement_plan=plan,
        ),
    )
    store.save_run(run)
    policy_calls = 0

    def should_not_run(_outcome):
        nonlocal policy_calls
        policy_calls += 1
        raise AssertionError("staged decisions must not be reevaluated")

    registry = RunContinuationRegistry()
    registry.replace_owner(
        "rebuilt-owner",
        (
            ContinuationPolicyRegistration(
                plugin_id=plugin_id,
                policy_id="boundary",
                handler=should_not_run,
                generation_id="gen-after",
                execution_contract_hash=controller_ref.execution_contract_hash,
                contract_hash=controller_ref.contract_hash,
                active_when_controller_id=old_activation.controller_id,
            ),
        ),
    )
    controllers = _session_controller_registry(
        plugin_id,
        controller_ref.execution_contract_hash,
        [],
        generation_id="gen-after",
    )

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
        session_controllers=controllers,
    ).settle(session_id=session.session_id, run_id=run.run_id)

    remaining = store.load_session_record(session.session_id).controls.activations
    assert policy_calls == 0
    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "complete"
    assert [item.activation_id for item in remaining] == [new_activation.activation_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_available", [True, False])
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_runtime_startup_reconciliation_replays_pending_settlement_without_running(
    tmp_path,
    policy_available,
    backend,
    monkeypatch,
) -> None:
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller_ref = _controller("dev.example.mode", "boundary", "gen-before")
    plan = RunContinuationSettlementPlan(
        plugin_id=controller_ref.plugin_id,
        policy_id=controller_ref.policy_id,
        execution_contract_hash=controller_ref.execution_contract_hash,
        contract_hash=controller_ref.contract_hash,
        action="continue",
        harness=capture_harness(store, session.session_id),
        prompt="Resume the durable boundary work.",
        role="user",
        semantic_tag="goal-continue",
        coalesce_key="goal-continuation",
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(
            controllers=[controller_ref],
            settlement_plan=plan,
        ),
    )
    store.save_run(run)
    policy_calls = 0

    def should_not_run(_outcome):
        nonlocal policy_calls
        policy_calls += 1
        raise AssertionError("startup must replay the staged plan")

    registry = RunContinuationRegistry()
    registry.replace_owner(
        "rebuilt-owner",
        (
            ContinuationPolicyRegistration(
                plugin_id=controller_ref.plugin_id,
                policy_id=controller_ref.policy_id,
                handler=should_not_run,
                generation_id="gen-after",
                execution_contract_hash=controller_ref.execution_contract_hash,
                contract_hash=controller_ref.contract_hash,
            ),
        ),
    )
    if not policy_available:
        registry = RunContinuationRegistry()
    provider = CountingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="recovery", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        continuations=registry,
    )

    async def drop_notifications(_events):
        pass

    monkeypatch.setattr(runtime.event_recorder, "publish_committed", drop_notifications)
    first = await runtime.reconcile_pending_run_continuations()
    second = await runtime.reconcile_pending_run_continuations()

    restored = store.load_run(session.session_id, run.run_id)
    queued = runtime.queued_inputs.eligible(store.load_runtime_session(session.session_id))
    events, _ = store.load_events_page(session.session_id, limit=100)
    assert first == (run.run_id,)
    assert second == ()
    assert policy_calls == 0
    assert provider.calls == 0
    assert restored.continuation is not None
    assert restored.continuation.action == "continue"
    assert [item.prompt for item in queued] == ["Resume the durable boundary work."]
    assert queued[0].role == "user"
    assert queued[0].scaffold is True
    assert queued[0].semantic_tag == "goal-continue"
    assert queued[0].coalesce_key == "goal-continuation"
    assert [event.event_type for event in events].count("run_continuation_resolved") == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_snapshot_recovery_failure_keeps_continuation_retryable(tmp_path, caplog) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    plugin_id = "dev.example.owner"
    activation = _activation(plugin_id, "mode")
    session = SessionRecord(controls=SessionControlState(revision=1, activations=[activation]))
    store.create_session(session)
    controller_ref = _controller(plugin_id, "boundary", "gen-owner").model_copy(
        update={
            "active_when_controller_id": activation.controller_id,
            "activation_id": activation.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status="aborted",
        metadata={
            "extension_catalog_id": "cat-unavailable",
            "extension_generations": [],
        },
        continuation=RunContinuationCheckpoint(controllers=[controller_ref]),
    )
    store.save_run(run)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            CountingProvider(),
            ModelMetadata(model_id="recovery", provider_id="continuation-test"),
        ),
        tools=ToolRegistry(),
    )

    class UnavailableCatalog:
        async def synchronize(self):
            pass

        def snapshot(self):
            raise RuntimeError("catalog storage is unavailable")

    runtime.bind_extensions(catalog=UnavailableCatalog())  # type: ignore[arg-type]

    first = await runtime.reconcile_pending_run_continuations()
    second = await runtime.reconcile_pending_run_continuations()

    restored = store.load_run(session.session_id, run.run_id)
    remaining = store.load_session_record(session.session_id).controls.activations
    assert first == ()
    assert second == ()
    assert restored.continuation is not None
    assert restored.continuation.state == "pending"
    assert restored.continuation.action is None
    assert [item.activation_id for item in remaining] == [activation.activation_id]
    assert "current environment binding failed" in caplog.text
    assert "catalog storage is unavailable" in caplog.text
    with pytest.raises(SessionBusyError, match="unresolved terminal Run continuation"):
        await runtime.run("Do not start a newer Run", session_id=session.session_id)
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_repeated_continuation_settlement_enqueues_once(tmp_path, backend) -> None:
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_goal")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "goal",
        (_policy("dev.ouro.goal", "gen_goal", "continue once"),),
    )
    queue = QueuedInputQueue(store)
    service = RunContinuationService(store=store, registry=registry, queue=queue)

    first = await service.settle(session_id=session.session_id, run_id=run.run_id)
    second = await service.settle(session_id=session.session_id, run_id=run.run_id)

    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert first.settled is True
    assert first.queued is True
    assert second.settled is False
    assert len(queued) == 1
    assert queued[0].queue_id == first.run.continuation.queue_id
    events, _ = store.load_events_page(session.session_id, limit=100)
    assert tuple(events) == first.events
    assert second.events == ()
    assert [event.event_type for event in events] == [
        "follow_up_input_queued",
        "run_continuation_resolved",
    ]
    assert events[0].payload["queue_id"] == queued[0].queue_id


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("superseded", [False, True])
async def test_cancel_after_continuation_commit_uses_run_settlement_without_receipts(
    tmp_path, monkeypatch, backend, superseded
):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(
            controllers=[_controller("dev.example.goal", "goal", "gen_goal")]
        ),
    )
    store.save_run(run)
    queue = QueuedInputQueue(store)
    if superseded:
        queue.enqueue(
            snapshot=store.load_runtime_session(session.session_id),
            prompt="user has priority",
            title=None,
            role="user",
            scaffold=False,
        )
    prompt = "One retained continuation intent. " * 2048
    registry = RunContinuationRegistry()
    registry.replace_owner("goal", (_policy("dev.example.goal", "gen_goal", prompt),))
    service = RunContinuationService(store=store, registry=registry, queue=queue)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    commit = store.commit_session

    def held_commit(request):
        result = commit(request)
        if any(
            item.continuation and item.continuation.state == "resolved" for item in request.runs
        ):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10)
        return result

    monkeypatch.setattr(store, "commit_session", held_commit)
    task = asyncio.create_task(service.settle(session_id=session.session_id, run_id=run.run_id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        release.set()
        result = await asyncio.wait_for(task, timeout=10)
        checkpoint = store.load_run(session.session_id, run.run_id).continuation
        assert checkpoint.state == "resolved" and checkpoint.settlement_plan is None
        assert result.settled and result.queued is (not superseded)
        assert result.diagnostics == checkpoint.settlement_diagnostics
        events, _ = store.load_events_page(session.session_id, limit=100)
        resolutions = [event for event in events if event.event_type == "run_continuation_resolved"]
        assert len(resolutions) == 1
        assert resolutions[0].payload["queued"] is (not superseded)
        assert resolutions[0].payload["diagnostics"] == list(result.diagnostics)
        assert bool(result.diagnostics) is superseded
        assert prompt not in store.load_run(session.session_id, run.run_id).model_dump_json()
        accepted = store.load_input(session.session_id, checkpoint.queue_id)
        if superseded:
            assert accepted is None
        else:
            assert accepted.item.prompt == prompt
        again = await service.settle(session_id=session.session_id, run_id=run.run_id)
        assert not again.settled
        assert len(queue.eligible_session(store.load_session_record(session.session_id))) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_bound_policy_can_settle_after_its_activation_was_released(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    activation = _activation("dev.example.mode", "mode")
    controller = _controller("dev.example.mode", "boundary", "gen-before").model_copy(
        update={
            "active_when_controller_id": activation.controller_id,
            "activation_id": activation.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    outcomes: list[RunOutcome] = []

    def settle_owner(outcome: RunOutcome) -> ContinuationDecision:
        outcomes.append(outcome)
        return ContinuationDecision(action="complete", reason="owner_settled")

    registry = RunContinuationRegistry()
    registry.replace_owner(
        "mode",
        (
            ContinuationPolicyRegistration(
                plugin_id=controller.plugin_id,
                policy_id=controller.policy_id,
                handler=settle_owner,
                generation_id="gen-after",
                execution_contract_hash=controller.execution_contract_hash,
                contract_hash=controller.contract_hash,
                active_when_controller_id=activation.controller_id,
            ),
        ),
    )

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
    ).settle(session_id=session.session_id, run_id=run.run_id)

    assert [outcome.run_id for outcome in outcomes] == [run.run_id]
    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "complete"
    assert settlement.run.continuation.reason == "owner_settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_releasing_controller_cancels_its_queued_continuation(tmp_path, backend) -> None:
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    canonical_controller_id = "dev.ouro.goal/session_controllers/mode"
    activation = SessionControlActivation(
        controller_id=canonical_controller_id,
        plugin_id="dev.ouro.goal",
        controller_local_id="mode",
        plugin_class="user",
        release_id="digest-dev.ouro.goal",
        generation_id="gen_goal",
        execution_contract_hash="execution-dev.ouro.goal",
        claims=[SessionControlClaim(domain="dev.ouro.goal-mode")],
    )
    session = SessionRecord(controls=SessionControlState(revision=1, activations=[activation]))
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_goal").model_copy(
        update={
            "active_when_controller_id": canonical_controller_id,
            "activation_id": activation.activation_id,
        }
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    policy = _policy("dev.ouro.goal", "gen_goal", "continue goal")
    registry.replace_owner(
        "goal",
        (replace(policy, active_when_controller_id=canonical_controller_id),),
    )
    queue = QueuedInputQueue(store)

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=queue,
    ).settle(session_id=session.session_id, run_id=run.run_id)
    assert settlement.queued is True
    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert queued[0].session_control_activation_id == activation.activation_id

    released = SessionControlCoordinator(store).force_release(
        session_id=session.session_id,
        activation_id=activation.activation_id,
    )

    assert released.data["cancelled_queue_ids"] == [queued[0].queue_id]
    events, _ = store.load_events_page(session.session_id, limit=100)
    assert events[-2].event_type == "session_control_transition"
    assert events[-2].payload["phase"] == "applied"
    assert events[-1].event_type == "queued_input_cancelled"
    assert events[-1].payload["queue_id"] == queued[0].queue_id
    assert events[-1].payload["activation_id"] == activation.activation_id
    assert queue.eligible(store.load_runtime_session(session.session_id)) == []


@pytest.mark.asyncio
async def test_continuation_matches_rebuilt_generation_by_execution_contract(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_before_restart")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "goal-after-restart",
        (_policy("dev.ouro.goal", "gen_after_restart", "continue after restart"),),
    )
    queue = QueuedInputQueue(store)

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=queue,
    ).settle(session_id=session.session_id, run_id=run.run_id)

    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert settlement.queued is True
    assert len(queued) == 1
    assert queued[0].generation_id == "gen_before_restart"
    assert queued[0].execution_contract_hash == controller.execution_contract_hash


@pytest.mark.asyncio
async def test_queued_user_input_supersedes_stale_automatic_continuation(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_goal")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "goal",
        (_policy("dev.ouro.goal", "gen_goal", "stale automatic scaffold"),),
    )
    queue = QueuedInputQueue(store)
    queue.enqueue(
        snapshot=store.load_runtime_session(session.session_id),
        prompt="Use this newer user guidance",
        title=None,
        role="user",
        scaffold=False,
    )
    service = RunContinuationService(store=store, registry=registry, queue=queue)

    settlement = await service.settle(session_id=session.session_id, run_id=run.run_id)

    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert settlement.queued is False
    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "pause"
    assert settlement.run.continuation.reason == "superseded_by_queued_input"
    assert [(item.source, item.prompt) for item in queued] == [
        ("user", "Use this newer user guidance")
    ]


@pytest.mark.asyncio
async def test_command_session_input_requires_explicit_permissions() -> None:
    registry = PluginCommandRegistry()
    registry.register(
        PluginCommand(
            plugin_id="dev.ouro.unprivileged",
            name="goal",
            generation_id="gen_unprivileged",
            handler=lambda request: PluginCommandResult(
                command_id=request.command_id,
                session_inputs=[SessionInputRequest(prompt="not authorized")],
            ),
        )
    )

    with pytest.raises(PluginCommandError, match="sessions.write and background.run"):
        await registry.execute("dev.ouro.unprivileged:goal")


@pytest.mark.asyncio
async def test_command_declared_session_requirement_is_host_enforced() -> None:
    registry = PluginCommandRegistry()
    registry.register(
        PluginCommand(
            plugin_id="dev.ouro.session_command",
            name="start",
            requires_session=True,
            handler=lambda _request: "started",
        )
    )

    with pytest.raises(PluginCommandError, match="requires a target Session"):
        await registry.execute("dev.ouro.session_command:start")

    result = await registry.execute(
        "dev.ouro.session_command:start",
        session_id="session-1",
    )
    assert result.output == "started"


@pytest.mark.asyncio
async def test_plugin_command_short_alias_reports_ambiguity(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CountingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="alias", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
    )

    class Commands:
        def list_commands(self):
            return [
                PluginCommandView(
                    command_id="dev.ouro.first:goal",
                    plugin_id="dev.ouro.first",
                    name="goal",
                ),
                PluginCommandView(
                    command_id="dev.ouro.second:goal",
                    plugin_id="dev.ouro.second",
                    name="goal",
                ),
            ]

    with pytest.raises(AmbiguousPluginCommandError) as exc_info:
        await submit_plugin_command(Commands(), runtime, "goal", "target", session_id=None)
    assert store.list_sessions() == []
    assert str(exc_info.value) == (
        "Ambiguous plugin command /goal; use one of: /dev.ouro.first:goal, /dev.ouro.second:goal"
    )
    await runtime.close()


def _controller(
    plugin_id: str,
    policy_id: str,
    generation_id: str,
) -> RunContinuationControllerRef:
    return RunContinuationControllerRef(
        plugin_id=plugin_id,
        policy_id=policy_id,
        version="1.0.0",
        package_digest=f"digest-{plugin_id}",
        generation_id=generation_id,
        contract_hash=f"contract-{plugin_id}",
        execution_contract_hash=f"execution-{plugin_id}",
    )


def _activation(
    plugin_id: str,
    controller_id: str,
) -> SessionControlActivation:
    return SessionControlActivation(
        controller_id=f"{plugin_id}/session_controllers/{controller_id}",
        plugin_id=plugin_id,
        controller_local_id=controller_id,
        plugin_class="user",
        release_id=f"release-{plugin_id}",
        generation_id=f"generation-{plugin_id}",
        execution_contract_hash=f"execution-{plugin_id}",
        claims=[SessionControlClaim(domain=f"{plugin_id}.{controller_id}")],
    )


def _session_controller_registry(
    plugin_id: str,
    execution_contract_hash: str,
    lifecycle_calls: list,
    *,
    generation_id: str = "gen-mode",
) -> PluginSessionControllerRegistry:
    async def deactivate(call):
        lifecycle_calls.append(call)
        return PluginSessionControllerOutput()

    controller = PluginSessionController(
        controller_id="mode",
        description="Own one generic Session mode.",
        claims=(SessionControlClaim(domain=f"{plugin_id}.mode"),),
        activate_handler=lambda _call: PluginSessionControllerOutput(),
        deactivate_handler=deactivate,
        plugin_id=plugin_id,
        plugin_class="user",
        release_id=f"release-{plugin_id}",
        package_digest=f"release-{plugin_id}",
        generation_id=generation_id,
        contract_hash="session-controller-contract",
        execution_contract_hash=execution_contract_hash,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("session-controller", (controller,))
    return registry


def _policy(
    plugin_id: str,
    generation_id: str,
    prompt: str,
    *,
    priority: int = 100,
) -> ContinuationPolicyRegistration:
    def decide(_outcome):
        return ContinuationDecision(action="continue", prompt=prompt)

    return ContinuationPolicyRegistration(
        plugin_id=plugin_id,
        policy_id="goal",
        handler=decide,
        priority=priority,
        generation_id=generation_id,
        execution_contract_hash=f"execution-{plugin_id}",
        contract_hash=f"contract-{plugin_id}",
    )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("continue_run", [False, True])
async def test_continuation_state_queue_and_event_share_native_failure_boundary(
    tmp_path, backend, continue_run
):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_goal")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller] if continue_run else []),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    policy_calls = []
    if continue_run:
        policy = _policy("dev.ouro.goal", "gen_goal", "retained input")

        def decide(outcome):
            policy_calls.append(outcome.run_id)
            return policy.handler(outcome)

        registry.replace_owner("goal", (replace(policy, handler=decide),))
    service = RunContinuationService(store=store, registry=registry, queue=QueuedInputQueue(store))

    with session_connection(store, session.session_id) as connection:
        connection.execute(
            "CREATE TRIGGER fail_continuation_event BEFORE INSERT ON events BEGIN "
            "SELECT RAISE(ABORT, 'continuation event'); END"
        )
    with pytest.raises(PersistenceError):
        await service.settle(session_id=session.session_id, run_id=run.run_id)
    # Files may still own a cross-file input decision, but none of its native
    # state or events can escape a failed transaction.
    with session_connection(store, session.session_id) as connection:
        stored_run = RunRecord.model_validate_json(
            connection.execute(
                "SELECT payload FROM runs WHERE run_id = ?", (run.run_id,)
            ).fetchone()[0]
        )
        stored_session = SessionRecord.model_validate_json(
            connection.execute("SELECT payload FROM sessions").fetchone()[0]
        )
        assert stored_run.continuation.state == "pending"
        assert stored_session.pending_input_ids == ()
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_continuation_event")
    with store.session_mutation(session.session_id):
        pass
    await service.settle(session_id=session.session_id, run_id=run.run_id)

    before = store.load_session(session.session_id)
    repeated = await service.settle(session_id=session.session_id, run_id=run.run_id)
    assert not repeated.settled and repeated.events == ()
    after = store.load_session(session.session_id)
    assert before.events == after.events
    assert [event.event_type for event in after.events] == (
        ["follow_up_input_queued"] if continue_run else []
    ) + ["run_continuation_resolved"]
    assert after.events[-1].payload["action"] == ("continue" if continue_run else "complete")
    assert after.events[-1].payload["queued"] is continue_run
    checkpoint = repeated.run.continuation
    assert checkpoint.state == "resolved" and checkpoint.settlement_plan is None
    assert after.session.pending_input_ids == ((checkpoint.queue_id,) if continue_run else ())
    assert policy_calls == ([run.run_id] if continue_run else [])
    if continue_run:
        assert (
            store.load_input(session.session_id, checkpoint.queue_id).item.prompt
            == "retained input"
        )
