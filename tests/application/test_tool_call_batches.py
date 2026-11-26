import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

import ouro.application.runs.interactions as run_interactions
import ouro.application.tool_calls as tool_calls_module
import ouro.tools.executor as executor_module
from ouro.application.inputs.requests import UserInputRequest
from ouro.application.runs.tool_batches import load_tool_batch
from ouro.application.runtime.service import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_policies import ToolPolicyDecision, ToolPolicyRegistration
from ouro.core.tools import ToolCallRequest, ToolInvocation
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v7 as sdk
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolInputRequest, ToolRegistry, ToolResult
from ouro.tools.builtin.todolist import builtin_todolist_tool
from tests.application.helpers import running_runtime_task
from tests.application.test_user_input_admission import HeldProvider
from tests.application.tool_runtime import batch_runtime
from tests.event_capture import recorded_events
from tests.run_facts import run_tool_results


def _result(invocation, content: str, *, is_error: bool = False) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        is_error=is_error,
    )


@pytest.mark.asyncio
async def test_approval_wait_is_committed_once_without_a_snapshot_rewrite(
    tmp_path, monkeypatch
) -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Deploy", description="Deploy changes.", permission_mode="approval"
            ),
            handler=lambda invocation, context: _result(invocation, "deployed"),
        )
    )
    runtime = batch_runtime(
        tmp_path, registry=registry, first_calls=[ToolCallRequest(id="deploy", name="Deploy")]
    )
    commits = []
    snapshot_writes = []
    original_commit = runtime.store.commit_session
    original_save_run = runtime.store.save_run

    def commit(request):
        commits.extend(run for run in request.runs if run.status == "waiting_approval")
        return original_commit(request)

    def save_run(run):
        if run.status == "waiting_approval":
            snapshot_writes.append(run)
        original_save_run(run)

    monkeypatch.setattr(runtime.store, "commit_session", commit)
    monkeypatch.setattr(runtime.store, "save_run", save_run)
    try:
        result = await runtime.run("deploy")
        persisted = runtime.store.load_run(result.session.session_id, result.run.run_id)

        assert result.run.status == "waiting_approval"
        assert len(commits) == 1
        assert snapshot_writes == []
        assert result.run == persisted
        assert result.run.active_tool_batch == commits[0].active_tool_batch
    finally:
        await runtime.close()


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("drop_notifications", [False, True])
@pytest.mark.asyncio
async def test_modified_approval_starts_from_call_facts_without_embedding_bodies_in_run(
    tmp_path, store_type, monkeypatch, drop_notifications
):
    store = store_type(tmp_path / "sessions.db" if store_type is SQLiteSessionStore else tmp_path)
    store.initialize()
    original = {"text": "original arguments " * 2000}
    approved = {"text": "approved arguments " * 2000}
    content = "actual result body " * 2000
    observed = []
    writes = []
    commit_session = store.commit_session

    def commit(request):
        writes.append(request)
        return commit_session(request)

    monkeypatch.setattr(store, "commit_session", commit)

    def execute(invocation, context):
        durable = store.load_run(invocation.session_id, invocation.run_id)
        identity = durable.active_tool_batch.identity(invocation.run_id, invocation.tool_call_id)
        call = store.load_tool_call(invocation.session_id, identity)
        start = store.load_tool_call_start(invocation.session_id, identity, 1)
        state = store.load_tool_call_state(invocation.session_id, identity)
        assert call.request.arguments == original
        assert start.materialize(call).arguments == approved == invocation.arguments
        assert state.status == "running" and state.attempt == 1
        assert state.prepared_binding is None and state.arguments_override is None
        assert state.approval.modified_arguments is None
        assert state.outcome_attempt is None
        observed.append(identity)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=content,
            structured={"outcome_unknown": True, "answer": "distinct structured value"},
        )

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Write", description="Write once", permission_mode="approval"
            ),
            handler=execute,
        )
    )
    runtime = RuntimeService(
        store=store,
        tools=registry,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {"id": "write_once", "name": "Write", "arguments": original}
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="written"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    if drop_notifications:

        async def discard_notifications(events):
            pass

        monkeypatch.setattr(runtime.event_recorder, "publish_committed", discard_notifications)
    try:
        pending = await runtime.run("write")
        assert pending.run.status == "waiting_approval"
        completed = await runtime.approve_tool(
            session_id=pending.session.session_id,
            approved=True,
            arguments_override=approved,
        )
        assert completed.run.status == "completed", completed.run.error
        assert len(observed) == 1
        identity = observed[0]
        outcome = store.load_tool_outcome(pending.session.session_id, identity, 1)
        assert outcome.value.content == content
        assert outcome.value.structured["answer"] == "distinct structured value"
        state = store.load_tool_call_state(pending.session.session_id, identity)
        assert state.status == "completed"
        assert state.result_message_id is not None and state.proposals is None
        acceptance = next(request for request in writes if request.tool_facts.calls)
        assert acceptance.runs[0].active_tool_batch.call_ids == ("write_once",)
        assert any(step.message and step.message.tool_calls for step in acceptance.steps)
        sid = pending.session.session_id
        live = recorded_events(sid)
        if not drop_notifications:
            requested = next(event for event in live if event.event_type == "tool_call_requested")
            finished = next(event for event in live if event.event_type == "tool_call_completed")
            assert requested.payload["arguments"] == original
            assert finished.payload["result_data"] == outcome.value.structured
        snapshot = store.load_session(sid)
        assert "events" not in snapshot.model_dump()
        assert snapshot.tool_facts.calls[0].request.arguments == original
        assert snapshot.tool_facts.outcomes[0].value.structured == outcome.value.structured
        for kind, expected in (
            ("tool_call_requested", "planned"),
            ("tool_call_started", "running"),
            ("tool_call_completed", "completed"),
        ):
            request = next(
                request
                for request in writes
                if any(event.event_type == kind for event in request.events)
            )
            assert all(state.status == expected for state in request.call_states)
            if kind == "tool_call_completed":
                assert request.tool_facts.outcomes
                assert (
                    store.load_tool_outcome(
                        sid, identity, request.events[-1].payload["outcome_attempt"]
                    )
                    is not None
                )
        assert any(event.event_type == "tool_call_requested" for event in acceptance.events)
        for request in writes:
            for run in request.runs:
                payload = run.model_dump_json()
                assert original["text"] not in payload
                assert approved["text"] not in payload
                assert content not in payload
    finally:
        await runtime.close()


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.asyncio
async def test_tool_wait_persists_the_question_and_resumes_committed_state(
    tmp_path, store_type
) -> None:
    seen = []

    def begin(call: sdk.ToolCall) -> sdk.ToolInputRequest:
        seen.append("begin")
        call.session_state.replace(state_schema_version=1, payload={"stage": "waiting"})
        return sdk.ToolInputRequest(data={"question": "continue?"})

    def resume(call: sdk.ToolResumeCall) -> sdk.ToolOutput:
        seen.append(call.session_state.read().payload)
        call.session_state.replace(state_schema_version=1, payload={"stage": "finished"})
        return sdk.ToolOutput(content="accepted")

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="workflow",
                    exposed_name="workflow__advance",
                    description="Advance workflow.",
                    permission_mode="auto",
                    handler=begin,
                    resume_handler=resume,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path
    )
    store.initialize()
    runtime = RuntimeService(
        store=store,
        tools=registry,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {"id": "workflow", "name": "workflow__advance", "arguments": {}}
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="done"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    try:
        waiting = await runtime.run("advance")
        assert waiting.run.status == "waiting_user"
        batch = load_tool_batch(store, waiting.run)
        assert batch is not None
        assert batch.calls[0].input_request.harness_change is None
        assert batch.calls[0].input_request.data == {"question": "continue?"}
        persisted = store.load_run(waiting.session.session_id, waiting.run.run_id)
        assert persisted.active_tool_batch == batch.head()

        completed = await runtime.answer_user(
            session_id=waiting.session.session_id,
            run_id=waiting.run.run_id,
            answer={"accepted": True},
        )
        assert completed.run.status == "completed"
        assert seen == ["begin", {"stage": "waiting"}]
        assert run_tool_results(store, completed)[0].content == "accepted"
    finally:
        await runtime.close()


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("second_changes_state", [False, True])
async def test_questions_in_one_batch_cannot_publish_from_a_stale_harness(
    tmp_path, store_type, second_changes_state
):
    def ask(call: sdk.ToolCall) -> sdk.ToolInputRequest:
        original = call.session_state.read()
        if call.tool_call_id == "first" or second_changes_state:
            call.session_state.replace(state_schema_version=1, payload={"stage": call.tool_call_id})
        return sdk.ToolInputRequest(
            data={"question": call.tool_call_id, "was_empty": original is None}
        )

    tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="ask",
                    exposed_name="WorkflowQuestion",
                    description="Ask about this step.",
                    handler=ask,
                    permission_mode="auto",
                )
            ]
        ),
        owner_id="example.workflow",
    ).tools[0]
    registry = ToolRegistry()
    registry.register(tool)
    async with batch_runtime(
        tmp_path,
        store_type=store_type,
        registry=registry,
        mode="sequential",
        first_calls=[
            ToolCallRequest(id=key, name="WorkflowQuestion", source_index=index)
            for index, key in enumerate(("first", "second"))
        ],
    ) as app:
        result = await app.run("ask about both steps")
        assert app.store.load_harness(result.session.session_id).state.states[0].payload == {
            "stage": "first"
        }
        assert result.run.status == "failed"
        assert "SessionCommitConflict" in result.run.error


