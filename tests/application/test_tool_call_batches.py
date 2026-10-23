import asyncio
import json
from datetime import timedelta

import pytest

import ouro.application.tool_calls as tool_calls_module
from ouro.application.runtime.service import RuntimeService
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest, ToolInvocation
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v5 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from ouro.tools.builtin.todolist import builtin_todolist_tool
from tests.application.helpers import running_runtime_task


def _result(invocation, content: str, *, is_error: bool = False) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        is_error=is_error,
    )


def _runtime(tmp_path, *, registry, first_calls, mode="sequential") -> RuntimeService:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        *(
                            ProviderStreamEvent(
                                type="tool_call_delta",
                                tool_call_delta=ToolCallDelta(
                                    index=tool_call.source_index,
                                    id_delta=tool_call.id,
                                    name_delta=tool_call.name,
                                    arguments_delta=json.dumps(tool_call.arguments),
                                ),
                            )
                            for tool_call in first_calls
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="batch complete"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
        tool_execution_config=ToolExecutionConfig(mode=mode, max_concurrency=2),
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
    runtime = _runtime(
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
@pytest.mark.asyncio
async def test_tool_wait_returns_consumed_state_change_and_resumes_committed_state(
    tmp_path, store_type, monkeypatch
) -> None:
    seen = []
    outcomes = []

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
    original_request = runtime._execute_model_tool_batch

    async def request_tools(request):
        result = await original_request(request)
        outcomes.append(result)
        return result

    monkeypatch.setattr(runtime, "_execute_model_tool_batch", request_tools)
    try:
        waiting = await runtime.run("advance")
        assert waiting.run.status == "waiting_user"
        batch = waiting.run.active_tool_batch
        assert batch is not None
        assert batch.calls[0].input_request.harness_change is None
        assert outcomes[0].input_request == batch.calls[0].input_request
        persisted = store.load_run(waiting.session.session_id, waiting.run.run_id)
        assert persisted.active_tool_batch == batch

        completed = await runtime.answer_user(
            session_id=waiting.session.session_id,
            run_id=waiting.run.run_id,
            answer={"accepted": True},
        )
        assert completed.run.status == "completed"
        assert seen == ["begin", {"stage": "waiting"}]
        assert completed.tool_results[0].content == "accepted"
    finally:
        await runtime.close()


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
async def test_empty_policy_registry_skips_policy_validation_but_keeps_host_preflight(
    tmp_path, monkeypatch, arguments, permission, expected_error
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
    runtime = _runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="example", name="Example", arguments=arguments)],
    )
    policy_validations = []
    validate = tool_calls_module.tool_argument_error

    def policy_argument_error(definition, arguments):
        policy_validations.append(definition.name)
        return validate(definition, arguments)

    monkeypatch.setattr(tool_calls_module, "tool_argument_error", policy_argument_error)
    try:
        result = await runtime.run("run example")
        assert policy_validations == []
        assert len(executed) == int(permission == "auto" and expected_error is None)
        if permission == "approval":
            assert result.run.status == "waiting_approval"
        else:
            assert result.run.status == "completed"
            assert result.tool_results[0].structured.get("error_type") == expected_error
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_empty_policy_registry_preserves_control_refresh_failures(tmp_path):
    runtime = _runtime(tmp_path, registry=ToolRegistry(), first_calls=[])
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
        _, mode, reason, sequence = await coordinator._evaluate_tool_policies(
            [], 0, session=session, run=run, invocation=invocation, phase="execution_boundary"
        )
        assert refreshed == [session.controls]
        assert mode == "deny"
        assert reason == "Required Session controls are unavailable"
        assert sequence == 0
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
    runtime = _runtime(tmp_path, registry=registry, first_calls=[])
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
    runtime = _runtime(
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
    append_event = runtime.store.append_ordered_event

    def capture_planned_state(event):
        # Check the persisted state before its event is recorded. Asynchronous
        # Host sinks may observe a later Run phase by the time they receive it.
        if event.event_type == "tool_batch_planned":
            assert event.run_id is not None
            durable_run = runtime.store.load_run(event.session_id, event.run_id)
            assert durable_run.active_tool_batch is not None
            planned_states.append(
                (
                    durable_run.phase,
                    durable_run.active_tool_batch.phase,
                    [call.status for call in durable_run.active_tool_batch.calls],
                )
            )
        return append_event(event)

    monkeypatch.setattr(runtime.store, "append_ordered_event", capture_planned_state)

    result = await runtime.run("run both")
    snapshot = runtime.store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert not any(item.is_error for item in result.tool_results)
    assert result.tool_results[2].structured == {"todos": todos}
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
    event_types = [event.event_type for event in snapshot.events]
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
    runtime = _runtime(
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
    assert [item.structured["error_type"] for item in result.tool_results] == [
        "ExclusiveToolBatchViolation",
        "ExclusiveToolBatchViolation",
    ]
    assert all("No tools were executed." in item.text_content for item in result.tool_results)
    assert all("Call each separately" in item.text_content for item in result.tool_results)
    assert any(event.event_type == "tool_batch_rejected" for event in snapshot.events)


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
    runtime = _runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_confirm", name="plans__confirm")],
    )

    pending = await runtime.run("confirm the plan")
    durable_pending = runtime.store.load_run(pending.session.session_id, pending.run.run_id)

    assert pending.run.status == "waiting_user"
    assert durable_pending.active_tool_batch is not None
    waiting_call = durable_pending.active_tool_batch.calls[0]
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
        event for event in snapshot.events if event.event_type == "ask_user_resolved"
    )
    assert resumed.run.status == "completed"
    assert resumed.tool_results[0].structured == {"answer": answer}
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
    runtime = _runtime(
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
        event for event in snapshot.events if event.event_type == "ask_user_resolved"
    )
    requested_event = next(
        event for event in snapshot.events if event.event_type == "ask_user_requested"
    )

    assert result.run.status == "completed"
    assert sorted(executed_after_request) == ["call_after_1", "call_after_2"]
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert isinstance(resolution, sdk.UnattendedResolution)
    assert resolution.resolver_id == "ouro.afk"
    assert requested_event.payload["interaction_mode"] == "unattended"
    assert requested_event.payload["will_suspend"] is False
    assert resolved_event.payload["resolution"]["kind"] == "unattended"
    assert resolved_event.payload["resolved_by"] == "host_policy"
    assert all(event.event_type != "run_status_changed" for event in snapshot.events)


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
    runtime = _runtime(
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
    assert len(result.tool_results) == 1
    assert result.tool_results[0].structured["error_type"] == "UserInputUnavailable"
    assert result.tool_results[0].structured["resolved_by"] == "host_policy"
    assert result.tool_results[0].structured["input_request"] == {
        "type": "form",
        "fields": ["environment"],
    }


@pytest.mark.asyncio
async def test_unattended_resume_cannot_request_user_input_again(tmp_path) -> None:
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
    runtime = _runtime(
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
    assert result.tool_results[0].structured["error_type"] == "UnattendedInteractionLoop"
    assert result.tool_results[0].structured["input_request"] == {"stage": "second"}


@pytest.mark.asyncio
async def test_resumable_tool_can_request_multiple_inputs_and_owns_final_result(tmp_path) -> None:
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
    runtime = _runtime(
        tmp_path,
        registry=registry,
        first_calls=[ToolCallRequest(id="call_workflow", name="workflow__advance")],
    )

    first = await runtime.run("advance")
    second = await runtime.answer_user(
        session_id=first.session.session_id,
        run_id=first.run.run_id,
        answer={"choice": "next"},
    )
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
    assert completed.tool_results[0].content == "workflow accepted"
    assert completed.tool_results[0].structured == {"accepted": {"choice": "accept"}}


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
    runtime = _runtime(
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
    runtime = _runtime(
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
        event for event in snapshot.events if event.event_type == "approval_resolved"
    )

    assert result.run.status == "completed"
    assert executions == 1
    assert resolved_event.payload["approved"] is True
    assert resolved_event.payload["resolved_by"] == "host_policy"
    assert resolved_event.payload["resolver_id"] == "ouro.afk"


@pytest.mark.asyncio
async def test_sequential_batch_stops_after_input_request_until_answer(tmp_path) -> None:
    executed_after_request: list[str] = []

    def request_input(_invocation):
        return sdk.ToolInputRequest(data={"type": "choice", "options": ["a", "b"]})

    def after_request(invocation, context) -> ToolResult:
        executed_after_request.append(invocation.tool_call_id)
        return _result(invocation, "continued")

    sdk_tool = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="choose",
                    exposed_name="flow__choose",
                    description="Choose a branch.",
                    permission_mode="auto",
                    handler=request_input,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(sdk_tool)
    registry.register(
        Tool(
            definition=ToolDefinition(name="AfterChoice", description="Continue the flow."),
            handler=after_request,
        )
    )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="sequential",
        first_calls=[
            ToolCallRequest(id="call_choose", name="flow__choose", source_index=0),
            ToolCallRequest(id="call_after", name="AfterChoice", source_index=1),
        ],
    )

    waiting = await runtime.run("choose and continue")

    assert waiting.run.status == "waiting_user"
    assert executed_after_request == []

    completed = await runtime.answer_user(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        answer={"choice": "b"},
    )

    assert completed.run.status == "completed"
    assert executed_after_request == ["call_after"]


@pytest.mark.asyncio
async def test_parallel_batch_does_not_start_queued_calls_after_input_request(tmp_path) -> None:
    started: list[str] = []

    async def request_input(call: sdk.ToolCall):
        started.append(call.tool_call_id)
        return sdk.ToolInputRequest(data={"type": "confirm"})

    async def complete(invocation, context) -> ToolResult:
        started.append(invocation.tool_call_id)
        await asyncio.sleep(0)
        return _result(invocation, "done")

    sdk_tool = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="confirm",
                    exposed_name="parallel__confirm",
                    description="Confirm before queued work starts.",
                    permission_mode="auto",
                    handler=request_input,
                )
            ]
        ),
    ).tools[0]
    registry = ToolRegistry()
    registry.register(sdk_tool)
    for name in ("Concurrent", "Queued"):
        registry.register(
            Tool(definition=ToolDefinition(name=name, description=name), handler=complete)
        )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_confirm", name="parallel__confirm", source_index=0),
            ToolCallRequest(id="call_concurrent", name="Concurrent", source_index=1),
            ToolCallRequest(id="call_queued", name="Queued", source_index=2),
        ],
    )

    waiting = await runtime.run("confirm parallel work")

    assert waiting.run.status == "waiting_user"
    assert "call_confirm" in started
    assert "call_queued" not in started

    completed = await runtime.answer_user(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        answer=True,
    )

    assert completed.run.status == "completed"
    assert started.count("call_confirm") == 1
    assert started.count("call_queued") == 1


