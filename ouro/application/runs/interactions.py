"""Resolve durable tool interactions under the originating Run's execution ownership."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue

from ouro.application.harness import capture_session_harness
from ouro.application.runs.lifecycle import RunLifecycle, find_active_run
from ouro.application.runs.ownership import RunExecutionOwnership
from ouro.application.runs.tool_batches import load_batch_states, load_tool_batch
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.core.agents import AgentWaitResolution
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batch_transitions import resolve_tool_approval
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tool_facts import ToolCallIdentity, ToolQuestion
from ouro.core.tools import ToolResult

if TYPE_CHECKING:
    from ouro.application.tool_calls import ToolCallCoordinator


class StaleToolQuestion(ValueError):
    """The observed question was replaced before its answer could be applied."""


@dataclass(frozen=True)
class _ResolvedInteraction:
    batch: ToolCallBatchRecord
    events: tuple[EventRecord, ...]
    harness: HarnessCallSnapshot | None = None
    changed_call_ids: frozenset[str] | None = None


@dataclass(kw_only=True)
class ParkedRunExecution:
    ownership: RunExecutionOwnership
    runs: RunLifecycle
    recovery: SessionRecoveryService
    continue_batch: Callable[
        [SessionRecord, RunRecord, ToolCallBatchRecord, AbortSignal], Awaitable[RunResult]
    ]
    finish: Callable[[RunResult], Awaitable[RunResult]]
    release_resources: Callable[[str], None]
    bind_run: Callable[[RunRecord], Awaitable[None]]
    tool_calls_for_run: Callable[[str], ToolCallCoordinator]

    async def _target(self, session_id: str, run_id: str | None) -> RunRecord:
        snapshot = await run_blocking(self.recovery.recover_runtime, session_id)
        if run_id is not None:
            for run in snapshot.runs:
                if run.run_id == run_id:
                    return run
            return await run_blocking(self.runs.store.load_run, session_id, run_id)
        return find_active_run(snapshot.runs, history_epoch=snapshot.session.history_epoch)

    async def _resolve(
        self,
        target: RunRecord,
        decide: Callable[[SessionRecord, RunRecord, AbortSignal], Awaitable[_ResolvedInteraction]],
    ) -> RunResult:
        async with self.ownership.parked_run(target) as (session, run, signal):
            try:
                decision = await decide(session, run, signal)
                session, run, batch, events = await run_blocking(
                    self.runs.save_resolved_batch,
                    session=session,
                    run=run,
                    batch=decision.batch,
                    events=decision.events,
                    harness=decision.harness,
                    changed_call_ids=decision.changed_call_ids,
                )
                await self.runs.events.publish_committed(events)
                if target.status == "waiting_approval" and run.status != "running":
                    result = RunResult(session=session, run=run)
                else:
                    result = await self.continue_batch(session, run, batch, signal)
            except SessionBusyError:
                self.release_resources(run.run_id)
                aborted = await run_blocking(
                    self.ownership.abort_state, session.session_id, run.run_id
                )
                if aborted is None:
                    raise
                session, run = aborted
                interrupted = await run_blocking(load_tool_batch, self.runs.store, run)
                if interrupted is not None:
                    _, events = await run_blocking(
                        self.runs.save_aborted_batch,
                        session_id=session.session_id,
                        run_id=run.run_id,
                        batch=interrupted.model_copy(update={"phase": "aborted"}),
                    )
                    await self.runs.events.publish_committed(events)
                result = await self.runs.finalize_requested_abort(
                    RunResult(session=session, run=run)
                )
            except BaseException:
                # A failed answer or commit can leave a newly bound catalog on a parked Run.
                self.release_resources(run.run_id)
                raise
        return await self.finish(result)

    async def approve(
        self,
        session_id: str,
        run_id: str | None,
        approval_id: str | None,
        *,
        approved: bool,
        reason: str,
        arguments_override: dict[str, object] | None,
    ) -> RunResult:
        def require_approval(run: RunRecord) -> None:
            if run.status != "waiting_approval":
                raise ValueError(f"run is not waiting for approval: {run.status}")
            if run.active_tool_batch is None:
                raise ValueError("run has no active tool batch")

        target = await self._target(session_id, run_id or None)
        require_approval(target)
        selected = approval_id or target.pending_approval_id
        if selected is None:
            raise ValueError("run has no pending approval")

        async def decide(
            session: SessionRecord, run: RunRecord, _signal: AbortSignal
        ) -> _ResolvedInteraction:
            require_approval(run)
            batch = await run_blocking(load_tool_batch, self.runs.store, run)
            assert batch is not None
            batch = resolve_tool_approval(
                batch,
                approval_id=selected,
                approved=approved,
                reason=reason,
                arguments_override=arguments_override,
            )
            call = next(
                call
                for call in batch.calls
                if call.approval is not None and call.approval.approval_id == selected
            )
            event = EventRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="approval_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "approval_id": selected,
                    "tool_call_id": call.request.id,
                    "tool_call_status": call.status,
                    "approved": approved,
                    "reason": None if approved else reason,
                    "resolved_by": "user",
                    "resolver_id": None,
                },
            )
            return _ResolvedInteraction(
                batch, (event,), changed_call_ids=frozenset({call.request.id})
            )

        return await self._resolve(target, decide)

    def pending_question(self, session_id: str, run_id: str) -> ToolQuestion:
        store = self.runs.store
        with store.session_read(session_id):
            run = store.load_run(session_id, run_id)
            if run.status != "waiting_user":
                raise ValueError(f"run is not waiting for user input: {run.status}")
            for state in load_batch_states(store, run):
                if state.status == "waiting_user" and state.input_request is not None:
                    return ToolQuestion(state.identity, state.input_revision)
        raise ValueError("run has no tool call waiting for user input")

    async def answer(
        self, session_id: str, run_id: str, answer: JsonValue, question: ToolQuestion | None = None
    ) -> RunResult:
        def require_question(run: RunRecord) -> None:
            if run.status != "waiting_user":
                raise ValueError(f"run is not waiting for user input: {run.status}")
            if run.active_tool_batch is None:
                raise ValueError("run has no active tool batch")

        async def decide(
            session: SessionRecord, run: RunRecord, signal: AbortSignal
        ) -> _ResolvedInteraction:
            require_question(run)
            if question is None and run.active_tool_batch != target.active_tool_batch:
                raise StaleToolQuestion("pending question changed while acquiring the Run")
            batch = await run_blocking(load_tool_batch, self.runs.store, run)
            assert batch is not None
            waiting_call = next(
                (
                    call
                    for call in batch.calls
                    if call.status == "waiting_user"
                    and (question is None or call.question(run_id, batch.batch_id) == question)
                ),
                None,
            )
            if waiting_call is None:
                raise StaleToolQuestion("pending question changed before the answer was applied")
            await self.bind_run(run)
            session, answer_harness = await run_blocking(
                capture_session_harness,
                self.runs.store,
                session.session_id,
                run=run,
                usage_service=self.runs.usage_service,
            )
            batch = await self.tool_calls_for_run(run.run_id).resolve_user_answer(
                batch,
                run=run,
                session=session,
                tool_call_id=waiting_call.request.id,
                answer=answer,
                harness=answer_harness,
                signal=signal,
                workspace=session.workspace,
            )
            pending_events: list[EventRecord] = []
            resolved_call = next(
                call for call in batch.calls if call.request.id == waiting_call.request.id
            )
            pending_events.append(
                EventRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="ask_user_resolved",
                    payload={
                        "batch_id": batch.batch_id,
                        "turn_id": batch.turn_id,
                        "tool_call_id": waiting_call.request.id,
                        "tool_call_status": resolved_call.status,
                        "answer": answer,
                        "resolution": {
                            "kind": "answer",
                            "value": answer,
                            "resolved_by": "user",
                            "resolver_id": None,
                        },
                        "resolved_by": "user",
                        "resolver_id": None,
                    },
                )
            )
            if resolved_call.status == "waiting_user" and resolved_call.input_request is not None:
                pending_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="ask_user_requested",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": resolved_call.request.id,
                            "tool_name": resolved_call.request.name,
                            "input_revision": resolved_call.input_revision,
                            "request": resolved_call.input_request.data,
                        },
                    )
                )
            return _ResolvedInteraction(
                batch,
                tuple(pending_events),
                harness=answer_harness,
                changed_call_ids=frozenset({waiting_call.request.id}),
            )

        target = await self._target(session_id, run_id)
        require_question(target)
        return await self._resolve(target, decide)

    async def resume_child(
        self, session_id: str, identity: ToolCallIdentity, resolution: AgentWaitResolution
    ) -> RunResult:
        run_id, tool_call_id = identity.run_id, identity.tool_call_id

        async def decide(
            session: SessionRecord, run: RunRecord, _signal: AbortSignal
        ) -> _ResolvedInteraction:
            if run.status != "waiting_child" or run.active_tool_batch is None:
                raise ValueError("parent Run no longer has a durable child wait")
            if run.active_tool_batch.batch_id != identity.batch_id:
                raise ValueError("Agent wait belongs to another native batch")
            batch = await run_blocking(load_tool_batch, self.runs.store, run)
            assert batch is not None
            waiting_call = next(
                (
                    call
                    for call in batch.calls
                    if call.request.id == tool_call_id
                    and call.status == "waiting_child"
                    and call.agent_wait is not None
                ),
                None,
            )
            if waiting_call is None or waiting_call.agent_wait is None:
                raise ValueError("parent ToolCall no longer has a pending child wait")
            resolved_wait = waiting_call.agent_wait.model_copy(update={"resolution": resolution})
            outcome = await run_blocking(
                self.runs.store.load_tool_outcome, session_id, identity, waiting_call.attempt
            )
            if outcome is None:
                raise PersistenceError("Agent wait has no recorded child observation")
            result = ToolResult.model_validate(outcome.value.model_dump())
            calls = [
                call.model_copy(
                    update={
                        "status": "failed" if result.is_error else "completed",
                        "agent_wait": resolved_wait,
                        "result": result,
                    }
                )
                if call.request.id == tool_call_id
                else call
                for call in batch.calls
            ]
            batch = batch.model_copy(
                update={"calls": calls, "phase": "executing", "updated_at": utc_now()}
            )
            resolved = EventRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="agent_wait_resolved",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "tool_call_id": tool_call_id,
                    "wait_id": resolved_wait.wait_id,
                    "delegation_id": resolved_wait.delegation_id,
                    "child_run_id": resolved_wait.child_run_id,
                    "resolution": resolution.model_dump(mode="json"),
                },
            )
            completed = EventRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                event_type="tool_call_completed",
                payload={
                    "batch_id": batch.batch_id,
                    "turn_id": batch.turn_id,
                    "tool_call_id": result.tool_call_id,
                    "tool_name": result.tool_name,
                    "status": "failed" if result.is_error else "completed",
                    "is_error": result.is_error,
                    "source_index": waiting_call.request.source_index,
                    "outcome_attempt": waiting_call.attempt,
                },
            )
            return _ResolvedInteraction(
                batch, (resolved, completed), changed_call_ids=frozenset({tool_call_id})
            )

        target = await self._target(session_id, run_id)
        if target.status != "waiting_child":
            raise ValueError(f"run is not waiting for a child: {target.status}")
        return await self._resolve(target, decide)
