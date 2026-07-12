from __future__ import annotations

import pytest

from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder
from ouro.core.events import EventRecord
from ouro.core.inner_loop import InnerAgentLoop, InnerLoopPorts
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import ApprovalRecord, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolBatchHandlingResult, ToolCallBatchRecord
from ouro.providers import FauxProvider
from ouro.tools import ToolInvocation, ToolRegistry, ToolResult


async def _prepare_context(
    emitted,
    sequence,
    *,
    session,
    run,
    messages,
    tools,
    model_round,
    abort_signal,
):
    del emitted, session, run, model_round, abort_signal
    return ContextBuilder().build(messages=messages, tools=tools), sequence


@pytest.mark.asyncio
async def test_inner_loop_pauses_for_approval_through_outer_port() -> None:
    session = SessionRecord(title="Inner loop")
    run = RunRecord(session_id=session.session_id, status="running")
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="user",
            text="Write file",
            sequence=1,
        )
    ]
    saved_waiting_state: list[tuple[SessionRecord, RunRecord]] = []

    async def emit(
        emitted,
        sequence,
        *,
        session_id,
        run_id,
        event_type,
        payload,
    ):
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type=event_type,
            sequence=sequence + 1,
            payload=payload,
        )
        emitted.append(event)
        return event.sequence

    async def record_hook_failures(emitted, sequence, failures):
        assert failures == []
        return sequence

    async def handle_tool_batch(
        emitted,
        sequence,
        session,
        run,
        tool_calls,
        *,
        turn_id,
        message_sequence,
        assistant_text,
        assistant_reasoning,
        abort_signal,
    ):
        del emitted, abort_signal
        assistant_message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=assistant_text,
            sequence=message_sequence,
        )
        invocation = ToolInvocation(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=turn_id,
            tool_call_id=tool_calls[0].id,
            tool_name=tool_calls[0].name,
            arguments=tool_calls[0].arguments,
        )
        approval = ApprovalRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            tool_call_id=invocation.tool_call_id,
        )
        batch = ToolCallBatchRecord(
            turn_id=turn_id,
            assistant_message_id=assistant_message.message_id,
            phase="awaiting_interaction",
            calls=[
                ToolBatchCall(
                    request=tool_calls[0],
                    status="awaiting_approval",
                    invocation=invocation,
                    approval=approval,
                )
            ],
        )
        return ToolBatchHandlingResult(
            status="waiting_approval",
            batch=batch,
            sequence=sequence,
            messages=[assistant_message],
        )

    def record_usage(run, usage):
        del usage
        return run

    def aborted_result(**kwargs):
        del kwargs
        return None

    def save_waiting_state(*, session, run):
        saved_waiting_state.append((session, run))
        return session

    async def complete_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("approval pause must not complete the run")

    async def fail_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("approval pause must not fail the run")

    async def pause_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("approval pause must not hit the run budget")

    inner_loop = InnerAgentLoop(
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={"id": "call_1", "name": "Write", "arguments": {}},
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        run_budget=RunBudgetPolicy(max_model_rounds=1),
        ports=InnerLoopPorts(
            prepare_context=_prepare_context,
            emit=emit,
            record_hook_failures=record_hook_failures,
            handle_tool_batch=handle_tool_batch,
            record_usage=record_usage,
            aborted_result=aborted_result,
            save_waiting_state=save_waiting_state,
            complete_run=complete_run,
            fail_run=fail_run,
            pause_run=pause_run,
        ),
    )

    result = await inner_loop.run(
        session=session,
        run=run,
        messages=messages,
        sequence=0,
        emitted=[],
        abort_signal=AbortSignal(),
    )

    assert result.run.status == "waiting_approval"
    assert result.session.status == "waiting_approval"
    assert result.run.active_tool_batch is not None
    assert result.run.active_tool_batch.calls[0].request.name == "Write"
    assert result.run.active_tool_batch.calls[0].approval is not None
    assert result.run.active_tool_batch.calls[0].approval.tool_call_id == "call_1"
    assert saved_waiting_state == [(result.session, result.run)]
    assert [event.event_type for event in result.events] == [
        "model_request_started",
        "model_message_completed",
    ]


@pytest.mark.asyncio
async def test_inner_loop_accumulates_reasoning_and_passes_to_tool_handler() -> None:
    session = SessionRecord(title="Reasoning loop")
    run = RunRecord(session_id=session.session_id, status="running")
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="user",
            text="Use tool",
            sequence=1,
        )
    ]
    captured: dict[str, str] = {}

    async def emit(emitted, sequence, *, session_id, run_id, event_type, payload):
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type=event_type,
            sequence=sequence + 1,
            payload=payload,
        )
        emitted.append(event)
        return event.sequence

    async def record_hook_failures(emitted, sequence, failures):
        assert failures == []
        return sequence

    async def handle_tool_batch(
        emitted,
        sequence,
        session,
        run,
        tool_calls,
        *,
        turn_id,
        message_sequence,
        assistant_text,
        assistant_reasoning,
        abort_signal,
    ):
        del emitted, abort_signal
        captured["assistant_text"] = assistant_text
        captured["assistant_reasoning"] = assistant_reasoning
        assistant_message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=assistant_text,
            sequence=message_sequence,
        )
        result_message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="tool_result",
            text="result",
            sequence=message_sequence + 1,
        )
        result = ToolResult(
            tool_call_id=tool_calls[0].id,
            tool_name=tool_calls[0].name,
            content="result",
        )
        batch = ToolCallBatchRecord(
            turn_id=turn_id,
            assistant_message_id=assistant_message.message_id,
            phase="completed",
            calls=[
                ToolBatchCall(
                    request=tool_calls[0],
                    status="completed",
                    result=result,
                )
            ],
        )
        return ToolBatchHandlingResult(
            status="completed",
            batch=batch,
            sequence=sequence,
            messages=[assistant_message, result_message],
            results=[result],
        )

    def record_usage(run, usage):
        del usage
        return run

    def aborted_result(**kwargs):
        del kwargs
        return None

    def save_waiting_state(*, session, run):
        del run
        return session

    async def complete_run(*args, **kwargs):
        del args, kwargs
        return RunResult(session=session, run=run)

    async def fail_run(*args, **kwargs):
        del args, kwargs
        return RunResult(session=session, run=run)

    async def pause_run(*args, **kwargs):
        del args, kwargs
        return RunResult(session=session, run=run)

    inner_loop = InnerAgentLoop(
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="reasoning_delta",
                        reasoning_content="I should use the tool.",
                    ),
                    ProviderStreamEvent(
                        type="text_delta",
                        text="Calling ",
                    ),
                    ProviderStreamEvent(
                        type="text_delta",
                        text="tool.",
                    ),
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={"id": "call_1", "name": "Write", "arguments": {}},
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        run_budget=RunBudgetPolicy(max_model_rounds=2),
        ports=InnerLoopPorts(
            prepare_context=_prepare_context,
            emit=emit,
            record_hook_failures=record_hook_failures,
            handle_tool_batch=handle_tool_batch,
            record_usage=record_usage,
            aborted_result=aborted_result,
            save_waiting_state=save_waiting_state,
            complete_run=complete_run,
            fail_run=fail_run,
            pause_run=pause_run,
        ),
    )

    await inner_loop.run(
        session=session,
        run=run,
        messages=messages,
        sequence=0,
        emitted=[],
        abort_signal=AbortSignal(),
    )

    assert captured["assistant_text"] == "Calling tool."
    assert captured["assistant_reasoning"] == "I should use the tool."
