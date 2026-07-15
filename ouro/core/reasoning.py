from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ouro.core.base import OuroModel

ReasoningMode = Literal["default", "enabled", "disabled"]
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ReasoningActivation = Literal["unsupported", "always_on", "toggle"]
ReasoningOutput = Literal["none", "summary", "text"]
ReasoningContinuation = Literal["none", "text", "opaque"]
ReasoningVariant = Literal[
    "default",
    "enabled",
    "disabled",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]

REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


class ReasoningRequest(OuroModel):
    """Provider-neutral user intent for one model route."""

    mode: ReasoningMode = "default"
    effort: ReasoningEffort | None = None

    @model_validator(mode="after")
    def _validate_mode_and_effort(self) -> ReasoningRequest:
        if self.mode == "disabled" and self.effort is not None:
            raise ValueError("disabled reasoning cannot specify an effort")
        return self


class ReasoningCapability(OuroModel):
    """What an endpoint can control and preserve across model turns."""

    activation: ReasoningActivation = "unsupported"
    supported_efforts: tuple[ReasoningEffort, ...] = Field(default_factory=tuple)
    default: ReasoningRequest = Field(default_factory=ReasoningRequest)
    output: ReasoningOutput = "none"
    continuation: ReasoningContinuation = "none"

    @model_validator(mode="after")
    def _validate_capability(self) -> ReasoningCapability:
        if len(set(self.supported_efforts)) != len(self.supported_efforts):
            raise ValueError("supported reasoning efforts must not contain duplicates")
        if self.activation == "unsupported":
            if self.supported_efforts:
                raise ValueError("unsupported reasoning cannot declare effort controls")
            if self.default.mode != "default" or self.default.effort is not None:
                raise ValueError("unsupported reasoning cannot declare a non-default intent")
        if self.activation == "always_on" and self.default.mode == "disabled":
            raise ValueError("always-on reasoning cannot be disabled by default")
        if self.default.effort is not None and self.default.effort not in self.supported_efforts:
            raise ValueError("default reasoning effort must be supported by the endpoint")
        if self.continuation == "text" and self.output not in {"summary", "text"}:
            raise ValueError("text reasoning continuation requires summary or text output")
        return self


class ReasoningResolution(OuroModel):
    """Strictly resolved request used for one provider call."""

    requested: ReasoningRequest
    effective: ReasoningRequest
    variant: ReasoningVariant


def resolve_reasoning(
    capability: ReasoningCapability,
    request: ReasoningRequest | None = None,
) -> ReasoningResolution:
    """Resolve one request without silently approximating unsupported controls."""

    requested = request or ReasoningRequest()
    effective = requested
    if requested.mode == "default" and requested.effort is None:
        effective = capability.default
    elif requested.effort is not None and requested.mode == "default":
        effective = requested.model_copy(update={"mode": "enabled"})

    if capability.activation == "unsupported":
        if effective.mode != "default" or effective.effort is not None:
            raise ValueError("endpoint does not support reasoning controls")
    elif capability.activation == "always_on":
        if effective.mode == "disabled":
            raise ValueError("endpoint reasoning is always on and cannot be disabled")
        if effective.mode == "default":
            effective = effective.model_copy(update={"mode": "enabled"})

    if effective.effort is not None:
        if effective.effort not in capability.supported_efforts:
            raise ValueError(f"reasoning effort {effective.effort!r} is not supported by endpoint")
        if effective.mode != "enabled":
            effective = effective.model_copy(update={"mode": "enabled"})

    variant: ReasoningVariant = effective.effort or effective.mode
    return ReasoningResolution(
        requested=requested,
        effective=effective,
        variant=variant,
    )
