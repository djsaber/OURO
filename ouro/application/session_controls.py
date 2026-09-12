from __future__ import annotations

from typing import Any, cast

from pydantic import JsonValue

from ouro.application.harness import capture_session_harness
from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.usage import UsageService
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.errors import OuroError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot, accept_state_changes
from ouro.core.run_transitions import plan_run_transition
from ouro.core.session_commits import (
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    SessionCommitStep,
)
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlActivation,
    SessionControlClaim,
    SessionControlState,
    SessionControlTransitionResult,
)
from ouro.core.state import RunRecord, SessionRecord
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
        await run_blocking(
            self.store.append_ordered_event,
            self._event(
                session_id,
                operation_id,
                transition.action,
                "started",
            ),
        )
        try:
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
                events=(self._event(session_id, operation_id, transition.action, "applied"),),
                output={"session_control": result.model_dump(mode="json")},
            )
            committed = await run_blocking(
                self.commit,
                request,
                released_activation_ids=tuple(result.released_activation_ids),
                resolving_tool_call_id=resolving_tool_call_id,
            )
            return self.with_cancellations(result, committed)
        except BaseException as exc:
            # An undecided operation has no published business state to compensate.
            # A durable decision is recovered before reporting a failure after commit.
            recovered = await run_blocking(self.store.load_commit, session_id, operation_id)
            if recovered is not None:
                return self.with_cancellations(result, recovered)
            await run_blocking(
                self.store.append_ordered_event,
                self._event(
                    session_id, operation_id, transition.action, "failed", type(exc).__name__
                ),
            )
            raise

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
    ) -> SessionCommitResult:
        """Include released controllers' pending work in the same durable decision."""
        with self.store.session_mutation(request.before.session_id):
            after = (request.after or request.before).model_copy(
                update={"updated_at": utc_now(), "last_activity_at": utc_now()},
            )
            runs = list(request.runs)
            cancellations = {}
            for activation in request.before.controls.activations:
                if activation.activation_id not in released_activation_ids:
                    continue
                after, cancelled_run = self._cancel_pending(
                    after,
                    activation,
                    exclude_tool_call_id=resolving_tool_call_id,
                )
                if cancelled_run is not None:
                    runs = [run for run in runs if run.run_id != cancelled_run.run_id]
                    runs.append(cancelled_run)
                after, queue_ids = QueuedInputQueue(self.store).without_session_control(
                    after,
                    controller_id=activation.controller_id,
                    activation_id=activation.activation_id,
                )
                cancellations[activation.activation_id] = {
                    "run_id": cancelled_run.run_id if cancelled_run else None,
                    "queue_ids": list(queue_ids),
                }
            output = dict(request.output)
            if cancellations:
                output["control_cancellations"] = cast(JsonValue, cancellations)
            prepared = request.model_copy(
                update={"after": after, "runs": tuple(runs), "output": output}
            )
            try:
                committed = self.store.commit_session(prepared)
            except SessionCommitConflict:
                raise
            except PersistenceError:
                recovered = self.store.load_commit(request.before.session_id, request.operation_id)
                if recovered is None:
                    raise
                committed = recovered
        self.store.refresh_catalog(request.before.session_id)
        return committed

    @staticmethod
    def with_cancellations(
        result: SessionControlTransitionResult,
        committed: SessionCommitResult,
    ) -> SessionControlTransitionResult:
        cancellations = cast(
            dict[str, dict[str, Any]], committed.output.get("control_cancellations", {})
        )
        data = dict(result.data)
        queue_ids = []
        for activation_id in result.released_activation_ids:
            cancellation = cancellations.get(activation_id)
            if cancellation is None:
                continue
            if cancellation.get("run_id") is not None:
                data["cancelled_run_id"] = cancellation["run_id"]
            queue_ids.extend(cancellation.get("queue_ids", []))
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
            committed = self.commit(
                SessionCommit(
                    before=before,
                    output={"session_control": result.model_dump(mode="json")},
                    steps=(
                        SessionCommitStep(harness=state.model_copy(update={"controls": controls})),
                    ),
                ),
                released_activation_ids=(activation_id,),
            )
            return self.with_cancellations(result, committed)
        return result

    @staticmethod
    def _event(
        session_id: str, operation_id: str, action: str, phase: str, error: str | None = None
    ) -> EventRecord:
        return EventRecord(
            session_id=session_id,
            event_type="session_control_transition",
            payload={
                "operation_id": operation_id,
                "action": action,
                "phase": phase,
                "error_type": error,
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
    ) -> tuple[SessionRecord, RunRecord | None]:
        if session.active_run_id is None:
            return session, None
        run = self.store.load_run(session.session_id, session.active_run_id)
        batch = run.active_tool_batch
        if (
            run.history_epoch != session.history_epoch
            or run.status != "waiting_user"
            or batch is None
        ):
            return session, None
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
            return session, None
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
        aborted_run = run.model_copy(
            update={
                "status": "aborted",
                "ended_at": now,
                "error": "session_control_released",
                "pending_approval_id": None,
                "active_tool_batch": aborted_batch,
                "metadata": run_metadata,
            }
        )
        transition = plan_run_transition(
            session,
            current_run=run,
            next_run=aborted_run,
            now=now,
        )
        return transition.session, transition.run


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
