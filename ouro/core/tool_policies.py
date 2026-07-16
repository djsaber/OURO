from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.redaction import redact_text
from ouro.core.tools import ToolSource

ToolPolicyAction = Literal["ignore", "require_approval", "deny"]
ToolPolicyMode = Literal["approval", "deny"]


class ToolPolicyCall(OuroModel):
    """Bounded preflight view evaluated before the Host executes a tool."""

    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    source: ToolSource = "builtin"
    workspace: str | None = None


class ToolPolicyDecision(OuroModel):
    """A restrictive decision that can never lower the Host's own policy."""

    action: ToolPolicyAction = "ignore"
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_reason(self) -> ToolPolicyDecision:
        if self.action != "ignore" and self.reason is None:
            raise ValueError(f"{self.action} tool policy decision requires a reason")
        if self.action == "ignore" and self.reason is not None:
            raise ValueError("ignore tool policy decision cannot contain a reason")
        return self


ToolPolicyHandler = Callable[
    [ToolPolicyCall],
    ToolPolicyDecision | Awaitable[ToolPolicyDecision],
]


@dataclass(frozen=True)
class ToolPolicyRegistration:
    plugin_id: str
    policy_id: str
    handler: ToolPolicyHandler
    priority: int = 100
    timeout_seconds: float = 5.0
    owner: str | None = None
    generation_id: str | None = None


class ToolPolicyFailure(OuroModel):
    plugin_id: str
    policy_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    error_type: str
    message: str


class AttributedToolPolicyDecision(OuroModel):
    plugin_id: str
    policy_id: str
    generation_id: str | None = None
    decision: ToolPolicyDecision


@dataclass
class ToolPolicyEvaluation:
    mode: ToolPolicyMode | None = None
    reason: str | None = None
    decisions: list[AttributedToolPolicyDecision] = field(default_factory=list)
    failures: list[ToolPolicyFailure] = field(default_factory=list)


class ToolPolicyRegistry:
    """Owner-aware registry for deterministic, restrictive tool preflight policies.

    Policies may require approval or deny a call, but cannot grant permission or
    rewrite arguments. A policy failure is fail-closed so a broken guard cannot
    silently widen the Host's security boundary.
    """

    def __init__(self) -> None:
        self._registrations: list[ToolPolicyRegistration] = []

    def replace_owner(
        self,
        owner: str,
        registrations: tuple[ToolPolicyRegistration, ...],
    ) -> None:
        identities = [(item.plugin_id, item.policy_id) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate tool policy identities")
        if any(item.timeout_seconds <= 0 for item in registrations):
            raise ValueError("tool policy timeout must be positive")
        retained = [item for item in self._registrations if item.owner != owner]
        self._registrations = [
            *retained,
            *(replace(item, owner=owner) for item in registrations),
        ]

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> list[ToolPolicyRegistration]:
        return list(self._registrations)

    def restore(self, snapshot: list[ToolPolicyRegistration]) -> None:
        self._registrations = list(snapshot)

    def registered(self) -> list[ToolPolicyRegistration]:
        return sorted(
            self._registrations,
            key=lambda item: (item.priority, item.plugin_id, item.policy_id, item.owner or ""),
        )

    async def evaluate(self, call: ToolPolicyCall) -> ToolPolicyEvaluation:
        evaluation = ToolPolicyEvaluation()
        for registration in self.registered():
            try:
                raw = await invoke_bounded_callback(
                    registration.handler,
                    call,
                    timeout_seconds=registration.timeout_seconds,
                )
                if not isinstance(raw, ToolPolicyDecision):
                    raise TypeError("tool policy must return ToolPolicyDecision")
                if raw.action == "ignore":
                    continue
                attributed = AttributedToolPolicyDecision(
                    plugin_id=registration.plugin_id,
                    policy_id=registration.policy_id,
                    generation_id=registration.generation_id,
                    decision=raw,
                )
                evaluation.decisions.append(attributed)
                if raw.action == "deny":
                    evaluation.mode = "deny"
                    evaluation.reason = raw.reason
                    return evaluation
                if evaluation.mode is None:
                    evaluation.mode = "approval"
                    evaluation.reason = raw.reason
            except TimeoutError:
                failure = ToolPolicyFailure(
                    plugin_id=registration.plugin_id,
                    policy_id=registration.policy_id,
                    session_id=call.session_id,
                    run_id=call.run_id,
                    tool_call_id=call.tool_call_id,
                    error_type="TimeoutError",
                    message=(
                        f"tool policy timed out after {registration.timeout_seconds:g} seconds"
                    ),
                )
                evaluation.failures.append(failure)
                evaluation.mode = "deny"
                evaluation.reason = "tool policy evaluation failed"
                return evaluation
            except Exception as exc:  # noqa: BLE001 - extension isolation boundary
                failure = ToolPolicyFailure(
                    plugin_id=registration.plugin_id,
                    policy_id=registration.policy_id,
                    session_id=call.session_id,
                    run_id=call.run_id,
                    tool_call_id=call.tool_call_id,
                    error_type=type(exc).__name__,
                    message=redact_text(str(exc)),
                )
                evaluation.failures.append(failure)
                evaluation.mode = "deny"
                evaluation.reason = "tool policy evaluation failed"
                return evaluation
        return evaluation


__all__ = [
    "AttributedToolPolicyDecision",
    "ToolPolicyAction",
    "ToolPolicyCall",
    "ToolPolicyDecision",
    "ToolPolicyEvaluation",
    "ToolPolicyFailure",
    "ToolPolicyMode",
    "ToolPolicyRegistration",
    "ToolPolicyRegistry",
]
