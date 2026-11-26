from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from time import monotonic
from typing import Literal, Protocol, cast

from pydantic import JsonValue, TypeAdapter

from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness, capture_session_harness
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.application.session_controls import (
    SessionControlCoordinator,
    SessionControlError,
)
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.agents import (
    AgentControlRequest,
    AgentControlResult,
    AgentWaitRequest,
)
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import CallbackCancelled, run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.errors import SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessStateChange, accept_state_changes
from ouro.core.interactions import AnswerResolution, ToolInputResolution, UnattendedResolution
from ouro.core.messages import MessageRecord, ModelResponseRecord, ProviderContinuationArtifact
from ouro.core.observers import (
    FrozenJson,
    ToolCallFinishedObservation,
    ToolCallRequestedObservation,
    freeze_json,
)
from ouro.core.redaction import redact_text
from ouro.core.run_drivers import DriverCheckpoint
from ouro.core.session_controller_registry import SessionControllerRegistry
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlState,
    SessionControlTransitionResult,
)
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batch_transitions import tool_batch_suspension
from ouro.core.tool_batches import (
    ToolBatchCall,
    ToolCallBatchRecord,
    ToolExecutionMode,
)
from ouro.core.tool_facts import ToolCallIdentity
from ouro.core.tool_policies import ToolPolicyCall, ToolPolicyMode, ToolPolicyRegistry
from ouro.core.tool_requests import ToolBatchHandlingResult
from ouro.core.tools import (
    ToolCallProducer,
    ToolCallRequest,
    ToolExecutionOutcome,
    ToolInputRequest,
    ToolInvocation,
    ToolResult,
    ToolResultFeedback,
    ToolResultValue,
)
from ouro.tools import ToolBatchExecutor, ToolExecutionContext, ToolExecutor, ToolRegistry
from ouro.tools.base import ToolExecutionAborted, ToolExecutionInterrupted

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_AFK_RESOLVER_ID = "ouro.afk"
_AFK_UNATTENDED_REASON = "AFK mode does not wait for user input"
_OBSERVER_TOOL_CONTENT_LIMIT = 32_768


class AgentControlExecutor(Protocol):
    async def execute_control(
        self,
        request: AgentControlRequest,
        *,
        invocation: ToolInvocation,
        identity: ToolCallIdentity,
        session: SessionRecord,
        run: RunRecord,
    ) -> AgentControlResult: ...

    async def wait_parked(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        tool_call_id: str,
        wait: AgentWaitRequest,
    ) -> None: ...


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_plugin_class(value: object) -> Literal["builtin", "user"] | None:
    return value if value in {"builtin", "user"} else None


