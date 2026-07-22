from __future__ import annotations

from ouro.application.queued_inputs import QueuedInputQueue
from ouro.core.base import utc_now
from ouro.core.errors import OuroError
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlActivation,
    SessionControlClaim,
    SessionControlState,
    SessionControlTransitionResult,
)
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
    """Host-owned persistence and claim arbitration for plugin Session controllers."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def force_release(
        self,
        *,
        session_id: str,
        activation_id: str,
        reason: str = "host_force_release",
    ) -> SessionControlTransitionResult:
        """Revoke a Host activation even when its owner callback is unavailable."""

        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            activation = next(
                (
                    item
                    for item in session.controls.activations
                    if item.activation_id == activation_id
                ),
                None,
            )
            if activation is None:
                return SessionControlTransitionResult(
                    action="release",
                    changed=False,
                    session_revision=session.controls.revision,
                    output="Session controller is already inactive.",
                    data={"forced": True, "reason": reason},
                )
            controls = _replace_control_state(
                session.controls,
                [
                    item
                    for item in session.controls.activations
                    if item.activation_id != activation_id
                ],
            )
            self._save_controls(session, controls)
        cancelled_run_id = self._cancel_owned_pending_interaction(
            session_id=session_id,
            controller_id=activation.controller_id,
            activation_id=activation.activation_id,
        )
        cancelled_queue_ids = QueuedInputQueue(self.store).cancel_for_session_control(
            session_id=session_id,
            controller_id=activation.controller_id,
            activation_id=activation.activation_id,
        )
        data = {
            "forced": True,
            "reason": reason,
            "controller_id": activation.controller_id,
        }
        if cancelled_run_id is not None:
            data["cancelled_run_id"] = cancelled_run_id
        if cancelled_queue_ids:
            data["cancelled_queue_ids"] = list(cancelled_queue_ids)
        return SessionControlTransitionResult(
            action="release",
            changed=True,
            session_revision=controls.revision,
            released_activation_ids=[activation_id],
            output=(
                "Session controller Host gate was force-released; "
                "owner-private cleanup was not confirmed."
            ),
            data=data,
        )

    def reconcile_interrupted(self, session_id: str) -> bool:
        """Fail closed after a process stopped inside a lifecycle transition."""

        deactivating: list[SessionControlActivation] = []
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if not any(
                item.phase in {"activating", "deactivating"}
                for item in session.controls.activations
            ):
                return False
            values: list[SessionControlActivation] = []
            for activation in session.controls.activations:
                if activation.phase == "activating":
                    values.append(
                        activation.model_copy(
                            update={
                                "phase": "failed",
                                "revision": activation.revision + 1,
                                "updated_at": utc_now(),
                                "error": "activation interrupted before commit",
                            }
                        )
                    )
                elif activation.phase == "deactivating":
                    deactivating.append(activation)
                else:
                    values.append(activation)
            controls = _replace_control_state(session.controls, values)
            self._save_controls(session, controls)
        for activation in deactivating:
            self._cancel_owned_pending_interaction(
                session_id=session_id,
                controller_id=activation.controller_id,
                activation_id=activation.activation_id,
            )
            QueuedInputQueue(self.store).cancel_for_session_control(
                session_id=session_id,
                controller_id=activation.controller_id,
                activation_id=activation.activation_id,
            )
        return True

    async def apply(
        self,
        transition: AttributedSessionControlTransition,
        *,
        session_id: str,
        controllers: PluginSessionControllerRegistry,
        workspace: str | None = None,
        resolving_tool_call_id: str | None = None,
    ) -> SessionControlTransitionResult:
        if transition.action == "acquire":
            return await self._acquire(
                transition,
                session_id=session_id,
                controllers=controllers,
                workspace=workspace,
            )
        if transition.action == "release":
            return await self._release(
                transition,
                session_id=session_id,
                controllers=controllers,
                workspace=workspace,
                resolving_tool_call_id=resolving_tool_call_id,
            )
        return await self._replace(
            transition,
            session_id=session_id,
            controllers=controllers,
            workspace=workspace,
            resolving_tool_call_id=resolving_tool_call_id,
        )

    async def _acquire(
        self,
        transition: AttributedSessionControlTransition,
        *,
        session_id: str,
        controllers: PluginSessionControllerRegistry,
        workspace: str | None,
    ) -> SessionControlTransitionResult:
        assert transition.controller_id is not None
        canonical_id = f"{transition.plugin_id}/session_controllers/{transition.controller_id}"
        controller = self._controller_for_transition(
            controllers,
            canonical_id,
            transition,
        )
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            self._check_session_revision(session.controls, transition.expected_session_revision)
            existing = next(
                (
                    activation
                    for activation in session.controls.activations
                    if activation.controller_id == canonical_id and activation.phase != "failed"
                ),
                None,
            )
            if existing is not None:
                if existing.phase == "deactivating":
                    raise SessionControlConflictError(
                        f"session controller is being released: {canonical_id}"
                    )
                if existing.execution_contract_hash != transition.execution_contract_hash:
                    raise SessionControlConflictError(
                        "session controller is already active under another execution contract: "
                        f"{canonical_id}"
                    )
                return SessionControlTransitionResult(
                    action="acquire",
                    changed=False,
                    session_revision=session.controls.revision,
                    activation=existing,
                    output=f"Session controller is already active: {canonical_id}",
                    data=dict(existing.data),
                )
            conflicts = _claim_conflicts(controller.claims, session.controls.activations)
            if conflicts:
                details = ", ".join(f"{domain} held by {owner}" for domain, owner in conflicts)
                raise SessionControlConflictError(
                    f"session control claim conflict for {canonical_id}: {details}"
                )
            activation = self._new_activation(controller)
            preparing = _replace_control_state(
                session.controls,
                [*session.controls.activations, activation],
            )
            self._save_controls(session, preparing)

        try:
            output = await controller.activate(
                session_id=session_id,
                activation_id=activation.activation_id,
                payload=transition.payload,
                workspace=workspace,
            )
        except PluginSessionControllerError as exc:
            self._rollback_activation(session_id, activation.activation_id)
            raise SessionControlError(str(exc)) from exc

        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            current = _require_activation(session.controls, activation.activation_id)
            if current.phase != "activating":
                raise SessionControlConflictError(
                    "session controller activation changed before commit: "
                    f"{activation.activation_id}"
                )
            active = current.model_copy(
                update={
                    "phase": "active",
                    "revision": current.revision + 1,
                    "owner_state_revision": output.owner_state_revision,
                    "updated_at": utc_now(),
                    "data": dict(output.data),
                }
            )
            controls = _replace_activation(session.controls, active)
            self._save_controls(session, controls)
        return SessionControlTransitionResult(
            action="acquire",
            changed=True,
            session_revision=controls.revision,
            activation=active,
            output=output.output,
            data=dict(output.data),
        )

    async def _release(
        self,
        transition: AttributedSessionControlTransition,
        *,
        session_id: str,
        controllers: PluginSessionControllerRegistry,
        workspace: str | None,
        resolving_tool_call_id: str | None,
    ) -> SessionControlTransitionResult:
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            self._check_session_revision(session.controls, transition.expected_session_revision)
            activation = _select_release_activation(session.controls, transition)
            if activation is None:
                return SessionControlTransitionResult(
                    action="release",
                    changed=False,
                    session_revision=session.controls.revision,
                    output="Session controller is already inactive.",
                )
            if activation.plugin_id != transition.plugin_id:
                raise SessionControlConflictError(
                    "a plugin cannot release another plugin's Session controller: "
                    f"{activation.controller_id}"
                )
            if activation.phase == "activating":
                raise SessionControlConflictError(
                    "Session controller activation is still in progress: "
                    f"{activation.controller_id}"
                )
            if activation.phase == "deactivating":
                return SessionControlTransitionResult(
                    action="release",
                    changed=False,
                    session_revision=session.controls.revision,
                    activation=activation,
                    output="Session controller is already being released.",
                )
            if transition.expected_activation_revision is not None and (
                activation.revision != transition.expected_activation_revision
            ):
                raise SessionControlStaleRevisionError(
                    "session controller activation revision changed: "
                    f"expected {transition.expected_activation_revision}, "
                    f"found {activation.revision}"
                )
            try:
                controller = controllers.get(activation.controller_id)
            except KeyError as exc:
                raise SessionControlContractError(
                    f"session controller is unavailable: {activation.controller_id}"
                ) from exc
            self._validate_activation_controller(activation, controller)
            deactivating = activation.model_copy(
                update={
                    "phase": "deactivating",
                    "revision": activation.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            controls = _replace_activation(session.controls, deactivating)
            self._save_controls(session, controls)

        cleanup_error: PluginSessionControllerError | None = None
        output = PluginSessionControllerOutput()
        try:
            output = await controller.deactivate(
                session_id=session_id,
                activation_id=activation.activation_id,
                reason=transition.reason,
                workspace=workspace,
            )
        except PluginSessionControllerError as exc:
            cleanup_error = exc

        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            current = next(
                (
                    item
                    for item in session.controls.activations
                    if item.activation_id == activation.activation_id
                ),
                None,
            )
            if current is not None:
                controls = _replace_control_state(
                    session.controls,
                    [
                        item
                        for item in session.controls.activations
                        if item.activation_id != activation.activation_id
                    ],
                )
                self._save_controls(session, controls)
            else:
                controls = session.controls
        cancelled_run_id = self._cancel_owned_pending_interaction(
            session_id=session_id,
            controller_id=activation.controller_id,
            activation_id=activation.activation_id,
            exclude_tool_call_id=resolving_tool_call_id,
        )
        cancelled_queue_ids = QueuedInputQueue(self.store).cancel_for_session_control(
            session_id=session_id,
            controller_id=activation.controller_id,
            activation_id=activation.activation_id,
        )
        if cleanup_error is not None:
            raise SessionControlError(
                f"Session controller was released, but owner cleanup failed: {cleanup_error}"
            ) from cleanup_error
        data = dict(output.data)
        if cancelled_run_id is not None:
            data["cancelled_run_id"] = cancelled_run_id
        if cancelled_queue_ids:
            data["cancelled_queue_ids"] = list(cancelled_queue_ids)
        return SessionControlTransitionResult(
            action="release",
            changed=True,
            session_revision=controls.revision,
            released_activation_ids=[activation.activation_id],
            output=output.output,
            data=data,
        )

    async def _replace(
        self,
        transition: AttributedSessionControlTransition,
        *,
        session_id: str,
        controllers: PluginSessionControllerRegistry,
        workspace: str | None,
        resolving_tool_call_id: str | None,
    ) -> SessionControlTransitionResult:
        assert transition.controller_id is not None
        canonical_id = f"{transition.plugin_id}/session_controllers/{transition.controller_id}"
        controller = self._controller_for_transition(
            controllers,
            canonical_id,
            transition,
        )
        replaced_ids = set(transition.replace_activation_ids)
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            self._check_session_revision(session.controls, transition.expected_session_revision)
            targets = [
                activation
                for activation in session.controls.activations
                if activation.activation_id in replaced_ids
            ]
            if len(targets) != len(replaced_ids):
                found = {activation.activation_id for activation in targets}
                missing = ", ".join(sorted(replaced_ids - found))
                raise SessionControlConflictError(
                    f"replace target activation is unavailable: {missing}"
                )
            if any(target.plugin_id != transition.plugin_id for target in targets):
                raise SessionControlConflictError(
                    "a plugin cannot replace another plugin's Session controller"
                )
            if any(target.controller_id == canonical_id for target in targets):
                raise SessionControlConflictError(
                    "replace cannot reacquire the same Session controller; use release/acquire"
                )
            if any(target.phase != "active" for target in targets):
                raise SessionControlConflictError("replace targets must all be in the active phase")
            if transition.expected_activation_revision is not None:
                if len(targets) != 1:
                    raise SessionControlStaleRevisionError(
                        "expected_activation_revision requires exactly one replace target"
                    )
                if targets[0].revision != transition.expected_activation_revision:
                    raise SessionControlStaleRevisionError(
                        "session controller activation revision changed: "
                        f"expected {transition.expected_activation_revision}, "
                        f"found {targets[0].revision}"
                    )
            target_controllers: list[tuple[SessionControlActivation, PluginSessionController]] = []
            for target in targets:
                try:
                    target_controller = controllers.get(target.controller_id)
                except KeyError as exc:
                    raise SessionControlContractError(
                        f"replace target controller is unavailable: {target.controller_id}"
                    ) from exc
                self._validate_activation_controller(target, target_controller)
                target_controllers.append((target, target_controller))
            conflicts = _claim_conflicts(
                controller.claims,
                session.controls.activations,
                ignored_activation_ids=replaced_ids,
            )
            if conflicts:
                details = ", ".join(f"{domain} held by {owner}" for domain, owner in conflicts)
                raise SessionControlConflictError(
                    f"session control claim conflict for {canonical_id}: {details}"
                )
            activation = self._new_activation(controller)
            staged_targets = {
                target.activation_id: target.model_copy(
                    update={
                        "phase": "deactivating",
                        "revision": target.revision + 1,
                        "updated_at": utc_now(),
                    }
                )
                for target in targets
            }
            staged = _replace_control_state(
                session.controls,
                [
                    staged_targets.get(item.activation_id, item)
                    for item in session.controls.activations
                ]
                + [activation],
            )
            self._save_controls(session, staged)

        try:
            output = await controller.activate(
                session_id=session_id,
                activation_id=activation.activation_id,
                payload=transition.payload,
                workspace=workspace,
            )
        except PluginSessionControllerError as exc:
            self._rollback_replacement(
                session_id,
                activation.activation_id,
                targets,
            )
            raise SessionControlError(str(exc)) from exc

        cleanup_errors: list[str] = []
        for target, target_controller in target_controllers:
            try:
                await target_controller.deactivate(
                    session_id=session_id,
                    activation_id=target.activation_id,
                    reason=transition.reason or "replaced",
                    workspace=workspace,
                )
            except PluginSessionControllerError as exc:
                cleanup_errors.append(f"{target.controller_id}: {exc}")

        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            current = _require_activation(session.controls, activation.activation_id)
            if current.phase != "activating":
                raise SessionControlConflictError(
                    f"replacement activation changed before commit: {activation.activation_id}"
                )
            active = current.model_copy(
                update={
                    "phase": "active",
                    "revision": current.revision + 1,
                    "owner_state_revision": output.owner_state_revision,
                    "updated_at": utc_now(),
                    "data": dict(output.data),
                }
            )
            values = [
                item
                for item in session.controls.activations
                if item.activation_id not in replaced_ids
                and item.activation_id != activation.activation_id
            ]
            controls = _replace_control_state(session.controls, [*values, active])
            self._save_controls(session, controls)
        data = dict(output.data)
        if cleanup_errors:
            data["cleanup_errors"] = cleanup_errors
        cancelled_run_ids = {
            run_id
            for target in targets
            if (
                run_id := self._cancel_owned_pending_interaction(
                    session_id=session_id,
                    controller_id=target.controller_id,
                    activation_id=target.activation_id,
                    exclude_tool_call_id=resolving_tool_call_id,
                )
            )
            is not None
        }
        if cancelled_run_ids:
            data["cancelled_run_ids"] = sorted(cancelled_run_ids)
        cancelled_queue_ids = [
            queue_id
            for target in targets
            for queue_id in QueuedInputQueue(self.store).cancel_for_session_control(
                session_id=session_id,
                controller_id=target.controller_id,
                activation_id=target.activation_id,
            )
        ]
        if cancelled_queue_ids:
            data["cancelled_queue_ids"] = sorted(cancelled_queue_ids)
        return SessionControlTransitionResult(
            action="replace",
            changed=True,
            session_revision=controls.revision,
            activation=active,
            released_activation_ids=sorted(replaced_ids),
            output=output.output,
            data=data,
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
            phase="activating",
        )

    def _rollback_activation(self, session_id: str, activation_id: str) -> None:
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if not any(
                item.activation_id == activation_id for item in session.controls.activations
            ):
                return
            controls = _replace_control_state(
                session.controls,
                [
                    item
                    for item in session.controls.activations
                    if item.activation_id != activation_id
                ],
            )
            self._save_controls(session, controls)

    def _rollback_replacement(
        self,
        session_id: str,
        activation_id: str,
        targets: list[SessionControlActivation],
    ) -> None:
        originals = {item.activation_id: item for item in targets}
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            values: list[SessionControlActivation] = []
            for item in session.controls.activations:
                if item.activation_id == activation_id:
                    continue
                values.append(originals.get(item.activation_id, item))
            controls = _replace_control_state(session.controls, values)
            self._save_controls(session, controls)

    def _save_controls(self, session, controls: SessionControlState) -> None:
        now = utc_now()
        self.store.save_session(
            session.model_copy(
                update={
                    "controls": controls,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
        )

    def _cancel_owned_pending_interaction(
        self,
        *,
        session_id: str,
        controller_id: str,
        activation_id: str,
        exclude_tool_call_id: str | None = None,
    ) -> str | None:
        """Abort a waiting Run whose pending tool is activation-bound to this controller."""

        cancelled_run_id: str | None = None
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if session.active_run_id is None:
                return None
            run = self.store.load_run(session_id, session.active_run_id)
            batch = run.active_tool_batch
            if run.status != "waiting_user" or batch is None:
                return None
            owned = [
                call
                for call in batch.calls
                if call.status == "waiting_user"
                and call.request.id != exclude_tool_call_id
                and call.invocation is not None
                and call.invocation.active_when_controller_id == controller_id
            ]
            if not owned:
                return None
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
                                "controller_id": controller_id,
                                "activation_id": activation_id,
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
                "controller_id": controller_id,
                "activation_id": activation_id,
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
                "controller_id": controller_id,
                "activation_id": activation_id,
            }
            aborted_run = run.model_copy(
                update={
                    "status": "aborted",
                    "ended_at": utc_now(),
                    "error": "session_control_released",
                    "pending_approval_id": None,
                    "active_tool_batch": aborted_batch,
                    "metadata": run_metadata,
                }
            )
            updated_session = session.model_copy(
                update={
                    "status": "idle",
                    "active_run_id": None,
                    "last_run_id": aborted_run.run_id,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                }
            )
            self.store.save_run(aborted_run)
            self.store.save_session(updated_session)
            cancelled_run_id = aborted_run.run_id
        self.store.refresh_catalog(session_id)
        return cancelled_run_id


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
            if activation.phase == "failed" or activation.activation_id in ignored:
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


def _replace_activation(
    controls: SessionControlState,
    activation: SessionControlActivation,
) -> SessionControlState:
    found = False
    values: list[SessionControlActivation] = []
    for current in controls.activations:
        if current.activation_id == activation.activation_id:
            values.append(activation)
            found = True
        else:
            values.append(current)
    if not found:
        raise SessionControlConflictError(
            f"session controller activation disappeared: {activation.activation_id}"
        )
    return _replace_control_state(controls, values)


def _require_activation(
    controls: SessionControlState,
    activation_id: str,
) -> SessionControlActivation:
    try:
        return next(item for item in controls.activations if item.activation_id == activation_id)
    except StopIteration as exc:
        raise SessionControlConflictError(
            f"session controller activation disappeared: {activation_id}"
        ) from exc


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
        (
            item
            for item in controls.activations
            if item.controller_id == canonical_id and item.phase != "failed"
        ),
        None,
    )


__all__ = [
    "SessionControlConflictError",
    "SessionControlContractError",
    "SessionControlCoordinator",
    "SessionControlError",
    "SessionControlStaleRevisionError",
]
