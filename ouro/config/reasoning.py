from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field, field_validator, model_validator

from ouro.core.base import Metadata, OuroModel
from ouro.core.reasoning import (
    REASONING_EFFORTS,
    ReasoningActivation,
    ReasoningCapability,
    ReasoningContinuation,
    ReasoningEffort,
    ReasoningOutput,
    ReasoningRequest,
    ReasoningVariant,
)

REASONING_PRESETS: tuple[str, ...] = (
    "none",
    "openai-effort",
    "reasoning-content-effort",
    "thinking-toggle",
    "enable-thinking-toggle",
    "anthropic-adaptive-effort",
)

_RESERVED_REQUEST_FIELDS = frozenset(
    {
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "model",
        "stream",
        "system",
        "temperature",
        "tools",
        "response_format",
    }
)


class ReasoningProfileConfig(OuroModel):
    """Static native payload fragments for each resolved reasoning variant."""

    name: str
    variants: dict[ReasoningVariant, Metadata] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reasoning profile name must not be empty")
        return normalized

    @model_validator(mode="after")
    def _protect_core_request_fields(self) -> ReasoningProfileConfig:
        for variant, fragment in self.variants.items():
            reserved = sorted(_RESERVED_REQUEST_FIELDS.intersection(fragment))
            if reserved:
                raise ValueError(
                    f"reasoning variant {variant!r} overrides reserved request fields: "
                    f"{', '.join(reserved)}"
                )
        return self

    @property
    def owned_fields(self) -> frozenset[str]:
        return frozenset(key for fragment in self.variants.values() for key in fragment)


class EndpointReasoningConfig(OuroModel):
    """Endpoint reasoning contract plus its deterministic native encoding profile."""

    capability: ReasoningCapability = Field(default_factory=ReasoningCapability)
    profile: ReasoningProfileConfig | None = None

    @model_validator(mode="after")
    def _validate_profile_coverage(self) -> EndpointReasoningConfig:
        variants = self.profile.variants if self.profile is not None else {}
        if self.capability.activation == "unsupported":
            if self.profile is not None:
                raise ValueError("unsupported reasoning cannot define a payload profile")
            return self
        if self.profile is None:
            raise ValueError("controllable reasoning requires a payload profile")

        required = {"enabled"}
        if self.capability.activation == "toggle":
            required.add("disabled")
        required.update(self.capability.supported_efforts)
        missing = sorted(required.difference(variants))
        if missing:
            raise ValueError(
                "reasoning profile is missing required variants: " + ", ".join(missing)
            )
        allowed = {
            "default",
            "enabled",
            *self.capability.supported_efforts,
        }
        if self.capability.activation == "toggle":
            allowed.add("disabled")
        unexpected = sorted(set(variants).difference(allowed))
        if unexpected:
            raise ValueError(
                "reasoning profile defines unsupported variants: " + ", ".join(unexpected)
            )
        return self

    def validate_request_options(self, request_options: Mapping[str, object]) -> None:
        if self.profile is None:
            return
        collisions = sorted(self.profile.owned_fields.intersection(request_options))
        if collisions:
            raise ValueError(
                "request_options conflict with reasoning-owned fields: " + ", ".join(collisions)
            )


def unsupported_reasoning() -> EndpointReasoningConfig:
    return EndpointReasoningConfig()


def mapped_reasoning(
    *,
    name: str,
    activation: ReasoningActivation,
    variants: Mapping[ReasoningVariant, Mapping[str, object]],
    supported_efforts: tuple[ReasoningEffort, ...] = (),
    default: ReasoningRequest | None = None,
    output: ReasoningOutput = "none",
    continuation: ReasoningContinuation = "none",
) -> EndpointReasoningConfig:
    """Build a validated profile for templates and compatible endpoint config."""

    capability = ReasoningCapability.model_validate(
        {
            "activation": activation,
            "supported_efforts": supported_efforts,
            "default": default or ReasoningRequest(),
            "output": output,
            "continuation": continuation,
        }
    )
    profile = ReasoningProfileConfig.model_validate(
        {
            "name": name,
            "variants": {key: dict(value) for key, value in variants.items()},
        }
    )
    return EndpointReasoningConfig(capability=capability, profile=profile)


def openai_effort_reasoning(
    *,
    supported_efforts: tuple[ReasoningEffort, ...] = REASONING_EFFORTS,
    default_effort: ReasoningEffort = "medium",
    activation: ReasoningActivation = "toggle",
    output: ReasoningOutput = "none",
    continuation: ReasoningContinuation = "none",
) -> EndpointReasoningConfig:
    variants: dict[ReasoningVariant, Mapping[str, object]] = {
        # "enabled" must be a concrete native instruction.  Leaving the
        # payload empty would merely inherit the provider default, which can
        # be "none" and therefore would not honor the canonical intent.
        "enabled": {"reasoning_effort": default_effort},
        **{effort: {"reasoning_effort": effort} for effort in supported_efforts},
    }
    if activation == "toggle":
        variants["disabled"] = {"reasoning_effort": "none"}
    return mapped_reasoning(
        name="openai-effort",
        activation=activation,
        supported_efforts=supported_efforts,
        default=ReasoningRequest(mode="enabled", effort=default_effort),
        output=output,
        continuation=continuation,
        variants=variants,
    )


def toggle_reasoning(
    *,
    name: str,
    enabled: Mapping[str, object],
    disabled: Mapping[str, object],
    default_enabled: bool = True,
    output: ReasoningOutput = "text",
    continuation: ReasoningContinuation = "text",
) -> EndpointReasoningConfig:
    return mapped_reasoning(
        name=name,
        activation="toggle",
        default=ReasoningRequest(mode="enabled" if default_enabled else "disabled"),
        output=output,
        continuation=continuation,
        variants={"enabled": enabled, "disabled": disabled},
    )


def reasoning_preset(name: str) -> EndpointReasoningConfig:
    """Resolve a common native contract for CLI-created compatible endpoints."""

    normalized = name.strip().casefold()
    if normalized == "none":
        return unsupported_reasoning()
    if normalized == "openai-effort":
        return openai_effort_reasoning()
    if normalized == "reasoning-content-effort":
        return openai_effort_reasoning(output="text", continuation="text")
    if normalized == "thinking-toggle":
        return toggle_reasoning(
            name="thinking-object-toggle",
            enabled={"thinking": {"type": "enabled"}},
            disabled={"thinking": {"type": "disabled"}},
        )
    if normalized == "enable-thinking-toggle":
        return toggle_reasoning(
            name="enable-thinking-toggle",
            enabled={"enable_thinking": True},
            disabled={"enable_thinking": False},
        )
    if normalized == "anthropic-adaptive-effort":
        efforts: tuple[ReasoningEffort, ...] = ("low", "medium", "high", "xhigh", "max")
        return mapped_reasoning(
            name="anthropic-adaptive-effort",
            activation="toggle",
            supported_efforts=efforts,
            default=ReasoningRequest(mode="enabled", effort="high"),
            output="summary",
            continuation="opaque",
            variants={
                "disabled": {"thinking": {"type": "disabled"}},
                "enabled": {"thinking": {"type": "adaptive"}},
                **{
                    effort: {
                        "thinking": {"type": "adaptive"},
                        "output_config": {"effort": effort},
                    }
                    for effort in efforts
                },
            },
        )
    available = ", ".join(REASONING_PRESETS)
    raise ValueError(f"unknown reasoning preset: {name}; available: {available}")
