from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ouro.core.base import OuroModel

ReasoningMode = Literal["default", "enabled", "disabled"]
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ReasoningSummary = Literal["auto", "concise", "detailed", "summarized", "omitted"]
ReasoningActivation = Literal["unsupported", "always_on", "toggle"]
ReasoningOutput = Literal["none", "summary", "text"]
ReasoningContinuation = Literal["none", "text", "opaque"]
ReasoningEffortVariant = Literal[
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
    """Independent reasoning preferences; effort only applies while thinking is enabled."""

    mode: ReasoningMode = "default"
    effort: ReasoningEffort | None = None
    # None inherits the endpoint preference; provider_default omits the native parameter.
    summary: ReasoningSummary | Literal["provider_default"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ReasoningCapability(OuroModel):
    """What an endpoint can control and preserve across model turns."""

    activation: ReasoningActivation = "unsupported"
    supported_efforts: tuple[ReasoningEffort, ...] = Field(default_factory=tuple)
    supported_summaries: tuple[ReasoningSummary, ...] = Field(default_factory=tuple)
    default: ReasoningRequest = Field(default_factory=ReasoningRequest)
    output: ReasoningOutput = "none"
    continuation: ReasoningContinuation = "none"

    @model_validator(mode="after")
    def _validate_capability(self) -> ReasoningCapability:
        if len(set(self.supported_summaries)) != len(self.supported_summaries):
            raise ValueError("supported reasoning summaries must not contain duplicates")
        if (
            self.default.summary not in {None, "provider_default"}
            and self.default.summary not in self.supported_summaries
        ):
            raise ValueError("default reasoning summary must be supported by the endpoint")
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
        if self.supported_efforts and self.default.effort is None:
            if len(self.supported_efforts) != 1:
                raise ValueError("reasoning with multiple efforts must declare a default effort")
            self.default = self.default.model_copy(update={"effort": self.supported_efforts[0]})
        return self


class ReasoningResolution(OuroModel):
    """Strictly resolved request used for one provider call."""

    requested: ReasoningRequest
    effective: ReasoningRequest
    effort_variant: ReasoningEffortVariant


def resolve_reasoning(
    capability: ReasoningCapability,
    request: ReasoningRequest | None = None,
) -> ReasoningResolution:
    """Resolve one request without silently approximating unsupported controls."""

    requested = request or ReasoningRequest()
    mode = requested.mode
    if mode == "default":
        mode = "enabled" if requested.effort is not None else capability.default.mode
    effort = requested.effort if requested.effort is not None else capability.default.effort

    summary = capability.default.summary if requested.summary is None else requested.summary
    if summary not in {None, "provider_default"} and summary not in capability.supported_summaries:
        raise ValueError(f"reasoning summary {summary!r} is not supported by endpoint")
    if capability.activation == "unsupported":
        if mode != "default" or effort is not None:
            raise ValueError("endpoint does not support reasoning controls")
    elif capability.activation == "always_on":
        if mode == "disabled":
            raise ValueError("endpoint reasoning is always on and cannot be disabled")
        mode = "enabled"

    if effort is not None and effort not in capability.supported_efforts:
        raise ValueError(f"reasoning effort {effort!r} is not supported by endpoint")

    effective = ReasoningRequest(
        mode=mode, effort=effort if mode == "enabled" else None, summary=summary
    )
    effort_variant: ReasoningEffortVariant = effective.effort or effective.mode
    return ReasoningResolution(
        requested=requested,
        effective=effective,
        effort_variant=effort_variant,
    )
