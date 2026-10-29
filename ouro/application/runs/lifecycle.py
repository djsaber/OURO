from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import ExitStack
from typing import Any

from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
from ouro.application.inputs.queue import consumption_changes
from ouro.application.runs.tool_batches import load_tool_batch, prepare_batch_changes
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.usage import SummaryRequestRecorder, UsageService
from ouro.core.base import new_id, utc_now
from ouro.core.billing import UsagePurpose
from ouro.core.callbacks import run_blocking
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessStateChange, accept_state_changes
from ouro.core.inputs import InputRecord
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import (
    ContentBlock,
    MessageRecord,
    ModelResponseRecord,
    ProviderContinuationArtifact,
    SkillActivationContent,
    TextContent,
    ToolExecutionTiming,
    ToolMessageReference,
    ToolResultReference,
)
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.results import RunResult
from ouro.core.run_budget import RunPauseReason
from ouro.core.run_drivers import DriverCheckpoint, DriverResume
from ouro.core.run_transitions import plan_run_transition
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.state import (
    ACTIVE_RUN_STATUSES,
    RunPhase,
    RunRecord,
    RunStatus,
    SessionContinuationRequest,
    SessionRecord,
)
from ouro.core.tool_batches import ToolCallBatchRecord, ToolCallState, ToolResultProposals
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolFactConflict,
    ToolOutcomeRecord,
)
from ouro.core.tools import ToolInvocation, ToolResult, ToolResultValue
from ouro.sessions import RuntimeSessionSnapshot, SessionStore


def reserve_run(
    store: SessionStore,
    run: RunRecord,
    *,
    configure_session: Callable[[SessionRecord], SessionRecord] | None = None,
) -> RunRecord:
    """Reserve an identified queued Run through the shared Run/Session transition.

    Producers persist their intent before this boundary. The execution lock
    prevents a Host in another process from racing configuration and reservation.
    """

    if run.status != "queued":
        raise ValueError("A reserved Run must be queued")
    lock = store.acquire_session_lock(run.session_id, run.run_id)
    if lock is None:
        raise SessionBusyError(f"Session is executing: {run.session_id}")
    try:
        with store.session_mutation(run.session_id):
            session = store.load_session_record(run.session_id)
            if session.active_run_id is not None:
                raise SessionBusyError(f"Session has an active Run: {session.active_run_id}")
            try:
                store.load_run(run.session_id, run.run_id)
            except FileNotFoundError:
                pass
            else:
                raise SessionBusyError(f"Run identity is already reserved: {run.run_id}")
            run = run.model_copy(update={"history_epoch": session.history_epoch})
            configured = configure_session(session) if configure_session is not None else session
            transition = plan_run_transition(configured, current_run=None, next_run=run)
            store.commit_session(
                SessionCommit(before=session, after=transition.session, runs=(transition.run,))
            )
    finally:
        lock.release()
    store.refresh_catalog(run.session_id)
    return transition.run


class SessionContinuationSuperseded(SessionCommitConflict):
    """The accepted request was consumed or revoked before execution could start."""


class DelegationStartRevoked(SessionCommitConflict):
    """The parent cancelled or withdrew an accepted child before its first start."""


def require_current_continuation(
    session: SessionRecord, request: SessionContinuationRequest | None
) -> None:
    if request is not None and not request.is_current(session):
        raise SessionContinuationSuperseded("Session continuation was consumed or superseded")