def test_completed_tool_result_timing_uses_monotonic_elapsed(monkeypatch) -> None:
    started_at = utc_now()
    result = ToolResult(tool_call_id="call_timed", tool_name="TimedTool")
    monkeypatch.setattr(tool_calls_module, "monotonic", lambda: 104.25)

    completed = tool_calls_module._complete_tool_result_timing(
        result,
        started=(started_at, 100.0),
    )

    assert completed.started_at == started_at
    assert completed.ended_at == started_at + timedelta(seconds=4.25)


@pytest.mark.parametrize(
    ("arguments", "permission", "expected_error"),
    [
        ({"value": 1}, "auto", None),
        ({"value": "invalid"}, "auto", "ToolArgumentValidationError"),
        ({"value": 1}, "deny", "ToolPermissionDenied"),
        ({"value": 1}, "approval", None),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("with_policy", [True, False])
async def test_tool_arguments_checked_once_per_admission_boundary(
    tmp_path, monkeypatch, arguments, permission, expected_error, with_policy
):
    registry = ToolRegistry()
    executed = []

    def execute(invocation, context):
        executed.append(invocation)
        return _result(invocation, "done")

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Example",
                description="Example tool.",
                permission_mode=permission,
                parameters_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            ),
            handler=execute,
        )
    )
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="example", name="Example", arguments=arguments)],
    )
    if with_policy:
        runtime.registries.tool_policies.replace_owner(
            "guard",
            (ToolPolicyRegistration("guard", "observe", lambda call: ToolPolicyDecision()),),
        )
    validations = []
    validate = executor_module.tool_argument_error

    def policy_argument_error(definition, arguments):
        validations.append(definition.name)
        return validate(definition, arguments)

    monkeypatch.setattr(executor_module, "tool_argument_error", policy_argument_error)
    try:
        result = await runtime.run("run example")
        assert validations == ["Example"] * (2 if executed else 1)
        assert len(executed) == int(permission == "auto" and expected_error is None)
        if permission == "approval":
            assert result.run.status == "waiting_approval"
        else:
            assert result.run.status == "completed"
            assert (
                run_tool_results(runtime.store, result)[0].structured.get("error_type")
                == expected_error
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_session_permission_changed_between_siblings_gates_the_next_start(tmp_path):
    executed = []

    def act(invocation, context):
        executed.append(invocation.tool_call_id)
        runtime.set_session_approval_mode(invocation.session_id, "deny")
        return _result(invocation, "done")

    registry = ToolRegistry()
    registry.register(Tool(definition=ToolDefinition(name="Act", description="Act"), handler=act))
    calls = [ToolCallRequest(id=str(i), name="Act", source_index=i) for i in range(2)]
    async with batch_runtime(tmp_path, registry=registry, first_calls=calls) as runtime:
        result = await runtime.run("act")
        assert result.run.status == "completed", result.run.error
        assert executed == ["0"]
        first, second = run_tool_results(runtime.store, result)
        assert not first.is_error and second.structured["error_type"] == "ToolPermissionDenied"
        assert [
            event.payload["tool_call_id"]
            for event in recorded_events(result.session.session_id)
            if event.event_type == "tool_call_started"
        ] == ["0"]


@pytest.mark.asyncio
async def test_empty_policy_registry_preserves_control_refresh_failures(tmp_path):
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="Example", description="Example"),
            handler=lambda invocation, context: _result(invocation, "done"),
        )
    )
    runtime = batch_runtime(tmp_path, registry=registry, first_calls=[])
    session = SessionRecord()
    runtime.store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    coordinator = runtime._tool_calls_for_run(run.run_id)
    refreshed = []

    def refresh(controls):
        refreshed.append(controls)
        return "Required Session controls are unavailable"

    coordinator.refresh_controls = refresh
    invocation = ToolInvocation(
        session_id=session.session_id, run_id=run.run_id, tool_name="Example"
    )
    try:
        _, outcome = await coordinator._prepare_call(
            session=session, run=run, invocation=invocation, phase="execution_boundary"
        )
        assert refreshed == [session.controls]
        assert outcome.result.structured == {
            "error_type": "ToolPermissionDenied",
            "error_message": "Required Session controls are unavailable",
        }
        assert not recorded_events(session.session_id)
    finally:
        await runtime.close()


