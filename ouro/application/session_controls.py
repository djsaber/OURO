from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ouro.application.harness import capture_session_harness
from ouro.application.inputs.queue import QueuedInputQueue
from ouro.application.runs.tool_batches import load_tool_batch, prepare_batch_changes
from ouro.application.usage import UsageService
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import settle_owned_tasks
from ouro.core.errors import OuroError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot, accept_state_changes
from ouro.core.inputs import InputStateChange
from ouro.core.run_transitions import plan_run_transition
from ouro.core.session_commits import (
    SessionCommit,
    SessionCommitConflict,
    SessionCommitStep,
)
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlActivation,
    SessionControlClaim,
    SessionControlState,
    SessionControlTransitionResult,
)
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallState
from ouro.core.tool_facts import ToolCallFacts
from ouro.core.tools import ToolResult
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerError,
    PluginSessionControllerOutput,
    PluginSessionControllerRegistry,
)
from ouro.sessions import SessionStore


class SessionControlError(OuroError):
    pass


class SessionControlConflictError(SessionControlError):
    pass


class SessionControlContractError(SessionControlError):
    pass


class SessionControlStaleRevisionError(SessionControlError):
    pass


@dataclass(frozen=True)
class SessionControlCancellation:
    activation_id: str
    run_id: str | None
    queue_ids: tuple[str, ...]


@dataclass(frozen=True)
class SessionControlCommitOutcome:
    """Return values owned by this controller call, not a persistent command receipt."""

    cancellations: tuple[SessionControlCancellation, ...] = ()
    stop_requested_run_id: str | None = None
    events: tuple[EventRecord, ...] = ()