class RunLifecycle:
    """Apply and persist run/session state transitions at durable boundaries."""

    def __init__(
        self,
        *,
        store: SessionStore,
        events: RuntimeEventRecorder,
        usage_service: UsageService | None = None,
        validate_resource_refs: Callable[[str, Iterable[str]], None] | None = None,
    ) -> None:
        self.store = store
        self.events = events
        self.usage_service = usage_service
        self.session_controls = SessionControlCoordinator(store, usage_service=usage_service)
        self._validate_resource_refs = validate_resource_refs

    def start(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        snapshot: RuntimeSessionSnapshot,
        resume: bool = False,
        input_messages: tuple[MessageRecord, ...] = (),
        accepted_inputs: tuple[InputRecord, ...] = (),
        consumed_inputs: tuple[str, ...] = (),
        continuation_request: SessionContinuationRequest | None = None,
        events: tuple[EventRecord, ...] = (),
    ) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]]:
        """Persist the shared session/run transition into an active run."""

        with ExitStack() as locks:
            if not resume and run.origin == "delegation":
                if session.parent_session_id is None or run.parent_run_id is None:
                    raise PersistenceError("Delegated Run has no parent identity")
                # Keep the parent's cancellation/withdrawal boundary stable through
                # the child's first start commit. No provider or plugin runs here.
                locks.enter_context(self.store.session_mutation(session.parent_session_id))
                parent = self.store.load_session_record(session.parent_session_id)
                parent_run = self.store.load_run(parent.session_id, run.parent_run_id)
                if (
                    parent_run.history_epoch != parent.history_epoch
                    or parent_run.abort_requested
                    or parent_run.status in {"aborted", "failed"}
                ):
                    raise DelegationStartRevoked("Parent revoked delegation before child start")
            locks.enter_context(self.store.session_mutation(session.session_id))
            latest_session = self.store.load_session_record(session.session_id)
            require_current_continuation(latest_session, continuation_request)
            existing = maybe_active_run(snapshot.runs, history_epoch=latest_session.history_epoch)
            if existing is not None and existing.run_id != run.run_id:
                raise SessionBusyError(f"session already has an active run: {existing.run_id}")
            if latest_session.active_run_id not in {None, run.run_id}:
                raise SessionBusyError(
                    f"session already has an active run: {latest_session.active_run_id}"
                )
            try:
                current_run = self.store.load_run(session.session_id, run.run_id)
            except FileNotFoundError:
                current_run = None
            transition = plan_run_transition(
                latest_session,
                current_run=current_run,
                next_run=run.model_copy(update={"status": "running"}),
                resume=resume,
            )
            after = transition.session
            if run.kind == "agent":
                after = after.model_copy(update={"continuation_request": None})
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest_session,
                    after=after,
                    runs=(transition.run,),
                    steps=tuple(SessionCommitStep(message=item) for item in input_messages),
                    inputs=accepted_inputs,
                    input_changes=consumption_changes(consumed_inputs, input_messages),
                    events=(
                        EventRecord(
                            session_id=session.session_id,
                            run_id=run.run_id,
                            event_type="run_status_changed" if resume else "run_started",
                            payload={
                                "from_status": current_run.status
                                if current_run is not None
                                else None,
                                "to_status": "running",
                                "resumed": True,
                            }
                            if resume
                            else {"trigger": run.trigger, "origin": run.origin, "kind": run.kind},
                        ),
                        *events,
                    ),
                )
            )
            for message, canonical in zip(input_messages, committed.messages, strict=True):
                message.sequence = canonical.sequence
                message.before_harness_version = canonical.before_harness_version
            active_run = transition.run
            active_session = committed.session
        self.store.refresh_catalog(active_session.session_id)
        return active_session, active_run, committed.events

    def settle_continuation_request(
        self, session_id: str, request: SessionContinuationRequest
    ) -> None:
        """Consume an intent that needs no new execution (already active or empty)."""
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if request.is_current(session):
                self.store.commit_session(
                    SessionCommit(
                        before=session,
                        after=session.model_copy(update={"continuation_request": None}),
                    )
                )

    def append_input_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        prompt: str,
        role: MessageRole,
        scaffold: bool,
        skill_activations: tuple[SkillActivationContent, ...] = (),
        resource_refs: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        semantic_tag: SemanticTag | None = None,
    ) -> MessageRecord:
        message = self.create_input_message(
            session=session,
            run=run,
            prompt=prompt,
            role=role,
            scaffold=scaffold,
            skill_activations=skill_activations,
            sequence=0,
            resource_refs=resource_refs,
            metadata=metadata,
            semantic_tag=semantic_tag,
        )
        self._append_message(session=session, run=run, message=message)
        return message

    def create_input_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        prompt: str,
        role: MessageRole,
        scaffold: bool,
        skill_activations: tuple[SkillActivationContent, ...] = (),
        sequence: int,
        resource_refs: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        semantic_tag: SemanticTag | None = None,
    ) -> MessageRecord:
        """Validate and construct an input without crossing a persistence boundary."""

        unique_resource_refs = list(dict.fromkeys(resource_refs))
        if unique_resource_refs and self._validate_resource_refs is None:
            raise ValueError(
                "input message contains resource_refs but no ResourceService is configured"
            )
        if self._validate_resource_refs is not None:
            self._validate_resource_refs(session.session_id, unique_resource_refs)
        content: list[ContentBlock] = []
        content.extend(skill_activations)
        if prompt:
            content.append(TextContent(text=prompt))
        message = MessageRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            role=role,
            sequence=sequence,
            content=content,
            semantic_tag=semantic_tag,
            scaffold=scaffold,
        )
        if unique_resource_refs:
            message = message.model_copy(update={"resource_refs": unique_resource_refs})
        if metadata:
            message = message.model_copy(update={"metadata": dict(metadata)})
        return message

    def append_assistant_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        text: str,
        turn_id: str,
        reasoning_content: str = "",
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
    ) -> MessageRecord:
        """Durably commit a completed assistant turn without completing its run."""

        with self.store.session_mutation(session.session_id):
            current = self.store.load_session_record(session.session_id)
            if run.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            existing = self.store.find_assistant_message(session.session_id, run.run_id, turn_id)
            if existing is not None:
                return existing
            message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="assistant",
                text=text,
                turn_id=turn_id,
            ).model_copy(
                update={
                    "reasoning_content": reasoning_content,
                    "model_response": model_response,
                    "provider_continuation": provider_continuation,
                }
            )
            self._append_message(session=session, run=run, message=message)
            return message

    def save_late_tool_result(
        self,
        *,
        identity: ToolCallIdentity,
        attempt: int,
        invocation: ToolInvocation,
        result: ToolResultValue,
    ) -> None:
        """Keep an owned execution's observation without reopening its business scope."""
        self.store.commit_tool_outcome(
            self._prepare_tool_outcome(identity, attempt, invocation, result)
        )

    def save_tool_result(
        self,
        *,
        identity: ToolCallIdentity,
        attempt: int,
        invocation: ToolInvocation,
        result: ToolResult,
    ) -> bool:
        """Save the observation and its pending proposals before business callbacks."""
        outcome = self._prepare_tool_outcome(identity, attempt, invocation, result)
        with self.store.session_mutation(invocation.session_id):
            session = self.store.load_session_record(invocation.session_id)
            run = self.store.load_run(invocation.session_id, identity.run_id)
            state = self.store.load_tool_call_state(invocation.session_id, identity)
            if state is None:
                raise PersistenceError("tool result has no accepted Call state")
            if (
                run.history_epoch != session.history_epoch
                or run.status not in ACTIVE_RUN_STATUSES
                or run.abort_requested
                or session.active_run_id != run.run_id
                or run.active_tool_batch is None
                or run.active_tool_batch.batch_id != identity.batch_id
                or state.attempt != attempt
                or state.result_message_id is not None
            ):
                self.store.commit_tool_outcome(outcome)
                return False
            accepted = state.model_copy(
                update={
                    "status": "failed" if result.is_error else "completed",
                    "outcome_attempt": attempt,
                    "proposals": ToolResultProposals.from_result(result),
                    "feedback": None,
                }
            )
            self.store.commit_session(
                SessionCommit(
                    before=session,
                    tool_facts=ToolCallFacts(outcomes=(outcome,)),
                    call_states=(accepted,),
                )
            )
            return True

    def _prepare_tool_outcome(
        self,
        identity: ToolCallIdentity,
        attempt: int,
        invocation: ToolInvocation,
        result: ToolResultValue,
    ) -> ToolOutcomeRecord:
        call = self.store.load_tool_call(invocation.session_id, identity)
        start = (
            self.store.load_tool_call_start(invocation.session_id, identity, attempt)
            if attempt > 0
            else None
        )
        if call is None or (
            attempt > 0 and (start is None or start.materialize(call) != invocation)
        ):
            raise PersistenceError("tool result does not match its original execution binding")
        if result.resource_refs:
            if self._validate_resource_refs is None:
                raise ValueError("tool result resources require a ResourceService")
            self._validate_resource_refs(invocation.session_id, result.resource_refs)
        value = ToolResultValue.model_validate(
            result.model_dump(include=set(ToolResultValue.model_fields))
        )
        previous = self.store.load_tool_outcome(invocation.session_id, identity, attempt)
        if previous is not None:
            if not previous.value.same_observation(value):
                raise ToolFactConflict("a certain Outcome cannot be replaced")
            return previous
        return ToolOutcomeRecord(
            session_id=invocation.session_id, identity=identity, attempt=attempt, value=value
        )

    def publish_tool_batch(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        recovering: bool = False,
    ) -> tuple[
        SessionRecord, RunRecord, ToolCallBatchRecord, list[MessageRecord], tuple[EventRecord, ...]
    ]:
        """Publish a resolved source-ordered frame, Call state and Driver ready together."""
        ordered = sorted(batch.calls, key=lambda call: call.request.source_index)
        if not ordered or any(call.result is None for call in ordered):
            raise ValueError("publishing a tool batch requires feedback for every Call")
        resource_refs = [
            ref for call in ordered if call.result is not None for ref in call.result.resource_refs
        ]
        if resource_refs:
            if self._validate_resource_refs is None:
                raise ValueError(
                    "tool result contains resource_refs but no ResourceService is configured"
                )
            self._validate_resource_refs(session.session_id, resource_refs)
        with self.store.session_mutation(session.session_id):
            current = self.store.load_session_record(session.session_id)
            latest = self.store.load_run(session.session_id, run.run_id)
            if latest.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            identities = [
                ToolCallIdentity(
                    run_id=run.run_id, batch_id=batch.batch_id, tool_call_id=call.request.id
                )
                for call in ordered
            ]
            states = [
                self.store.load_tool_call_state(session.session_id, identity)
                for identity in identities
            ]
            if any(state is None for state in states):
                raise PersistenceError("tool publication requires accepted Call states")
            selected = [state for state in states if state is not None]
            published = [state.result_message_id is not None for state in selected]
            if any(published):
                if not all(published):
                    raise PersistenceError("tool frame has partially published Call states")
                messages = []
                for state in selected:
                    assert state.result_message_id is not None
                    message = self.store.load_message(session.session_id, state.result_message_id)
                    if (
                        message is None
                        or message.outcome_ref is None
                        or message.outcome_ref.identity != state.identity
                    ):
                        raise PersistenceError(
                            "published Call is missing its fixed feedback message"
                        )
                    messages.append(message)
                completed = batch.model_copy(
                    update={
                        "phase": "completed",
                        "calls": [
                            call.model_copy(
                                update={
                                    "status": state.status,
                                    "result_message_id": state.result_message_id,
                                }
                            )
                            for call, state in zip(ordered, selected, strict=True)
                        ],
                    }
                )
                return current, latest, completed, messages, ()
            if not recovering:
                self._require_active(latest)
            elif latest.status not in {"aborted", "failed", "recoverable"}:
                raise SessionBusyError("only an interrupted Run can publish recovery feedback")
            if (
                latest.active_tool_batch is None
                or latest.active_tool_batch.batch_id != batch.batch_id
            ):
                raise SessionCommitConflict("tool publication belongs to another active batch")
            if set(latest.active_tool_batch.call_ids) != {call.request.id for call in ordered}:
                raise SessionCommitConflict("tool frame must publish all Calls in its native batch")
            messages = []
            published_states = []
            published_calls = []
            changes = {}
            for call, state in zip(ordered, selected, strict=True):
                result = call.result
                assert result is not None
                if not recovering and call.feedback != state.feedback:
                    raise SessionCommitConflict("tool feedback changed before publication")
                if state.outcome_attempt is None and call.status not in {
                    "unknown_outcome",
                    "aborted",
                    "failed",
                    "rejected",
                }:
                    raise PersistenceError("certain tool result must be saved before publication")
                message = MessageRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    turn_id=batch.turn_id,
                    role="tool_result",
                    scaffold=batch.producer.kind == "driver",
                    content=[*result.content_blocks],
                    resource_refs=list(result.resource_refs),
                    tool_result=ToolResultReference(
                        tool_call_id=call.request.id,
                        tool_name=call.request.name,
                        producer=batch.producer,
                        is_error=result.is_error,
                    ),
                    tool_execution=ToolExecutionTiming(
                        started_at=result.started_at,
                        ended_at=result.ended_at or result.started_at,
                    ),
                    outcome_ref=ToolMessageReference(
                        identity=state.identity,
                        outcome_attempt=state.outcome_attempt,
                        feedback_is_error=result.is_error
                        if state.outcome_attempt is None
                        else None,
                    ),
                    tool_feedback=call.feedback,
                )
                messages.append(message)
                if (
                    not recovering
                    and state.proposals is not None
                    and state.proposals.harness_change is not None
                ):
                    changes[message.message_id] = state.proposals.harness_change
                published_states.append(
                    state.model_copy(
                        update={
                            "status": call.status,
                            "result_message_id": message.message_id,
                            "proposals": None,
                            "feedback": None,
                        }
                    )
                )
                published_calls.append(
                    call.model_copy(update={"result_message_id": message.message_id})
                )
            checkpoint = latest.driver_checkpoint
            if checkpoint is not None and checkpoint.state == "pending":
                if checkpoint.batch_id != batch.batch_id:
                    raise SessionCommitConflict("Driver checkpoint belongs to another batch")
                checkpoint = checkpoint.model_copy(update={"state": "ready"})
            completed_batch = batch.model_copy(
                update={
                    "phase": "completed",
                    "calls": published_calls,
                    "updated_at": utc_now(),
                }
            )
            head = (
                completed_batch.head()
                if recovering or (checkpoint and checkpoint.state == "ready")
                else None
            )
            updated = latest.model_copy(
                update={
                    "active_tool_batch": head,
                    "driver_checkpoint": checkpoint,
                    "phase": latest.phase if recovering else "model_streaming",
                    "pending_approval_id": None,
                    "metadata": {**latest.metadata, "transcript_reconciled": True}
                    if recovering
                    else latest.metadata,
                }
            )
            events = (
                tuple(
                    EventRecord(
                        session_id=run.session_id,
                        run_id=run.run_id,
                        turn_id=batch.turn_id,
                        event_type="recovery_tool_result_committed",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_call_id": call.request.id,
                            "tool_name": call.request.name,
                            "outcome_unknown": call.status == "unknown_outcome",
                        },
                    )
                    for call in ordered
                )
                if recovering
                else (
                    EventRecord(
                        session_id=run.session_id,
                        run_id=run.run_id,
                        turn_id=batch.turn_id,
                        event_type="tool_batch_completed",
                        payload={
                            "batch_id": batch.batch_id,
                            "turn_id": batch.turn_id,
                            "tool_result_count": len(messages),
                        },
                    ),
                )
            )
            committed_events = self.append_messages(
                session=current,
                run=latest,
                messages=messages,
                allow_interrupted=recovering,
                state_changes=changes,
                run_update=updated,
                call_states=tuple(published_states),
                events=events,
            )
            return (
                self.store.load_session_record(session.session_id),
                updated,
                completed_batch,
                messages,
                committed_events,
            )

    def record_usage(
        self,
        run: RunRecord,
        usage: Usage,
        *,
        model: ModelMetadata,
        request_id: str,
        purpose: UsagePurpose,
        workspace: str | None = None,
    ) -> str | None:
        """Record original consumption without a Session transaction or Run copy."""
        if self.usage_service is not None:
            return self.usage_service.record_provider_usage(
                run=run,
                workspace=workspace,
                usage=usage,
                model=model,
                request_id=request_id,
                purpose=purpose,
            ).entry_id
        return None

    def usage_for_run(self, run: RunRecord) -> dict[str, int | float] | None:
        return (
            self.usage_service.run_values(run.session_id, run.run_id)
            if self.usage_service is not None
            else None
        )

    def prepare_summary_requests(self, session_id: str, run_id: str) -> SummaryRequestRecorder:
        """Capture usage ownership before the strategy starts its first request."""
        with self.store.session_read(session_id):
            session = self.store.load_session_record(session_id)
            run = self.store.load_run(session_id, run_id)
        return SummaryRequestRecorder(self.store, run, session.workspace, self.usage_service)

    def consume_driver_checkpoint(
        self, session_id: str, run_id: str
    ) -> tuple[RunRecord, DriverResume]:
        """Commit consumption and return its Run and native results in one store operation."""
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            run = self.store.load_run(session_id, run_id)
            self._require_active(run)
            checkpoint = run.driver_checkpoint
            batch = load_tool_batch(self.store, run)
            if checkpoint is not None and checkpoint.state == "consumed":
                raise ValueError(
                    "Driver checkpoint was consumed; interrupted plugin code cannot be replayed"
                )
            if (
                checkpoint is None
                or checkpoint.state != "ready"
                or batch is None
                or batch.batch_id != checkpoint.batch_id
                or batch.phase != "completed"
            ):
                raise ValueError("Run has no ready Driver checkpoint")
            if run.history_epoch != session.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            updated = run.model_copy(
                update={
                    "driver_checkpoint": checkpoint.model_copy(update={"state": "consumed"}),
                    "active_tool_batch": None,
                }
            )
            self.store.commit_session(SessionCommit(before=session, after=session, runs=(updated,)))
            return updated, DriverResume(
                checkpoint=checkpoint.payload,
                results=tuple(call.result for call in batch.calls if call.result is not None),
            )

    def save_batch_state(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord,
        status: RunStatus,
        phase: RunPhase,
        harness: HarnessCallSnapshot | None = None,
        driver_checkpoint: DriverCheckpoint | None = None,
        changed_call_ids: frozenset[str] | None = None,
        started_call_ids: frozenset[str] = frozenset(),
        assistant_message: MessageRecord | None = None,
        events: tuple[EventRecord, ...] = (),
    ) -> tuple[SessionRecord, RunRecord, ToolCallBatchRecord, tuple[EventRecord, ...]]:
        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            self._require_active(latest_run)
            if latest_run.history_epoch != latest_session.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            changes = [
                call.input_request.harness_change
                for call in batch.calls
                if call.status in {"waiting_user", "resolving_input"}
                and call.input_request is not None
                and call.input_request.harness_change is not None
            ]
            steps: tuple[SessionCommitStep, ...] = (
                (SessionCommitStep(message=assistant_message),)
                if assistant_message is not None
                else ()
            )
            if changes:
                draft = accept_state_changes(
                    harness or capture_harness(self.store, session.session_id, run=run),
                    changes,
                )
                steps += (SessionCommitStep(harness=draft.state),)
                batch = batch.model_copy(
                    update={
                        "calls": [
                            call.model_copy(
                                update={
                                    "input_request": call.input_request.model_copy(
                                        update={"harness_change": None},
                                    )
                                }
                            )
                            if call.status in {"waiting_user", "resolving_input"}
                            and call.input_request is not None
                            else call
                            for call in batch.calls
                        ]
                    }
                )
            accepted = steps[-1].harness if steps else None
            controls = accepted.controls if accepted is not None else latest_session.controls
            activations = {item.controller_id: item.activation_id for item in controls.activations}
            batch = batch.model_copy(
                update={
                    "calls": [
                        call.model_copy(
                            update={
                                "invocation": call.invocation.model_copy(
                                    update={
                                        "interaction_activation_id": activations.get(
                                            call.invocation.interaction_controller_id
                                        ),
                                    }
                                )
                            }
                        )
                        if call.status == "waiting_user"
                        and call.invocation is not None
                        and call.invocation.interaction_controller_id is not None
                        and call.invocation.interaction_activation_id is None
                        else call
                        for call in batch.calls
                    ]
                }
            )
            batch, tool_facts, call_states = prepare_batch_changes(
                self.store,
                latest_run,
                batch,
                changed_call_ids=changed_call_ids,
                started_call_ids=started_call_ids,
            )
            batch_events = self._batch_events(latest_run, batch, tool_facts, status)
            pending = batch.pending_approvals
            checkpoint = driver_checkpoint or latest_run.driver_checkpoint
            if driver_checkpoint is not None:
                if (
                    driver_checkpoint.batch_id != batch.batch_id
                    or driver_checkpoint.state != "pending"
                    or (
                        latest_run.driver_checkpoint is not None
                        and latest_run.driver_checkpoint.state != "consumed"
                    )
                ):
                    raise ValueError("Driver checkpoint must bind one new native tool batch")
            if batch.phase == "completed":
                raise ValueError("completed tool frames must use atomic publication")
            updated_run = latest_run.model_copy(
                update={
                    "status": status,
                    "phase": phase,
                    "active_tool_batch": batch.head(),
                    "driver_checkpoint": checkpoint,
                    "pending_approval_id": pending[0].approval_id if pending else None,
                }
            )
            transition = plan_run_transition(
                latest_session,
                current_run=latest_run,
                next_run=updated_run,
            )
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest_session.model_copy(
                        update={
                            "harness_version": harness.harness_version,
                            "history_epoch": harness.history_epoch,
                            "last_message_id": harness.last_message_id,
                        }
                    )
                    if harness is not None
                    else latest_session,
                    after=transition.session,
                    steps=steps,
                    runs=(transition.run,),
                    tool_facts=tool_facts,
                    call_states=call_states,
                    events=(*batch_events, *events),
                )
            )
            updated_run = transition.run
            updated_session = committed.session
            if assistant_message is not None:
                canonical = committed.messages[0]
                assistant_message.sequence = canonical.sequence
                assistant_message.before_harness_version = canonical.before_harness_version
        if status in {"waiting_approval", "waiting_user", "waiting_child"}:
            self.store.refresh_catalog(updated_session.session_id)
        return updated_session, updated_run, batch, committed.events

    @staticmethod
    def _batch_events(
        run: RunRecord,
        batch: ToolCallBatchRecord,
        facts: ToolCallFacts,
        status: RunStatus,
    ) -> tuple[EventRecord, ...]:
        """Describe the changes owned by this batch commit, without copying bodies."""
        events: list[EventRecord] = []

        def event(kind: str, **payload: Any) -> None:
            events.append(
                EventRecord(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    turn_id=batch.turn_id,
                    event_type=kind,
                    payload={"batch_id": batch.batch_id, "turn_id": batch.turn_id, **payload},
                )
            )

        if facts.calls:
            event(
                "tool_batch_planned",
                tool_call_count=len(batch.calls),
                execution_mode=batch.execution_mode,
            )
            for call in facts.calls:
                event(
                    "tool_call_requested",
                    tool_call_id=call.request.id,
                    tool_name=call.request.name,
                    arguments=call.request.arguments,
                    producer=call.producer.model_dump(mode="json"),
                    source_index=call.request.source_index,
                )
        for start in facts.starts:
            started_call = next(
                item for item in batch.calls if item.request.id == start.identity.tool_call_id
            )
            event(
                "tool_call_started",
                tool_call_id=started_call.request.id,
                tool_name=started_call.request.name,
                source_index=started_call.request.source_index,
                attempt=start.attempt,
            )
        previous = run.active_tool_batch
        if previous is not None and previous.batch_id == batch.batch_id:
            if previous.phase != batch.phase and batch.phase == "executing":
                event(
                    "tool_batch_started",
                    execution_mode=batch.execution_mode,
                    tool_calls=[
                        {
                            "tool_call_id": call.request.id,
                            "tool_name": call.request.name,
                            "status": call.status,
                            "source_index": call.request.source_index,
                        }
                        for call in batch.calls
                    ],
                )
            elif previous.phase != batch.phase and batch.phase == "committing_results":
                event(
                    "tool_batch_results_committing",
                    tool_result_count=sum(call.result is not None for call in batch.calls),
                )
        if status != run.status:
            event("run_status_changed", to_status=status, status=status)
        return tuple(events)

    def save_aborted_batch(
        self,
        *,
        session_id: str,
        run_id: str,
        batch: ToolCallBatchRecord,
        events: tuple[EventRecord, ...] = (),
    ) -> tuple[RunRecord, tuple[EventRecord, ...]]:
        """Attach final audit outcomes while an interrupted Run is settling."""

        with self.store.session_mutation(session_id):
            latest_run = self.store.load_run(session_id, run_id)
            if latest_run.status != "aborted" and not latest_run.abort_requested:
                return latest_run, ()
            current = latest_run.active_tool_batch
            if current is not None and current.batch_id != batch.batch_id:
                raise SessionBusyError(f"aborted run batch changed: {run_id} ({current.batch_id})")
            batch = batch.model_copy(
                update={
                    "calls": [
                        call.model_copy(
                            update={
                                "status": "unknown_outcome"
                                if call.status == "running"
                                else "aborted"
                            }
                        )
                        if call.status not in {"completed", "failed", "rejected", "unknown_outcome"}
                        else call
                        for call in batch.calls
                    ]
                }
            )
            batch, tool_facts, call_states = prepare_batch_changes(
                self.store,
                latest_run,
                batch,
                accept_proposals=False,
            )
            updated_run = latest_run.model_copy(update={"active_tool_batch": batch.head()})
            current_session = self.store.load_session_record(session_id)
            committed = self.store.commit_session(
                SessionCommit(
                    before=current_session,
                    runs=(updated_run,),
                    tool_facts=tool_facts,
                    call_states=call_states,
                    events=(
                        *events,
                        EventRecord(
                            session_id=session_id,
                            run_id=run_id,
                            turn_id=batch.turn_id,
                            event_type="tool_batch_aborted",
                            payload={
                                "batch_id": batch.batch_id,
                                "turn_id": batch.turn_id,
                                "tool_result_count": 0,
                            },
                        ),
                    ),
                )
            )
            return updated_run, committed.events

    async def complete(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        final_text: str,
        reasoning_content: str = "",
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
        turn_id: str | None = None,
        had_tool_error: bool,
        assistant_already_committed: bool = False,
    ) -> RunResult:
        persisted = await run_blocking(
            self._complete,
            session=session,
            run=run,
            final_text=final_text,
            reasoning_content=reasoning_content,
            model_response=model_response,
            provider_continuation=provider_continuation,
            turn_id=turn_id,
            had_tool_error=had_tool_error,
            assistant_already_committed=assistant_already_committed,
        )
        if isinstance(persisted, RunResult):
            return persisted
        completed_session, completed_run, events = persisted
        await self.events.publish_committed(events)
        return RunResult(
            session=completed_session,
            run=completed_run,
            final_text=final_text,
            had_tool_error=had_tool_error,
        )

    def _complete(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        final_text: str,
        reasoning_content: str,
        model_response: ModelResponseRecord | None,
        provider_continuation: ProviderContinuationArtifact | None,
        turn_id: str | None,
        had_tool_error: bool,
        assistant_already_committed: bool,
    ) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]] | RunResult:
        """Persist the complete transition as one blocking operation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or latest_run.abort_requested:
                return RunResult(
                    session=latest_session,
                    run=latest_run,
                    final_text=final_text,
                    had_tool_error=had_tool_error,
                )
            steps: tuple[SessionCommitStep, ...] = ()
            if not assistant_already_committed and (
                final_text or reasoning_content or provider_continuation is not None
            ):
                assistant_message = MessageRecord.text(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    role="assistant",
                    text=final_text,
                    turn_id=turn_id,
                )
                assistant_message = assistant_message.model_copy(
                    update={
                        "reasoning_content": reasoning_content,
                        "model_response": model_response,
                        "provider_continuation": provider_continuation,
                    }
                )
                steps = (SessionCommitStep(message=assistant_message),)
            now = utc_now()
            completed_run = latest_run.model_copy(
                update={
                    "status": "completed",
                    "ended_at": now,
                    "pending_approval_id": None,
                    "active_tool_batch": None,
                    "metadata": dict(latest_run.metadata),
                }
            )
            transition = plan_run_transition(
                latest_session,
                current_run=latest_run,
                next_run=completed_run,
                now=now,
            )
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest_session,
                    after=transition.session,
                    runs=(transition.run,),
                    steps=steps,
                    events=(
                        EventRecord(
                            session_id=session.session_id,
                            run_id=run.run_id,
                            event_type="run_completed",
                            payload={"final_text_length": len(final_text)},
                        ),
                    ),
                )
            )
            completed_run = transition.run
            completed_session = committed.session
        self.store.refresh_catalog(completed_session.session_id)
        return completed_session, completed_run, committed.events

    async def fail(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        error: str,
        error_code: str | None = None,
        final_text: str,
        reasoning_content: str = "",
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
        turn_id: str | None = None,
        had_tool_error: bool,
    ) -> RunResult:
        persisted = await run_blocking(
            self._fail,
            session=session,
            run=run,
            error=error,
            error_code=error_code,
            final_text=final_text,
            reasoning_content=reasoning_content,
            model_response=model_response,
            provider_continuation=provider_continuation,
            turn_id=turn_id,
            had_tool_error=had_tool_error,
        )
        if isinstance(persisted, RunResult):
            return persisted
        failed_session, failed_run, events = persisted
        await self.events.publish_committed(events)
        return RunResult(
            session=failed_session,
            run=failed_run,
            final_text=final_text,
            had_tool_error=had_tool_error,
            error_code=error_code,
        )

    def _fail(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        error: str,
        final_text: str,
        reasoning_content: str,
        model_response: ModelResponseRecord | None,
        provider_continuation: ProviderContinuationArtifact | None,
        turn_id: str | None,
        had_tool_error: bool,
        error_code: str | None = None,
    ) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]] | RunResult:
        """Persist the failed transition as one blocking operation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or latest_run.abort_requested:
                return RunResult(
                    session=latest_session,
                    run=latest_run,
                    final_text=final_text,
                    had_tool_error=had_tool_error,
                )
            steps: tuple[SessionCommitStep, ...] = ()
            if final_text or reasoning_content or provider_continuation is not None:
                metadata: dict[str, Any] = {
                    "incomplete": True,
                    "failure_reason": error,
                }
                assistant_message = MessageRecord.text(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    role="assistant",
                    text=final_text,
                    turn_id=turn_id,
                ).model_copy(
                    update={
                        "metadata": metadata,
                        "reasoning_content": reasoning_content,
                        "model_response": model_response,
                        "provider_continuation": provider_continuation,
                    }
                )
                steps = (SessionCommitStep(message=assistant_message),)
            now = utc_now()
            failed_run = latest_run.model_copy(
                update={
                    "status": "failed",
                    "ended_at": now,
                    "error": error,
                    "error_code": error_code,
                    "pending_approval_id": None,
                    # Failure must retain committed outcomes until recovery
                    # publishes their canonical tool-result messages.
                    "active_tool_batch": latest_run.active_tool_batch,
                    "metadata": dict(latest_run.metadata),
                }
            )
            transition = plan_run_transition(
                latest_session,
                current_run=latest_run,
                next_run=failed_run,
                now=now,
            )
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest_session,
                    after=transition.session,
                    runs=(transition.run,),
                    steps=steps,
                    events=(
                        EventRecord(
                            session_id=session.session_id,
                            run_id=run.run_id,
                            event_type="run_failed",
                            payload={
                                "error": error,
                                **({"error_code": error_code} if error_code else {}),
                            },
                        ),
                    ),
                )
            )
            failed_run = transition.run
            failed_session = committed.session
        self.store.refresh_catalog(failed_session.session_id)
        return failed_session, failed_run, committed.events

    async def pause(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        reason: RunPauseReason,
        next_model_round: int,
        final_text: str,
        had_tool_error: bool,
    ) -> RunResult:
        persisted = await run_blocking(
            self._pause,
            session=session,
            run=run,
            reason=reason,
            next_model_round=next_model_round,
            final_text=final_text,
            had_tool_error=had_tool_error,
        )
        if isinstance(persisted, RunResult):
            return persisted
        paused_session, paused_run, events = persisted
        await self.events.publish_committed(events)
        return RunResult(
            session=paused_session,
            run=paused_run,
            final_text=final_text,
            had_tool_error=had_tool_error,
        )

    def _pause(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        reason: RunPauseReason,
        next_model_round: int,
        final_text: str,
        had_tool_error: bool,
    ) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]] | RunResult:
        """Persist the paused transition as one blocking operation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or latest_run.abort_requested:
                return RunResult(
                    session=latest_session,
                    run=latest_run,
                    final_text=final_text,
                    had_tool_error=had_tool_error,
                )
            metadata = dict(latest_run.metadata)
            metadata["pause"] = {
                "kind": (
                    "context_maintenance"
                    if reason == "context_blocked"
                    else "turn_policy"
                    if reason == "turn_policy_blocked"
                    else "soft_budget"
                ),
                "reason": reason,
                "next_model_round": next_model_round,
            }
            now = utc_now()
            paused_run = latest_run.model_copy(
                update={
                    "status": "paused",
                    "ended_at": now,
                    "pending_approval_id": None,
                    "active_tool_batch": None,
                    "metadata": metadata,
                }
            )
            transition = plan_run_transition(
                latest_session,
                current_run=latest_run,
                next_run=paused_run,
                now=now,
            )
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest_session,
                    after=transition.session,
                    runs=(transition.run,),
                    events=(
                        EventRecord(
                            session_id=session.session_id,
                            run_id=run.run_id,
                            event_type="run_paused",
                            payload={"reason": reason, "next_model_round": next_model_round},
                        ),
                    ),
                )
            )
            paused_run = transition.run
            paused_session = committed.session
        self.store.refresh_catalog(paused_session.session_id)
        return paused_session, paused_run, committed.events

    def _append_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        message: MessageRecord,
        allow_interrupted: bool = False,
    ) -> None:
        self.append_messages(
            session=session,
            run=run,
            messages=[message],
            allow_interrupted=allow_interrupted,
        )

    def append_messages(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        allow_interrupted: bool = False,
        state_changes: dict[str, HarnessStateChange] | None = None,
        consumed_inputs: tuple[str, ...] = (),
        operation_id: str | None = None,
        run_update: RunRecord | None = None,
        call_states: tuple[ToolCallState, ...] = (),
        events: tuple[EventRecord, ...] = (),
    ) -> tuple[EventRecord, ...]:
        """Persist one ordered message batch and advance the Session projection once."""

        if not messages:
            return ()
        if any(
            message.session_id != session.session_id or message.run_id != run.run_id
            for message in messages
        ):
            raise ValueError("message batch must belong to the supplied Session and Run")
        with self.store.session_mutation(session.session_id):
            current = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            reconcilable_statuses = {"aborted", "failed", "recoverable"}
            if not allow_interrupted or latest_run.status not in reconcilable_statuses:
                self._require_active(latest_run)
            draft = (
                HarnessCallSnapshot(
                    session_id=current.session_id,
                    harness_version=current.harness_version,
                    history_epoch=current.history_epoch,
                    last_message_id=current.last_message_id,
                    run_id=run.run_id,
                    state=self.store.load_harness(
                        current.session_id, current.harness_version
                    ).state,
                )
                if state_changes
                else None
            )
            owners: set[str] = set()
            control_message: MessageRecord | None = None
            steps = []
            for message in messages:
                change = (state_changes or {}).get(message.message_id)
                if change is not None:
                    assert draft is not None
                    if change.owner_id in owners:
                        raise SessionCommitConflict(
                            "tool batch has competing owner state proposals"
                        )
                    owners.add(change.owner_id)
                    if change.controls is not None:
                        if control_message is not None:
                            raise SessionCommitConflict(
                                "tool batch has competing control proposals"
                            )
                        control_message = message
                    draft = accept_state_changes(draft, [change])
                steps.append(
                    SessionCommitStep(
                        message=message,
                        harness=draft.state if change is not None and draft is not None else None,
                    )
                )
            after = current.model_copy(
                update={
                    "last_activity_at": utc_now(),
                    "updated_at": utc_now(),
                    "last_run_id": run.run_id,
                }
            )
            request = SessionCommit(
                operation_id=operation_id or new_id("op"),
                before=current,
                after=after,
                steps=tuple(steps),
                input_changes=consumption_changes(consumed_inputs, messages),
                runs=(run_update,) if run_update is not None else (),
                call_states=call_states,
                events=events,
            )
            if control_message is not None:
                assert draft is not None and control_message.tool_result is not None
                active_ids = {item.activation_id for item in draft.state.controls.activations}
                released = tuple(
                    item.activation_id
                    for item in current.controls.activations
                    if item.activation_id not in active_ids
                )
                control_commit = self.session_controls.commit(
                    request,
                    released_activation_ids=released,
                    resolving_tool_call_id=control_message.tool_result.tool_call_id,
                )
                committed_events = control_commit.events
                committed_messages: list[MessageRecord] = []
                for message in messages:
                    canonical = self.store.load_message(session.session_id, message.message_id)
                    if canonical is None:
                        raise PersistenceError("committed tool frame is missing its message")
                    committed_messages.append(canonical)
            else:
                committed = self.store.commit_session(request)
                committed_events = committed.events
                committed_messages = list(committed.messages)
            for message, canonical in zip(messages, committed_messages, strict=True):
                message.sequence = canonical.sequence
                message.before_harness_version = canonical.before_harness_version
        # A durable message batch changes the bounded list projection even
        # while its Run remains active. Refresh only this Session; catalog
        # query paths never compensate by scanning canonical history.
        self.store.refresh_catalog(session.session_id)
        return committed_events

    @staticmethod
    def _require_active(run: RunRecord) -> None:
        if run.status not in ACTIVE_RUN_STATUSES or run.abort_requested:
            raise SessionBusyError(f"run is no longer active: {run.run_id} ({run.status})")


def abort_run_state(
    store: SessionStore,
    *,
    session_id: str,
    run_id: str | None = None,
    reason: str,
) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]]:
    """Persist the canonical terminal state used by every Run abort initiator."""

    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        run = (
            store.load_run(session_id, run_id)
            if run_id
            else find_active_run(
                store.load_pending_runs(session_id), history_epoch=session.history_epoch
            )
        )
        if run.status not in ACTIVE_RUN_STATUSES:
            return session, run, ()
        now = utc_now()
        metadata = dict(run.metadata)
        metadata["abort_reason"] = reason
        aborted_run = run.model_copy(
            update={
                "status": "aborted",
                "ended_at": now,
                "pending_approval_id": None,
                "abort_requested": True,
                "error": reason,
                "metadata": metadata,
            }
        )
        transition = plan_run_transition(
            session,
            current_run=run,
            next_run=aborted_run,
            now=now,
        )
        committed = store.commit_session(
            SessionCommit(
                before=session,
                after=transition.session,
                runs=(transition.run,),
                events=(
                    EventRecord(
                        session_id=session_id,
                        run_id=run.run_id,
                        event_type="run_aborted",
                        payload={"reason": reason},
                    ),
                ),
            )
        )
        aborted_run = transition.run
        idle_session = committed.session
    store.refresh_catalog(idle_session.session_id)
    return idle_session, aborted_run, committed.events


def request_run_abort_state(
    store: SessionStore,
    *,
    session_id: str,
    run_id: str | None = None,
    reason: str,
) -> tuple[SessionRecord, RunRecord, tuple[EventRecord, ...]]:
    """Persist cancellation intent without claiming that execution has stopped."""

    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        run = (
            store.load_run(session_id, run_id)
            if run_id
            else find_active_run(
                store.load_pending_runs(session_id), history_epoch=session.history_epoch
            )
        )
        if run.status not in ACTIVE_RUN_STATUSES or run.abort_requested:
            return session, run, ()
        metadata = dict(run.metadata)
        metadata["abort_reason"] = reason
        requested_run = run.model_copy(
            update={
                "abort_requested": True,
                "metadata": metadata,
            }
        )
        transition = plan_run_transition(
            session,
            current_run=run,
            next_run=requested_run,
        )
        committed = store.commit_session(
            SessionCommit(
                before=session,
                after=transition.session,
                runs=(transition.run,),
                events=(
                    EventRecord(
                        session_id=session_id,
                        run_id=run.run_id,
                        event_type="run_abort_requested",
                        payload={"reason": reason},
                    ),
                ),
            )
        )
        requested_run = transition.run
        active_session = committed.session
    store.refresh_catalog(active_session.session_id)
    return active_session, requested_run, committed.events


def find_active_run(runs: list[RunRecord], *, history_epoch: int) -> RunRecord:
    active = [
        run
        for run in runs
        if run.status in ACTIVE_RUN_STATUSES and run.history_epoch == history_epoch
    ]
    if len(active) > 1:
        raise PersistenceError("multiple active runs: " + ", ".join(run.run_id for run in active))
    if active:
        return active[0]
    raise ValueError("no active run")


def maybe_active_run(runs: list[RunRecord], *, history_epoch: int) -> RunRecord | None:
    try:
        return find_active_run(runs, history_epoch=history_epoch)
    except ValueError:
        return None