def test_runtime_publishes_permission_updates_to_existing_and_future_coordinators(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="Deploy", description="Deploy changes."),
            handler=lambda invocation, context: _result(invocation, "deployed"),
        )
    )
    runtime = batch_runtime(tmp_path, registry=registry, first_calls=[])
    existing = runtime._tool_calls_for_run("existing").executor
    invocation = ToolInvocation(
        session_id="session",
        run_id="existing",
        tool_name="Deploy",
    )

    runtime.replace_tool_permission_overrides(
        {"Deploy": "deny"},
        {"dev.ouro.deploy": "approval"},
    )

    assert existing.preflight(invocation).invocation.permission_mode == "deny"
    future = runtime._tool_calls_for_run("future").executor
    assert future.preflight(invocation).invocation.permission_mode == "deny"
    assert runtime.tool_permission_overrides() == {"Deploy": "deny"}
    assert runtime.plugin_permission_overrides() == {"dev.ouro.deploy": "approval"}


@pytest.mark.asyncio
async def test_parallel_batch_preserves_one_assistant_message_and_source_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    completed: list[str] = []
    first_started = asyncio.Event()
    second_completed = asyncio.Event()

    async def first(invocation, context) -> ToolResult:
        first_started.set()
        await asyncio.wait_for(second_completed.wait(), timeout=1.0)
        completed.append("first")
        return _result(invocation, "first result")

    async def second(invocation, context) -> ToolResult:
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        completed.append("second")
        second_completed.set()
        return _result(invocation, "second result")

    registry.register(
        Tool(definition=ToolDefinition(name="First", description="first"), handler=first)
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Second", description="second"), handler=second)
    )
    registry.register(builtin_todolist_tool())
    todos = [{"title": "Run both tools", "status": "in_progress"}]
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="First", source_index=0),
            ToolCallRequest(id="call_2", name="Second", source_index=1),
            ToolCallRequest(
                id="call_todo", name="TodoList", arguments={"todos": todos}, source_index=2
            ),
        ],
    )
    tool_result_batches: list[list[str]] = []
    commit_session = runtime.store.commit_session

    def capture_message_batch(commit):
        result_ids = [
            step.message.tool_result.tool_call_id
            for step in commit.steps
            if step.message is not None and step.message.tool_result is not None
        ]
        if result_ids:
            tool_result_batches.append(result_ids)
        return commit_session(commit)

    monkeypatch.setattr(runtime.store, "commit_session", capture_message_batch)
    planned_states: list[tuple[str, str, list[str]]] = []

    def capture_planned_state(request):
        committed = capture_message_batch(request)
        event = next(
            (event for event in committed.events if event.event_type == "tool_batch_planned"), None
        )
        if event is not None:
            assert event.run_id is not None
            durable_run = runtime.store.load_run(event.session_id, event.run_id)
            assert durable_run.active_tool_batch is not None
            planned_states.append(
                (
                    durable_run.phase,
                    durable_run.active_tool_batch.phase,
                    [call.status for call in load_tool_batch(runtime.store, durable_run).calls],
                )
            )
        return committed

    monkeypatch.setattr(runtime.store, "commit_session", capture_planned_state)

    result = await runtime.run("run both")
    snapshot = runtime.store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert not any(item.is_error for item in run_tool_results(runtime.store, result))
    assert run_tool_results(runtime.store, result)[2].structured == {"todos": todos}
    assert not runtime.store.load_harness(result.session.session_id).state.states
    assert completed == ["second", "first"]
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
        "tool_result",
        "tool_result",
        "assistant",
    ]
    call_ids = ["call_1", "call_2", "call_todo"]
    assert [call.id for call in snapshot.messages[1].tool_calls] == call_ids
    assert planned_states == [("tool_preflight", "planned", ["planned"] * 3)]
    assert [
        message.tool_result.tool_call_id
        for message in snapshot.messages[2:5]
        if message.tool_result is not None
    ] == call_ids
    assert tool_result_batches == [call_ids]
    tool_timings = [message.tool_execution for message in snapshot.messages[2:5]]
    assert all(timing is not None for timing in tool_timings)
    assert all(
        timing.ended_at >= timing.started_at for timing in tool_timings if timing is not None
    )
    event_types = [event.event_type for event in recorded_events(snapshot.session.session_id)]
    committing_index = event_types.index("tool_batch_results_committing")
    assert committing_index > max(
        index for index, event_type in enumerate(event_types) if event_type == "tool_call_completed"
    )
    assert committing_index < event_types.index("tool_batch_completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_exclusive_tool_rejects_mixed_batch_before_any_handler_runs(tmp_path, mode) -> None:
    registry = ToolRegistry()
    executed: list[str] = []

    def execute(invocation, context) -> ToolResult:
        executed.append(invocation.tool_name)
        return _result(invocation, "must not execute")

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="EnterWorkflow",
                description="Enter a workflow.",
                batch_behavior="exclusive",
            ),
            handler=execute,
        )
    )
    registry.register(
        Tool(
            definition=ToolDefinition(name="Write", description="Write data."),
            handler=execute,
        )
    )
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        mode=mode,
        first_calls=[
            ToolCallRequest(id="call_enter", name="EnterWorkflow", source_index=0),
            ToolCallRequest(id="call_write", name="Write", source_index=1),
        ],
    )

    result = await runtime.run("enter and write")
    snapshot = runtime.store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert executed == []
    assert [item.structured["error_type"] for item in run_tool_results(runtime.store, result)] == [
        "ExclusiveToolBatchViolation",
        "ExclusiveToolBatchViolation",
    ]
    assert all(
        "No tools were executed." in item.text_content
        for item in run_tool_results(runtime.store, result)
    )
    assert all(
        item.structured["exclusive_tools"] == ["EnterWorkflow"]
        for item in run_tool_results(runtime.store, result)
    )
    assert any(
        event.event_type == "tool_batch_rejected"
        for event in recorded_events(snapshot.session.session_id)
    )