class ToolCallCoordinator:
    """Coordinate complete assistant tool-call batches across application boundaries."""

    def __init__(
        self,
        *,
        executor: ToolExecutor,
        registry: ToolRegistry,
        events: RuntimeEventRecorder,
        runs: RunLifecycle,
        execution_config: ToolExecutionConfig,
        default_workspace: str | None,
        is_aborted: Callable[[str, str], bool],
        tool_policies: ToolPolicyRegistry | None = None,
        session_controls: SessionControlCoordinator | None = None,
        session_controllers: SessionControllerRegistry | None = None,
        agent_controls: AgentControlExecutor | None = None,
        refresh_controls: Callable[[SessionControlState], str | None] | None = None,
    ) -> None:
        self.executor = executor
        self.batch_executor = ToolBatchExecutor(executor)
        self.registry = registry
        self.events = events
        self.runs = runs
        self.execution_config = execution_config
        self.default_workspace = default_workspace
        self.is_aborted = is_aborted
        self.tool_policies = tool_policies or ToolPolicyRegistry()
        self.session_controls = session_controls
        self.session_controllers = session_controllers or SessionControllerRegistry()
        self.agent_controls = agent_controls
        self.refresh_controls = refresh_controls

    async def handle_batch(
        self,
        session: SessionRecord,
        run: RunRecord,
        tool_calls: list[ToolCallRequest],
        *,
        turn_id: str,
        assistant_text: str,
        assistant_reasoning: str,
        assistant_model_response: ModelResponseRecord | None,
        assistant_continuation: ProviderContinuationArtifact | None,
        abort_signal: AbortSignal,
        driver_checkpoint: DriverCheckpoint | None = None,
    ) -> ToolBatchHandlingResult:
        assistant_message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=assistant_text,
            turn_id=turn_id,
        ).model_copy(
            update={
                "tool_calls": list(tool_calls),
                "reasoning_content": assistant_reasoning,
                "model_response": assistant_model_response,
                "provider_continuation": assistant_continuation,
            }
        )
        batch = ToolCallBatchRecord(
            batch_id=driver_checkpoint.batch_id
            if driver_checkpoint is not None
            else new_id("batch"),
            turn_id=turn_id,
            assistant_message_id=assistant_message.message_id,
            execution_mode=self._effective_mode(tool_calls),
            max_concurrency=self.execution_config.max_concurrency,
            calls=[ToolBatchCall(request=request) for request in tool_calls],
        )
        head = batch.head()
        assistant_message.call_refs = tuple(
            head.identity(run.run_id, request.id)
            for request in sorted(tool_calls, key=lambda request: request.source_index)
        )
        return await self._handle_batch(
            session=session,
            run=run,
            batch=batch,
            assistant_message=assistant_message,
            abort_signal=abort_signal,
            driver_checkpoint=driver_checkpoint,
        )

    async def handle_driver_batch(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        tool_calls: tuple[ToolCallRequest, ...],
        turn_id: str,
        abort_signal: AbortSignal,
        checkpoint: DriverCheckpoint,
    ) -> ToolBatchHandlingResult:
        if run.driver.is_default:
            raise ValueError("programmatic tool calls require a plugin Driver")
        if not tool_calls:
            raise ValueError("programmatic tool batch must contain at least one call")
        batch = ToolCallBatchRecord(
            batch_id=checkpoint.batch_id,
            turn_id=turn_id,
            producer=ToolCallProducer(kind="driver", driver_id=run.driver.driver_id),
            execution_mode=self._effective_mode(list(tool_calls)),
            max_concurrency=self.execution_config.max_concurrency,
            calls=[ToolBatchCall(request=request) for request in tool_calls],
        )
        return await self._handle_batch(
            session=session,
            run=run,
            batch=batch,
            assistant_message=None,
            abort_signal=abort_signal,
            driver_checkpoint=checkpoint,
        )

    async def _handle_batch(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        assistant_message: MessageRecord | None,
        abort_signal: AbortSignal,
        driver_checkpoint: DriverCheckpoint | None,
    ) -> ToolBatchHandlingResult:
        tool_calls = [call.request for call in batch.calls]
        turn_id = batch.turn_id
        _, _, _, committed_events = await run_blocking(
            self.runs.save_batch_state,
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_preflight",
            driver_checkpoint=driver_checkpoint,
            assistant_message=assistant_message,
        )
        await self.events.publish_committed(committed_events)

        calls: list[ToolBatchCall] = []
        pending_events: list[EventRecord] = []
        exclusive_tools = self._exclusive_tools_in_mixed_batch(tool_calls)
        if exclusive_tools:
            calls = [
                ToolBatchCall(
                    request=request,
                    status="failed",
                    result=ToolResult(
                        tool_call_id=request.id,
                        tool_name=request.name,
                        is_error=True,
                        content=(
                            "No tools were executed. These tools must each be the only "
                            f"tool call in a response: {', '.join(exclusive_tools)}."
                        ),
                        structured={
                            "error_type": "ExclusiveToolBatchViolation",
                            "exclusive_tools": exclusive_tools,
                        },
                        ended_at=utc_now(),
                    ),
                )
                for request in tool_calls
            ]
            pending_events.append(
                EventRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="tool_batch_rejected",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": turn_id,
                        "reason": "exclusive_tool_mixed_batch",
                        "exclusive_tools": exclusive_tools,
                    },
                )
            )
        else:
            for request in tool_calls:
                call, call_events = await self._preflight_call(
                    session=session,
                    run=run,
                    batch_id=batch.batch_id,
                    turn_id=turn_id,
                    request=request,
                    producer=batch.producer,
                    abort_signal=abort_signal,
                )
                calls.append(call)
                pending_events.extend(call_events)
        batch = batch.model_copy(update={"calls": calls, "updated_at": utc_now()})

        suspension = tool_batch_suspension(batch)
        if suspension is not None:
            batch = batch.model_copy(
                update={"phase": suspension.batch_phase, "updated_at": utc_now()}
            )
            session, run, batch, committed_events = await run_blocking(
                self.runs.save_batch_state,
                session=session,
                run=run,
                batch=batch,
                status=suspension.status,
                phase=suspension.run_phase,
                events=tuple(pending_events),
            )
            await self.events.publish_committed(committed_events)
            return ToolBatchHandlingResult(
                session=session,
                run=run,
                status=suspension.status,
                had_tool_error=any(call.result.is_error for call in batch.calls if call.result),
            )

        return await self.execute_batch(
            session=session,
            run=run,
            batch=batch,
            abort_signal=abort_signal,
            events=tuple(pending_events),
            driver_continues=True,
        )

    async def execute_batch(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        abort_signal: AbortSignal,
        events: tuple[EventRecord, ...] = (),
        driver_continues: bool = False,
    ) -> ToolBatchHandlingResult:
        ready_calls = [call for call in batch.calls if call.status == "ready" and call.invocation]
        batch = batch.model_copy(update={"phase": "executing", "updated_at": utc_now()})
        _, _, _, committed_events = await run_blocking(
            self.runs.save_batch_state,
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_execution",
            events=events,
        )
        await self.events.publish_committed(committed_events)
        checkpoint_batch = batch
        head = batch.head()
        positions = {call.request.id: index for index, call in enumerate(batch.calls)}
        checkpoint_lock = asyncio.Lock()
        aborted_events: list[EventRecord] = []
        resumed_harness: dict[str, HarnessCallSnapshot] = {}
        execution_started_at: dict[str, tuple[datetime, float]] = {}

        def current_call(tool_call_id: str) -> ToolBatchCall:
            return checkpoint_batch.calls[positions[tool_call_id]]

        def replace_call(call: ToolBatchCall) -> ToolCallBatchRecord:
            calls = checkpoint_batch.calls.copy()
            calls[positions[call.request.id]] = call
            return checkpoint_batch.model_copy(update={"calls": calls, "updated_at": utc_now()})

        async def recheck_before_execute(
            invocation: ToolInvocation,
        ) -> ToolExecutionOutcome:
            async with checkpoint_lock:
                call = current_call(invocation.tool_call_id)
                current, prepared = await self._prepare_call(
                    session=session,
                    run=run,
                    invocation=invocation,
                    phase="execution_boundary",
                    approved=call.approved,
                )
                if (
                    prepared.status == "approval_required"
                    and current.interaction_mode == "unattended"
                ):
                    return _interaction_error_outcome(
                        prepared.invocation,
                        error_type="ApprovalUnavailable",
                        error_message=(
                            "tool permission tightened before execution while the Session "
                            "was unattended"
                        ),
                        structured={"policy_reason": prepared.invocation.tool_policy_reason},
                    )
                return prepared

        async def checkpoint_started(invocation: ToolInvocation) -> None:
            nonlocal checkpoint_batch
            async with checkpoint_lock:
                call = current_call(invocation.tool_call_id)
                checkpoint_batch = replace_call(
                    call.model_copy(update={"status": "running", "invocation": invocation})
                )
                suspension = tool_batch_suspension(checkpoint_batch)
                _, _, checkpoint_batch, committed_events = await run_blocking(
                    self.runs.save_batch_state,
                    session=session,
                    run=run,
                    batch=checkpoint_batch,
                    status=suspension.status if suspension is not None else "running",
                    phase=suspension.run_phase if suspension is not None else "tool_execution",
                    changed_call_ids=frozenset({invocation.tool_call_id}),
                    started_call_ids=frozenset({invocation.tool_call_id}),
                )
                await self.events.publish_committed(committed_events)
                execution_started_at[invocation.tool_call_id] = (utc_now(), monotonic())

        async def preserve_late_result(invocation: ToolInvocation, value: ToolResultValue) -> None:
            call = current_call(invocation.tool_call_id)
            await run_blocking(
                self.runs.save_late_tool_result,
                identity=head.identity(run.run_id, invocation.tool_call_id),
                attempt=call.attempt,
                invocation=invocation,
                result=_complete_tool_result_timing(
                    value, started=execution_started_at.get(invocation.tool_call_id)
                ),
            )

        def outcome_events(
            completed_call: ToolBatchCall, outcome: ToolExecutionOutcome
        ) -> tuple[EventRecord, ...]:
            pending_events: list[EventRecord] = []
            resolution = completed_call.input_resolution
            if isinstance(resolution, UnattendedResolution):
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="ask_user_resolved",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": completed_call.request.id,
                            "tool_call_status": completed_call.status,
                            "resolution": resolution.model_dump(mode="json"),
                            "resolved_by": resolution.resolved_by,
                            "resolver_id": resolution.resolver_id,
                        },
                    )
                )
            if outcome.status == "approval_required":
                assert completed_call.approval is not None
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="approval_requested",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "approval_id": completed_call.approval.approval_id,
                            "tool_call_id": completed_call.request.id,
                            "tool_name": completed_call.request.name,
                        },
                    )
                )
            elif outcome.status == "waiting_user":
                assert completed_call.input_request is not None
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="ask_user_requested",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": completed_call.request.id,
                            "tool_name": completed_call.request.name,
                            "input_revision": completed_call.input_revision,
                            "request": completed_call.input_request.data,
                            "source_index": completed_call.request.source_index,
                            **_interaction_tool_identity(completed_call.invocation),
                        },
                    )
                )
            elif outcome.status == "waiting_child":
                assert completed_call.agent_wait is not None
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="agent_wait_requested",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": completed_call.request.id,
                            "wait_id": completed_call.agent_wait.wait_id,
                            "delegation_id": completed_call.agent_wait.delegation_id,
                        },
                    )
                )
            elif completed_call.result is not None:
                result = completed_call.result
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="tool_call_completed",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": result.tool_call_id,
                            "tool_name": result.tool_name,
                            "status": completed_call.status,
                            "is_error": result.is_error,
                            "started_at": result.started_at.isoformat()
                            if result.started_at is not None
                            else None,
                            "ended_at": result.ended_at.isoformat()
                            if result.ended_at is not None
                            else None,
                            "result_data": result.structured,
                            "source_index": completed_call.request.source_index,
                            "outcome_attempt": None
                            if completed_call.status == "unknown_outcome"
                            else completed_call.attempt,
                            "feedback": completed_call.feedback.model_dump(mode="json")
                            if completed_call.feedback
                            else None,
                        },
                    )
                )
            return tuple(pending_events)

        async def checkpoint_outcome(outcome: ToolExecutionOutcome) -> None:
            nonlocal checkpoint_batch
            call_harness = resumed_harness.pop(
                outcome.invocation.tool_call_id, execution_context.harness
            )
            outcome = await self._apply_agent_control(
                outcome,
                identity=head.identity(run.run_id, outcome.invocation.tool_call_id),
                session=session,
                run=run,
                context=execution_context,
            )
            if outcome.result is not None:
                outcome = outcome.model_copy(
                    update={
                        "result": _complete_tool_result_timing(
                            outcome.result,
                            started=execution_started_at.pop(
                                outcome.invocation.tool_call_id,
                                None,
                            ),
                        )
                    }
                )
            if outcome.result is not None and outcome.status == "completed":
                observed_call = current_call(outcome.invocation.tool_call_id)
                # The observation itself settles a started call unless a controller
                # still has to decide the tool's proposed Session transitions.
                settled = (
                    observed_call.with_outcome(outcome)
                    if observed_call.status == "running"
                    and not outcome.result.session_control_transitions
                    else None
                )
                observation_events = await run_blocking(
                    self.runs.save_tool_result,
                    identity=head.identity(run.run_id, outcome.invocation.tool_call_id),
                    attempt=observed_call.attempt,
                    invocation=outcome.invocation,
                    result=outcome.result,
                    events=outcome_events(settled, outcome) if settled is not None else (),
                )
                if observation_events is None:
                    raise SessionBusyError("tool result belongs to an inactive execution")
                if settled is not None:
                    async with checkpoint_lock:
                        checkpoint_batch = replace_call(settled)
                    await self.events.publish_committed(observation_events)
                    return
            outcome = await self._stage_control_transitions(
                outcome, session=session, harness=call_harness
            )
            async with checkpoint_lock:
                item = current_call(outcome.invocation.tool_call_id)
                completed_call = item.with_outcome(outcome)
                checkpoint_batch = replace_call(completed_call)
                pending_events = outcome_events(completed_call, outcome)
                try:
                    suspension = tool_batch_suspension(checkpoint_batch)
                    _, _, checkpoint_batch, committed_events = await run_blocking(
                        self.runs.save_batch_state,
                        session=session,
                        run=run,
                        batch=checkpoint_batch,
                        harness=call_harness if outcome.status == "waiting_user" else None,
                        status=suspension.status if suspension is not None else "running",
                        phase=(
                            suspension.run_phase if suspension is not None else "tool_execution"
                        ),
                        changed_call_ids=frozenset({outcome.invocation.tool_call_id}),
                        events=pending_events,
                    )
                    await self.events.publish_committed(committed_events)
                except SessionBusyError:
                    if not abort_signal.is_aborted():
                        raise
                    aborted_events.extend(
                        event
                        for event in pending_events
                        if event.event_type == "tool_call_completed"
                    )

        async def resolve_outcome(outcome: ToolExecutionOutcome) -> ToolExecutionOutcome:
            nonlocal checkpoint_batch
            if outcome.status != "waiting_user":
                return outcome
            current = await run_blocking(self.runs.store.load_session_record, session.session_id)
            if current.interaction_mode != "unattended":
                return outcome
            request = outcome.input_request
            if request is None:
                raise ValueError("waiting tool outcome has no input request")
            resolution = UnattendedResolution(
                resolver_id=_AFK_RESOLVER_ID,
                reason=_AFK_UNATTENDED_REASON,
            )
            async with checkpoint_lock:
                call = current_call(outcome.invocation.tool_call_id)
                resolving_call = call.model_copy(
                    update={
                        "status": "resolving_input",
                        "invocation": outcome.invocation,
                        "input_request": request,
                        "input_resolution": resolution,
                        "input_revision": call.input_revision + 1,
                    }
                )
                checkpoint_batch = replace_call(resolving_call)
                requested = EventRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="ask_user_requested",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": batch.turn_id,
                        "tool_call_id": resolving_call.request.id,
                        "tool_name": resolving_call.request.name,
                        "request": request.data,
                        "input_revision": resolving_call.input_revision,
                        "source_index": resolving_call.request.source_index,
                        "interaction_mode": current.interaction_mode,
                        "will_suspend": False,
                        **_interaction_tool_identity(resolving_call.invocation),
                    },
                )
                _, _, checkpoint_batch, committed_events = await run_blocking(
                    self.runs.save_batch_state,
                    session=session,
                    run=run,
                    batch=checkpoint_batch,
                    status="running",
                    phase="human_interaction",
                    harness=execution_context.harness,
                    events=(requested,),
                    changed_call_ids=frozenset({outcome.invocation.tool_call_id}),
                )
                await self.events.publish_committed(committed_events)
                current, resume_harness = await run_blocking(
                    capture_session_harness,
                    self.runs.store,
                    session.session_id,
                    run=run,
                    usage_service=self.runs.usage_service,
                )
                resumed_harness[outcome.invocation.tool_call_id] = resume_harness
            return await self._resume_input(
                resolving_call,
                resolution=resolution,
                session=current,
                run=run,
                on_start=checkpoint_started,
                context=replace(execution_context, harness=resume_harness),
            )

        execution_context = ToolExecutionContext(
            harness=await run_blocking(
                capture_harness,
                self.runs.store,
                session.session_id,
                run=run,
                usage_service=self.runs.usage_service,
            ),
            signal=abort_signal,
            on_late_result=preserve_late_result,
            workspace=session.workspace or self.default_workspace,
            max_output_chars=self.execution_config.max_output_chars,
        )
        await self.batch_executor.execute(
            [call.invocation for call in ready_calls if call.invocation is not None],
            mode=batch.execution_mode,
            max_concurrency=batch.max_concurrency,
            signal=abort_signal,
            context=execution_context,
            pre_execute=recheck_before_execute,
            resolve_outcome=resolve_outcome,
            on_start=checkpoint_started,
            on_outcome=checkpoint_outcome,
        )
        if abort_signal.is_aborted():
            aborted_batch = checkpoint_batch.model_copy(
                update={"phase": "aborted", "updated_at": utc_now()}
            )
            run, committed_events = await run_blocking(
                self.runs.save_aborted_batch,
                session_id=session.session_id,
                run_id=run.run_id,
                batch=aborted_batch,
                events=tuple(aborted_events),
            )
            await self.events.publish_committed(committed_events)
            return ToolBatchHandlingResult(
                session=session,
                run=run,
                status="completed",
            )
        suspension = tool_batch_suspension(checkpoint_batch)
        if suspension is not None:
            waiting_batch = checkpoint_batch.model_copy(
                update={"phase": suspension.batch_phase, "updated_at": utc_now()}
            )
            session, run, waiting_batch, committed_events = await run_blocking(
                self.runs.save_batch_state,
                session=session,
                run=run,
                batch=waiting_batch,
                status=suspension.status,
                phase=suspension.run_phase,
            )
            await self.events.publish_committed(committed_events)
            if suspension.status == "waiting_child":
                assert suspension.call.agent_wait is not None
                if self.agent_controls is None:
                    raise RuntimeError("Agent control executor is unavailable")
                await self.agent_controls.wait_parked(
                    session=session,
                    run=run,
                    batch=waiting_batch,
                    tool_call_id=suspension.call.request.id,
                    wait=suspension.call.agent_wait,
                )
            return ToolBatchHandlingResult(
                session=session,
                run=run,
                status=suspension.status,
                had_tool_error=any(
                    call.result.is_error for call in waiting_batch.calls if call.result
                ),
            )
        batch = checkpoint_batch
        await self.events.emit(
            run,
            event_type="tool_batch_results_committing",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "tool_result_count": len(batch.calls),
            },
        )
        session, run, completed_batch, _, committed_events = await run_blocking(
            self.runs.publish_tool_batch,
            session=session,
            run=run,
            batch=batch,
            driver_continues=driver_continues,
        )
        await self.events.publish_committed(committed_events)
        for call in completed_batch.calls:
            assert call.result is not None
            await self.after(session, run, call.result, invocation=call.invocation)
        return ToolBatchHandlingResult(
            session=session,
            run=run,
            status="completed",
            had_tool_error=any(
                call.result.is_error for call in completed_batch.calls if call.result
            ),
        )

    async def _apply_agent_control(
        self,
        outcome: ToolExecutionOutcome,
        *,
        identity: ToolCallIdentity,
        session: SessionRecord,
        run: RunRecord,
        context: ToolExecutionContext,
    ) -> ToolExecutionOutcome:
        request = outcome.control_request
        if request is None:
            return outcome
        invocation = outcome.invocation
        controls = self.agent_controls
        if controls is None:
            return _agent_control_error(
                invocation,
                error_type="AgentControlUnavailable",
                error_message="Agent control is unavailable on this Host",
            )
        try:
            control = await context.invoke_owned(
                lambda: controls.execute_control(
                    request,
                    invocation=invocation,
                    identity=identity,
                    session=session,
                    run=run,
                )
            )
        except ToolExecutionAborted:
            return ToolExecutionOutcome(status="aborted", invocation=invocation)
        except (CallbackCancelled, ToolExecutionInterrupted) as exc:
            if isinstance(exc.result, AgentControlResult):
                observed = _agent_control_outcome(invocation, exc.result)
                if observed.result is not None and context.on_late_result is not None:
                    await context.on_late_result(
                        invocation,
                        observed.result.observation(),
                    )
            if isinstance(exc, ToolExecutionInterrupted) and exc.propagate_cancel:
                raise asyncio.CancelledError from None
            return ToolExecutionOutcome(status="unknown_outcome", invocation=invocation)
        except Exception as exc:  # noqa: BLE001 - application control boundary.
            return _agent_control_error(
                invocation,
                error_type=type(exc).__name__,
                error_message=redact_text(str(exc)),
            )
        assert isinstance(control, AgentControlResult)
        return _agent_control_outcome(invocation, control)

    async def _stage_control_transitions(
        self,
        outcome: ToolExecutionOutcome,
        *,
        session: SessionRecord,
        harness: HarnessCallSnapshot | None,
    ) -> ToolExecutionOutcome:
        result = outcome.result
        if result is None or not result.session_control_transitions:
            return outcome
        invocation = outcome.invocation
        if (
            self.session_controls is None
            or harness is None
            or invocation.plugin_id is None
            or invocation.generation_id is None
            or invocation.execution_contract_hash is None
        ):
            return _settled_control_outcome(
                outcome,
                ToolResultFeedback(
                    error_type="SessionControlContractError",
                    error_message=(
                        "Tool Session control transition has no active plugin execution contract."
                    ),
                ),
            )
        applied: list[SessionControlTransitionResult] = []
        draft = accept_state_changes(
            harness, [result.harness_change] if result.harness_change else []
        )
        try:
            for transition in result.session_control_transitions:
                attributed = AttributedSessionControlTransition(
                    **transition.model_dump(mode="python"),
                    plugin_id=invocation.plugin_id,
                    generation_id=invocation.generation_id,
                    execution_contract_hash=invocation.execution_contract_hash,
                )
                draft, settled = await self.session_controls.stage(
                    attributed,
                    session=session,
                    draft=draft,
                    controllers=self.session_controllers,
                    workspace=session.workspace or self.default_workspace,
                )
                applied.append(settled)
        except SessionControlError as exc:
            return _settled_control_outcome(
                outcome,
                ToolResultFeedback(error_type=type(exc).__name__, error_message=str(exc)),
            )
        change = HarnessStateChange(
            session_id=draft.session_id,
            harness_version=draft.harness_version,
            history_epoch=draft.history_epoch,
            last_message_id=draft.last_message_id,
            owner_id=invocation.plugin_id,
            state=next(
                (item for item in draft.state.states if item.owner_id == invocation.plugin_id), None
            ),
            controls=draft.state.controls,
        )
        return _settled_control_outcome(
            outcome, ToolResultFeedback(controls=tuple(applied)), change=change
        )

    async def resolve_user_answer(
        self,
        batch: ToolCallBatchRecord,
        *,
        session: SessionRecord,
        run: RunRecord,
        tool_call_id: str,
        answer: JsonValue,
        harness: HarnessCallSnapshot,
        signal: AbortSignal,
        workspace: str | None = None,
    ) -> ToolCallBatchRecord:
        answer = _JSON_VALUE_ADAPTER.validate_python(answer)
        resolution = AnswerResolution(value=answer)
        call = next((item for item in batch.calls if item.request.id == tool_call_id), None)
        if call is None:
            raise ValueError(f"unknown tool call: {tool_call_id}")
        if call.status != "waiting_user" or call.invocation is None:
            raise ValueError(f"tool call is not waiting for user input: {tool_call_id}")
        identity = batch.head().identity(run.run_id, tool_call_id)
        attempt: int | None = None
        resume_started: tuple[datetime, float] | None = None

        async def start_resume(invocation: ToolInvocation) -> None:
            nonlocal attempt, resume_started
            started = call.model_copy(
                update={
                    "status": "running",
                    "invocation": invocation,
                    "input_resolution": resolution,
                }
            )
            _, _, started_batch, committed_events = await run_blocking(
                self.runs.save_batch_state,
                session=session,
                run=run,
                batch=batch.model_copy(
                    update={
                        "calls": [
                            started if item.request.id == tool_call_id else item
                            for item in batch.calls
                        ],
                    }
                ),
                status="running",
                phase="tool_execution",
                changed_call_ids=frozenset({tool_call_id}),
                started_call_ids=frozenset({tool_call_id}),
            )
            await self.events.publish_committed(committed_events)
            attempt = next(
                item.attempt for item in started_batch.calls if item.request.id == tool_call_id
            )

            resume_started = (utc_now(), monotonic())

        async def preserve_resume_result(
            invocation: ToolInvocation, value: ToolResultValue
        ) -> None:
            assert attempt is not None
            await run_blocking(
                self.runs.save_late_tool_result,
                identity=identity,
                attempt=attempt,
                invocation=invocation,
                result=_complete_tool_result_timing(value, started=resume_started),
            )

        context = ToolExecutionContext(
            signal=signal,
            harness=harness,
            workspace=workspace or self.default_workspace,
            max_output_chars=self.execution_config.max_output_chars,
            on_late_result=preserve_resume_result,
        )
        outcome = await self._resume_input(
            call,
            resolution=resolution,
            session=session,
            run=run,
            on_start=start_resume,
            context=context,
        )
        if attempt is not None:
            outcome = await self._apply_agent_control(
                outcome,
                identity=identity,
                session=session,
                run=run,
                context=context,
            )
            if outcome.result is not None:
                outcome = outcome.model_copy(
                    update={
                        "result": _complete_tool_result_timing(
                            outcome.result, started=resume_started
                        )
                    }
                )
            if outcome.status == "completed" and outcome.result is not None:
                accepted = await run_blocking(
                    self.runs.save_tool_result,
                    identity=identity,
                    attempt=attempt,
                    invocation=outcome.invocation,
                    result=outcome.result,
                )
                if accepted is None:
                    raise SessionBusyError("tool result belongs to an inactive execution")
            outcome = await self._stage_control_transitions(
                outcome, session=session, harness=harness
            )
        resolved = call.with_outcome(outcome, resolution=resolution)
        resolved_calls = [
            resolved if item.request.id == tool_call_id else item for item in batch.calls
        ]
        phase = (
            "ready"
            if all(
                call.status not in {"awaiting_approval", "waiting_user"} for call in resolved_calls
            )
            else "awaiting_interaction"
        )
        return batch.model_copy(
            update={"calls": resolved_calls, "phase": phase, "updated_at": utc_now()}
        )

    async def _resume_input(
        self,
        call: ToolBatchCall,
        *,
        resolution: ToolInputResolution,
        session: SessionRecord,
        run: RunRecord,
        on_start: Callable[[ToolInvocation], Awaitable[None]],
        context: ToolExecutionContext,
    ) -> ToolExecutionOutcome:
        original, request = call.invocation, call.input_request
        assert original is not None and request is not None
        invocation = original
        tool = None
        if invocation.resumable is not False:
            _, prepared = await self._prepare_call(
                session=session,
                run=run,
                invocation=invocation,
                phase="resume",
                harness=context.harness,
                approved=call.approved,
            )
            if prepared.status == "approval_required":
                return _interaction_error_outcome(
                    invocation,
                    error_type="ToolResumeApprovalRequired",
                    error_message="Tool resume requires a new approval under the current policy.",
                    structured={"policy_reason": prepared.invocation.tool_policy_reason},
                )
            if prepared.status != "ready":
                # No new attempt started. The rejection belongs to the original Call.
                return prepared.model_copy(update={"invocation": invocation})
            tool = self.registry.get(invocation.tool_name)
            invocation = prepared.invocation
        if (
            tool is None
            or not tool.definition.resumable
            or (
                isinstance(resolution, UnattendedResolution)
                and not tool.definition.supports_unattended
            )
        ):
            if isinstance(resolution, AnswerResolution):
                return ToolExecutionOutcome(
                    status="completed",
                    invocation=invocation,
                    result=ToolResult(
                        tool_call_id=call.request.id,
                        tool_name=call.request.name,
                        content=_tool_input_result_content(resolution.value),
                        structured={"answer": resolution.value},
                        ended_at=utc_now(),
                    ),
                )
            return _user_input_unavailable(original, resolution, request)
        await on_start(invocation)
        resumed = await self.executor.resume_prepared(
            tool,
            invocation,
            resolution=resolution,
            request=request,
            context=context,
        )
        if resumed.status != "waiting_user" or isinstance(resolution, AnswerResolution):
            return resumed
        assert resumed.input_request is not None
        return _user_input_unavailable(resumed.invocation, resolution, resumed.input_request)

    async def after(
        self,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
        *,
        invocation: ToolInvocation | None,
    ) -> None:
        structured = cast(
            "Mapping[str, FrozenJson]",
            freeze_json(result.structured),
        )
        await self.events.observe(
            ToolCallFinishedObservation(
                session_id=session.session_id,
                run_id=run.run_id,
                tool_call_id=result.tool_call_id,
                tool_name=result.tool_name,
                canonical_tool_id=(
                    invocation.canonical_tool_id
                    if invocation is not None and invocation.canonical_tool_id is not None
                    else f"ouro.host/tools/{result.tool_name}"
                ),
                owner_id=(
                    invocation.owner_id
                    if invocation is not None and invocation.owner_id is not None
                    else "ouro.host"
                ),
                effect=invocation.effect if invocation is not None else "unknown",
                batch_behavior=(
                    invocation.batch_behavior if invocation is not None else "compatible"
                ),
                is_error=result.is_error,
                content=result.text_content[:_OBSERVER_TOOL_CONTENT_LIMIT],
                structured=structured,
                resource_refs=tuple(result.resource_refs),
                exit_code=result.exit_code,
            )
        )

    async def _preflight_call(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch_id: str,
        turn_id: str,
        request: ToolCallRequest,
        producer: ToolCallProducer,
        abort_signal: AbortSignal,
    ) -> tuple[ToolBatchCall, tuple[EventRecord, ...]]:
        pending_events: list[EventRecord] = []
        try:
            definition = self.registry.get(request.name).definition
            metadata = definition.metadata
        except KeyError:
            # Preserve the executor's typed unknown-tool outcome.  Plugin
            # identity is enrichment for known tools, not a new lookup failure
            # path that bypasses model recovery.
            definition = None
            metadata = {}
        invocation = ToolInvocation(
            producer=producer,
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=turn_id,
            tool_call_id=request.id,
            tool_name=request.name,
            canonical_tool_id=definition.identity
            if definition
            else f"ouro.host/tools/{request.name}",
            owner_id=_optional_text(metadata.get("plugin_id")) or "ouro.host",
            source=definition.source if definition is not None else "builtin",
            effect=definition.effect if definition is not None else "unknown",
            batch_behavior=(definition.batch_behavior if definition is not None else "compatible"),
            permission_control=(
                definition.permission_control if definition is not None else "host"
            ),
            arguments=request.arguments,
            resumable=definition.resumable if definition is not None else None,
            execution_variant=definition.execution_variant if definition is not None else None,
            plugin_id=_optional_text(metadata.get("plugin_id")),
            plugin_version=_optional_text(metadata.get("plugin_version")),
            plugin_class=_optional_plugin_class(metadata.get("plugin_class")),
            release_id=_optional_text(metadata.get("release_id")),
            package_digest=_optional_text(metadata.get("package_digest")),
            generation_id=_optional_text(metadata.get("generation_id")),
            execution_contract_hash=_optional_text(metadata.get("execution_contract_hash")),
            active_when_controller_id=(
                definition.active_when_controller_id if definition is not None else None
            ),
            interaction_controller_id=(
                definition.interaction_controller_id if definition is not None else None
            ),
        )
        arguments = cast(
            "Mapping[str, FrozenJson]",
            freeze_json(request.arguments),
        )
        await self.events.observe(
            ToolCallRequestedObservation(
                session_id=session.session_id,
                run_id=run.run_id,
                tool_call_id=request.id,
                tool_name=request.name,
                canonical_tool_id=invocation.canonical_tool_id or f"ouro.host/tools/{request.name}",
                owner_id=invocation.owner_id or "ouro.host",
                effect=invocation.effect,
                batch_behavior=invocation.batch_behavior,
                arguments=arguments,
            )
        )
        if abort_signal.is_aborted() or self.is_aborted(session.session_id, run.run_id):
            result = ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                is_error=True,
                content=abort_signal.reason,
                structured={"aborted": True},
                ended_at=utc_now(),
            )
            return (
                ToolBatchCall(
                    request=request, status="aborted", invocation=invocation, result=result
                ),
                tuple(pending_events),
            )

        session, outcome = await self._prepare_call(
            session=session,
            run=run,
            invocation=invocation,
            phase="preflight",
        )
        if outcome.status == "approval_required":
            assert outcome.approval is not None
            pending_events.append(
                EventRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="approval_requested",
                    payload={
                        "batch_id": batch_id,
                        "turn_id": turn_id,
                        "approval_id": outcome.approval.approval_id,
                        "tool_call_id": request.id,
                        "tool_name": request.name,
                    },
                )
            )
            if session.interaction_mode == "unattended":
                approval = outcome.approval.model_copy(
                    update={
                        "status": "approved",
                        "resolved_at": utc_now(),
                        "resolved_by": "host_policy",
                        "resolver_id": _AFK_RESOLVER_ID,
                    }
                )
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="approval_resolved",
                        payload={
                            "batch_id": batch_id,
                            "turn_id": turn_id,
                            "approval_id": approval.approval_id,
                            "tool_call_id": request.id,
                            "tool_call_status": "ready",
                            "approved": True,
                            "reason": None,
                            "resolved_by": approval.resolved_by,
                            "resolver_id": approval.resolver_id,
                        },
                    )
                )
                return (
                    ToolBatchCall(
                        request=request,
                        status="ready",
                        invocation=outcome.invocation,
                        approval=approval,
                    ),
                    tuple(pending_events),
                )
            return (
                ToolBatchCall(
                    request=request,
                    status="awaiting_approval",
                    invocation=outcome.invocation,
                    approval=outcome.approval,
                ),
                tuple(pending_events),
            )
        if outcome.status == "ready":
            return (
                ToolBatchCall(request=request, status="ready", invocation=outcome.invocation),
                tuple(pending_events),
            )
        return (
            ToolBatchCall(
                request=request,
                status="failed" if outcome.result and outcome.result.is_error else "completed",
                invocation=outcome.invocation,
                result=outcome.result,
            ),
            tuple(pending_events),
        )

    async def _prepare_call(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        invocation: ToolInvocation,
        phase: str,
        approved: bool = False,
        harness: HarnessCallSnapshot | None = None,
    ) -> tuple[SessionRecord, ToolExecutionOutcome]:
        # Current controls may activate a previously hidden policy at this boundary.
        has_policies = bool(self.tool_policies.snapshot())
        if harness is None:
            if has_policies:
                session, harness = await run_blocking(
                    capture_session_harness,
                    self.runs.store,
                    session.session_id,
                    run=run,
                    usage_service=self.runs.usage_service,
                )
            else:
                session = await run_blocking(
                    self.runs.store.load_session_record, session.session_id
                )
        control_error = self.refresh_controls(session.controls) if self.refresh_controls else None
        tool = self.executor.resolve_call(invocation)
        if isinstance(tool, ToolExecutionOutcome):
            return session, tool
        mode: ToolPolicyMode | None = "deny" if control_error else None
        reason = control_error
        if has_policies and not control_error:
            invocation, mode, reason = await self._evaluate_policies(
                invocation, session, run, phase, harness
            )
        return session, self.executor.authorize(
            invocation,
            tool.definition,
            approved=approved,
            approval_mode=session.approval_mode,
            policy_mode=mode,
            policy_reason=reason,
        )

    async def _evaluate_policies(
        self,
        invocation: ToolInvocation,
        session: SessionRecord,
        run: RunRecord,
        phase: str,
        harness: HarnessCallSnapshot | None,
    ) -> tuple[ToolInvocation, ToolPolicyMode | None, str | None]:
        policy = await self.tool_policies.evaluate(
            ToolPolicyCall(
                harness=harness,
                session_id=session.session_id,
                run_id=run.run_id,
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                canonical_tool_id=(
                    invocation.canonical_tool_id or f"ouro.host/tools/{invocation.tool_name}"
                ),
                owner_id=invocation.owner_id or "ouro.host",
                arguments=invocation.arguments,
                source=invocation.source,
                effect=invocation.effect,
                batch_behavior=invocation.batch_behavior,
                permission_control=invocation.permission_control,
                workspace=session.workspace or self.default_workspace,
            )
        )
        if not policy.decisions and not policy.failures:
            return invocation, None, None
        policy_ids = [
            f"{item.plugin_id}/tool_policies/{item.policy_id}" for item in policy.decisions
        ]
        policy_ids.extend(
            f"{failure.plugin_id}/tool_policies/{failure.policy_id}" for failure in policy.failures
        )
        await self.events.emit(
            run,
            event_type="tool_policy_evaluated",
            payload={
                "tool_call_id": invocation.tool_call_id,
                "tool_name": invocation.tool_name,
                "phase": phase,
                "mode": policy.mode,
                "reason": policy.reason,
                "policy_ids": list(dict.fromkeys(policy_ids)),
                "failures": [failure.model_dump(mode="json") for failure in policy.failures],
            },
        )
        invocation = invocation.model_copy(
            update={
                "tool_policy_ids": list(dict.fromkeys([*invocation.tool_policy_ids, *policy_ids])),
                "tool_policy_reason": policy.reason or invocation.tool_policy_reason,
            }
        )
        return invocation, policy.mode, policy.reason

    def _exclusive_tools_in_mixed_batch(self, calls: list[ToolCallRequest]) -> list[str]:
        if len(calls) < 2:
            return []
        exclusive: list[str] = []
        for call in calls:
            try:
                definition = self.registry.get(call.name).definition
            except KeyError:
                continue
            if definition.batch_behavior == "exclusive":
                exclusive.append(call.name)
        return list(dict.fromkeys(exclusive))

    def _effective_mode(self, calls: list[ToolCallRequest]) -> ToolExecutionMode:
        if self.execution_config.mode == "sequential":
            return "sequential"
        for call in calls:
            try:
                definition = self.registry.get(call.name).definition
            except KeyError:
                continue
            if definition.execution_mode == "sequential":
                return "sequential"
        return "parallel"


