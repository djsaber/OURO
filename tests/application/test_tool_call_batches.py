import asyncio
import json

import pytest

from ouro.application.runtime import RuntimeService
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.core.tools import ToolCallRequest, ToolInvocation
from ouro.plugins.api import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v1 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


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


def test_runtime_reuses_one_tool_coordinator_for_each_run(tmp_path) -> None:
    runtime = _runtime(tmp_path, registry=ToolRegistry(), first_calls=[])

    first = runtime._tool_calls_for_run("run_scope")
    second = runtime._tool_calls_for_run("run_scope")

    assert first is second


def test_runtime_publishes_permission_updates_to_existing_and_future_coordinators(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="Deploy", description="Deploy changes."),
            handler=lambda invocation: _result(invocation, "deployed"),
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
async def test_parallel_batch_preserves_one_assistant_message_and_source_order(tmp_path) -> None:
    registry = ToolRegistry()
    completed: list[str] = []
    first_started = asyncio.Event()
    second_completed = asyncio.Event()

    async def first(invocation) -> ToolResult:
        first_started.set()
        await asyncio.wait_for(second_completed.wait(), timeout=1.0)
        completed.append("first")
        return _result(invocation, "first result")

    async def second(invocation) -> ToolResult:
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
    runtime = _runtime(
        tmp_path,
        registry=registry,
        mode="parallel",
        first_calls=[
            ToolCallRequest(id="call_1", name="First", source_index=0),
            ToolCallRequest(id="call_2", name="Second", source_index=1),
        ],
    )
    planned_states: list[tuple[str, str, list[str]]] = []

    def capture_planned_state(event) -> None:
        if event.event_type != "tool_batch_planned":
            return
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

    runtime.add_event_sink(capture_planned_state)

    result = await runtime.run("run both")
    snapshot = runtime.store.load_session(result.session.session_id)

    assert completed == ["second", "first"]
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
        "tool_result",
        "assistant",
    ]
    assert [call.id for call in snapshot.messages[1].tool_calls] == ["call_1", "call_2"]
    assert planned_states == [("tool_preflight", "planned", ["planned", "planned"])]
    assert [
        message.tool_result.tool_call_id
        for message in snapshot.messages[2:4]
        if message.tool_result is not None
    ] == ["call_1", "call_2"]
    tool_timings = [message.tool_execution for message in snapshot.messages[2:4]]
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
async def test_exclusive_tool_rejects_mixed_batch_before_any_handler_runs(tmp_path) -> None:
    registry = ToolRegistry()
    executed: list[str] = []

    def execute(invocation) -> ToolResult:
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
        mode="parallel",
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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="confirm_plan",
                    exposed_name="plans__confirm",
                    description="Confirm a plan.",
                    permission_mode="auto",
                    handler=confirm_plan,
                )
            ]
        )
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

    def after_request(invocation) -> ToolResult:
        executed_after_request.append(invocation.tool_call_id)
        return _result(invocation, "continued batch")

    contribution = adapt_sdk_contributions(
        sdk.ContributionSet(
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
        )
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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="form",
                    exposed_name="afk__form",
                    description="Collect a form from the user.",
                    permission_mode="auto",
                    handler=request_input,
                )
            ]
        )
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
        sdk.ContributionSet(
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
        )
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
        sdk.ContributionSet(
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
        )
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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="deploy",
                    exposed_name="deploy__confirm",
                    description="Approve execution, then collect deployment details.",
                    permission_mode="approval",
                    handler=request_input,
                )
            ]
        )
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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="deploy",
                    exposed_name="afk__deploy",
                    description="Deployment requiring approval.",
                    permission_mode="approval",
                    handler=execute,
                )
            ]
        )
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

    def after_request(invocation) -> ToolResult:
        executed_after_request.append(invocation.tool_call_id)
        return _result(invocation, "continued")

    sdk_tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="choose",
                    exposed_name="flow__choose",
                    description="Choose a branch.",
                    permission_mode="auto",
                    handler=request_input,
                )
            ]
        )
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

    async def complete(invocation) -> ToolResult:
        started.append(invocation.tool_call_id)
        await asyncio.sleep(0)
        return _result(invocation, "done")

    sdk_tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="confirm",
                    exposed_name="parallel__confirm",
                    description="Confirm before queued work starts.",
                    permission_mode="auto",
                    handler=request_input,
                )
            ]
        )
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

    async def blocking(invocation) -> ToolResult:
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

    run_task = asyncio.create_task(runtime.run("run three"))
    await asyncio.wait_for(two_started.wait(), timeout=1)
    session = runtime.store.list_sessions()[0]
    snapshot = runtime.store.load_session(session.session_id)
    active = next(run for run in snapshot.runs if run.status == "running")
    assert active.active_tool_batch is not None
    assert [call.status for call in active.active_tool_batch.calls] == [
        "running",
        "running",
        "ready",
    ]
    started_events = [event for event in snapshot.events if event.event_type == "tool_call_started"]
    assert [event.payload["tool_call_id"] for event in started_events] == started

    release.set()
    result = await asyncio.wait_for(run_task, timeout=2)
    assert result.run.status == "completed"


@pytest.mark.asyncio
async def test_batch_waits_for_every_approval_and_marks_running_before_execution(tmp_path) -> None:
    registry = ToolRegistry()
    started = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def protected(invocation) -> ToolResult:
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
    await asyncio.wait_for(started.wait(), timeout=1)
    executing = runtime.store.load_session(pending.session.session_id)
    executing_run = next(item for item in executing.runs if item.run_id == pending.run.run_id)
    assert executing.session.status == "running"
    assert executing_run.status == "running"
    assert executing_run.phase == "tool_execution"
    release.set()

    resumed = await asyncio.wait_for(resume_task, timeout=1)
    assert resumed.run.status == "completed"
    assert sorted(executed) == ["call_1", "call_2", "call_3"]


@pytest.mark.asyncio
async def test_tool_failure_does_not_cancel_sibling_call(tmp_path) -> None:
    registry = ToolRegistry()
    completed: list[str] = []

    async def broken(invocation) -> ToolResult:
        raise RuntimeError("broken tool")

    async def healthy(invocation) -> ToolResult:
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

    async def first(invocation) -> ToolResult:
        await asyncio.sleep(0.02)
        order.append(invocation.tool_call_id)
        return _result(invocation, "first")

    async def second(invocation) -> ToolResult:
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