@pytest.mark.asyncio
async def test_sdk_tool_structured_input_round_trips_through_wait_and_resume(tmp_path) -> None:
    requested = {
        "type": "plan_confirmation",
        "plan": {"title": "Release", "steps": ["test", "publish"]},
        "choices": [
            {"id": "accept", "label": "Accept"},
            {"id": "revise", "label": "Revise"},
        ],
    }

    def confirm_plan(_call: sdk.ToolCall) -> sdk.ToolInputRequest:
        return sdk.ToolInputRequest(data=requested)

    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="confirm_plan",
                    exposed_name="plans__confirm",
                    description="Confirm a plan.",
                    permission_mode="auto",
                    handler=confirm_plan,
                )
            ]
        ),
    )
    registry = ToolRegistry()
    registry.register(contributions.tools[0])
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_confirm", name="plans__confirm")],
    )

    pending = await runtime.run("confirm the plan")
    durable_pending = runtime.store.load_run(pending.session.session_id, pending.run.run_id)

    assert pending.run.status == "waiting_user"
    assert durable_pending.active_tool_batch is not None
    waiting_call = load_tool_batch(runtime.store, durable_pending).calls[0]
    assert waiting_call.status == "waiting_user"
    assert waiting_call.result is None
    assert waiting_call.input_request is not None
    assert waiting_call.input_request.data == requested

    answer = {"choice": "revise", "notes": ["Add rollback", "Notify support"]}
    resumed = await runtime.answer_user(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        answer=answer,
    )
    snapshot = runtime.store.load_session(pending.session.session_id)
    result_message = next(message for message in snapshot.messages if message.role == "tool_result")
    resolved_event = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "ask_user_resolved"
    )
    assert resumed.run.status == "completed"
    assert run_tool_results(runtime.store, resumed)[0].structured == {"answer": answer}
    assert result_message.text_content == json.dumps(
        answer,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert resolved_event.payload["answer"] == answer


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_unattended_session_resumes_capable_tool_without_waiting(
    tmp_path,
    mode: str,
) -> None:
    resolutions: list[sdk.ToolInputResolution] = []
    executed_after_request: list[str] = []

    def request_input(_call: sdk.ToolCall) -> sdk.ToolInputRequest:
        return sdk.ToolInputRequest(data={"type": "confirm", "subject": "release"})

    def resume(call: sdk.ToolResumeCall) -> sdk.ToolOutput:
        resolutions.append(call.resolution)
        return sdk.ToolOutput(
            content="continued without user input",
            structured={"resolved_by": call.resolution.resolved_by},
        )

    def after_request(invocation, context) -> ToolResult:
        executed_after_request.append(invocation.tool_call_id)
        return _result(invocation, "continued batch")

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="confirm",
                    exposed_name="afk__confirm",
                    description="Confirm or apply unattended policy.",
                    permission_mode="auto",
                    handler=request_input,
                    resume_handler=resume,
                    supports_unattended=True,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    registry.register(
        Tool(
            definition=ToolDefinition(name="AfterAFK", description="Continue the batch."),
            handler=after_request,
        )
    )
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        mode=mode,
        first_calls=[
            ToolCallRequest(id="call_confirm", name="afk__confirm", source_index=0),
            ToolCallRequest(id="call_after_1", name="AfterAFK", source_index=1),
            ToolCallRequest(id="call_after_2", name="AfterAFK", source_index=2),
        ],
    )

    result = await runtime.run(
        "continue unattended",
        approval_mode="auto",
        interaction_mode="unattended",
    )
    snapshot = runtime.store.load_session(result.session.session_id)
    resolved_event = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "ask_user_resolved"
    )
    requested_event = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "ask_user_requested"
    )

    assert result.run.status == "completed"
    assert sorted(executed_after_request) == ["call_after_1", "call_after_2"]
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert isinstance(resolution, sdk.UnattendedResolution)
    assert resolution.resolver_id == "ouro.afk"
    assert [
        event.payload["attempt"]
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "tool_call_started"
        and event.payload["tool_call_id"] == "call_confirm"
    ] == [1, 2]
    assert requested_event.payload["interaction_mode"] == "unattended"
    assert requested_event.payload["will_suspend"] is False
    assert resolved_event.payload["resolution"]["kind"] == "unattended"
    assert resolved_event.payload["resolved_by"] == "host_policy"
    assert all(
        event.event_type != "run_status_changed"
        for event in recorded_events(snapshot.session.session_id)
    )


@pytest.mark.asyncio
async def test_unattended_session_returns_explicit_error_for_unsupported_tool(tmp_path) -> None:
    def request_input(_call: sdk.ToolCall) -> sdk.ToolInputRequest:
        return sdk.ToolInputRequest(data={"type": "form", "fields": ["environment"]})

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="form",
                    exposed_name="afk__form",
                    description="Collect a form from the user.",
                    permission_mode="auto",
                    handler=request_input,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_form", name="afk__form")],
    )

    result = await runtime.run(
        "collect details unattended",
        approval_mode="auto",
        interaction_mode="unattended",
    )

    assert result.run.status == "completed"
    assert len(run_tool_results(runtime.store, result)) == 1
    assert (
        run_tool_results(runtime.store, result)[0].structured["error_type"]
        == "UserInputUnavailable"
    )
    assert [
        event.payload["attempt"]
        for event in recorded_events(result.session.session_id)
        if event.event_type == "tool_call_started"
    ] == [1]
    assert run_tool_results(runtime.store, result)[0].structured["resolved_by"] == "host_policy"
    assert run_tool_results(runtime.store, result)[0].structured["input_request"] == {
        "type": "form",
        "fields": ["environment"],
    }


def _confirming_runtime(tmp_path, invoked, resume_handler=None):
    def request(invocation, context):
        invoked.append("execute")
        return ToolInputRequest(data={"question": "Confirm?"})

    def resume(invocation, resolution, request, context):
        invoked.append("resume")
        return _result(invocation, "resumed")

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Confirm",
                description="Requires input.",
                resumable=True,
                supports_unattended=True,
            ),
            handler=request,
            resume_handler=resume_handler or resume,
        )
    )
    return batch_runtime(
        tmp_path, registry=registry, first_calls=[ToolCallRequest(id="confirm", name="Confirm")]
    )


async def test_parallel_policy_wait_leaves_sibling_free_to_start(tmp_path):
    checking, release, read_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    checked = []
    started = []

    async def policy(call):
        if call.tool_call_id == "guarded" and call.tool_call_id in checked:
            checking.set()
            await release.wait()
            return ToolPolicyDecision(action="deny", reason="revoked before start")
        checked.append(call.tool_call_id)
        return ToolPolicyDecision()

    async def read(invocation, context):
        started.append(invocation.tool_call_id)
        read_started.set()
        return _result(invocation, "ok")

    tools = ToolRegistry()
    tools.register(Tool(definition=ToolDefinition(name="Probe", description="Read"), handler=read))
    async with batch_runtime(
        tmp_path,
        registry=tools,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="guarded", name="Probe"),
            ToolCallRequest(id="read", name="Probe", source_index=1),
        ],
    ) as app:
        app.registries.tool_policies.replace_owner(
            "guard", (ToolPolicyRegistration("guard", "check", policy, timeout_seconds=30),)
        )
        task = asyncio.create_task(app.run("inspect both"))
        try:
            await asyncio.wait_for(checking.wait(), 10)
            await asyncio.wait_for(read_started.wait(), 3)
        finally:
            release.set()
            result = await task
        assert result.run.status == "completed" and started == ["read"]
        assert [r.is_error for r in run_tool_results(app.store, result)] == [True, False]


@pytest.mark.parametrize("interaction_mode", ["interactive", "unattended"])
async def test_resume_honors_policy_tightened_by_initial_call(tmp_path, interaction_mode):
    invoked = []

    def guard(call):
        if invoked:
            return ToolPolicyDecision(action="deny", reason="permission revoked")
        return ToolPolicyDecision()

    async with _confirming_runtime(tmp_path, invoked) as runtime:
        runtime.registries.tool_policies.replace_owner(
            "guard", (ToolPolicyRegistration("guard", "revoke", guard),)
        )
        result = await runtime.run("confirm", interaction_mode=interaction_mode)
        if interaction_mode == "interactive":
            assert result.run.status == "waiting_user"
            result = await runtime.answer_user(
                session_id=result.session.session_id, run_id=result.run.run_id, answer=True
            )
        assert result.run.status == "completed", result.run.error
        assert invoked == ["execute"]
        assert [
            event.payload["attempt"]
            for event in recorded_events(result.session.session_id)
            if event.event_type == "tool_call_started"
        ] == [1]
        feedback = run_tool_results(runtime.store, result)
        assert len(feedback) == 1
        assert feedback[0].structured["error_type"] == "ToolPermissionDenied"
        assert "permission revoked" in feedback[0].text_content