def _tool_input_result_content(answer: object) -> str:
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":"))


def _complete_tool_result_timing[ResultT: ToolResultValue](
    result: ResultT,
    *,
    started: tuple[datetime, float] | None,
) -> ResultT:
    if started is None:
        instant = result.ended_at or result.started_at
        return result.model_copy(update={"started_at": instant, "ended_at": instant})
    started_at, monotonic_started_at = started
    elapsed = max(monotonic() - monotonic_started_at, 0.0)
    return result.model_copy(
        update={
            "started_at": started_at,
            "ended_at": started_at + timedelta(seconds=elapsed),
        }
    )


def _settled_control_outcome(
    outcome: ToolExecutionOutcome,
    feedback: ToolResultFeedback,
    *,
    change: HarnessStateChange | None = None,
) -> ToolExecutionOutcome:
    assert outcome.result is not None
    return outcome.model_copy(
        update={
            "result": feedback.materialize(outcome.result).model_copy(
                update={"harness_change": change, "session_control_transitions": []}
            ),
            "feedback": feedback,
        }
    )


def _agent_control_outcome(
    invocation: ToolInvocation, control: AgentControlResult
) -> ToolExecutionOutcome:
    if control.wait is not None:
        return ToolExecutionOutcome(
            status="waiting_child", invocation=invocation, agent_wait=control.wait
        )
    return ToolExecutionOutcome(
        status="completed",
        invocation=invocation,
        result=ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            is_error=control.is_error,
            content=control.content,
            structured=control.structured,
            resource_refs=list(control.resource_refs),
            ended_at=utc_now(),
        ),
    )


