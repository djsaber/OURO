from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Literal

from pydantic import JsonValue, TypeAdapter

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs import RunLifecycle
from ouro.application.session_controls import (
    SessionControlCoordinator,
    SessionControlError,
)
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.approvals import ApprovalResolvedBy
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.errors import SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookRegistry
from ouro.core.interactions import AnswerResolution, UnattendedResolution
from ouro.core.messages import MessageRecord, ProviderContinuationArtifact
from ouro.core.session_controls import AttributedSessionControlTransition
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import (
    ToolBatchCall,
    ToolBatchHandlingResult,
    ToolBatchOutcomeStatus,
    ToolCallBatchRecord,
    ToolCallStatus,
    ToolExecutionMode,
)
from ouro.core.tool_policies import ToolPolicyCall, ToolPolicyMode, ToolPolicyRegistry
from ouro.core.tools import (
    ToolCallRequest,
    ToolExecutionOutcome,
    ToolInputRequest,
    ToolInvocation,
    ToolResult,
)
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.tools import ToolBatchExecutor, ToolExecutionContext, ToolExecutor, ToolRegistry
from ouro.tools.schema import tool_argument_error

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_AFK_RESOLVER_ID = "ouro.afk"
_AFK_UNATTENDED_REASON = "AFK mode does not wait for user input"


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
        hooks: HookRegistry,
        events: RuntimeEventRecorder,
        runs: RunLifecycle,
        execution_config: ToolExecutionConfig,
        default_workspace: str | None,
        artifact_root: str | None,
        is_aborted: Callable[[str, str], bool],
        tool_policies: ToolPolicyRegistry | None = None,
        session_controls: SessionControlCoordinator | None = None,
        session_controllers: PluginSessionControllerRegistry | None = None,
    ) -> None:
        self.executor = executor
        self.batch_executor = ToolBatchExecutor(executor)
        self.registry = registry
        self.hooks = hooks
        self.events = events
        self.runs = runs
        self.execution_config = execution_config
        self.default_workspace = default_workspace
        self.artifact_root = artifact_root
        self.is_aborted = is_aborted
        self.tool_policies = tool_policies or ToolPolicyRegistry()
        self.session_controls = session_controls
        self.session_controllers = (
            session_controllers or PluginSessionControllerRegistry()
        )

    async def handle_batch(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        tool_calls: list[ToolCallRequest],
        *,
        turn_id: str,
        message_sequence: int,
        assistant_text: str,
        assistant_reasoning: str,
        assistant_continuation: ProviderContinuationArtifact | None,
        abort_signal: AbortSignal,
    ) -> ToolBatchHandlingResult:
        assistant_message = self.runs.append_assistant_tool_batch_message(
            session=session,
            run=run,
            tool_calls=tool_calls,
            text=assistant_text,
            sequence=message_sequence,
            turn_id=turn_id,
            reasoning_content=assistant_reasoning,
            provider_continuation=assistant_continuation,
        )
        batch = ToolCallBatchRecord(
            turn_id=turn_id,
            assistant_message_id=assistant_message.message_id,
            execution_mode=self._effective_mode(tool_calls),
            max_concurrency=self.execution_config.max_concurrency,
            calls=[ToolBatchCall(request=request) for request in tool_calls],
        )
        self.runs.save_batch_state(
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_preflight",
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_batch_planned",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": turn_id,
                "tool_call_count": len(tool_calls),
                "execution_mode": batch.execution_mode,
            },
        )

        exclusive_tools = self._exclusive_tools_in_mixed_batch(tool_calls)
        if exclusive_tools:
            rejected_calls = [
                ToolBatchCall(
                    request=request,
                    status="failed",
                    result=ToolResult(
                        tool_call_id=request.id,
                        tool_name=request.name,
                        is_error=True,
                        content=(
                            "Tool batch rejected: exclusive tools must be called alone. "
                            f"Exclusive tools in this batch: {', '.join(exclusive_tools)}"
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
            batch = batch.model_copy(update={"calls": rejected_calls, "updated_at": utc_now()})
            sequence = await self.events.emit(
                emitted,
                sequence,
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
            executed = await self.execute_batch(
                emitted,
                sequence,
                session=session,
                run=run,
                batch=batch,
                message_sequence=message_sequence + 1,
                abort_signal=abort_signal,
            )
            return executed.model_copy(update={"messages": [assistant_message, *executed.messages]})

        calls: list[ToolBatchCall] = []
        for request in tool_calls:
            call, sequence = await self._preflight_call(
                emitted,
                sequence,
                session=session,
                run=run,
                batch_id=batch.batch_id,
                turn_id=turn_id,
                request=request,
                abort_signal=abort_signal,
            )
            calls.append(call)
        batch = batch.model_copy(update={"calls": calls, "updated_at": utc_now()})

        pending_approvals = batch.pending_approvals
        waiting_user = next((call for call in calls if call.status == "waiting_user"), None)
        if pending_approvals or waiting_user is not None:
            phase = "awaiting_interaction"
            status: ToolBatchOutcomeStatus = (
                "waiting_approval" if pending_approvals else "waiting_user"
            )
            batch = batch.model_copy(update={"phase": phase, "updated_at": utc_now()})
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status=status,
                phase="human_interaction",
            )
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_status_changed",
                payload={
                    "to_status": status,
                    "status": status,
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                },
            )
            return ToolBatchHandlingResult(
                status=status,
                batch=batch,
                messages=[assistant_message],
                input_request=waiting_user.input_request if waiting_user is not None else None,
                sequence=sequence,
            )

        batch = batch.model_copy(update={"phase": "ready", "updated_at": utc_now()})
        self.runs.save_batch_state(
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_execution",
        )
        executed = await self.execute_batch(
            emitted,
            sequence,
            session=session,
            run=run,
            batch=batch,
            message_sequence=message_sequence + 1,
            abort_signal=abort_signal,
        )
        return executed.model_copy(update={"messages": [assistant_message, *executed.messages]})

    async def execute_batch(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        message_sequence: int,
        abort_signal: AbortSignal,
    ) -> ToolBatchHandlingResult:
        final_calls: list[ToolBatchCall] = []
        for call in batch.calls:
            if (
                call.status != "ready"
                or call.invocation is None
                or call.approval is None
                or call.approval.status != "modified"
            ):
                final_calls.append(call)
                continue
            invocation, policy_mode, policy_reason, sequence = await self._evaluate_tool_policies(
                emitted,
                sequence,
                session=session,
                run=run,
                invocation=call.invocation,
                phase="approval_modification",
            )
            prepared = self.executor.preflight(
                invocation,
                approved=True,
                policy_mode=policy_mode,
                policy_reason=policy_reason,
            )
            final_calls.append(
                call.model_copy(
                    update={
                        "status": (
                            "ready"
                            if prepared.status == "ready"
                            else (
                                "failed"
                                if prepared.result is not None and prepared.result.is_error
                                else "completed"
                            )
                        ),
                        "invocation": prepared.invocation,
                        "result": prepared.result,
                    }
                )
            )
        batch = batch.model_copy(update={"calls": final_calls, "updated_at": utc_now()})
        ready_calls = [call for call in batch.calls if call.status == "ready" and call.invocation]
        batch = batch.model_copy(update={"phase": "executing", "updated_at": utc_now()})
        self.runs.save_batch_state(
            session=session,
            run=run,
            batch=batch,
            status="running",
            phase="tool_execution",
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_batch_started",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "execution_mode": batch.execution_mode,
                "tool_calls": [
                    {
                        "tool_call_id": call.request.id,
                        "tool_name": call.request.name,
                        "status": call.status,
                        "source_index": call.request.source_index,
                    }
                    for call in batch.calls
                ],
            },
        )
        checkpoint_batch = batch
        checkpoint_lock = asyncio.Lock()
        unattended_resolutions: dict[str, UnattendedResolution] = {}

        async def checkpoint_started(invocation: ToolInvocation) -> None:
            nonlocal checkpoint_batch, sequence
            async with checkpoint_lock:
                started_call: ToolBatchCall | None = None
                calls: list[ToolBatchCall] = []
                for item in checkpoint_batch.calls:
                    if item.request.id != invocation.tool_call_id:
                        calls.append(item)
                        continue
                    started_call = item.model_copy(update={"status": "running"})
                    calls.append(started_call)
                if started_call is None:
                    raise ValueError(f"unknown tool call in batch: {invocation.tool_call_id}")
                checkpoint_batch = checkpoint_batch.model_copy(
                    update={"calls": calls, "updated_at": utc_now()}
                )
                waiting_user = next(
                    (call for call in checkpoint_batch.calls if call.status == "waiting_user"),
                    None,
                )
                self.runs.save_batch_state(
                    session=session,
                    run=run,
                    batch=checkpoint_batch,
                    status="waiting_user" if waiting_user is not None else "running",
                    phase="human_interaction" if waiting_user is not None else "tool_execution",
                )
                sequence = await self.events.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="tool_call_started",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": batch.turn_id,
                        "tool_call_id": invocation.tool_call_id,
                        "tool_name": invocation.tool_name,
                        "source_index": started_call.request.source_index,
                    },
                )

        async def checkpoint_outcome(outcome: ToolExecutionOutcome) -> None:
            nonlocal checkpoint_batch, sequence
            outcome = await self._apply_control_transitions(
                outcome,
                session=session,
            )
            async with checkpoint_lock:
                checkpoint_calls: list[ToolBatchCall] = []
                completed_call: ToolBatchCall | None = None
                for item in checkpoint_batch.calls:
                    if item.request.id != outcome.invocation.tool_call_id:
                        checkpoint_calls.append(item)
                        continue
                    result = outcome.result
                    waiting_for_input = outcome.status == "waiting_user"
                    completed_call = item.model_copy(
                        update={
                            "status": _tool_call_status_for_outcome(outcome),
                            "input_request": (
                                outcome.input_request if waiting_for_input else item.input_request
                            ),
                            "input_resolution": (
                                None if waiting_for_input else item.input_resolution
                            ),
                            "input_revision": item.input_revision + int(waiting_for_input),
                            "result": result,
                        }
                    )

                    checkpoint_calls.append(completed_call)
                checkpoint_batch = checkpoint_batch.model_copy(
                    update={"calls": checkpoint_calls, "updated_at": utc_now()}
                )
                try:
                    waiting_user = next(
                        (call for call in checkpoint_batch.calls if call.status == "waiting_user"),
                        None,
                    )
                    self.runs.save_batch_state(
                        session=session,
                        run=run,
                        batch=checkpoint_batch,
                        status="waiting_user" if waiting_user is not None else "running",
                        phase=(
                            "human_interaction" if waiting_user is not None else "tool_execution"
                        ),
                    )
                except SessionBusyError:
                    if not abort_signal.is_aborted():
                        raise
                resolution = (
                    unattended_resolutions.pop(completed_call.request.id, None)
                    if completed_call is not None
                    else None
                )
                if resolution is not None:
                    assert completed_call is not None
                    sequence = await self.events.emit(
                        emitted,
                        sequence,
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
                if completed_call is not None and outcome.status == "waiting_user":
                    assert completed_call.input_request is not None
                    sequence = await self.events.emit(
                        emitted,
                        sequence,
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="ask_user_requested",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": completed_call.request.id,
                            "tool_name": completed_call.request.name,
                            "request": completed_call.input_request.data,
                            "question": completed_call.input_request.data,
                            "source_index": completed_call.request.source_index,
                        },
                    )
                elif completed_call is not None and completed_call.result is not None:
                    result = completed_call.result
                    sequence = await self.events.emit(
                        emitted,
                        sequence,
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
                            "source_index": completed_call.request.source_index,
                        },
                    )

        async def resolve_outcome(outcome: ToolExecutionOutcome) -> ToolExecutionOutcome:
            nonlocal checkpoint_batch, sequence
            if outcome.status != "waiting_user" or session.interaction_mode != "unattended":
                return outcome
            request = outcome.input_request
            if request is None:
                raise ValueError("waiting tool outcome has no input request")
            resolution = UnattendedResolution(
                resolver_id=_AFK_RESOLVER_ID,
                reason=_AFK_UNATTENDED_REASON,
            )
            async with checkpoint_lock:
                resolving_call: ToolBatchCall | None = None
                calls: list[ToolBatchCall] = []
                for item in checkpoint_batch.calls:
                    if item.request.id != outcome.invocation.tool_call_id:
                        calls.append(item)
                        continue
                    resolving_call = item.model_copy(
                        update={
                            "status": "resolving_input",
                            "invocation": outcome.invocation,
                            "input_request": request,
                            "input_resolution": resolution,
                            "input_revision": item.input_revision + 1,
                        }
                    )
                    calls.append(resolving_call)
                if resolving_call is None:
                    raise ValueError(
                        f"unknown unattended tool call in batch: {outcome.invocation.tool_call_id}"
                    )
                checkpoint_batch = checkpoint_batch.model_copy(
                    update={"calls": calls, "updated_at": utc_now()}
                )
                self.runs.save_batch_state(
                    session=session,
                    run=run,
                    batch=checkpoint_batch,
                    status="running",
                    phase="human_interaction",
                )
                sequence = await self.events.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="ask_user_requested",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": batch.turn_id,
                        "tool_call_id": resolving_call.request.id,
                        "tool_name": resolving_call.request.name,
                        "request": request.data,
                        "question": request.data,
                        "source_index": resolving_call.request.source_index,
                        "interaction_mode": session.interaction_mode,
                        "will_suspend": False,
                    },
                )
            resolved = await self._resolve_unattended_input(
                outcome,
                resolution=resolution,
                request=request,
                abort_signal=abort_signal,
                context=execution_context,
            )
            unattended_resolutions[outcome.invocation.tool_call_id] = resolution
            return resolved

        execution_context = ToolExecutionContext(
            signal=abort_signal,
            workspace=session.workspace or self.default_workspace,
            artifact_root=self.artifact_root,
            workspace_only=self.execution_config.workspace_only,
            max_output_chars=self.execution_config.max_output_chars,
        )
        await self.batch_executor.execute(
            [call.invocation for call in ready_calls if call.invocation is not None],
            mode=batch.execution_mode,
            max_concurrency=batch.max_concurrency,
            signal=abort_signal,
            context=execution_context,
            resolve_outcome=resolve_outcome,
            on_start=checkpoint_started,
            on_outcome=checkpoint_outcome,
        )
        waiting_user = next(
            (call for call in checkpoint_batch.calls if call.status == "waiting_user"),
            None,
        )
        if waiting_user is not None:
            waiting_batch = checkpoint_batch.model_copy(
                update={"phase": "awaiting_interaction", "updated_at": utc_now()}
            )
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=waiting_batch,
                status="waiting_user",
                phase="human_interaction",
            )
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="run_status_changed",
                payload={
                    "to_status": "waiting_user",
                    "status": "waiting_user",
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                },
            )
            return ToolBatchHandlingResult(
                status="waiting_user",
                batch=waiting_batch,
                input_request=waiting_user.input_request,
                sequence=sequence,
            )
        batch = checkpoint_batch.model_copy(
            update={
                "phase": "committing_results",
                "updated_at": utc_now(),
            }
        )
        try:
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=batch,
                status="running",
                phase="result_commit",
            )
        except SessionBusyError:
            if not abort_signal.is_aborted():
                raise
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_batch_results_committing",
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "tool_result_count": sum(call.result is not None for call in batch.calls),
            },
        )

        messages: list[MessageRecord] = []
        results: list[ToolResult] = []
        committed_calls: list[ToolBatchCall] = []
        for call in sorted(batch.calls, key=lambda item: item.request.source_index):
            if call.result is None:
                committed_calls.append(call)
                continue
            sequence = await self.after(emitted, sequence, session, run, call.result)
            message = self.runs.append_tool_result_message(
                session=session,
                run=run,
                result=call.result,
                sequence=message_sequence + len(messages),
                turn_id=batch.turn_id,
            )
            messages.append(message)
            results.append(call.result)
            committed_calls.append(
                call.model_copy(update={"result_message_id": message.message_id})
            )

        terminal_phase = "aborted" if abort_signal.is_aborted() else "completed"
        completed_batch = batch.model_copy(
            update={
                "phase": terminal_phase,
                "calls": committed_calls,
                "updated_at": utc_now(),
            }
        )
        if terminal_phase == "aborted":
            self.runs.save_aborted_batch(
                session_id=session.session_id,
                run_id=run.run_id,
                batch=completed_batch,
            )
        else:
            self.runs.save_batch_state(
                session=session,
                run=run,
                batch=None,
                status="running",
                phase="model_streaming",
            )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type=(
                "tool_batch_aborted" if terminal_phase == "aborted" else "tool_batch_completed"
            ),
            payload={
                "batch_id": batch.batch_id,
                "turn_id": batch.turn_id,
                "tool_result_count": len(results),
            },
        )
        return ToolBatchHandlingResult(
            status="completed",
            batch=completed_batch,
            messages=messages,
            results=results,
            sequence=sequence,
        )

    async def _apply_control_transitions(
        self,
        outcome: ToolExecutionOutcome,
        *,
        session: SessionRecord,
    ) -> ToolExecutionOutcome:
        result = outcome.result
        if result is None or not result.session_control_transitions:
            return outcome
        invocation = outcome.invocation
        if (
            self.session_controls is None
            or invocation.plugin_id is None
            or invocation.generation_id is None
            or invocation.execution_contract_hash is None
        ):
            return outcome.model_copy(
                update={
                    "result": result.model_copy(
                        update={
                            "is_error": True,
                            "content": (
                                "Tool Session control transition has no active "
                                "plugin execution contract."
                            ),
                            "structured": {
                                "error_type": "SessionControlContractError"
                            },
                            "session_control_transitions": [],
                        }
                    )
                }
            )
        outputs: list[str] = []
        applied: list[dict[str, object]] = []
        try:
            for transition in result.session_control_transitions:
                attributed = AttributedSessionControlTransition(
                    **transition.model_dump(mode="python"),
                    plugin_id=invocation.plugin_id,
                    generation_id=invocation.generation_id,
                    execution_contract_hash=invocation.execution_contract_hash,
                )
                settled = await self.session_controls.apply(
                    attributed,
                    session_id=session.session_id,
                    controllers=self.session_controllers,
                    workspace=session.workspace or self.default_workspace,
                    resolving_tool_call_id=invocation.tool_call_id,
                )
                if settled.output:
                    outputs.append(settled.output)
                applied.append(settled.model_dump(mode="json"))
        except SessionControlError as exc:
            return outcome.model_copy(
                update={
                    "result": result.model_copy(
                        update={
                            "is_error": True,
                            "content": str(exc),
                            "structured": {
                                **result.structured,
                                "error_type": type(exc).__name__,
                                "error_message": str(exc),
                            },
                            "session_control_transitions": [],
                        }
                    )
                }
            )
        content_parts = [result.content] if result.content else []
        content_parts.extend(outputs)
        return outcome.model_copy(
            update={
                "result": result.model_copy(
                    update={
                        "content": "\n".join(content_parts),
                        "structured": {
                            **result.structured,
                            "session_controls": applied,
                        },
                        "session_control_transitions": [],
                    }
                )
            }
        )

    def resolve_approval(
        self,
        batch: ToolCallBatchRecord,
        *,
        approval_id: str,
        approved: bool,
        reason: str,
        arguments_override: dict[str, object] | None,
        resolved_by: ApprovalResolvedBy = "user",
        resolver_id: str | None = None,
    ) -> ToolCallBatchRecord:
        resolved_calls: list[ToolBatchCall] = []
        found = False
        for call in batch.calls:
            approval = call.approval
            if approval is None or approval.approval_id != approval_id:
                resolved_calls.append(call)
                continue
            found = True
            if call.status != "awaiting_approval" or call.invocation is None:
                raise ValueError(f"approval is not pending: {approval_id}")
            if approved:
                prepared = self.executor.preflight(
                    call.invocation,
                    approved=True,
                    arguments_override=arguments_override,
                )
                resolved_approval = approval.model_copy(
                    update={
                        "status": "modified" if arguments_override is not None else "approved",
                        "resolved_at": utc_now(),
                        "resolved_by": resolved_by,
                        "resolver_id": resolver_id,
                        "modified_arguments": arguments_override,
                    }
                )
                if prepared.status == "ready":
                    resolved_calls.append(
                        call.model_copy(
                            update={
                                "status": "ready",
                                "invocation": prepared.invocation,
                                "approval": resolved_approval,
                            }
                        )
                    )
                else:
                    resolved_calls.append(
                        call.model_copy(
                            update={
                                "status": "failed",
                                "invocation": prepared.invocation,
                                "approval": resolved_approval,
                                "result": prepared.result,
                            }
                        )
                    )
            else:
                result = self.executor.rejected_result(call.invocation, reason)
                resolved_calls.append(
                    call.model_copy(
                        update={
                            "status": "rejected",
                            "approval": approval.model_copy(
                                update={
                                    "status": "rejected",
                                    "resolved_at": utc_now(),
                                    "resolved_by": resolved_by,
                                    "resolver_id": resolver_id,
                                    "reason": reason,
                                }
                            ),
                            "result": result,
                        }
                    )
                )
        if not found:
            raise ValueError(f"unknown approval: {approval_id}")
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

    async def resolve_user_answer(
        self,
        batch: ToolCallBatchRecord,
        *,
        session: SessionRecord,
        tool_call_id: str,
        answer: JsonValue,
        workspace: str | None = None,
    ) -> ToolCallBatchRecord:
        answer = _JSON_VALUE_ADAPTER.validate_python(answer)
        resolution = AnswerResolution(value=answer)
        resolved_calls: list[ToolBatchCall] = []
        found = False
        for call in batch.calls:
            if call.request.id != tool_call_id:
                resolved_calls.append(call)
                continue
            found = True
            if call.status != "waiting_user" or call.invocation is None:
                raise ValueError(f"tool call is not waiting for user input: {tool_call_id}")
            definition = self.registry.get(call.request.name).definition
            if definition.resumable:
                assert call.input_request is not None
                outcome = await self.executor.resume(
                    call.invocation,
                    resolution=resolution,
                    request=call.input_request,
                    context=ToolExecutionContext(
                        workspace=workspace or self.default_workspace,
                        artifact_root=self.artifact_root,
                        workspace_only=self.execution_config.workspace_only,
                        max_output_chars=self.execution_config.max_output_chars,
                    ),
                )
                outcome = await self._apply_control_transitions(
                    outcome,
                    session=session,
                )
                result = outcome.result
                status = _tool_call_status_for_outcome(outcome)
                waiting_for_more = outcome.status == "waiting_user"
                resolved_calls.append(
                    call.model_copy(
                        update={
                            "status": status,
                            "invocation": outcome.invocation,
                            "input_request": (
                                outcome.input_request if waiting_for_more else call.input_request
                            ),
                            "input_resolution": None if waiting_for_more else resolution,
                            "input_revision": call.input_revision + int(waiting_for_more),
                            "result": result,
                        }
                    )
                )
                continue
            resolved_calls.append(
                call.model_copy(
                    update={
                        "status": "completed",
                        "input_resolution": resolution,
                        "result": ToolResult(
                            tool_call_id=call.request.id,
                            tool_name=call.request.name,
                            content=_tool_input_result_content(answer),
                            structured={"answer": answer},
                            ended_at=utc_now(),
                        ),
                    }
                )
            )
        if not found:
            raise ValueError(f"unknown tool call: {tool_call_id}")
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

    async def _resolve_unattended_input(
        self,
        outcome: ToolExecutionOutcome,
        *,
        resolution: UnattendedResolution,
        request: ToolInputRequest,
        abort_signal: AbortSignal,
        context: ToolExecutionContext,
    ) -> ToolExecutionOutcome:
        definition = self.registry.get(outcome.invocation.tool_name).definition
        if not definition.resumable or not definition.supports_unattended:
            return _interaction_error_outcome(
                outcome.invocation,
                error_type="UserInputUnavailable",
                error_message=(
                    "AFK mode does not wait for user input and this tool does not "
                    "support unattended recovery. Continue using available context."
                ),
                structured={
                    "resolved_by": resolution.resolved_by,
                    "resolver_id": resolution.resolver_id,
                    "input_request": request.data,
                },
            )
        resumed = await self.executor.resume(
            outcome.invocation,
            resolution=resolution,
            request=request,
            signal=abort_signal,
            context=context,
        )
        if resumed.status != "waiting_user":
            return resumed
        return _interaction_error_outcome(
            resumed.invocation,
            error_type="UnattendedInteractionLoop",
            error_message=(
                "a tool cannot request more user input after receiving an unattended resolution"
            ),
            structured={
                "resolved_by": resolution.resolved_by,
                "resolver_id": resolution.resolver_id,
                "input_request": (
                    resumed.input_request.data if resumed.input_request is not None else None
                ),
            },
        )

    async def after(
        self,
        emitted: list[EventRecord],
        sequence: int,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
    ) -> int:
        hook_result = await self.hooks.dispatch(
            HookContext(
                hook_name="after_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"result": result.model_dump(mode="json")},
            )
        )
        return await self.events.record_hook_failures(
            emitted,
            sequence,
            hook_result.failures,
        )

    async def _preflight_call(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch_id: str,
        turn_id: str,
        request: ToolCallRequest,
        abort_signal: AbortSignal,
    ) -> tuple[ToolBatchCall, int]:
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
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=turn_id,
            tool_call_id=request.id,
            tool_name=request.name,
            canonical_tool_id=_canonical_tool_id(request.name, metadata),
            owner_id=_optional_text(metadata.get("plugin_id")) or "ouro.host",
            source=definition.source if definition is not None else "builtin",
            effect=definition.effect if definition is not None else "unknown",
            batch_behavior=(definition.batch_behavior if definition is not None else "compatible"),
            permission_control=(
                definition.permission_control if definition is not None else "host"
            ),
            arguments=request.arguments,
            plugin_id=_optional_text(metadata.get("plugin_id")),
            plugin_version=_optional_text(metadata.get("plugin_version")),
            plugin_class=_optional_plugin_class(metadata.get("plugin_class")),
            release_id=_optional_text(metadata.get("release_id")),
            package_digest=_optional_text(metadata.get("package_digest")),
            builtin_contract_hash=_optional_text(metadata.get("builtin_contract_hash")),
            generation_id=_optional_text(metadata.get("generation_id")),
            contract_hash=_optional_text(metadata.get("contract_hash")),
            execution_contract_hash=_optional_text(metadata.get("execution_contract_hash")),
            active_when_controller_id=(
                definition.active_when_controller_id if definition is not None else None
            ),
        )
        sequence = await self.events.emit(
            emitted,
            sequence,
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="tool_call_requested",
            payload={
                "batch_id": batch_id,
                "turn_id": turn_id,
                "tool_call_id": request.id,
                "tool_name": request.name,
                "arguments": request.arguments,
                "source_index": request.source_index,
            },
        )
        before_result = await self.hooks.dispatch(
            HookContext(
                hook_name="before_tool_call",
                session_id=session.session_id,
                run_id=run.run_id,
                data={"tool_name": request.name, "arguments": request.arguments},
            )
        )
        sequence = await self.events.record_hook_failures(
            emitted,
            sequence,
            before_result.failures,
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
            return ToolBatchCall(
                request=request, status="aborted", invocation=invocation, result=result
            ), sequence

        invocation, policy_mode, policy_reason, sequence = await self._evaluate_tool_policies(
            emitted,
            sequence,
            session=session,
            run=run,
            invocation=invocation,
            phase="preflight",
        )
        outcome = self.executor.preflight(
            invocation,
            approval_mode=session.approval_mode,
            policy_mode=policy_mode,
            policy_reason=policy_reason,
        )
        if outcome.status == "approval_required":
            assert outcome.approval is not None
            sequence = await self.events.emit(
                emitted,
                sequence,
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
            if session.interaction_mode == "unattended":
                approval = outcome.approval.model_copy(
                    update={
                        "status": "approved",
                        "resolved_at": utc_now(),
                        "resolved_by": "host_policy",
                        "resolver_id": _AFK_RESOLVER_ID,
                    }
                )
                prepared = self.executor.preflight(
                    outcome.invocation,
                    approved=True,
                    approval_mode=session.approval_mode,
                    policy_mode=policy_mode,
                    policy_reason=policy_reason,
                )
                status: ToolCallStatus = (
                    "ready"
                    if prepared.status == "ready"
                    else (
                        "failed"
                        if prepared.result is not None and prepared.result.is_error
                        else "completed"
                    )
                )
                sequence = await self.events.emit(
                    emitted,
                    sequence,
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="approval_resolved",
                    payload={
                        "batch_id": batch_id,
                        "turn_id": turn_id,
                        "approval_id": approval.approval_id,
                        "tool_call_id": request.id,
                        "tool_call_status": status,
                        "approved": True,
                        "reason": None,
                        "resolved_by": approval.resolved_by,
                        "resolver_id": approval.resolver_id,
                    },
                )
                return ToolBatchCall(
                    request=request,
                    status=status,
                    invocation=prepared.invocation,
                    approval=approval,
                    result=prepared.result,
                ), sequence
            return ToolBatchCall(
                request=request,
                status="awaiting_approval",
                invocation=outcome.invocation,
                approval=outcome.approval,
            ), sequence
        if outcome.status == "waiting_user":
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="ask_user_requested",
                payload={
                    "batch_id": batch_id,
                    "turn_id": turn_id,
                    "tool_call_id": request.id,
                    "tool_name": request.name,
                    "request": outcome.input_request.data
                    if outcome.input_request is not None
                    else None,
                    "question": outcome.input_request.data
                    if outcome.input_request is not None
                    else None,
                },
            )
            return ToolBatchCall(
                request=request,
                status="waiting_user",
                invocation=outcome.invocation,
                input_request=outcome.input_request,
            ), sequence
        if outcome.status == "ready":
            return ToolBatchCall(
                request=request, status="ready", invocation=outcome.invocation
            ), sequence
        return ToolBatchCall(
            request=request,
            status="failed" if outcome.result and outcome.result.is_error else "completed",
            invocation=outcome.invocation,
            result=outcome.result,
        ), sequence

    async def _evaluate_tool_policies(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        invocation: ToolInvocation,
        phase: str,
    ) -> tuple[ToolInvocation, ToolPolicyMode | None, str | None, int]:
        try:
            definition = self.registry.get(invocation.tool_name).definition
        except KeyError:
            return invocation, None, None, sequence
        if tool_argument_error(definition, invocation.arguments) is not None:
            return invocation, None, None, sequence
        policy = await self.tool_policies.evaluate(
            ToolPolicyCall(
                session_id=session.session_id,
                run_id=run.run_id,
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                canonical_tool_id=(
                    invocation.canonical_tool_id or f"ouro.host/tools/{invocation.tool_name}"
                ),
                owner_id=invocation.owner_id or "ouro.host",
                arguments=dict(invocation.arguments),
                source=invocation.source,
                effect=invocation.effect,
                batch_behavior=invocation.batch_behavior,
                permission_control=invocation.permission_control,
                workspace=session.workspace or self.default_workspace,
            )
        )
        policy_ids = [
            f"{item.plugin_id}/tool_policies/{item.policy_id}" for item in policy.decisions
        ]
        if policy.failures:
            policy_ids.extend(
                f"{item.plugin_id}/tool_policies/{item.policy_id}" for item in policy.failures
            )
        if policy.mode is not None or policy.failures:
            sequence = await self.events.emit(
                emitted,
                sequence,
                session_id=session.session_id,
                run_id=run.run_id,
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
        return invocation, policy.mode, policy.reason, sequence

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


def _tool_call_status_for_outcome(outcome: ToolExecutionOutcome) -> ToolCallStatus:
    if outcome.status == "waiting_user":
        return "waiting_user"
    if outcome.status == "aborted":
        return "aborted"
    if outcome.result is not None and outcome.result.is_error:
        return "failed"
    return "completed"


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


def _canonical_tool_id(name: str, metadata: dict[str, object]) -> str:
    plugin_id = _optional_text(metadata.get("plugin_id"))
    contribution_id = _optional_text(metadata.get("contribution_id"))
    if plugin_id is not None and contribution_id is not None:
        return f"{plugin_id}/tools/{contribution_id}"
    return f"ouro.host/tools/{name}"