@pytest.mark.parametrize("legacy_resume_start", [False, True])
async def test_recovery_uses_persisted_resume_start_after_unattended_request(
    tmp_path, monkeypatch, legacy_resume_start
):
    invoked = []
    async with _confirming_runtime(tmp_path, invoked) as runtime:
        persist = runtime.run_lifecycle.save_batch_state

        def interrupt_after_request(**kwargs):
            if kwargs["batch"].calls[0].status != "resolving_input":
                return persist(**kwargs)
            if legacy_resume_start:
                kwargs["started_call_ids"] = frozenset({"confirm"})
            persist(**kwargs)
            raise RuntimeError("interrupted after the input request")

        monkeypatch.setattr(runtime.run_lifecycle, "save_batch_state", interrupt_after_request)
        result = await runtime.run("confirm", interaction_mode="unattended")
        assert result.run.status == "failed"
        snapshot = await runtime.run_recovery.reconcile(
            runtime.store.load_runtime_session(result.session.session_id)
        )
        run = next(item for item in snapshot.runs if item.run_id == result.run.run_id)
        call = load_tool_batch(runtime.store, run).calls[0]
        assert call.status == ("unknown_outcome" if legacy_resume_start else "aborted")
        assert call.attempt == (2 if legacy_resume_start else 1)
        assert invoked == ["execute"]


async def test_abort_during_answer_stops_resume_and_retains_unknown_outcome(tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def resume(invocation, resolution, request, context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with _confirming_runtime(tmp_path, [], resume_handler=resume) as runtime:
        waiting = await runtime.run("confirm")
        target = {"session_id": waiting.session.session_id, "run_id": waiting.run.run_id}
        answer = asyncio.create_task(runtime.answer_user(**target, answer=True))
        try:
            async with asyncio.timeout(5):
                await started.wait()
                await runtime.abort_run(**target)
                result = await answer
            assert result.run.status == "aborted"
            assert stopped.is_set()
            batch = load_tool_batch(runtime.store, result.run)
            assert batch.calls[0].attempt == 2
            assert batch.calls[0].status == "unknown_outcome"
        finally:
            answer.cancel()
            await asyncio.gather(answer, return_exceptions=True)


@pytest.mark.asyncio
async def test_unattended_resume_returns_the_question_that_still_needs_input(tmp_path) -> None:
    def request_input(_call: sdk.ToolCall) -> sdk.ToolInputRequest:
        return sdk.ToolInputRequest(data={"stage": "first"})

    def resume(_call: sdk.ToolResumeCall) -> sdk.ToolInputRequest:
        return sdk.ToolInputRequest(data={"stage": "second"})

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="loop",
                    exposed_name="afk__loop",
                    description="Invalid unattended interaction loop.",
                    permission_mode="auto",
                    handler=request_input,
                    resume_handler=resume,
                    supports_unattended=True,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_loop", name="afk__loop")],
    )

    result = await runtime.run(
        "do not loop",
        approval_mode="auto",
        interaction_mode="unattended",
    )

    assert result.run.status == "completed"
    assert (
        run_tool_results(runtime.store, result)[0].structured["error_type"]
        == "UserInputUnavailable"
    )
    assert run_tool_results(runtime.store, result)[0].structured["input_request"] == {
        "stage": "second"
    }


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.asyncio
async def test_resumable_tool_can_request_multiple_inputs_and_owns_final_result(
    tmp_path, monkeypatch, store_type
) -> None:
    initial_calls = 0
    resume_calls: list[tuple[object, object]] = []

    def begin(_call: sdk.ToolCall) -> sdk.ToolInputRequest:
        nonlocal initial_calls
        initial_calls += 1
        return sdk.ToolInputRequest(data={"stage": "first"})

    def resume(call: sdk.ToolResumeCall) -> sdk.ToolInputRequest | sdk.ToolOutput:
        resume_calls.append((call.request, call.answer))
        if call.request == {"stage": "first"}:
            return sdk.ToolInputRequest(data={"stage": "second"})
        return sdk.ToolOutput(
            content="workflow accepted",
            structured={"accepted": call.answer},
        )

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="workflow",
                    exposed_name="workflow__advance",
                    description="Advance an interactive workflow.",
                    permission_mode="auto",
                    handler=begin,
                    resume_handler=resume,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_workflow", name="workflow__advance")],
        store_type=store_type,
    )

    first = await runtime.run("advance")
    first_question = runtime.parked_runs.pending_question(
        first.session.session_id, first.run.run_id
    )

    def unavailable_harness(*_args, **_kwargs):
        raise OSError("question state unavailable")

    with monkeypatch.context() as failing:
        failing.setattr(run_interactions, "capture_session_harness", unavailable_harness)
        with pytest.raises(OSError, match="question state unavailable"):
            await runtime.answer_user(
                session_id=first.session.session_id, run_id=first.run.run_id, answer="retry"
            )
    assert first.run.run_id not in runtime._run_resources
    assert not runtime.store.is_session_locked(first.session.session_id)
    assert not resume_calls
    batch_reads = []

    def read_batch(store, run):
        batch_reads.append(run.run_id)
        assert store.is_session_locked(run.session_id)
        return load_tool_batch(store, run)

    monkeypatch.setattr(run_interactions, "load_tool_batch", read_batch)
    commits = []
    commit_session = runtime.store.commit_session

    def commit(request):
        commits.append(request)
        return commit_session(request)

    monkeypatch.setattr(runtime.store, "commit_session", commit)
    held = HeldProvider()
    other_store = store_type(
        tmp_path / "sessions.db" if store_type is SQLiteSessionStore else tmp_path
    )
    async with RuntimeService(
        store=other_store,
        tools=registry,
        adapter=RuntimeAdapter(held, ModelMetadata(model_id="faux", provider_id="faux")),
    ) as other_host:
        supervisor = RuntimeSupervisor(other_host, max_concurrency=1)
        try:
            blocker = await supervisor.submit_user_input(UserInputRequest(prompt="occupy permit"))
            assert await asyncio.wait_for(held.started.get(), 5) == "occupy permit"
            target = {"session_id": first.session.session_id, "run_id": first.run.run_id}
            stale = await supervisor.resolve_tool_input(**target, answer="queued answer for first")
            assert supervisor.status(stale).status == "queued"
            second = await runtime.answer_user(**target, answer={"choice": "next"})
            assert batch_reads == [first.run.run_id]
            held.release.set()
            await asyncio.wait_for(blocker.wait(), 10)
            failed = await asyncio.wait_for(supervisor.wait(stale), 10)
            assert (
                failed.status == "failed" and failed.diagnostics[-1].code == "stale_tool_question"
            )
            assert other_store.load_run(**target) == second.run
            assert resume_calls == [({"stage": "first"}, {"choice": "next"})]
        finally:
            held.release.set()
            await supervisor.shutdown()
    assert batch_reads == [first.run.run_id, first.run.run_id]
    with monkeypatch.context() as pending:
        pending.setattr(runtime.parked_runs, "pending_question", lambda *_args: first_question)
        with pytest.raises(ValueError, match="question changed"):
            await runtime.answer_user(
                session_id=first.session.session_id, run_id=first.run.run_id, answer="stale"
            )
    waiting = [
        request for request in commits if any(run.status == "waiting_user" for run in request.runs)
    ]
    assert len(waiting) == 1
    assert waiting[0].runs[0].active_tool_batch.phase == "awaiting_interaction"
    assert any(event.event_type == "ask_user_requested" for event in waiting[0].events)
    assert runtime.store.load_run(second.session.session_id, second.run.run_id) == second.run
    completed = await runtime.answer_user(
        session_id=second.session.session_id,
        run_id=second.run.run_id,
        answer={"choice": "accept"},
    )

    assert first.run.status == "waiting_user"
    assert second.run.status == "waiting_user"
    assert completed.run.status == "completed"
    assert initial_calls == 1
    assert resume_calls == [
        ({"stage": "first"}, {"choice": "next"}),
        ({"stage": "second"}, {"choice": "accept"}),
    ]
    assert run_tool_results(runtime.store, completed)[0].content == "workflow accepted"
    assert run_tool_results(runtime.store, completed)[0].structured == {
        "accepted": {"choice": "accept"}
    }