@pytest.mark.asyncio
async def test_parallel_batch_marks_only_handlers_that_actually_started(tmp_path) -> None:
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
    runtime = _runtime(
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
            assert [call.status for call in active.active_tool_batch.calls] == [
                "running",
                "running",
                "ready",
            ]
            started_events = [
                event for event in snapshot.events if event.event_type == "tool_call_started"
            ]
            assert [event.payload["tool_call_id"] for event in started_events] == started
        finally:
            release.set()
        result = await run_task
        assert result.run.status == "completed"


@pytest.mark.asyncio
async def test_batch_waits_for_every_approval_and_marks_running_before_execution(tmp_path) -> None:
    registry = ToolRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def protected(invocation, context) -> ToolResult:
        executed.append(invocation.tool_call_id)
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
    runtime = _runtime(
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
    batch = pending.run.active_tool_batch
    assert batch is not None
    assert [call.request.id for call in batch.calls] == ["call_1", "call_2", "call_3"]
    approval_ids = [approval.approval_id for approval in batch.pending_approvals]

    still_waiting = await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approval_id=approval_ids[0],
        approved=True,
    )
    assert still_waiting.run.status == "waiting_approval"
    assert executed == []

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
    runtime = _runtime(
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
    assert [tool_result.is_error for tool_result in result.tool_results] == [True, False]


@pytest.mark.asyncio
async def test_tool_definition_can_force_parallel_batch_to_run_sequentially(tmp_path) -> None:
    registry = ToolRegistry()
    order: list[str] = []

    async def first(invocation, context) -> ToolResult:
        await asyncio.sleep(0.02)
        order.append(invocation.tool_call_id)
        return _result(invocation, "first")

    async def second(invocation, context) -> ToolResult:
        order.append(invocation.tool_call_id)
        return _result(invocation, "second")

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="ForcedSequential",
                description="forced",
                execution_mode="sequential",
            ),
            handler=first,
        )
    )
    registry.register(
        Tool(definition=ToolDefinition(name="Normal", description="normal"), handler=second)
    )
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="ForcedSequential", source_index=0),
            ToolCallRequest(id="call_2", name="Normal", source_index=1),
        ],
    )

    await runtime.run("run both")

    assert order == ["call_1", "call_2"]
