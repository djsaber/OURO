from __future__ import annotations

from dataclasses import dataclass, field

from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.runs import maybe_active_run
from ouro.core.base import utc_now
from ouro.core.continuations import ContinuationFailure, RunContinuationRegistry
from ouro.core.messages import MessageRecord
from ouro.core.session_controls import SessionInputMode
from ouro.core.state import RunContinuationCheckpoint, RunOutcome, RunRecord
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
    idempotent queueing, provenance, and persistence. A stable queue id stored on
    the Run makes recovery safe if the process stops between queueing and marking
    the checkpoint resolved.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        registry: RunContinuationRegistry,
        queue: QueuedInputQueue,
    ) -> None:
        self.store = store
        self.registry = registry
        self.queue = queue

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
            or run.status in {"queued", "running", "waiting_user", "waiting_approval"}
        ):
            return RunContinuationSettlement(run=run)

        if run.status != "completed":
            resolved = self._resolve(
                run,
                action="pause",
                reason=f"terminal_run_{run.status}",
            )
            return RunContinuationSettlement(run=resolved, settled=True)

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
        evaluation = await self.registry.evaluate(
            outcome,
            controller_ids=controller_ids,
        )
        failures = tuple(evaluation.failures)
        if failures:
            resolved = self._resolve(
                run,
                action="pause",
                reason="continuation_policy_failed",
            )
            return RunContinuationSettlement(
                run=resolved,
                settled=True,
                failures=failures,
                diagnostics=("one or more continuation policies failed",),
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
            resolved = self._resolve(
                run,
                action="complete",
                reason="no_continuation_requested",
            )
            return RunContinuationSettlement(run=resolved, settled=True)

        attributed = evaluation.decisions[0]
        decision = attributed.decision
        controller_id = f"{attributed.plugin_id}/continuation_policies/{attributed.policy_id}"
        if decision.action != "continue":
            resolved = self._resolve(
                run,
                action=decision.action,
                controller_id=controller_id,
                reason=decision.reason,
                state_revision=decision.state_revision,
            )
            return RunContinuationSettlement(run=resolved, settled=True)

        assert decision.prompt is not None
        queued, resolved, diagnostic = self._enqueue_continuation(
            run=run,
            checkpoint=checkpoint,
            plugin_id=attributed.plugin_id,
            policy_id=attributed.policy_id,
            generation_id=attributed.generation_id,
            execution_contract_hash=attributed.execution_contract_hash,
            prompt=decision.prompt,
            mode=decision.mode,
            title=decision.title,
            reason=decision.reason,
            state_revision=decision.state_revision,
        )
        return RunContinuationSettlement(
            run=resolved,
            settled=True,
            queued=queued,
            diagnostics=((diagnostic,) if diagnostic is not None else ()),
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
        reason: str | None,
        state_revision: int | None,
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
                source="plugin",
                plugin_id=plugin_id,
                contribution_id=policy_id,
                generation_id=generation_id,
                execution_contract_hash=execution_contract_hash,
                caused_by_run_id=run.run_id,
                continuation_decision_id=checkpoint.decision_id,
            )
            resolved = self._resolve_locked(
                latest,
                action="continue",
                controller_id=f"{plugin_id}/continuation_policies/{policy_id}",
                reason=reason,
                state_revision=state_revision,
            )
            return True, resolved, None

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
        or message.metadata.get("queued_follow_up_id") == queue_id
        for message in messages
    )


__all__ = ["RunContinuationService", "RunContinuationSettlement"]