@pytest.mark.asyncio
async def test_approved_tool_can_transition_to_user_input_without_reexecution(tmp_path) -> None:
    executions = 0

    def request_input(_invocation):
        nonlocal executions
        executions += 1
        return sdk.ToolInputRequest(data={"type": "confirm", "value": "deploy"})

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="deploy",
                    exposed_name="deploy__confirm",
                    description="Approve execution, then collect deployment details.",
                    permission_mode="approval",
                    handler=request_input,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_deploy", name="deploy__confirm")],
    )

    approval = await runtime.run("deploy")
    waiting = await runtime.approve_tool(
        session_id=approval.session.session_id,
        run_id=approval.run.run_id,
        approved=True,
    )
    completed = await runtime.answer_user(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        answer={"environment": "staging"},
    )

    assert approval.run.status == "waiting_approval"
    assert waiting.run.status == "waiting_user"
    assert completed.run.status == "completed"
    assert executions == 1


@pytest.mark.asyncio
async def test_unattended_session_resolves_remaining_approval_with_policy_provenance(
    tmp_path,
) -> None:
    executions = 0

    def execute(call: sdk.ToolCall) -> sdk.ToolOutput:
        nonlocal executions
        executions += 1
        return sdk.ToolOutput(content=f"executed {call.tool_name}")

    contribution = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="deploy",
                    exposed_name="afk__deploy",
                    description="Deployment requiring approval.",
                    permission_mode="approval",
                    handler=execute,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(contribution)
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_deploy", name="afk__deploy")],
    )

    result = await runtime.run(
        "deploy unattended",
        approval_mode="approval",
        interaction_mode="unattended",
    )
    snapshot = runtime.store.load_session(result.session.session_id)
    resolved_event = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "approval_resolved"
    )

    assert result.run.status == "completed"
    assert executions == 1
    assert resolved_event.payload["approved"] is True
    assert resolved_event.payload["resolved_by"] == "host_policy"
    assert resolved_event.payload["resolver_id"] == "ouro.afk"


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_ask_user_and_independent_read_finish_in_one_model_exchange(tmp_path, mode):
    from ouro.tools import builtin_tools

    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    path = tmp_path / "notes.txt"
    path.write_text("independent content", encoding="utf-8")
    questions = [
        {
            "id": "color",
            "header": "Color",
            "question": "Which color?",
            "options": [
                {"label": "Red", "description": "Use red."},
                {"label": "Blue", "description": "Use blue."},
            ],
        }
    ]
    app = batch_runtime(
        tmp_path / "data",
        registry=registry,
        mode=mode,
        first_calls=[
            ToolCallRequest(id="q", name="AskUser", arguments={"questions": questions}),
            ToolCallRequest(id="read", name="Read", arguments={"path": str(path)}, source_index=1),
        ],
    )
    async with app:
        waiting = await app.run("Ask and read", approval_mode="auto")
        batch = load_tool_batch(app.store, waiting.run)
        assert [c.status for c in batch.calls] == ["waiting_user", "completed"]
        assert "independent content" in batch.calls[1].result.content
        assert waiting.run.model_round_count == 1
        done = await app.answer_user(
            session_id=waiting.session.session_id,
            run_id=waiting.run.run_id,
            answer={"answers": {"color": ["Blue"]}},
        )
        assert done.run.status == "completed", done.run.error
        assert done.run.model_round_count == 2
        history = app.store.load_session(done.session.session_id)
        assert [m.tool_result.tool_call_id for m in history.messages if m.tool_result] == [
            "q",
            "read",
        ]


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_two_resumable_calls_release_slots_and_accept_versioned_answers_in_reverse(
    tmp_path, store_type, monkeypatch
):
    from ouro.application.observations.activity import project_batch_interactions

    entered = asyncio.Barrier(2)
    starts = []

    async def begin(call):
        starts.append(call.tool_call_id)
        await entered.wait()
        return sdk.ToolInputRequest(data={"id": call.tool_call_id})

    def resume(call):
        if call.answer == "again":
            return sdk.ToolInputRequest(data={"confirm": True})
        return sdk.ToolOutput(content=call.answer)

    registry = ToolRegistry()
    registry.register(
        adapt_sdk_contributions(
            owner_id="example.flow",
            contributions=sdk.ContributionSet(
                tools=[
                    sdk.ToolContribution(
                        id="ask",
                        exposed_name="flow__ask",
                        description="Collect a choice",
                        handler=begin,
                        resume_handler=resume,
                        permission_mode="auto",
                    )
                ]
            ),
        ).tools[0]
    )
    registry.register(
        Tool(
            definition=ToolDefinition(name="Read", description="Read", effect="read_only"),
            handler=lambda inv, ctx: _result(inv, "independent result"),
        )
    )
    ids = ["q1", "q2", "read"]
    app = batch_runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        store_type=store_type,
        first_calls=[
            ToolCallRequest(id=key, name=name, source_index=i)
            for i, (key, name) in enumerate(
                zip(ids, ["flow__ask", "flow__ask", "Read"], strict=True)
            )
        ],
    )
    published_frames = []
    commit = app.store.commit_session

    def observe_commit(request):
        committed = commit(request)
        frame = [
            step.message.outcome_ref.identity.tool_call_id
            for step in request.steps
            if step.message and step.message.outcome_ref
        ]
        if frame:
            published_frames.append(frame)
        return committed

    monkeypatch.setattr(app.store, "commit_session", observe_commit)
    async with app:
        waiting = await asyncio.wait_for(app.run("ask twice and read"), 30)
        batch = load_tool_batch(app.store, waiting.run)
        assert [c.status for c in batch.calls] == ["waiting_user", "waiting_user", "completed"]
        q1, q2 = [p.question for p in project_batch_interactions(waiting.run, batch)]
        target = {"session_id": waiting.session.session_id, "run_id": waiting.run.run_id}
        again = await app.answer_user(**target, question=q2, answer="again")
        revised = project_batch_interactions(again.run, load_tool_batch(app.store, again.run))[
            1
        ].question
        assert revised.input_revision == q2.input_revision + 1
        with pytest.raises(run_interactions.StaleToolQuestion):
            await app.answer_user(**target, question=q2, answer="stale")
        middle = await app.answer_user(**target, question=revised, answer="second")
        assert load_tool_batch(app.store, middle.run).calls[1].result.content == "second"
        assert middle.run.model_round_count == 1
        assert not any(
            m.tool_result for m in app.store.load_session(waiting.session.session_id).messages
        )
        done = await app.answer_user(**target, question=q1, answer="first")
        assert done.run.status == "completed", done.run.error
        assert done.run.model_round_count == 2
        history = app.store.load_session(waiting.session.session_id)
        assert published_frames == [ids]
        assert [m.tool_result.tool_call_id for m in history.messages if m.tool_result] == ids
        assert sorted(starts) == ["q1", "q2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("abort", [False, True])
async def test_parallel_batch_marks_only_handlers_that_actually_started(tmp_path, abort) -> None:
    registry = ToolRegistry()
    two_started = asyncio.Event()
    release = asyncio.Event()
    started: list[str] = []

    async def blocking(invocation, context) -> ToolResult:
        started.append(invocation.tool_call_id)
        if len(started) == 2:
            two_started.set()
        await release.wait()
        return _result(invocation, invocation.tool_call_id)

    for name in ("First", "Second", "Third"):
        registry.register(
            Tool(definition=ToolDefinition(name=name, description=name), handler=blocking)
        )
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="First", source_index=0),
            ToolCallRequest(id="call_2", name="Second", source_index=1),
            ToolCallRequest(id="call_3", name="Third", source_index=2),
        ],
    )

    async with running_runtime_task(runtime, runtime.run("run three")) as run_task:
        try:
            await two_started.wait()
            session = runtime.store.list_sessions()[0]
            snapshot = runtime.store.load_session(session.session_id)
            active = next(run for run in snapshot.runs if run.status == "running")
            assert active.active_tool_batch is not None
            assert [call.status for call in load_tool_batch(runtime.store, active).calls] == [
                "running",
                "running",
                "ready",
            ]
            started_events = [
                event
                for event in recorded_events(snapshot.session.session_id)
                if event.event_type == "tool_call_started"
            ]
            assert [event.payload["tool_call_id"] for event in started_events] == started
            if abort:
                await runtime.abort_run(session_id=active.session_id, run_id=active.run_id)
                result = await asyncio.wait_for(asyncio.shield(run_task), 10)
                assert result.run.status == "aborted"
                assert [c.status for c in load_tool_batch(runtime.store, result.run).calls] == [
                    "unknown_outcome",
                    "unknown_outcome",
                    "aborted",
                ]
                assert sorted(started) == ["call_1", "call_2"]
                return
        finally:
            release.set()
        result = await run_task
        assert result.run.status == "completed"