def _agent_control_error(
    invocation: ToolInvocation,
    *,
    error_type: str,
    error_message: str,
) -> ToolExecutionOutcome:
    return ToolExecutionOutcome(
        status="completed",
        invocation=invocation,
        result=ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            is_error=True,
            content=(f"Tool {invocation.tool_name} failed: [{error_type}] {error_message}"),
            structured={
                "error_type": error_type,
                "error_message": error_message,
            },
            ended_at=utc_now(),
        ),
    )


def _user_input_unavailable(
    invocation: ToolInvocation, resolution: UnattendedResolution, request: ToolInputRequest
) -> ToolExecutionOutcome:
    return _interaction_error_outcome(
        invocation,
        error_type="UserInputUnavailable",
        error_message="The tool requested input while the user is unavailable.",
        structured={
            "resolved_by": resolution.resolved_by,
            "resolver_id": resolution.resolver_id,
            "input_request": request.data,
        },
    )


def _interaction_error_outcome(
    invocation: ToolInvocation,
    *,
    error_type: str,
    error_message: str,
    structured: dict[str, object],
) -> ToolExecutionOutcome:
    result = ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        is_error=True,
        content=f"Tool {invocation.tool_name} failed: [{error_type}] {error_message}",
        structured={
            "error_type": error_type,
            "error_message": error_message,
            **structured,
        },
        ended_at=utc_now(),
    )
    return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)


def _interaction_tool_identity(invocation: ToolInvocation | None) -> dict[str, JsonValue]:
    """Project the canonical identity required by Host interaction adapters."""

    return {
        "canonical_tool_id": invocation.canonical_tool_id if invocation is not None else None,
        "plugin_id": invocation.plugin_id if invocation is not None else None,
        "plugin_class": invocation.plugin_class if invocation is not None else None,
    }
