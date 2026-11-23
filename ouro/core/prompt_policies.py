from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.policy_registry import PolicyRegistry
from ouro.core.redaction import redact_text

PromptInputSource = Literal["user", "host", "plugin", "delegation"]
PromptPolicyAction = Literal["allow", "deny"]


class PromptAttachmentMetadata(OuroModel):
    resource_id: str | None
    media_type: str | None
    size: int | None = Field(ge=0)
    kind: Literal["upload", "reference"] = "upload"
    name: str | None = None
    path: str | None = None


class PromptSkillActivationMetadata(OuroModel):
    name: str = Field(min_length=1)
    source: Literal["user", "workspace", "plugin"]
    location: str = Field(min_length=1)


class PromptPolicyCall(OuroModel):
    """Bounded pre-persistence view of one prospective model input."""

    harness: HarnessCallSnapshot | None = None
    session_id: str
    prospective_run_id: str
    prompt: str = ""
    input_source: PromptInputSource = "user"
    workspace: str | None = None
    attachments: tuple[PromptAttachmentMetadata, ...] = ()
    skill_activation: PromptSkillActivationMetadata | None = None
    catalog_id: str | None = None
    execution_contracts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_input(self) -> PromptPolicyCall:
        if not self.prompt.strip() and self.skill_activation is None and not self.attachments:
            raise ValueError("prompt policy input requires a prompt or skill activation")
        return self


class PromptPolicyDecision(OuroModel):
    action: PromptPolicyAction = "allow"
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_reason(self) -> PromptPolicyDecision:
        if self.action == "deny" and self.reason is None:
            raise ValueError("deny prompt policy decision requires a reason")
        if self.action == "allow" and self.reason is not None:
            raise ValueError("allow prompt policy decision cannot contain a reason")
        return self


PromptPolicyHandler = Callable[
    [PromptPolicyCall],
    PromptPolicyDecision | Awaitable[PromptPolicyDecision],
]


@dataclass(frozen=True, slots=True)
class PromptPolicyRegistration:
    plugin_id: str
    policy_id: str
    handler: PromptPolicyHandler
    priority: int = 100
    timeout_seconds: float = 5.0
    owner: str | None = None
    generation_id: str | None = None
    active_when_controller_id: str | None = None


class PromptPolicyFailure(OuroModel):
    plugin_id: str
    policy_id: str
    session_id: str
    prospective_run_id: str
    error_type: str
    message: str


class AttributedPromptPolicyDecision(OuroModel):
    plugin_id: str
    policy_id: str
    generation_id: str | None = None
    decision: PromptPolicyDecision


@dataclass
class PromptPolicyEvaluation:
    allowed: bool = True
    reason: str | None = None
    rejected_by: AttributedPromptPolicyDecision | None = None
    decisions: list[AttributedPromptPolicyDecision] = field(default_factory=list)
    failures: list[PromptPolicyFailure] = field(default_factory=list)


class PromptPolicyRegistry(PolicyRegistry[PromptPolicyRegistration]):
    """Deterministic fail-closed guards for prompts entering canonical state."""

    _policy_name = "prompt policy"

    def register(self, registration: PromptPolicyRegistration) -> None:
        self.register_many((registration,))

    def register_many(self, registrations: tuple[PromptPolicyRegistration, ...]) -> None:
        self._require_mutable()
        if any(item.timeout_seconds <= 0 for item in registrations):
            raise ValueError("prompt policy timeout must be positive")
        self._registrations.extend(registrations)

    async def evaluate(self, call: PromptPolicyCall) -> PromptPolicyEvaluation:
        evaluation = PromptPolicyEvaluation()
        for registration in self.registered():
            try:
                raw = await invoke_bounded_callback(
                    registration.handler,
                    call,
                    timeout_seconds=registration.timeout_seconds,
                )
                if not isinstance(raw, PromptPolicyDecision):
                    raise TypeError("prompt policy must return PromptPolicyDecision")
                attributed = AttributedPromptPolicyDecision(
                    plugin_id=registration.plugin_id,
                    policy_id=registration.policy_id,
                    generation_id=registration.generation_id,
                    decision=raw,
                )
                evaluation.decisions.append(attributed)
                if raw.action == "deny":
                    evaluation.allowed = False
                    evaluation.reason = raw.reason
                    evaluation.rejected_by = attributed
                    return evaluation
            except TimeoutError:
                evaluation.allowed = False
                evaluation.reason = "prompt policy evaluation failed"
                evaluation.failures.append(
                    PromptPolicyFailure(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        session_id=call.session_id,
                        prospective_run_id=call.prospective_run_id,
                        error_type="TimeoutError",
                        message=(
                            "prompt policy timed out after "
                            f"{registration.timeout_seconds:g} seconds"
                        ),
                    )
                )
                return evaluation
            except Exception as exc:  # noqa: BLE001 - extension isolation boundary.
                evaluation.allowed = False
                evaluation.reason = "prompt policy evaluation failed"
                evaluation.failures.append(
                    PromptPolicyFailure(
                        plugin_id=registration.plugin_id,
                        policy_id=registration.policy_id,
                        session_id=call.session_id,
                        prospective_run_id=call.prospective_run_id,
                        error_type=type(exc).__name__,
                        message=redact_text(str(exc)),
                    )
                )
                return evaluation
        return evaluation


__all__ = [
    "AttributedPromptPolicyDecision",
    "PromptAttachmentMetadata",
    "PromptInputSource",
    "PromptPolicyAction",
    "PromptPolicyCall",
    "PromptPolicyDecision",
    "PromptPolicyEvaluation",
    "PromptPolicyFailure",
    "PromptPolicyHandler",
    "PromptPolicyRegistration",
    "PromptPolicyRegistry",
    "PromptSkillActivationMetadata",
]