@pytest.mark.asyncio
async def test_approval_only_blocks_its_call_and_marks_running_before_execution(
    tmp_path, monkeypatch
) -> None:
    registry = ToolRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def protected(invocation, context) -> ToolResult:
        executed.append(invocation.tool_call_id)
        if invocation.tool_call_id == "call_2":
            started.set()
            await release.wait()
        return _result(invocation, invocation.tool_call_id)

    for name in ("ProtectedOne", "ProtectedTwo"):
        registry.register(
            Tool(
                definition=ToolDefinition(
                    name=name,
                    description=name,
                    permission_mode="approval",
                ),
                handler=protected,
            )
        )
    registry.register(
        Tool(
            definition=ToolDefinition(name="Automatic", description="automatic"),
            handler=protected,
        )
    )
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="ProtectedOne", source_index=0),
            ToolCallRequest(id="call_2", name="ProtectedTwo", source_index=1),
            ToolCallRequest(id="call_3", name="Automatic", source_index=2),
        ],
    )

    pending = await runtime.run("run protected tools")
    batch = load_tool_batch(runtime.store, pending.run)
    assert batch is not None
    assert [call.request.id for call in batch.calls] == ["call_1", "call_2", "call_3"]
    assert executed == ["call_3"]
    approval_ids = [approval.approval_id for approval in batch.pending_approvals]

    still_waiting = await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approval_id=approval_ids[0],
        approved=True,
    )
    assert still_waiting.run.status == "waiting_approval"
    assert executed == ["call_3", "call_1"]
    with monkeypatch.context() as stale:
        stale.setattr(runtime.parked_runs, "_target", AsyncMock(return_value=pending.run))
        with pytest.raises(ValueError, match="approval is not pending"):
            await asyncio.wait_for(
                runtime.approve_tool(session_id=pending.session.session_id, approved=False), 2
            )
    assert executed == ["call_3", "call_1"]

    resume_task = asyncio.create_task(
        runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=approval_ids[1],
            approved=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    executing = runtime.store.load_session(pending.session.session_id)
    executing_run = next(item for item in executing.runs if item.run_id == pending.run.run_id)
    assert executing.session.status == "running"
    assert executing_run.status == "running"
    assert executing_run.phase == "tool_execution"
    release.set()

    resumed = await asyncio.wait_for(resume_task, timeout=5)
    assert resumed.run.status == "completed"
    assert sorted(executed) == ["call_1", "call_2", "call_3"]


@pytest.mark.asyncio
async def test_tool_failure_does_not_cancel_sibling_call(tmp_path) -> None:
    registry = ToolRegistry()
    completed: list[str] = []

    async def broken(invocation, context) -> ToolResult:
        raise RuntimeError("broken tool")

    async def healthy(invocation, context) -> ToolResult:
        completed.append(invocation.tool_call_id)
        return _result(invocation, "healthy")

    registry.register(
        Tool(definition=ToolDefinition(name="Broken", description="broken"), handler=broken)
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Healthy", description="healthy"), handler=healthy)
    )
    runtime = batch_runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="Broken", source_index=0),
            ToolCallRequest(id="call_2", name="Healthy", source_index=1),
        ],
    )

    result = await runtime.run("run both")

    assert result.run.status == "completed"
    assert completed == ["call_2"]
    assert [tool_result.is_error for tool_result in run_tool_results(runtime.store, result)] == [
        True,
        False,
    ]


