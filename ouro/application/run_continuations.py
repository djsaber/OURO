from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ouro.application.harness import capture_session_harness
from ouro.application.queued_inputs import QueuedInput, QueuedInputQueue
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.usage import UsageService
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.continuations import (
    AttributedContinuationDecision,
    ContinuationFailure,
    RunContinuationRegistry,
)
from ouro.core.harness_calls import accept_state_changes
from ouro.core.messages import MessageRecord
from ouro.core.redaction import redact_text
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlTransitionRequest,
)
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunContinuationSettlementPlan,
    RunOutcome,
    RunRecord,
    SessionRecord,
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
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.usage_service = usage_service
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
        try:
            return await self._settle(session_id=session_id, run_id=run_id, final_text=final_text)
        except asyncio.CancelledError:
            accepted = await run_blocking(self._accepted_settlement, session_id, run_id)
            if accepted is None:
                raise
            return accepted

    def _accepted_settlement(
        self, session_id: str, run_id: str
    ) -> RunContinuationSettlement | None:
        with self.store.session_mutation(session_id):
            run = self.store.load_run(session_id, run_id)
            checkpoint = run.continuation
            if checkpoint is None or checkpoint.state != "resolved":
                return None
            committed = self.store.load_commit(session_id, checkpoint.decision_id)
            if committed is None:
                return None
            result = committed.output.get("continuation")
            assert isinstance(result, dict)
            diagnostic = result.get("diagnostic")
            return RunContinuationSettlement(
                run=run,
                settled=True,
                queued=result.get("queued") is True,
                diagnostics=(diagnostic,) if isinstance(diagnostic, str) else (),
            )

    async def _settle(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str = "",
    ) -> RunContinuationSettlement:
        run = await run_blocking(self.store.load_run, session_id, run_id)
        checkpoint = run.continuation
        if (
            run.kind != "agent"
            or checkpoint is None
            or checkpoint.state == "resolved"
            or run.status
            in {"queued", "running", "waiting_user", "waiting_approval", "waiting_child"}
        ):
            return RunContinuationSettlement(run=run)

        withdrawn = await run_blocking(self.settle_withdrawn, run)
        if withdrawn is not None:
            return withdrawn
        if checkpoint.settlement_plan is not None:
            return await self._settle_plan(run, checkpoint.settlement_plan)
        before, draft = await run_blocking(
            capture_session_harness,
            self.store,
            session_id,
            run=run,
            usage_service=self.usage_service,
        )
        messages = await run_blocking(self.store.load_run_messages, session_id, run_id)
        # Only consumed inputs determine origin; in-run policy feedback is not a new input.
        inputs = [message for message in messages if "session_input_mode" in message.metadata]
        # Driver calls have canonical results without an assistant tool-call message.
        tool_call_ids = {
            (message.turn_id, call.id) for message in messages for call in message.tool_calls
        }
        tool_call_ids.update(
            (message.turn_id, message.tool_result.tool_call_id)
            for message in messages
            if message.tool_result
        )
        outcome = RunOutcome(
            harness=draft,
            session_id=run.session_id,
            run_id=run.run_id,
            status=run.status,
            origin=run.origin,
            final_text=final_text or _last_assistant_text(messages, run.run_id),
            error=run.error,
            usage=dict(run.usage),
            tool_call_count=len(tool_call_ids),
            automatic_continuation=(
                all(
                    message.metadata.get("continuation_decision_id") is not None
                    for message in inputs
                )
                if inputs
                else None
            ),
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
        evaluation = await self.registry.evaluate(
            outcome,
            controller_ids=controller_ids,
        )
        failures = tuple(evaluation.failures)
        if failures:
            return await run_blocking(
                self._settle_failures,
                run,
                failures=failures,
                reason="continuation_policy_failed",
                diagnostic="one or more continuation policies failed",
            )
        if len(evaluation.decisions) > 1:
            resolved = await run_blocking(
                self._resolve,
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
                bound = await run_blocking(
                    self._current_bound_controllers,
                    run.session_id,
                    checkpoint,
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
                    return await run_blocking(
                        self._settle_failures,
                        run,
                        failures=missing,
                        reason="continuation_terminal_decision_missing",
                        diagnostic=(
                            "activation-bound continuation policy did not settle an "
                            "abnormal terminal Run"
                        ),
                    )
                resolved = await run_blocking(
                    self._resolve,
                    run,
                    action="pause",
                    reason=f"terminal_run_{run.status}",
                )
                return RunContinuationSettlement(run=resolved, settled=True)
            resolved = await run_blocking(
                self._resolve,
                run,
                action="complete",
                reason="no_continuation_requested",
            )
            return RunContinuationSettlement(run=resolved, settled=True)

        attributed = evaluation.decisions[0]
        try:
            plan = self._plan_for_decision(run, checkpoint, attributed)
            draft = accept_state_changes(
                draft,
                [attributed.decision.harness_change] if attributed.decision.harness_change else [],
            )
            released = []
            for transition in plan.session_control_transitions:
                if self.session_controllers is None or attributed.generation_id is None:
                    raise ValueError("continuation controller execution contract is unavailable")
                draft, result = await self.session_controls.stage(
                    AttributedSessionControlTransition(
                        **transition.model_dump(mode="python"),
                        plugin_id=plan.plugin_id,
                        generation_id=attributed.generation_id,
                        execution_contract_hash=plan.execution_contract_hash,
                    ),
                    session=before,
                    draft=draft,
                    controllers=self.session_controllers,
                    workspace=before.workspace,
                )
                released.extend(result.released_activation_ids)
            plan = plan.model_copy(
                update={"harness": draft, "released_activation_ids": tuple(released)}
            )
        except Exception as exc:  # noqa: BLE001 - discard an invalid extension decision
            failure = self._failure(
                run,
                attributed.plugin_id,
                attributed.policy_id,
                type(exc).__name__,
                redact_text(str(exc)),
            )
            return await run_blocking(
                self._settle_failures,
                run,
                failures=(failure,),
                reason="continuation_decision_invalid",
                diagnostic="continuation policy returned an invalid terminal decision",
            )

        staged = await run_blocking(self._stage_plan, run, plan, before=before)
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

    def settle_withdrawn(self, run: RunRecord) -> RunContinuationSettlement | None:
        """Cancel an obsolete execution permit without loading its plugin generation."""
        with self.store.session_mutation(run.session_id):
            session = self.store.load_session_record(run.session_id)
            if run.history_epoch == session.history_epoch:
                return None
            latest = self.store.load_run(run.session_id, run.run_id)
            if latest.continuation is None or latest.continuation.state == "resolved":
                return RunContinuationSettlement(run=latest)
            resolved = self._resolve_locked(
                latest, action="pause", reason="session_history_withdrawn"
            )
            return RunContinuationSettlement(run=resolved, settled=True)

    def _stage_plan(
        self,
        run: RunRecord,
        plan: RunContinuationSettlementPlan,
        *,
        before: SessionRecord,
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
            self.store.commit_session(SessionCommit(before=before, runs=(staged,)))
            return staged

    async def _settle_plan(
        self,
        run: RunRecord,
        plan: RunContinuationSettlementPlan,
    ) -> RunContinuationSettlement:
        return await run_blocking(self._commit_plan, run, plan)

    def _commit_plan(
        self, run: RunRecord, plan: RunContinuationSettlementPlan
    ) -> RunContinuationSettlement:
        """Materialize a prepared decision without invoking any extension callback."""
        with self.store.session_mutation(run.session_id):
            before = self.store.load_session_record(run.session_id)
            latest = self.store.load_run(run.session_id, run.run_id)
            checkpoint = latest.continuation
            if checkpoint is None or checkpoint.state == "resolved":
                return RunContinuationSettlement(run=latest)
            draft = plan.harness
            # A plan without a proven state boundary cannot authorize execution.
            if draft is None:
                resolved = self._resolve_locked(
                    latest, action="pause", reason="continuation_state_unavailable"
                )
                return RunContinuationSettlement(run=resolved, settled=True)
            if (
                before.history_epoch != draft.history_epoch
                or before.harness_version != draft.harness_version
                or before.last_message_id != draft.last_message_id
            ):
                resolved = self._resolve_locked(
                    latest, action="pause", reason="continuation_state_changed"
                )
                return RunContinuationSettlement(run=resolved, settled=True)

            controller_ref = self._controller_ref_for_plan(checkpoint, plan)
            controller_id = f"{plan.plugin_id}/continuation_policies/{plan.policy_id}"
            after = before
            action, reason = plan.action, plan.reason
            diagnostic = None
            queued = False
            if action == "continue":
                if controller_ref.activation_id is not None and not any(
                    item.activation_id == controller_ref.activation_id
                    and item.controller_id == controller_ref.active_when_controller_id
                    for item in before.controls.activations
                ):
                    action, reason = "pause", "session_control_released"
                    diagnostic = "Session controller was released before continuation enqueue"
                elif before.active_run_id is not None and before.active_run_id != run.run_id:
                    action, reason = "pause", "superseded_by_active_run"
                    diagnostic = "a newer run superseded automatic continuation"
                elif self.queue.eligible_session(before):
                    action, reason = "pause", "superseded_by_queued_input"
                    diagnostic = "queued session input took precedence over automatic continuation"
                else:
                    assert plan.prompt is not None
                    item = QueuedInput(
                        queue_id=checkpoint.queue_id,
                        queued_at=utc_now(),
                        history_epoch=before.history_epoch,
                        prompt=plan.prompt,
                        mode=plan.mode,
                        title=plan.title,
                        role=plan.role,
                        scaffold=plan.scaffold,
                        semantic_tag=plan.semantic_tag,
                        coalesce_key=plan.coalesce_key,
                        batch_id=checkpoint.decision_id,
                        batch_index=0,
                        batch_size=1,
                        source="plugin",
                        plugin_id=plan.plugin_id,
                        contribution_id=plan.policy_id,
                        generation_id=controller_ref.generation_id,
                        execution_contract_hash=plan.execution_contract_hash,
                        caused_by_run_id=run.run_id,
                        continuation_decision_id=checkpoint.decision_id,
                        session_control_controller_id=controller_ref.active_when_controller_id,
                        session_control_activation_id=controller_ref.activation_id,
                    )
                    after, _ = self.queue.stage_batch(before, (item,))
                    queued = True
            resolved = _resolved_run(
                latest,
                action=action,
                controller_id=controller_id,
                reason=reason,
            )
            self.session_controls.commit(
                SessionCommit(
                    operation_id=checkpoint.decision_id,
                    before=before,
                    after=after,
                    steps=(SessionCommitStep(harness=draft.state),)
                    if action == plan.action
                    else (),
                    runs=(resolved,),
                    output={"continuation": {"queued": queued, "diagnostic": diagnostic}},
                ),
                released_activation_ids=plan.released_activation_ids
                if action == plan.action
                else (),
            )
            return RunContinuationSettlement(
                run=resolved,
                settled=True,
                queued=queued,
                diagnostics=(diagnostic,) if diagnostic else (),
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
        with self.store.session_mutation(run.session_id):
            before = self.store.load_session_record(run.session_id)
            latest = self.store.load_run(run.session_id, run.run_id)
            checkpoint = latest.continuation
            assert checkpoint is not None
            if checkpoint.state == "resolved":
                return RunContinuationSettlement(run=latest)
            resolved = _resolved_run(latest, action="pause", reason=reason)
            self.store.commit_session(
                SessionCommit(
                    operation_id=checkpoint.decision_id,
                    before=before,
                    runs=(resolved,),
                    output={"continuation": {"queued": False, "diagnostic": diagnostic}},
                )
            )
            return RunContinuationSettlement(
                run=resolved,
                settled=True,
                failures=failures,
                diagnostics=(
                    diagnostic,
                    "Session controls remain active; explicit release is required.",
                ),
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

    def _current_bound_controllers(
        self,
        session_id: str,
        checkpoint: RunContinuationCheckpoint,
    ) -> tuple[RunContinuationControllerRef, ...]:
        session = self.store.load_session_record(session_id)
        current = {
            (item.controller_id, item.activation_id) for item in session.controls.activations
        }
        return tuple(
            controller
            for controller in checkpoint.controllers
            if (controller.active_when_controller_id, controller.activation_id) in current
        )

    def _resolve(
        self,
        run: RunRecord,
        *,
        action: str,
        controller_id: str | None = None,
        reason: str | None = None,
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
            )

    def _resolve_locked(
        self,
        run: RunRecord,
        *,
        action: str,
        controller_id: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        resolved = _resolved_run(
            run,
            action=action,
            controller_id=controller_id,
            reason=reason,
        )
        assert run.continuation is not None
        self.store.commit_session(
            SessionCommit(
                operation_id=run.continuation.decision_id,
                before=self.store.load_session_record(run.session_id),
                runs=(resolved,),
                output={"continuation": {"queued": False, "diagnostic": None}},
            )
        )
        return resolved


def _resolved_run(
    run: RunRecord,
    *,
    action: str,
    controller_id: str | None = None,
    reason: str | None = None,
) -> RunRecord:
    checkpoint = run.continuation
    assert checkpoint is not None
    return run.model_copy(
        update={
            "continuation": checkpoint.model_copy(
                update={
                    "state": "resolved",
                    "action": action,
                    "selected_controller_id": controller_id,
                    "reason": reason,
                    "resolved_at": utc_now(),
                }
            )
        }
    )


def _last_assistant_text(messages: list[MessageRecord], run_id: str) -> str:
    for message in reversed(messages):
        if message.run_id == run_id and message.role == "assistant":
            return message.text_content
    return ""


__all__ = ["RunContinuationService", "RunContinuationSettlement"]
