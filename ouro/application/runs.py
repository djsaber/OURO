from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ouro.application.events import RuntimeEventRecorder
from ouro.application.harness import capture_harness
from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.usage import UsageService
from ouro.core.base import utc_now
from ouro.core.billing import UsagePurpose
from ouro.core.callbacks import run_blocking
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessStateChange, accept_state_changes
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
    ToolResultReference,
)
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.results import RunResult
from ouro.core.run_budget import RunPauseReason
from ouro.core.run_transitions import plan_run_transition
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.state import (
    ACTIVE_RUN_STATUSES,
    RunPhase,
    RunRecord,
    RunStatus,
    SessionRecord,
)
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolResult
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
            if configure_session is not None:
                session = configure_session(session)
            transition = plan_run_transition(session, current_run=None, next_run=run)
            store.save_run(transition.run)
            store.save_session(transition.session)
    finally:
        lock.release()
    store.refresh_catalog(run.session_id)
    return transition.run


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
        self._message_cache: dict[
            tuple[str, str],
            tuple[str | None, dict[tuple[str, str], MessageRecord]],
        ] = {}

    def start(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        snapshot: RuntimeSessionSnapshot,
        resume: bool = False,
        input_messages: tuple[MessageRecord, ...] = (),
        consumed_inputs: tuple[str, ...] = (),
    ) -> tuple[SessionRecord, RunRecord]:
        """Persist the shared session/run transition into an active run."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            existing = maybe_active_run(snapshot.runs, history_epoch=latest_session.history_epoch)
            if existing is not None and existing.run_id != run.run_id:
                raise SessionBusyError(f"session already has an active run: {existing.run_id}")
            if latest_session.active_run_id not in {None, run.run_id}:
                raise SessionBusyError(
                    f"session already has an active run: {latest_session.active_run_id}"
                )
            current_run = next(
                (item for item in snapshot.runs if item.run_id == run.run_id),
                None,
            )
            transition = plan_run_transition(
                latest_session,
                current_run=current_run,
                next_run=run.model_copy(update={"status": "running"}),
                resume=resume,
            )
            after = (
                QueuedInputQueue(self.store).without_inputs(transition.session, consumed_inputs)
                if consumed_inputs
                else transition.session
            )
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest_session,
                    after=after,
                    runs=(transition.run,),
                    steps=tuple(SessionCommitStep(message=item) for item in input_messages),
                )
            )
            for message, canonical in zip(input_messages, committed.messages, strict=True):
                message.sequence = canonical.sequence
                message.before_harness_version = canonical.before_harness_version
            active_run = transition.run
            active_session = committed.session
        self.store.refresh_catalog(active_session.session_id)
        return active_session, active_run

    def append_input_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        prompt: str,
        role: MessageRole,
        scaffold: bool,
        skill_activation: SkillActivationContent | None = None,
        sequence: int,
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
            skill_activation=skill_activation,
            sequence=sequence,
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
        skill_activation: SkillActivationContent | None = None,
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
        if skill_activation is not None:
            content.append(skill_activation)
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

    def append_assistant_tool_batch_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        tool_calls: list[ToolCallRequest],
        text: str,
        sequence: int,
        turn_id: str,
        reasoning_content: str = "",
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
    ) -> MessageRecord:
        existing = self._find_message(
            run,
            ("assistant_tools", turn_id),
        )
        if existing is not None:
            return existing
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=sequence,
            turn_id=turn_id,
        ).model_copy(
            update={
                "tool_calls": list(tool_calls),
                "reasoning_content": reasoning_content,
                "model_response": model_response,
                "provider_continuation": provider_continuation,
            }
        )
        self._append_message(session=session, run=run, message=message)
        return message

    def append_assistant_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        text: str,
        sequence: int,
        turn_id: str,
        reasoning_content: str = "",
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
    ) -> MessageRecord:
        """Durably commit a completed assistant turn without completing its run."""

        existing = self._find_message(
            run,
            ("assistant", turn_id),
        )
        if existing is not None:
            return existing
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=sequence,
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

    def append_tool_result_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        result: ToolResult,
        sequence: int,
        turn_id: str | None = None,
    ) -> MessageRecord:
        return self.append_tool_result_messages(
            session=session,
            run=run,
            results=[result],
            first_sequence=sequence,
            turn_id=turn_id,
        )[0]

    def append_tool_result_messages(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        results: list[ToolResult],
        first_sequence: int,
        turn_id: str | None = None,
    ) -> list[MessageRecord]:
        """Validate and commit one ordered tool-result message batch."""

        resource_refs = [reference for result in results for reference in result.resource_refs]
        if resource_refs and self._validate_resource_refs is None:
            raise ValueError(
                "tool result contains resource_refs but no ResourceService is configured"
            )
        if resource_refs and self._validate_resource_refs is not None:
            self._validate_resource_refs(session.session_id, resource_refs)

        messages: list[MessageRecord] = []
        missing: list[MessageRecord] = []
        state_changes: dict[str, HarnessStateChange] = {}
        missing_by_tool_call_id: dict[str, MessageRecord] = {}
        for index, result in enumerate(results):
            existing = self._find_message(
                run,
                ("tool_result", result.tool_call_id),
            )
            if existing is None:
                existing = missing_by_tool_call_id.get(result.tool_call_id)
            if existing is not None:
                messages.append(existing)
                continue
            message = MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id,
                role="tool_result",
                text=result.content,
                sequence=first_sequence + index,
                turn_id=turn_id,
            ).model_copy(
                update={
                    "resource_refs": result.resource_refs,
                    "tool_result": ToolResultReference(
                        tool_call_id=result.tool_call_id,
                        tool_name=result.tool_name,
                        is_error=result.is_error,
                    ),
                    "tool_execution": ToolExecutionTiming(
                        started_at=result.started_at,
                        ended_at=result.ended_at or result.started_at,
                    ),
                }
            )
            messages.append(message)
            missing.append(message)
            missing_by_tool_call_id[result.tool_call_id] = message
            if result.harness_change is not None:
                state_changes[message.message_id] = result.harness_change
        self.append_messages(
            session=session,
            run=run,
            messages=missing,
            allow_interrupted=True,
            state_changes=state_changes,
        )
        return messages

    def record_usage(
        self,
        run: RunRecord,
        usage: Usage,
        *,
        model: ModelMetadata,
        request_id: str,
        purpose: UsagePurpose,
    ) -> RunRecord:
        """Persist provider usage to both the global ledger and owning Run."""

        with self.store.session_mutation(run.session_id):
            latest = self.store.load_run(run.session_id, run.run_id)
            if self.usage_service is not None:
                entry = self.usage_service.record_provider_usage(
                    run=run,
                    workspace=self.store.load_session_record(run.session_id).workspace,
                    usage=usage,
                    model=model,
                    request_id=request_id,
                    purpose=purpose,
                )
                usage_entry_id = entry.entry_id
            else:
                usage_entry_id = ":".join(
                    (
                        purpose,
                        request_id,
                        model.provider_id,
                        model.endpoint_id or "",
                        model.model_id,
                    )
                )
            if usage_entry_id in latest.usage_entry_ids:
                return latest
            updated = latest.model_copy(
                update={
                    "usage": merge_usage(latest.usage, usage),
                    "usage_entry_ids": [*latest.usage_entry_ids, usage_entry_id],
                }
            )
            self.store.save_run(updated)
            return updated

    def record_usage_for_run(
        self,
        *,
        session_id: str,
        run_id: str,
        usage: Usage,
        model: ModelMetadata,
        request_id: str,
        purpose: UsagePurpose,
    ) -> RunRecord:
        run = self.store.load_run(session_id, run_id)
        return self.record_usage(
            run,
            usage,
            model=model,
            request_id=request_id,
            purpose=purpose,
        )

    def save_waiting_state(self, *, session: SessionRecord, run: RunRecord) -> SessionRecord:
        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            self._require_active(latest_run)
            updated_run = run.model_copy(
                update={
                    "usage": latest_run.usage,
                    "abort_requested": latest_run.abort_requested,
                }
            )
            transition = plan_run_transition(
                latest_session,
                current_run=latest_run,
                next_run=updated_run,
            )
            self.store.save_run(transition.run)
            self.store.save_session(transition.session)
            updated_session = transition.session
        self.store.refresh_catalog(updated_session.session_id)
        return updated_session

    def save_batch_state(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        batch: ToolCallBatchRecord | None,
        status: RunStatus,
        phase: RunPhase,
        harness: HarnessCallSnapshot | None = None,
    ) -> tuple[SessionRecord, RunRecord]:
        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            self._require_active(latest_run)
            if latest_run.history_epoch != latest_session.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            changes = [
                call.input_request.harness_change
                for call in (batch.calls if batch is not None else [])
                if call.status in {"waiting_user", "resolving_input"}
                and call.input_request is not None
                and call.input_request.harness_change is not None
            ]
            steps: tuple[SessionCommitStep, ...] = ()
            if changes:
                draft = accept_state_changes(
                    harness or capture_harness(self.store, session.session_id, run=run),
                    changes,
                )
                steps = (SessionCommitStep(harness=draft.state),)
                assert batch is not None
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
            if batch is not None:
                accepted = steps[0].harness if steps else None
                controls = accepted.controls if accepted is not None else latest_session.controls
                activations = {
                    item.controller_id: item.activation_id for item in controls.activations
                }
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
            pending = batch.pending_approvals if batch is not None else []
            updated_run = latest_run.model_copy(
                update={
                    "status": status,
                    "phase": phase,
                    "active_tool_batch": batch,
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
                )
            )
            updated_run = transition.run
            updated_session = committed.session
        if status in {"waiting_approval", "waiting_user", "waiting_child"}:
            self.store.refresh_catalog(updated_session.session_id)
        return updated_session, updated_run

    def save_aborted_batch(
        self,
        *,
        session_id: str,
        run_id: str,
        batch: ToolCallBatchRecord,
    ) -> RunRecord:
        """Attach final audit outcomes while an interrupted Run is settling."""

        with self.store.session_mutation(session_id):
            latest_run = self.store.load_run(session_id, run_id)
            if latest_run.status != "aborted" and not latest_run.abort_requested:
                return latest_run
            current = latest_run.active_tool_batch
            if current is not None and current.batch_id != batch.batch_id:
                raise SessionBusyError(f"aborted run batch changed: {run_id} ({current.batch_id})")
            updated_run = latest_run.model_copy(update={"active_tool_batch": batch})
            self.store.save_run(updated_run)
            return updated_run

    async def complete(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        final_text: str,
        reasoning_content: str = "",
        model_response: ModelResponseRecord | None = None,
        provider_continuation: ProviderContinuationArtifact | None = None,
        turn_id: str | None = None,
        tool_results: list[ToolResult],
    ) -> RunResult:
        persisted = await run_blocking(
            self._complete,
            emitted=emitted,
            session=session,
            run=run,
            messages=messages,
            final_text=final_text,
            reasoning_content=reasoning_content,
            model_response=model_response,
            provider_continuation=provider_continuation,
            turn_id=turn_id,
            tool_results=tool_results,
        )
        if isinstance(persisted, RunResult):
            return persisted
        completed_session, completed_run = persisted
        await self.events.emit(
            emitted,
            sequence,
            session_id=completed_session.session_id,
            run_id=completed_run.run_id,
            event_type="run_completed",
            payload={"final_text_length": len(final_text)},
        )
        return RunResult(
            session=completed_session,
            run=completed_run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _complete(
        self,
        *,
        emitted: list[EventRecord],
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        final_text: str,
        reasoning_content: str,
        model_response: ModelResponseRecord | None,
        provider_continuation: ProviderContinuationArtifact | None,
        turn_id: str | None,
        tool_results: list[ToolResult],
    ) -> tuple[SessionRecord, RunRecord] | RunResult:
        """Persist the complete transition as one blocking operation."""

        with self.store.session_mutation(session.session_id):
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or latest_run.abort_requested:
                return RunResult(
                    session=self.store.load_session_record(session.session_id),
                    run=latest_run,
                    final_text=final_text,
                    events=emitted,
                    tool_results=tool_results,
                )
            if final_text or reasoning_content or provider_continuation is not None:
                assistant_message = MessageRecord.text(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    role="assistant",
                    text=final_text,
                    sequence=len(messages) + 1,
                    turn_id=turn_id,
                )
                assistant_message = assistant_message.model_copy(
                    update={
                        "reasoning_content": reasoning_content,
                        "model_response": model_response,
                        "provider_continuation": provider_continuation,
                    }
                )
                self._append_message(session=session, run=latest_run, message=assistant_message)
                messages.append(assistant_message)

            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            self._require_active(latest_run)
            last_message_id = messages[-1].message_id if messages else session.last_message_id
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
            session_basis = latest_session.model_copy(
                update={
                    "last_message_id": last_message_id,
                    "message_count": len(messages),
                }
            )
            transition = plan_run_transition(
                session_basis,
                current_run=latest_run,
                next_run=completed_run,
                now=now,
            )
            self.store.save_run(transition.run)
            self.store.save_session(transition.session)
            completed_run = transition.run
            completed_session = transition.session
        self.store.refresh_catalog(completed_session.session_id)
        return completed_session, completed_run

    async def fail(
        self,
        emitted: list[EventRecord],
        sequence: int,
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
        tool_results: list[ToolResult],
    ) -> RunResult:
        persisted = await run_blocking(
            self._fail,
            emitted=emitted,
            session=session,
            run=run,
            error=error,
            final_text=final_text,
            reasoning_content=reasoning_content,
            model_response=model_response,
            provider_continuation=provider_continuation,
            turn_id=turn_id,
            tool_results=tool_results,
        )
        if isinstance(persisted, RunResult):
            return persisted
        failed_session, failed_run = persisted
        await self.events.emit(
            emitted,
            sequence,
            session_id=failed_session.session_id,
            run_id=failed_run.run_id,
            event_type="run_failed",
            payload={"error": error, **({"error_code": error_code} if error_code else {})},
        )
        return RunResult(
            session=failed_session,
            run=failed_run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _fail(
        self,
        *,
        emitted: list[EventRecord],
        session: SessionRecord,
        run: RunRecord,
        error: str,
        final_text: str,
        reasoning_content: str,
        model_response: ModelResponseRecord | None,
        provider_continuation: ProviderContinuationArtifact | None,
        turn_id: str | None,
        tool_results: list[ToolResult],
    ) -> tuple[SessionRecord, RunRecord] | RunResult:
        """Persist the failed transition as one blocking operation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or latest_run.abort_requested:
                return RunResult(
                    session=latest_session,
                    run=latest_run,
                    final_text=final_text,
                    events=emitted,
                    tool_results=tool_results,
                )
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
                    sequence=latest_session.message_count + 1,
                    turn_id=turn_id,
                ).model_copy(
                    update={
                        "metadata": metadata,
                        "reasoning_content": reasoning_content,
                        "model_response": model_response,
                        "provider_continuation": provider_continuation,
                    }
                )
                self._append_message(session=session, run=latest_run, message=assistant_message)
                latest_session = self.store.load_session_record(session.session_id)
                latest_run = self.store.load_run(session.session_id, run.run_id)
                self._require_active(latest_run)
            now = utc_now()
            failed_run = latest_run.model_copy(
                update={
                    "status": "failed",
                    "ended_at": now,
                    "error": error,
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
            self.store.save_run(transition.run)
            self.store.save_session(transition.session)
            failed_run = transition.run
            failed_session = transition.session
        self.store.refresh_catalog(failed_session.session_id)
        return failed_session, failed_run

    async def pause(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session: SessionRecord,
        run: RunRecord,
        reason: RunPauseReason,
        next_model_round: int,
        final_text: str,
        tool_results: list[ToolResult],
    ) -> RunResult:
        persisted = await run_blocking(
            self._pause,
            emitted=emitted,
            session=session,
            run=run,
            reason=reason,
            next_model_round=next_model_round,
            final_text=final_text,
            tool_results=tool_results,
        )
        if isinstance(persisted, RunResult):
            return persisted
        paused_session, paused_run = persisted
        await self.events.emit(
            emitted,
            sequence,
            session_id=paused_session.session_id,
            run_id=paused_run.run_id,
            event_type="run_paused",
            payload={
                "reason": reason,
                "next_model_round": next_model_round,
                "usage": paused_run.usage,
            },
        )
        return RunResult(
            session=paused_session,
            run=paused_run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    def _pause(
        self,
        *,
        emitted: list[EventRecord],
        session: SessionRecord,
        run: RunRecord,
        reason: RunPauseReason,
        next_model_round: int,
        final_text: str,
        tool_results: list[ToolResult],
    ) -> tuple[SessionRecord, RunRecord] | RunResult:
        """Persist the paused transition as one blocking operation."""

        with self.store.session_mutation(session.session_id):
            latest_session = self.store.load_session_record(session.session_id)
            latest_run = self.store.load_run(session.session_id, run.run_id)
            if latest_run.status not in ACTIVE_RUN_STATUSES or latest_run.abort_requested:
                return RunResult(
                    session=latest_session,
                    run=latest_run,
                    final_text=final_text,
                    events=emitted,
                    tool_results=tool_results,
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
            self.store.save_run(transition.run)
            self.store.save_session(transition.session)
            paused_run = transition.run
            paused_session = transition.session
        self.store.refresh_catalog(paused_session.session_id)
        return paused_session, paused_run

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
    ) -> None:
        """Persist one ordered message batch and advance the Session projection once."""

        if not messages:
            return
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
            if consumed_inputs:
                after = QueuedInputQueue(self.store).without_inputs(after, consumed_inputs)
            request = SessionCommit(before=current, after=after, steps=tuple(steps))
            if control_message is not None:
                assert draft is not None and control_message.tool_result is not None
                active_ids = {item.activation_id for item in draft.state.controls.activations}
                released = tuple(
                    item.activation_id
                    for item in current.controls.activations
                    if item.activation_id not in active_ids
                )
                committed = self.session_controls.commit(
                    request,
                    released_activation_ids=released,
                    resolving_tool_call_id=control_message.tool_result.tool_call_id,
                )
            else:
                committed = self.store.commit_session(request)
            for message, canonical in zip(messages, committed.messages, strict=True):
                message.sequence = canonical.sequence
                message.before_harness_version = canonical.before_harness_version
            cache_key = (session.session_id, run.run_id)
            cached = self._message_cache.get(cache_key)
            if cached is not None and cached[0] == current.last_message_id:
                cache = cached[1]
                for message in messages:
                    key = _message_cache_key(message)
                    if key is not None:
                        cache[key] = message
                self._message_cache[cache_key] = (committed.session.last_message_id, cache)
        # A durable message batch changes the bounded list projection even
        # while its Run remains active. Refresh only this Session; catalog
        # query paths never compensate by scanning canonical history.
        self.store.refresh_catalog(session.session_id)

    @staticmethod
    def _require_active(run: RunRecord) -> None:
        if run.status not in ACTIVE_RUN_STATUSES or run.abort_requested:
            raise SessionBusyError(f"run is no longer active: {run.run_id} ({run.status})")

    def _find_message(
        self,
        run: RunRecord,
        key: tuple[str, str],
    ) -> MessageRecord | None:
        cache_key = (run.session_id, run.run_id)
        with self.store.session_mutation(run.session_id):
            # This read also recovers a decided commit before inspecting the cache.
            current = self.store.load_session_record(run.session_id)
            if run.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Run belongs to withdrawn Session history")
            cached = self._message_cache.get(cache_key)
            if cached is None or cached[0] != current.last_message_id:
                cache = {}
                for message in self.store.load_run_messages(run.session_id, run.run_id):
                    message_key = _message_cache_key(message)
                    if message_key is not None:
                        cache[message_key] = message
                self._message_cache[cache_key] = (current.last_message_id, cache)
            else:
                cache = cached[1]
            return cache.get(key)

    def finish_run(self, run: RunRecord) -> None:
        self._message_cache.pop((run.session_id, run.run_id), None)


def find_run(runs: list[RunRecord], run_id: str) -> RunRecord:
    for run in runs:
        if run.run_id == run_id:
            return run
    raise KeyError(f"unknown run: {run_id}")


def abort_run_state(
    store: SessionStore,
    *,
    session_id: str,
    run_id: str | None = None,
    reason: str,
) -> tuple[SessionRecord, RunRecord, bool]:
    """Persist the canonical terminal state used by every Run abort initiator."""

    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        runs = store.load_runs(session_id)
        run = (
            find_run(runs, run_id)
            if run_id
            else find_active_run(runs, history_epoch=session.history_epoch)
        )
        if run.status not in ACTIVE_RUN_STATUSES:
            return session, run, False
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
        store.save_run(transition.run)
        store.save_session(transition.session)
        aborted_run = transition.run
        idle_session = transition.session
    store.refresh_catalog(idle_session.session_id)
    return idle_session, aborted_run, True


def request_run_abort_state(
    store: SessionStore,
    *,
    session_id: str,
    run_id: str | None = None,
    reason: str,
) -> tuple[SessionRecord, RunRecord, bool]:
    """Persist cancellation intent without claiming that execution has stopped."""

    with store.session_mutation(session_id):
        session = store.load_session_record(session_id)
        runs = store.load_runs(session_id)
        run = (
            find_run(runs, run_id)
            if run_id
            else find_active_run(runs, history_epoch=session.history_epoch)
        )
        if run.status not in ACTIVE_RUN_STATUSES or run.abort_requested:
            return session, run, False
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
        store.save_run(transition.run)
        store.save_session(transition.session)
        requested_run = transition.run
        active_session = transition.session
    store.refresh_catalog(active_session.session_id)
    return active_session, requested_run, True


def _message_cache_key(message: MessageRecord) -> tuple[str, str] | None:
    if message.role == "assistant" and message.turn_id is not None:
        return ("assistant_tools" if message.tool_calls else "assistant", message.turn_id)
    if message.role == "tool_result" and message.tool_result is not None:
        return ("tool_result", message.tool_result.tool_call_id)
    return None


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


def merge_usage(current: dict[str, int | float], usage: Usage) -> dict[str, int | float]:
    merged = dict(current)
    for key, value in usage.model_dump().items():
        if value is None:
            continue
        existing = merged.get(key, 0)
        merged[key] = existing + value if isinstance(existing, int | float) else value
    return merged
