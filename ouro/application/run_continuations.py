from __future__ import annotations

from dataclasses import dataclass, field

from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.runs import maybe_active_run
from ouro.application.session_controls import SessionControlCoordinator
from ouro.core.base import utc_now
from ouro.core.continuations import (
    AttributedContinuationDecision,
    ContinuationFailure,
    ContinuationPolicyRegistration,
    RunContinuationRegistry,
)
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import MessageRecord
from ouro.core.redaction import redact_text
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlTransitionRequest,
    SessionInputMode,
    SessionInputRole,
)
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunContinuationSettlementPlan,
    RunOutcome,
    RunRecord,
)
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sessions import SessionStore


@dataclass(frozen=True)
class RunContinuationSettlement:
    """Result of interpreting one durable post-Run continuation checkpoint."""

    run: RunRecord
    settled: bool = False
    queued: bool = False
    failures: tuple[ContinuationFailure, ...] = ()
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


class RunContinuationService:
    """Settle extension continuation decisions at a durable Run boundary.

    Policies only decide. This service remains responsible for conflict handling,
    exact control attribution, staged side effects, idempotent queueing, provenance,
    and persistence. The durable plan and stable queue id make recovery safe across
    release, queue, and checkpoint boundaries.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        registry: RunContinuationRegistry,
        queue: QueuedInputQueue,
        session_controls: SessionControlCoordinator | None = None,
        session_controllers: PluginSessionControllerRegistry | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.queue = queue
        self.session_controls = session_controls or SessionControlCoordinator(store)
        self.session_controllers = session_controllers

    async def settle(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str = "",
    ) -> RunContinuationSettlement:
        run = self.store.load_run(session_id, run_id)
        checkpoint = run.continuation
        if (
            run.kind != "agent"
            or checkpoint is None
            or checkpoint.state == "resolved"
            or run.status
            in {"queued", "running", "waiting_user", "waiting_approval", "waiting_child"}
        ):
            return RunContinuationSettlement(run=run)

        outcome = RunOutcome(
            session_id=run.session_id,
            run_id=run.run_id,
            status=run.status,
            origin=run.origin,
            final_text=final_text
            or _last_assistant_text(
                self.store.load_runtime_session(session_id).messages,
                run.run_id,
            ),
            error=run.error,
            usage=dict(run.usage),
        )
        controller_ids = {
            (
                controller.plugin_id,
                controller.policy_id,
                controller.execution_contract_hash,
                controller.contract_hash,
            )
            for controller in checkpoint.controllers
        }
        if checkpoint.settlement_plan is not None:
            return await self._settle_plan(run, checkpoint.settlement_plan)

        evaluation = await self.registry.evaluate(
            outcome,
            controller_ids=controller_ids,
        )
        failures = tuple(evaluation.failures)
        if failures:
            return self._settle_failures(
                run,
                failures=failures,
                reason="continuation_policy_failed",
                diagnostic="one or more continuation policies failed",
            )
        if len(evaluation.decisions) > 1:
            resolved = self._resolve(
                run,
                action="pause",
                reason="continuation_policy_conflict",
            )
            return RunContinuationSettlement(
                run=resolved,
                settled=True,
                diagnostics=(
                    "multiple continuation policies requested control; "
                    "automatic continuation paused",
                ),
            )
        if not evaluation.decisions:
            if run.status != "completed":
                bound = tuple(
                    controller
                    for controller in checkpoint.controllers
                    if controller.activation_id is not None
                    and self._controller_is_current(run.session_id, controller)
                )
                if bound:
                    missing = tuple(
                        self._failure(
                            run,
                            controller.plugin_id,
                            controller.policy_id,
                            "ContinuationTerminalDecisionMissing",
                            (
                                "activation-bound continuation policy returned no decision "
                                f"for terminal Run status {run.status}"
                            ),
                        )
                        for controller in bound
                    )
                    return self._settle_failures(
                        run,
                        failures=missing,
                        reason="continuation_terminal_decision_missing",
                        diagnostic=(
                            "activation-bound continuation policy did not settle an "
                            "abnormal terminal Run"
                        ),
                    )
                resolved = self._resolve(
                    run,
                    action="pause",
                    reason=f"terminal_run_{run.status}",
                )
                return RunContinuationSettlement(run=resolved, settled=True)
            resolved = self._resolve(
                run,
                action="complete",
                reason="no_continuation_requested",
            )
            return RunContinuationSettlement(run=resolved, settled=True)

        attributed = evaluation.decisions[0]
        try:
            plan = self._plan_for_decision(run, checkpoint, attributed)
        except (TypeError, ValueError) as exc:
            failure = self._failure(
                run,
                attributed.plugin_id,
                attributed.policy_id,
                type(exc).__name__,
                redact_text(str(exc)),
            )
            return self._settle_failures(
                run,
                failures=(failure,),
                reason="continuation_decision_invalid",
                diagnostic="continuation policy returned an invalid terminal decision",
            )

        staged = self._stage_plan(run, plan)
        staged_checkpoint = staged.continuation
        if staged_checkpoint is None or staged_checkpoint.state == "resolved":
            return RunContinuationSettlement(run=staged)
        assert staged_checkpoint.settlement_plan is not None
        return await self._settle_plan(staged, staged_checkpoint.settlement_plan)

    def _plan_for_decision(
        self,
        run: RunRecord,
        checkpoint: RunContinuationCheckpoint,
        attributed: AttributedContinuationDecision,
    ) -> RunContinuationSettlementPlan:
        controller_ref = next(
            (
                controller
                for controller in checkpoint.controllers
                if controller.plugin_id == attributed.plugin_id
                and controller.policy_id == attributed.policy_id
                and controller.execution_contract_hash == attributed.execution_contract_hash
                and controller.contract_hash == attributed.contract_hash
            ),
            None,
        )
        if controller_ref is None:
            raise ValueError("continuation decision does not match its durable controller")

        decision = attributed.decision
        if decision.action == "ignore":
            raise ValueError("attributed continuation decision cannot be ignore")
        if run.status != "completed" and decision.action not in {"pause", "fail"}:
            raise ValueError(
                f"terminal Run status {run.status} only permits pause or fail decisions"
            )
        transitions = self._normalize_control_transitions(
            decision.session_control_transitions,
            controller_ref=controller_ref,
        )
        if (
            run.status != "completed"
            and controller_ref.activation_id is not None
            and not transitions
        ):
            raise ValueError(
                "activation-bound abnormal terminal decision must release its controller"
            )
        return RunContinuationSettlementPlan(
            plugin_id=attributed.plugin_id,
            policy_id=attributed.policy_id,
            execution_contract_hash=controller_ref.execution_contract_hash,
            contract_hash=controller_ref.contract_hash,
            action=decision.action,
            prompt=decision.prompt,
            mode=decision.mode,
            title=decision.title,
            role=decision.role,
            scaffold=decision.scaffold,
            semantic_tag=decision.semantic_tag,
            coalesce_key=decision.coalesce_key,
            reason=decision.reason,
            state_revision=decision.state_revision,
            session_control_transitions=transitions,
        )

    @staticmethod
    def _normalize_control_transitions(
        transitions: list[SessionControlTransitionRequest],
        *,
        controller_ref: RunContinuationControllerRef,
    ) -> list[SessionControlTransitionRequest]:
        if not transitions:
            return []
        if len(transitions) != 1:
            raise ValueError("continuation decision may update only its one bound activation")
        if controller_ref.active_when_controller_id is None or controller_ref.activation_id is None:
            raise ValueError("unbound continuation policy cannot emit Session control transitions")
        transition = transitions[0]
        if transition.action not in {"release", "replace"}:
            raise ValueError(
                "continuation policy may only release or replace its checkpoint-bound activation"
            )
        prefix = f"{controller_ref.plugin_id}/session_controllers/"
        if not controller_ref.active_when_controller_id.startswith(prefix):
            raise ValueError("bound Session controller does not belong to the policy plugin")
        local_controller_id = controller_ref.active_when_controller_id.removeprefix(prefix)
        if transition.controller_id not in {None, local_controller_id}:
            raise ValueError("continuation transition selected another Session controller")
        if transition.action == "release":
            if transition.activation_id not in {None, controller_ref.activation_id}:
                raise ValueError("continuation transition selected another controller activation")
            return [
                transition.model_copy(
                    update={
                        "controller_id": local_controller_id,
                        "activation_id": controller_ref.activation_id,
                    }
                )
            ]
        if transition.activation_id is not None or set(transition.replace_activation_ids) not in (
            set(),
            {controller_ref.activation_id},
        ):
            raise ValueError("continuation transition selected another controller activation")
        return [
            transition.model_copy(
                update={
                    "controller_id": local_controller_id,
                    "activation_id": None,
                    "replace_activation_ids": [controller_ref.activation_id],
                }
            )
        ]

    def _stage_plan(
        self,
        run: RunRecord,
        plan: RunContinuationSettlementPlan,
    ) -> RunRecord:
        with self.store.session_mutation(run.session_id):
            latest = self.store.load_run(run.session_id, run.run_id)
            checkpoint = latest.continuation
            if checkpoint is None or checkpoint.state == "resolved":
                return latest
            if checkpoint.settlement_plan is not None:
                return latest
            staged = latest.model_copy(
                update={"continuation": checkpoint.model_copy(update={"settlement_plan": plan})}
            )
            self.store.save_run(staged)
            return staged

    async def _settle_plan(
        self,
        run: RunRecord,
        plan: RunContinuationSettlementPlan,
    ) -> RunContinuationSettlement:
        checkpoint = run.continuation
        assert checkpoint is not None
        controller_ref = self._controller_ref_for_plan(checkpoint, plan)
        controller_id = f"{plan.plugin_id}/continuation_policies/{plan.policy_id}"

        if plan.session_control_transitions:
            registration = self._current_registration(plan)
            if (
                registration is None
                or registration.generation_id is None
                or self.session_controllers is None
            ):
                failure = self._failure(
                    run,
                    plan.plugin_id,
                    plan.policy_id,
                    "ContinuationPolicyUnavailable",
                    "staged continuation control transition owner is unavailable",
                )
                return self._settle_failures(
                    run,
                    failures=(failure,),
                    reason="continuation_policy_failed",
                    diagnostic="staged continuation owner was unavailable during replay",
                )
            try:
                session = self.store.load_session_record(run.session_id)
                for transition in plan.session_control_transitions:
                    await self.session_controls.apply(
                        AttributedSessionControlTransition(
                            **transition.model_dump(mode="python"),
                            plugin_id=plan.plugin_id,
                            generation_id=registration.generation_id,
                            execution_contract_hash=plan.execution_contract_hash,
                        ),
                        session_id=run.session_id,
                        controllers=self.session_controllers,
                        workspace=session.workspace,
                    )
            except Exception as exc:  # noqa: BLE001 - durable extension boundary
                failure = self._failure(
                    run,
                    plan.plugin_id,
                    plan.policy_id,
                    type(exc).__name__,
                    redact_text(str(exc)),
                )
                return self._settle_failures(
                    run,
                    failures=(failure,),
                    reason="continuation_control_transition_failed",
                    diagnostic="staged Session control transition failed",
                )

        if plan.action != "continue":
            resolved = self._resolve(
                run,
                action=plan.action,
                controller_id=controller_id,
                reason=plan.reason,
                state_revision=plan.state_revision,
            )
            return RunContinuationSettlement(run=resolved, settled=True)

        registration = self._current_registration(plan)
        if registration is None:
            failure = self._failure(
                run,
                plan.plugin_id,
                plan.policy_id,
                "ContinuationPolicyUnavailable",
                "staged continuation policy execution contract is unavailable",
            )
            return self._settle_failures(
                run,
                failures=(failure,),
                reason="continuation_policy_failed",
                diagnostic="staged continuation policy was unavailable during replay",
            )
        assert plan.prompt is not None
        queued, resolved, diagnostic = self._enqueue_continuation(
            run=run,
            checkpoint=checkpoint,
            plugin_id=plan.plugin_id,
            policy_id=plan.policy_id,
            generation_id=registration.generation_id,
            execution_contract_hash=registration.execution_contract_hash,
            prompt=plan.prompt,
            mode=plan.mode,
            title=plan.title,
            role=plan.role,
            scaffold=plan.scaffold,
            semantic_tag=plan.semantic_tag,
            coalesce_key=plan.coalesce_key,
            reason=plan.reason,
            state_revision=plan.state_revision,
            session_control_controller_id=controller_ref.active_when_controller_id,
            session_control_activation_id=controller_ref.activation_id,
        )
        return RunContinuationSettlement(
            run=resolved,
            settled=True,
            queued=queued,
            diagnostics=((diagnostic,) if diagnostic is not None else ()),
        )

    def _current_registration(
        self,
        plan: RunContinuationSettlementPlan,
    ) -> ContinuationPolicyRegistration | None:
        return next(
            (
                registration
                for registration in self.registry.registered()
                if registration.plugin_id == plan.plugin_id
                and registration.policy_id == plan.policy_id
                and registration.execution_contract_hash == plan.execution_contract_hash
                and registration.contract_hash == plan.contract_hash
            ),
            None,
        )

    @staticmethod
    def _controller_ref_for_plan(
        checkpoint: RunContinuationCheckpoint,
        plan: RunContinuationSettlementPlan,
    ) -> RunContinuationControllerRef:
        try:
            return next(
                controller
                for controller in checkpoint.controllers
                if controller.plugin_id == plan.plugin_id
                and controller.policy_id == plan.policy_id
                and controller.execution_contract_hash == plan.execution_contract_hash
                and controller.contract_hash == plan.contract_hash
            )
        except StopIteration as exc:
            raise ValueError("staged continuation plan has no durable controller") from exc

    def _settle_failures(
        self,
        run: RunRecord,
        *,
        failures: tuple[ContinuationFailure, ...],
        reason: str,
        diagnostic: str,
    ) -> RunContinuationSettlement:
        checkpoint = run.continuation
        assert checkpoint is not None
        failed_ids = {(failure.plugin_id, failure.policy_id) for failure in failures}
        diagnostics = [diagnostic]
        released: set[str] = set()
        for controller in checkpoint.controllers:
            if (
                (controller.plugin_id, controller.policy_id) not in failed_ids
                or controller.activation_id is None
                or controller.activation_id in released
                or not self._controller_is_current(run.session_id, controller)
            ):
                continue
            result = self.session_controls.force_release(
                session_id=run.session_id,
                activation_id=controller.activation_id,
                reason=reason,
            )
            released.add(controller.activation_id)
            if result.changed:
                diagnostics.append(
                    "fail-closed released exact activation "
                    f"{controller.activation_id}; owner-private cleanup was not confirmed"
                )
        resolved = self._resolve(run, action="pause", reason=reason)
        return RunContinuationSettlement(
            run=resolved,
            settled=True,
            failures=failures,
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _failure(
        run: RunRecord,
        plugin_id: str,
        policy_id: str,
        error_type: str,
        message: str,
    ) -> ContinuationFailure:
        return ContinuationFailure(
            plugin_id=plugin_id,
            policy_id=policy_id,
            session_id=run.session_id,
            run_id=run.run_id,
            error_type=error_type,
            message=message,
        )

    def _enqueue_continuation(
        self,
        *,
        run: RunRecord,
        checkpoint: RunContinuationCheckpoint,
        plugin_id: str,
        policy_id: str,
        generation_id: str | None,
        execution_contract_hash: str | None,
        prompt: str,
        mode: SessionInputMode,
        title: str | None,
        role: SessionInputRole,
        scaffold: bool,
        semantic_tag: SemanticTag | None,
        coalesce_key: str | None,
        reason: str | None,
        state_revision: int | None,
        session_control_controller_id: str | None,
        session_control_activation_id: str | None,
    ) -> tuple[bool, RunRecord, str | None]:
        if generation_id is None or execution_contract_hash is None:
            resolved = self._resolve(
                run,
                action="pause",
                reason="continuation_execution_contract_missing",
            )
            return False, resolved, "continuation policy had no execution contract attribution"

        with self.store.session_mutation(run.session_id):
            snapshot = self.store.load_runtime_session(run.session_id)
            latest = next(item for item in snapshot.runs if item.run_id == run.run_id)
            latest_checkpoint = latest.continuation
            if latest_checkpoint is None or latest_checkpoint.state == "resolved":
                return False, latest, None

            if session_control_activation_id is not None and not any(
                activation.activation_id == session_control_activation_id
                and activation.controller_id == session_control_controller_id
                and activation.phase == "active"
                for activation in snapshot.session.controls.activations
            ):
                return (
                    False,
                    self._resolve_locked(
                        latest,
                        action="pause",
                        controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                        reason="session_control_released",
                        state_revision=state_revision,
                    ),
                    "Session controller was released before continuation enqueue",
                )

            if _input_is_durable(snapshot.messages, checkpoint.queue_id):
                return (
                    False,
                    self._resolve_locked(
                        latest,
                        action="continue",
                        controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                        reason=reason or "continuation_input_already_durable",
                        state_revision=state_revision,
                    ),
                    None,
                )

            queued = self.queue.eligible(snapshot)
            if any(item.queue_id == checkpoint.queue_id for item in queued):
                return (
                    False,
                    self._resolve_locked(
                        latest,
                        action="continue",
                        controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                        reason=reason or "continuation_input_already_queued",
                        state_revision=state_revision,
                    ),
                    None,
                )

            active = maybe_active_run(snapshot.runs)
            if active is not None and active.run_id != latest.run_id:
                return (
                    False,
                    self._resolve_locked(
                        latest,
                        action="pause",
                        controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                        reason="superseded_by_active_run",
                        state_revision=state_revision,
                    ),
                    "a newer run superseded automatic continuation",
                )
            if queued:
                return (
                    False,
                    self._resolve_locked(
                        latest,
                        action="pause",
                        controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                        reason="superseded_by_queued_input",
                        state_revision=state_revision,
                    ),
                    "queued session input took precedence over automatic continuation",
                )

            self.queue.enqueue(
                snapshot=snapshot,
                queue_id=checkpoint.queue_id,
                prompt=prompt,
                mode=mode,
                title=title,
                role=role,
                scaffold=scaffold,
                semantic_tag=semantic_tag,
                coalesce_key=coalesce_key,
                source="plugin",
                plugin_id=plugin_id,
                contribution_id=policy_id,
                generation_id=generation_id,
                execution_contract_hash=execution_contract_hash,
                caused_by_run_id=run.run_id,
                continuation_decision_id=checkpoint.decision_id,
                session_control_controller_id=session_control_controller_id,
                session_control_activation_id=session_control_activation_id,
            )
            resolved = self._resolve_locked(
                latest,
                action="continue",
                controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                reason=reason,
                state_revision=state_revision,
            )
            return True, resolved, None

    def _controller_is_current(
        self,
        session_id: str,
        controller: RunContinuationControllerRef,
    ) -> bool:
        if controller.activation_id is None:
            return True
        session = self.store.load_session_record(session_id)
        return any(
            activation.activation_id == controller.activation_id
            and activation.controller_id == controller.active_when_controller_id
            and activation.phase == "active"
            for activation in session.controls.activations
        )

    def _resolve(
        self,
        run: RunRecord,
        *,
        action: str,
        controller_id: str | None = None,
        reason: str | None = None,
        state_revision: int | None = None,
    ) -> RunRecord:
        with self.store.session_mutation(run.session_id):
            latest = self.store.load_run(run.session_id, run.run_id)
            checkpoint = latest.continuation
            if checkpoint is None or checkpoint.state == "resolved":
                return latest
            return self._resolve_locked(
                latest,
                action=action,
                controller_id=controller_id,
                reason=reason,
                state_revision=state_revision,
            )

    def _resolve_locked(
        self,
        run: RunRecord,
        *,
        action: str,
        controller_id: str | None = None,
        reason: str | None = None,
        state_revision: int | None = None,
    ) -> RunRecord:
        checkpoint = run.continuation
        assert checkpoint is not None
        resolved_checkpoint = checkpoint.model_copy(
            update={
                "state": "resolved",
                "action": action,
                "selected_controller_id": controller_id,
                "reason": reason,
                "state_revision": state_revision,
                "resolved_at": utc_now(),
            }
        )
        resolved = run.model_copy(update={"continuation": resolved_checkpoint})
        self.store.save_run(resolved)
        return resolved


def _last_assistant_text(messages: list[MessageRecord], run_id: str) -> str:
    for message in reversed(messages):
        if message.run_id == run_id and message.role == "assistant":
            return message.text_content
    return ""


def _input_is_durable(messages: list[MessageRecord], queue_id: str) -> bool:
    return any(
        message.metadata.get("queued_input_id") == queue_id
        for message in messages
    )


__all__ = ["RunContinuationService", "RunContinuationSettlement"]
