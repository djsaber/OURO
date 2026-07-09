from __future__ import annotations

import pytest

from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder
from ouro.core.events import EventRecord
from ouro.core.hooks import HookRegistry
from ouro.core.inner_loop import InnerAgentLoop, InnerLoopPorts
from ouro.core.messages import MessageRecord
from ouro.core.state import ApprovalRecord, RunRecord, SessionRecord
from ouro.providers import FauxProvider, ModelMetadata, ProviderStreamEvent
from ouro.tools import ToolExecutionOutcome, ToolInvocation, ToolRegistry


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

    async def handle_tool_call(
        emitted,
        sequence,
        session,
        run,
        tool_call,
        *,
        message_sequence,
        assistant_text,
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
            tool_call_id=str(tool_call["id"]),
            tool_name=str(tool_call["name"]),
            arguments=dict(tool_call.get("arguments") or {}),
        )
        approval = ApprovalRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            tool_call_id=invocation.tool_call_id,
        )
        return (
            ToolExecutionOutcome(
                status="approval_required",
                invocation=invocation,
                approval=approval,
            ),
            sequence,
            [assistant_message],
        )

    def record_usage(run, usage):
        del usage
        return run

    def checkpoint(**kwargs):
        return kwargs["session"], kwargs["run"]

    def aborted_result(**kwargs):
        del kwargs
        return None

    def save_waiting_state(*, session, run):
        saved_waiting_state.append((session, run))

    async def complete_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("approval pause must not complete the run")

    async def fail_run(*args, **kwargs):
        del args, kwargs
        raise AssertionError("approval pause must not fail the run")

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
        hooks=HookRegistry(),
        context_builder=ContextBuilder(),
        max_model_rounds=1,
        ports=InnerLoopPorts(
            emit=emit,
            record_hook_failures=record_hook_failures,
            handle_tool_call=handle_tool_call,
            record_usage=record_usage,
            checkpoint=checkpoint,
            aborted_result=aborted_result,
            save_waiting_state=save_waiting_state,
            complete_run=complete_run,
            fail_run=fail_run,
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
    assert result.run.metadata["pending_invocation"]["tool_name"] == "Write"
    assert result.run.metadata["pending_approval"]["tool_call_id"] == "call_1"
    assert saved_waiting_state == [(result.session, result.run)]
    assert [event.event_type for event in result.events] == ["model_request_started"]