@pytest.mark.parametrize(("mode", "limit", "expected"), [("sequential", 3, 1), ("parallel", 2, 2)])
@pytest.mark.parametrize("programmatic", [False, True], ids=["model", "programmatic-driver"])
async def test_new_batch_runs_with_host_mode_and_limit(
    tmp_path, mode, limit, expected, programmatic
):
    from ouro.config.runtime import RuntimeSettingsResolver
    from ouro.local_application import LocalApplication, build_local_configuration
    from tests.application.test_run_drivers import DRIVER, PLUGIN, plugin_source, set_driver_body

    config = build_local_configuration(tmp_path)
    config.update_tool_execution(mode=mode, max_concurrency=limit)
    full, release = asyncio.Event(), asyncio.Event()
    active = peak = 0

    async def read(invocation, context):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == expected:
            full.set()
        try:
            await release.wait()
            return _result(invocation, "done")
        finally:
            active -= 1

    registry = ToolRegistry()
    registry.register(
        Tool(definition=ToolDefinition(name="Read", description="Read"), handler=read)
    )
    app = batch_runtime(
        tmp_path / "data",
        registry=registry,
        first_calls=[
            ToolCallRequest(id=f"read_{i}", name="Read", source_index=i) for i in range(4)
        ],
    )
    app.settings_resolver = RuntimeSettingsResolver(tmp_path).snapshot
    async with LocalApplication.from_runtime(app, home=tmp_path) as application:
        if programmatic:
            package = plugin_source(tmp_path, "python-inprocess")
            set_driver_body(
                package,
                """async def review(context, resume):
    calls = tuple(sdk.RunToolCall("Read") for _ in range(4))
    result = await context.tools.execute(calls, checkpoint="read")
    return sdk.DriverFinished(str(len(result.results)))
""",
            )
            application.plugins.install(package)
            assert (await application.plugins.enable(PLUGIN)).status == "active"
        task = asyncio.create_task(
            app.run("read four items", driver_id=DRIVER if programmatic else None)
        )
        try:
            await asyncio.wait_for(full.wait(), 30)
            session = app.store.list_sessions()[0]
            run = app.store.load_run(session.session_id, session.active_run_id)
            assert (
                run.active_tool_batch.execution_mode,
                run.active_tool_batch.max_concurrency,
            ) == (mode, limit)
        finally:
            release.set()
            result = await task
        assert result.run.status == "completed", result.run.error
        assert len(run_tool_results(app.store, result)) == 4
        assert peak == expected


async def test_restart_dispatches_previously_approved_calls_with_saved_mode(tmp_path):
    from ouro.config.runtime import RuntimeSettingsResolver
    from ouro.core.tool_batch_transitions import resolve_tool_approval
    from ouro.local_application import build_local_configuration

    home, data = tmp_path / "home", tmp_path / "data"
    config = build_local_configuration(home)
    config.update_tool_execution(mode="parallel", max_concurrency=2)
    registry = ToolRegistry()
    full, release = asyncio.Event(), asyncio.Event()
    starts = []
    active = peak = 0

    async def action(inv, context):
        nonlocal active, peak
        starts.append(inv.tool_call_id)
        active += 1
        peak = max(active, peak)
        if active == 2:
            full.set()
        try:
            await release.wait()
            return _result(inv, "done")
        finally:
            active -= 1

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Action", description="Perform action", permission_mode="approval"
            ),
            handler=action,
        )
    )
    calls = [ToolCallRequest(id=f"act_{i}", name="Action", source_index=i) for i in range(3)]
    old = batch_runtime(data, registry=registry, first_calls=calls)
    old.settings_resolver = RuntimeSettingsResolver(home).snapshot
    async with old:
        waiting = await old.run("three actions")
        batch = load_tool_batch(old.store, waiting.run)
        assert batch.execution_mode == "parallel" and batch.max_concurrency == 2
        approvals = [item.approval_id for item in batch.pending_approvals]
        # An older host recorded each decision but held all starts until the last approval.
        for key in approvals[:2]:
            batch = resolve_tool_approval(
                batch, approval_id=key, approved=True, reason="", arguments_override=None
            )
        old.run_lifecycle.save_batch_state(
            session=waiting.session,
            run=waiting.run,
            batch=batch,
            status="waiting_approval",
            phase="human_interaction",
        )
        assert not starts

    config.update_tool_execution(mode="sequential", max_concurrency=1)
    cold = batch_runtime(data, registry=registry, first_calls=[])
    cold.adapter.provider = FauxProvider()
    cold.settings_resolver = RuntimeSettingsResolver(home).snapshot
    async with cold:
        await cold.refresh_configuration()
        task = asyncio.create_task(
            cold.approve_tool(
                session_id=waiting.session.session_id,
                run_id=waiting.run.run_id,
                approval_id=approvals[2],
                approved=True,
            )
        )
        try:
            await asyncio.wait_for(full.wait(), 30)
        finally:
            release.set()
            result = await task
        assert result.run.status == "completed", result.run.error
        assert peak == 2 and sorted(starts) == [call.id for call in calls]


async def test_supervised_answer_waits_for_inflight_sibling_to_hand_off(tmp_path):
    entered, requested, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    registry = ToolRegistry()

    async def ask(inv, context):
        await entered.wait()
        return ToolInputRequest(data={"question": "continue?"})

    async def read(inv, context):
        entered.set()
        await release.wait()
        return _result(inv, "read")

    for name, handler in (("Question", ask), ("Read", read)):
        registry.register(
            Tool(definition=ToolDefinition(name=name, description=name), handler=handler)
        )
    app = batch_runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="q", name="Question"),
            ToolCallRequest(id="r", name="Read", source_index=1),
        ],
    )

    def capture(event):
        if event.event_type == "ask_user_requested":
            requested.set()

    app.add_event_sink(capture)
    supervisor = RuntimeSupervisor(app, max_concurrency=1)
    reply = None
    try:
        handle = await supervisor.submit("question and slow read")
        await asyncio.wait_for(requested.wait(), 30)
        session = app.store.load_session_record(handle.session_id)
        run = app.store.load_run(handle.session_id, session.active_run_id)
        batch = load_tool_batch(app.store, run)
        assert run.status == "running" and [c.status for c in batch.calls] == [
            "waiting_user",
            "running",
        ]
        submitted = asyncio.Event()

        async def answer():
            submitted.set()
            return await supervisor.resolve_tool_input(
                session_id=run.session_id,
                run_id=run.run_id,
                answer="yes",
                question=batch.calls[0].question(run.run_id, batch.batch_id),
            )

        reply = asyncio.create_task(answer())
        await asyncio.wait_for(submitted.wait(), 5)
        assert not reply.done()
        release.set()
        assert (await supervisor.wait(handle)).status == "waiting_user"
        assert (await supervisor.wait(await reply)).status == "completed"
        snapshot = app.store.load_session(handle.session_id)
        assert [m.text_content for m in snapshot.messages if m.tool_result] == ["yes", "read"]
    finally:
        release.set()
        if reply is not None:
            await asyncio.gather(reply, return_exceptions=True)
        await supervisor.shutdown()
