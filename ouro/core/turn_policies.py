from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.policy_registry import PolicyRegistry
from ouro.core.redaction import redact_text

TurnPolicyAction = Literal["allow", "continue"]


class TurnCompletionCandidate(OuroModel):
    harness: HarnessCallSnapshot | None = None
    session_id: str
    run_id: str
    turn_id: str
    assistant_text: str
    assistant_reasoning: str = ""


class TurnPolicyDecision(OuroModel):
    action: TurnPolicyAction = "allow"
    feedback: str | None = Field(default=None, max_length=2000)

    @field_validator("feedback")
    @classmethod
    def normalize_feedback(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_feedback(self) -> Self:
        if self.action == "continue" and self.feedback is None:
            raise ValueError("continue turn policy decision requires feedback")
        if self.action == "allow" and self.feedback is not None:
            raise ValueError("allow turn policy decision cannot contain feedback")
        return self


TurnPolicyHandler = Callable[
    [TurnCompletionCandidate],
    TurnPolicyDecision | Awaitable[TurnPolicyDecision],
]


@dataclass(frozen=True)
class TurnPolicyRegistration:
    plugin_id: str
    policy_id: str
    handler: TurnPolicyHandler
    priority: int = 100
    timeout_seconds: float = 5.0
    owner: str | None = None
    generation_id: str | None = None
    active_when_controller_id: str | None = None


class TurnPolicyFailure(OuroModel):
    plugin_id: str
    policy_id: str
    session_id: str
    run_id: str
    turn_id: str
    error_type: str
    message: str


class AttributedTurnPolicyDecision(OuroModel):
    plugin_id: str
    policy_id: str
    generation_id: str | None = None
    decision: TurnPolicyDecision


@dataclass
class TurnPolicyEvaluation:
    decisions: list[AttributedTurnPolicyDecision] = field(default_factory=list)
    failures: list[TurnPolicyFailure] = field(default_factory=list)

    @property
    def should_continue(self) -> bool:
        return bool(self.decisions)

    @property
    def feedback(self) -> str:
        return "\n\n".join(
            item.decision.feedback or ""
            for item in self.decisions
            if item.decision.action == "continue"
        )


class TurnPolicyRegistry(PolicyRegistry[TurnPolicyRegistration]):
    """Evaluate candidate model completion without owning the agent loop."""

    _policy_name = "turn policy"

    async def evaluate(self, candidate: TurnCompletionCandidate) -> TurnPolicyEvaluation:
        evaluation = TurnPolicyEvaluation()
        for registration in self.registered():
            try:
                result = await invoke_bounded_callback(
                    registration.handler,
                    candidate,
                    timeout_seconds=registration.timeout_seconds,
                )
                if not isinstance(result, TurnPolicyDecision):
                    raise TypeError("turn policy must return TurnPolicyDecision")
                if result.action == "continue":
                    evaluation.decisions.append(
                        AttributedTurnPolicyDecision(
                            plugin_id=registration.plugin_id,
                            policy_id=registration.policy_id,
                            generation_id=registration.generation_id,
                            decision=result,
                        )
                    )
            except TimeoutError:
                evaluation.failures.append(
                    TurnPolicyFailure(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        session_id=candidate.session_id,
                        run_id=candidate.run_id,
                        turn_id=candidate.turn_id,
                        error_type="TimeoutError",
                        message=(
                            f"turn policy timed out after {registration.timeout_seconds:g} seconds"
                        ),
                    )
                )
                return evaluation
            except Exception as exc:  # noqa: BLE001 - extension isolation boundary.
                evaluation.failures.append(
                    TurnPolicyFailure(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        session_id=candidate.session_id,
                        run_id=candidate.run_id,
                        turn_id=candidate.turn_id,
                        error_type=type(exc).__name__,
                        message=redact_text(str(exc)),
                    )
                )
                return evaluation
        return evaluation


__all__ = [
    "AttributedTurnPolicyDecision",
    "TurnCompletionCandidate",
    "TurnPolicyAction",
    "TurnPolicyDecision",
    "TurnPolicyEvaluation",
    "TurnPolicyFailure",
    "TurnPolicyRegistration",
    "TurnPolicyRegistry",
]