class SessionControlCoordinator:
    """Stage controller callbacks and settle their state, claims and owned work together."""

    def __init__(self, store: SessionStore, *, usage_service: UsageService | None = None) -> None:
        self.store = store
        self.usage_service = usage_service

    async def apply(
        self,
        transition: AttributedSessionControlTransition,
        *,
        session_id: str,
        controllers: PluginSessionControllerRegistry,
        workspace: str | None = None,
        resolving_tool_call_id: str | None = None,
    ) -> SessionControlTransitionResult:
        before, draft = await run_blocking(
            capture_session_harness, self.store, session_id, usage_service=self.usage_service
        )
        operation_id = new_id("op")
        draft, result = await self.stage(
            transition,
            session=before,
            draft=draft,
            controllers=controllers,
            workspace=workspace,
        )
        request = SessionCommit(
            operation_id=operation_id,
            before=before,
            steps=(SessionCommitStep(harness=draft.state),),
            events=(self._event(session_id, operation_id, transition.action),),
        )
        committed = await self.commit_async(
            request,
            released_activation_ids=tuple(result.released_activation_ids),
            resolving_tool_call_id=resolving_tool_call_id,
        )
        return self.with_cancellations(result, committed)

    async def stage(
        self,
        transition: AttributedSessionControlTransition,
        *,
        session: SessionRecord,
        draft: HarnessCallSnapshot,
        controllers: PluginSessionControllerRegistry,
        workspace: str | None = None,
    ) -> tuple[HarnessCallSnapshot, SessionControlTransitionResult]:
        """Evaluate one transition in the parent's draft, without canonical writes."""
        controls = draft.state.controls
        self._check_session_revision(controls, transition.expected_session_revision)
        targets: list[SessionControlActivation] = []
        controller: PluginSessionController | None = None
        if transition.action == "release":
            target = _select_release_activation(controls, transition)
            if target is None:
                return draft, SessionControlTransitionResult(
                    action="release",
                    changed=False,
                    session_revision=controls.revision,
                    output="Session controller is already inactive.",
                )
            targets = [target]
        else:
            canonical_id = f"{transition.plugin_id}/session_controllers/{transition.controller_id}"
            controller = self._controller_for_transition(controllers, canonical_id, transition)
            if transition.action == "acquire":
                existing = next(
                    (item for item in controls.activations if item.controller_id == canonical_id),
                    None,
                )
                if existing is not None:
                    self._validate_activation_controller(existing, controller)
                    return draft, SessionControlTransitionResult(
                        action="acquire",
                        changed=False,
                        session_revision=controls.revision,
                        activation=existing,
                        output=f"Session controller is already active: {canonical_id}",
                        data=dict(existing.data),
                    )
            else:
                selected = set(transition.replace_activation_ids)
                targets = [item for item in controls.activations if item.activation_id in selected]
                if {item.activation_id for item in targets} != selected:
                    raise SessionControlConflictError("replace target activation is unavailable")
            conflicts = _claim_conflicts(
                controller.claims,
                controls.activations,
                ignored_activation_ids={item.activation_id for item in targets},
            )
            if conflicts:
                details = ", ".join(f"{domain} held by {owner}" for domain, owner in conflicts)
                raise SessionControlConflictError(f"session control claim conflict: {details}")
        if any(item.plugin_id != transition.plugin_id for item in targets):
            raise SessionControlConflictError(
                f"a plugin cannot {transition.action} another plugin's Session controller"
            )
        if transition.expected_activation_revision is not None and (
            len(targets) != 1 or targets[0].revision != transition.expected_activation_revision
        ):
            raise SessionControlStaleRevisionError("session controller activation revision changed")
        target_controllers = []
        for target in targets:
            try:
                target_controller = controllers.get(target.controller_id)
            except KeyError as exc:
                raise SessionControlContractError(
                    f"session controller is unavailable: {target.controller_id}"
                ) from exc
            self._validate_activation_controller(target, target_controller)
            target_controllers.append((target, target_controller))
        output = PluginSessionControllerOutput()
        try:
            for target, target_controller in target_controllers:
                output = await target_controller.deactivate(
                    session_id=session.session_id,
                    session_kind=session.session_kind,
                    activation_id=target.activation_id,
                    payload=transition.payload,
                    reason=transition.reason or ("replaced" if controller else None),
                    replacement_controller_id=(controller.canonical_id if controller else None),
                    workspace=workspace,
                    harness=draft,
                )
                draft = self._accept_output(draft, output)
                # The next lifecycle callback observes the accepted removal in the
                # shared draft, while canonical readers still see the old aggregate.
                remaining = [
                    item
                    for item in draft.state.controls.activations
                    if item.activation_id != target.activation_id
                ]
                draft = draft.model_copy(
                    update={
                        "state": draft.state.model_copy(
                            update={
                                "controls": draft.state.controls.model_copy(
                                    update={"activations": remaining}
                                )
                            }
                        )
                    }
                )
            if controller is not None:
                activation = self._new_activation(controller)
                output = await controller.activate(
                    session_id=session.session_id,
                    session_kind=session.session_kind,
                    activation_id=activation.activation_id,
                    payload=transition.payload,
                    workspace=workspace,
                    harness=draft,
                )
                draft = self._accept_output(draft, output)
                activation = activation.model_copy(
                    update={"data": dict(output.data), "usage_scope": output.usage_scope}
                )
            else:
                activation = None
        except PluginSessionControllerError as exc:
            raise SessionControlError(str(exc)) from exc
        removed_ids = {item.activation_id for item in targets}
        retained = [item for item in controls.activations if item.activation_id not in removed_ids]
        if activation is not None:
            retained.append(activation)
        controls = _replace_control_state(controls, retained)
        draft = draft.model_copy(
            update={"state": draft.state.model_copy(update={"controls": controls})}
        )
        return draft, SessionControlTransitionResult(
            action=transition.action,
            changed=True,
            session_revision=controls.revision,
            activation=activation,
            released_activation_ids=sorted(removed_ids),
            output=output.output,
            data=dict(output.data),
        )

    @staticmethod
    def _accept_output(
        draft: HarnessCallSnapshot, output: PluginSessionControllerOutput
    ) -> HarnessCallSnapshot:
        return accept_state_changes(draft, [output.harness_change] if output.harness_change else [])

    def commit(
        self,
        request: SessionCommit,
        *,
        released_activation_ids: tuple[str, ...] = (),
        resolving_tool_call_id: str | None = None,
        stop_run_reason: str | None = None,
    ) -> SessionControlCommitOutcome:
        """Commit state, released controllers' pending work and an optional execution stop."""
        with self.store.session_mutation(request.before.session_id):
            after = (request.after or request.before).model_copy(
                update={"updated_at": utc_now(), "last_activity_at": utc_now()},
            )
            runs = list(request.runs)
            tool_calls = list(request.tool_facts.calls)
            tool_starts = list(request.tool_facts.starts)
            tool_outcomes = list(request.tool_facts.outcomes)
            call_states = list(request.call_states)
            events = list(request.events)
            cancellations = []
            stopped_run_id = None
            input_changes = {change.input_id: change for change in request.input_changes}
            for activation in request.before.controls.activations:
                if activation.activation_id not in released_activation_ids:
                    continue
                after, cancelled_run, cancelled_facts, cancelled_states = self._cancel_pending(
                    after,
                    activation,
                    exclude_tool_call_id=resolving_tool_call_id,
                )
                tool_calls.extend(cancelled_facts.calls)
                tool_starts.extend(cancelled_facts.starts)
                tool_outcomes.extend(cancelled_facts.outcomes)
                call_states.extend(cancelled_states)
                if cancelled_run is not None:
                    runs = [run for run in runs if run.run_id != cancelled_run.run_id]
                    runs.append(cancelled_run)
                    events.append(
                        EventRecord(
                            session_id=after.session_id,
                            run_id=cancelled_run.run_id,
                            event_type="run_aborted",
                            payload={"reason": "session_control_released"},
                        )
                    )
                after, queue_ids = QueuedInputQueue(self.store).without_session_control(
                    after,
                    controller_id=activation.controller_id,
                    activation_id=activation.activation_id,
                )
                cancellations.append(
                    SessionControlCancellation(
                        activation_id=activation.activation_id,
                        run_id=cancelled_run.run_id if cancelled_run else None,
                        queue_ids=tuple(queue_ids),
                    )
                )
                for identity in queue_ids:
                    if identity not in input_changes:
                        events.append(
                            EventRecord(
                                session_id=after.session_id,
                                run_id=after.active_run_id,
                                event_type="queued_input_cancelled",
                                payload={
                                    "queue_id": identity,
                                    "reason": "session_control_released",
                                    "controller_id": activation.controller_id,
                                    "activation_id": activation.activation_id,
                                },
                            )
                        )
                    input_changes.setdefault(
                        identity, InputStateChange(input_id=identity, status="cancelled")
                    )
            if stop_run_reason is not None:
                current = self.store.load_session_record(request.before.session_id)
                if current.active_run_id is not None:
                    run = next(
                        (item for item in runs if item.run_id == current.active_run_id),
                        None,
                    ) or self.store.load_run(current.session_id, current.active_run_id)
                    if run.status in ACTIVE_RUN_STATUSES:
                        if not run.abort_requested:
                            events.append(
                                EventRecord(
                                    session_id=current.session_id,
                                    run_id=run.run_id,
                                    event_type="run_abort_requested",
                                    payload={"reason": stop_run_reason},
                                )
                            )
                        run = run.model_copy(
                            update={
                                "abort_requested": True,
                                "metadata": {**run.metadata, "abort_reason": stop_run_reason},
                            }
                        )
                        runs = [item for item in runs if item.run_id != run.run_id]
                        runs.append(run)
                        stopped_run_id = run.run_id
            prepared = request.model_copy(
                update={
                    "after": after,
                    "runs": tuple(runs),
                    "input_changes": tuple(input_changes.values()),
                    "tool_facts": ToolCallFacts(
                        calls=tuple(tool_calls),
                        starts=tuple(tool_starts),
                        outcomes=tuple(tool_outcomes),
                    ),
                    "call_states": tuple(call_states),
                    "events": tuple(events),
                }
            )
            try:
                committed_events = self.store.commit_session(prepared).events
            except SessionCommitConflict:
                raise
            except PersistenceError:
                with self.store.session_mutation(request.before.session_id):
                    recovered = self.store.load_session_record(request.before.session_id)
                    if recovered.last_commit_id != request.operation_id:
                        raise
                    # The shared fact follower discovers this committed revision even
                    # when the caller did not receive the transient notification result.
                    committed_events = ()
        self.store.refresh_catalog(request.before.session_id)
        return SessionControlCommitOutcome(tuple(cancellations), stopped_run_id, committed_events)

    async def commit_async(
        self,
        request: SessionCommit,
        *,
        released_activation_ids: tuple[str, ...] = (),
        resolving_tool_call_id: str | None = None,
        stop_run_reason: str | None = None,
    ) -> SessionControlCommitOutcome:
        """Settle the owned write, retaining its local return value across cancellation."""
        task = asyncio.create_task(
            run_blocking(
                self.commit,
                request,
                released_activation_ids=released_activation_ids,
                resolving_tool_call_id=resolving_tool_call_id,
                stop_run_reason=stop_run_reason,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned_tasks(task)
            if task.cancelled() or task.exception() is not None:
                raise
            return task.result()

    @staticmethod
    def with_cancellations(
        result: SessionControlTransitionResult,
        committed: SessionControlCommitOutcome,
    ) -> SessionControlTransitionResult:
        data = dict(result.data)
        queue_ids: list[str] = []
        for cancellation in committed.cancellations:
            if cancellation.activation_id not in result.released_activation_ids:
                continue
            if cancellation.run_id is not None:
                data["cancelled_run_id"] = cancellation.run_id
            queue_ids.extend(cancellation.queue_ids)
        if queue_ids:
            data["cancelled_queue_ids"] = queue_ids
        return result.model_copy(update={"data": data})

    def force_release(
        self,
        *,
        session_id: str,
        activation_id: str,
        reason: str = "host_force_release",
    ) -> SessionControlTransitionResult:
        with self.store.session_mutation(session_id):
            before = self.store.load_session_record(session_id)
            state = self.store.load_harness(session_id, before.harness_version).state
        retained = [
            item for item in before.controls.activations if item.activation_id != activation_id
        ]
        changed = len(retained) != len(before.controls.activations)
        controls = _replace_control_state(before.controls, retained) if changed else before.controls
        result = SessionControlTransitionResult(
            action="release",
            changed=changed,
            session_revision=controls.revision,
            released_activation_ids=[activation_id] if changed else [],
            output=(
                "Session controller Host gate was force-released; "
                "owner-private cleanup was not confirmed."
            )
            if changed
            else "Session controller is already inactive.",
            data={"forced": True, "reason": reason},
        )
        if changed:
            operation_id = new_id("op")
            committed = self.commit(
                SessionCommit(
                    operation_id=operation_id,
                    before=before,
                    steps=(
                        SessionCommitStep(harness=state.model_copy(update={"controls": controls})),
                    ),
                    events=(self._event(session_id, operation_id, "release"),),
                ),
                released_activation_ids=(activation_id,),
            )
            return self.with_cancellations(result, committed)
        return result

    @staticmethod
    def _event(session_id: str, operation_id: str, action: str) -> EventRecord:
        return EventRecord(
            session_id=session_id,
            event_type="session_control_transition",
            payload={
                "operation_id": operation_id,
                "action": action,
                "phase": "applied",
                "error_type": None,
            },
        )

    @staticmethod
    def _controller_for_transition(
        controllers: PluginSessionControllerRegistry,
        canonical_id: str,
        transition: AttributedSessionControlTransition,
    ) -> PluginSessionController:
        try:
            controller = controllers.get(canonical_id)
        except KeyError as exc:
            raise SessionControlContractError(
                f"session controller is not available in this Catalog: {canonical_id}"
            ) from exc
        if controller.plugin_id != transition.plugin_id:
            raise SessionControlContractError("session controller owner does not match transition")
        if controller.generation_id != transition.generation_id:
            raise SessionControlContractError(
                "session controller generation does not match transition"
            )
        if controller.execution_contract_hash != transition.execution_contract_hash:
            raise SessionControlContractError(
                "session controller execution contract does not match transition"
            )
        return controller

    @staticmethod
    def _validate_activation_controller(
        activation: SessionControlActivation,
        controller: PluginSessionController,
    ) -> None:
        if (
            controller.execution_contract_hash != activation.execution_contract_hash
            or controller.release_id != activation.release_id
        ):
            raise SessionControlContractError(
                "active Session controller cannot be handled by this Catalog contract: "
                f"{activation.controller_id}"
            )

    @staticmethod
    def _check_session_revision(
        controls: SessionControlState,
        expected: int | None,
    ) -> None:
        if expected is not None and controls.revision != expected:
            raise SessionControlStaleRevisionError(
                f"session control revision changed: expected {expected}, found {controls.revision}"
            )

    @staticmethod
    def _new_activation(controller: PluginSessionController) -> SessionControlActivation:
        if (
            controller.release_id is None
            or controller.generation_id is None
            or controller.execution_contract_hash is None
        ):
            raise SessionControlContractError(
                f"session controller has no active execution contract: {controller.canonical_id}"
            )
        return SessionControlActivation(
            controller_id=controller.canonical_id,
            plugin_id=controller.plugin_id,
            controller_local_id=controller.controller_id,
            plugin_class=controller.plugin_class,
            release_id=controller.release_id,
            generation_id=controller.generation_id,
            execution_contract_hash=controller.execution_contract_hash,
            claims=list(controller.claims),
        )

    def _cancel_pending(
        self,
        session: SessionRecord,
        activation: SessionControlActivation,
        *,
        exclude_tool_call_id: str | None,
    ) -> tuple[SessionRecord, RunRecord | None, ToolCallFacts, tuple[ToolCallState, ...]]:
        if session.active_run_id is None:
            return session, None, ToolCallFacts(), ()
        run = self.store.load_run(session.session_id, session.active_run_id)
        batch = load_tool_batch(self.store, run)
        if (
            run.history_epoch != session.history_epoch
            or run.status != "waiting_user"
            or batch is None
        ):
            return session, None, ToolCallFacts(), ()
        owned = [
            call
            for call in batch.calls
            if call.status == "waiting_user"
            and call.request.id != exclude_tool_call_id
            and call.invocation is not None
            and call.invocation.interaction_controller_id == activation.controller_id
            and call.invocation.interaction_activation_id == activation.activation_id
        ]
        if not owned:
            return session, None, ToolCallFacts(), ()
        owned_ids = {call.request.id for call in owned}
        calls = [
            call.model_copy(
                update={
                    "status": "aborted",
                    "result": ToolResult(
                        tool_call_id=call.request.id,
                        tool_name=call.request.name,
                        is_error=True,
                        content="Session controller was released while awaiting input.",
                        structured={
                            "error_type": "SessionControlReleased",
                            "controller_id": activation.controller_id,
                            "activation_id": activation.activation_id,
                        },
                        ended_at=utc_now(),
                    ),
                }
            )
            if call.request.id in owned_ids
            else call
            for call in batch.calls
        ]
        batch_metadata = dict(batch.metadata)
        batch_metadata["session_control_cancellation"] = {
            "controller_id": activation.controller_id,
            "activation_id": activation.activation_id,
        }
        aborted_batch = batch.model_copy(
            update={
                "phase": "aborted",
                "calls": calls,
                "updated_at": utc_now(),
                "metadata": batch_metadata,
            }
        )
        run_metadata = dict(run.metadata)
        run_metadata["session_control_cancellation"] = {
            "controller_id": activation.controller_id,
            "activation_id": activation.activation_id,
        }
        now = utc_now()
        aborted_batch, facts, states = prepare_batch_changes(
            self.store,
            run,
            aborted_batch,
            changed_call_ids=frozenset(owned_ids),
            accept_proposals=False,
        )
        aborted_run = run.model_copy(
            update={
                "status": "aborted",
                "ended_at": now,
                "error": "session_control_released",
                "pending_approval_id": None,
                "active_tool_batch": aborted_batch.head(),
                "metadata": run_metadata,
            }
        )
        transition = plan_run_transition(
            session,
            current_run=run,
            next_run=aborted_run,
            now=now,
        )
        return transition.session, transition.run, facts, states


def _claim_conflicts(
    requested: tuple[SessionControlClaim, ...],
    activations: list[SessionControlActivation],
    *,
    ignored_activation_ids: set[str] | None = None,
) -> list[tuple[str, str]]:
    conflicts: list[tuple[str, str]] = []
    ignored = ignored_activation_ids or set()
    for claim in requested:
        for activation in activations:
            if activation.activation_id in ignored:
                continue
            for held in activation.claims:
                if held.domain != claim.domain:
                    continue
                if held.access == "exclusive" or claim.access == "exclusive":
                    conflicts.append((claim.domain, activation.controller_id))
    return sorted(set(conflicts))


def _replace_control_state(
    controls: SessionControlState,
    activations: list[SessionControlActivation],
) -> SessionControlState:
    return SessionControlState(
        revision=controls.revision + 1,
        activations=activations,
    )


def _select_release_activation(
    controls: SessionControlState,
    transition: AttributedSessionControlTransition,
) -> SessionControlActivation | None:
    if transition.activation_id is not None:
        return next(
            (
                item
                for item in controls.activations
                if item.activation_id == transition.activation_id
            ),
            None,
        )
    assert transition.controller_id is not None
    canonical_id = f"{transition.plugin_id}/session_controllers/{transition.controller_id}"
    return next(
        (item for item in controls.activations if item.controller_id == canonical_id),
        None,
    )


__all__ = [
    "SessionControlConflictError",
    "SessionControlContractError",
    "SessionControlCoordinator",
    "SessionControlError",
    "SessionControlStaleRevisionError",
]
