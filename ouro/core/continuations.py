from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from pydantic import Field, model_validator

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.redaction import redact_text
from ouro.core.session_controls import SessionInputMode
from ouro.core.state import RunOutcome

ContinuationAction = Literal["ignore", "continue", "complete", "pause", "fail"]


class ContinuationDecision(OuroModel):
    """A policy decision interpreted by the application after a terminal Run."""

    action: ContinuationAction = "ignore"
    prompt: str | None = None
    mode: SessionInputMode = "follow_up"
    title: str | None = None
    reason: str | None = None
    state_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_action_payload(self) -> ContinuationDecision:
        if self.action == "continue":
            if self.prompt is None or not self.prompt.strip():
                raise ValueError("continue decision requires a non-empty prompt")
        elif self.prompt is not None:
            raise ValueError(f"{self.action} decision cannot contain a prompt")
        return self


ContinuationHandler = Callable[
    [RunOutcome],
    ContinuationDecision | Awaitable[ContinuationDecision],
]


@dataclass(frozen=True)
class ContinuationPolicyRegistration:
    plugin_id: str
    policy_id: str
    handler: ContinuationHandler
    priority: int = 100
    timeout_seconds: float = 10.0
    owner: str | None = None
    generation_id: str | None = None
    execution_contract_hash: str | None = None
    contract_hash: str | None = None
    active_when_controller_id: str | None = None


class ContinuationFailure(OuroModel):
    plugin_id: str
    policy_id: str
    session_id: str
    run_id: str
    error_type: str
    message: str


class AttributedContinuationDecision(OuroModel):
    plugin_id: str
    policy_id: str
    generation_id: str | None = None
    execution_contract_hash: str | None = None
    contract_hash: str | None = None
    decision: ContinuationDecision


@dataclass
class ContinuationEvaluation:
    decisions: list[AttributedContinuationDecision] = field(default_factory=list)
    failures: list[ContinuationFailure] = field(default_factory=list)


class RunContinuationRegistry:
    """Owner-aware registry for deterministic, post-Run continuation policies."""

    def __init__(self) -> None:
        self._registrations: list[ContinuationPolicyRegistration] = []
        self._frozen = False
        self._active_controller_ids: Callable[[], set[str]] | None = None

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("continuation registry structure is frozen")

    def replace_owner(
        self,
        owner: str,
        registrations: tuple[ContinuationPolicyRegistration, ...],
    ) -> None:
        self._require_mutable()
        identities = [(item.plugin_id, item.policy_id) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate continuation policy identities")
        if any(item.timeout_seconds <= 0 for item in registrations):
            raise ValueError("continuation policy timeout must be positive")
        retained = [item for item in self._registrations if item.owner != owner]
        self._registrations = [
            *retained,
            *(replace(item, owner=owner) for item in registrations),
        ]

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> list[ContinuationPolicyRegistration]:
        return list(self._registrations)

    def restore(self, snapshot: list[ContinuationPolicyRegistration]) -> None:
        self._require_mutable()
        self._registrations = list(snapshot)

    def registered(self) -> list[ContinuationPolicyRegistration]:
        active = (
            self._active_controller_ids()
            if self._active_controller_ids is not None
            else None
        )
        return sorted(
            (
                item
                for item in self._registrations
                if active is None
                or item.active_when_controller_id is None
                or item.active_when_controller_id in active
            ),
            key=lambda item: (item.priority, item.plugin_id, item.policy_id, item.owner or ""),
        )

    def scoped(
        self,
        active_controller_ids: Callable[[], set[str]],
    ) -> RunContinuationRegistry:
        scoped = RunContinuationRegistry()
        scoped.restore(self.snapshot())
        scoped._active_controller_ids = active_controller_ids
        scoped.freeze()
        return scoped

    async def evaluate(
        self,
        outcome: RunOutcome,
        *,
        controller_ids: set[tuple[str, str, str, str]] | None = None,
    ) -> ContinuationEvaluation:
        evaluation = ContinuationEvaluation()
        registrations = self.registered()
        available_ids = {
            (
                registration.plugin_id,
                registration.policy_id,
                registration.execution_contract_hash or "",
                registration.contract_hash or "",
            )
            for registration in registrations
        }
        if controller_ids is not None:
            for plugin_id, policy_id, execution_hash, contract_hash in sorted(
                controller_ids - available_ids
            ):
                evaluation.failures.append(
                    ContinuationFailure(
                        plugin_id=plugin_id,
                        policy_id=policy_id,
                        session_id=outcome.session_id,
                        run_id=outcome.run_id,
                        error_type="ContinuationPolicyUnavailable",
                        message=(
                            "required continuation policy execution contract is unavailable: "
                            f"{execution_hash}:{contract_hash}"
                        ),
                    )
                )
        for registration in registrations:
            identity = (
                registration.plugin_id,
                registration.policy_id,
                registration.execution_contract_hash or "",
                registration.contract_hash or "",
            )
            if controller_ids is not None and identity not in controller_ids:
                continue
            try:
                raw = await invoke_bounded_callback(
                    registration.handler,
                    outcome,
                    timeout_seconds=registration.timeout_seconds,
                )
                if not isinstance(raw, ContinuationDecision):
                    raise TypeError("continuation policy must return ContinuationDecision")
                if raw.action != "ignore":
                    evaluation.decisions.append(
                        AttributedContinuationDecision(
                            plugin_id=registration.plugin_id,
                            policy_id=registration.policy_id,
                            generation_id=registration.generation_id,
                            execution_contract_hash=registration.execution_contract_hash,
                            contract_hash=registration.contract_hash,
                            decision=raw,
                        )
                    )
            except TimeoutError:
                evaluation.failures.append(
                    ContinuationFailure(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        session_id=outcome.session_id,
                        run_id=outcome.run_id,
                        error_type="TimeoutError",
                        message=(
                            "continuation policy timed out after "
                            f"{registration.timeout_seconds:g} seconds"
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - extension isolation boundary
                evaluation.failures.append(
                    ContinuationFailure(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        session_id=outcome.session_id,
                        run_id=outcome.run_id,
                        error_type=type(exc).__name__,
                        message=redact_text(str(exc)),
                    )
                )
        return evaluation


__all__ = [
    "AttributedContinuationDecision",
    "ContinuationAction",
    "ContinuationDecision",
    "ContinuationEvaluation",
    "ContinuationFailure",
    "ContinuationPolicyRegistration",
    "RunContinuationRegistry",
]
