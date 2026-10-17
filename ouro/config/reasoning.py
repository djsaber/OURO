from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field, field_validator, model_validator

from ouro.core.base import Metadata, OuroModel
from ouro.core.errors import UnsupportedConfigVersionError
from ouro.core.reasoning import (
    REASONING_EFFORTS,
    ReasoningActivation,
    ReasoningCapability,
    ReasoningContinuation,
    ReasoningEffort,
    ReasoningEffortVariant,
    ReasoningOutput,
    ReasoningRequest,
    ReasoningSummary,
)

REASONING_EFFORT_PRESETS: tuple[str, ...] = (
    "none",
    "openai-chat-completions-effort",
    "openai-responses-effort",
    "reasoning-content-effort",
    "thinking-toggle",
    "enable-thinking-toggle",
    "anthropic-messages-adaptive-effort",
)

# Each summary preset lists supported modes in default preference order.
REASONING_SUMMARY_PRESETS: dict[str, tuple[ReasoningSummary, ...]] = {
    "none": (),
    "openai-responses-auto": ("auto",),
    "openai-responses": ("auto", "concise", "detailed"),
    "anthropic-messages": ("summarized", "omitted"),
}

_RESERVED_REQUEST_FIELDS = frozenset(
    {
        "input",
        "instructions",
        "max_output_tokens",
        "store",
        "include",
        "truncation",
        "previous_response_id",
        "conversation",
        "background",
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
    """Independent native payload mappings for effort and summary selections."""

    name: str
    effort: dict[ReasoningEffortVariant, Metadata] = Field(default_factory=dict)
    summary: dict[ReasoningSummary, Metadata] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reasoning profile name must not be empty")
        return normalized

    @model_validator(mode="after")
    def _protect_core_request_fields(self) -> ReasoningProfileConfig:
        for dimension, variants in (("effort", self.effort), ("summary", self.summary)):
            for variant, fragment in variants.items():
                reserved = sorted(_RESERVED_REQUEST_FIELDS.intersection(fragment))
                if reserved:
                    raise ValueError(
                        f"reasoning {dimension} variant {variant!r} "
                        "overrides reserved request fields: "
                        f"{', '.join(reserved)}"
                    )
        return self

    @property
    def owned_fields(self) -> frozenset[str]:
        return frozenset(
            key for fragment in (*self.effort.values(), *self.summary.values()) for key in fragment
        )


class EndpointReasoningConfig(OuroModel):
    """Endpoint reasoning contract plus its deterministic native encoding profile."""

    capability: ReasoningCapability = Field(default_factory=ReasoningCapability)
    profile: ReasoningProfileConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _require_current_format(cls, value: object) -> object:
        if isinstance(value, Mapping):
            capability, profile = value.get("capability"), value.get("profile")
            if (isinstance(capability, Mapping) and "default_effort" in capability) or (
                isinstance(profile, Mapping) and "variants" in profile
            ):
                raise UnsupportedConfigVersionError(
                    "unsupported reasoning configuration format; run ouro-upgrade"
                )
        return value

    @model_validator(mode="after")
    def _validate_profile_coverage(self) -> EndpointReasoningConfig:
        capability = self.capability
        if capability.activation == "unsupported" and not capability.supported_summaries:
            if self.profile is not None:
                raise ValueError("unsupported reasoning cannot define a payload profile")
            return self
        if self.profile is None:
            raise ValueError("controllable reasoning requires a payload profile")

        required: set[ReasoningEffortVariant] = set(capability.supported_efforts)
        if capability.activation != "unsupported":
            required.add("enabled")
        if capability.activation == "toggle":
            required.add("disabled")
        allowed = required | ({"default"} if required else set())
        summaries = set(capability.supported_summaries)
        for dimension, variants, needed, accepted in (
            ("effort", self.profile.effort, required, allowed),
            ("summary", self.profile.summary, summaries, summaries),
        ):
            missing = sorted(needed.difference(variants))
            if missing:
                raise ValueError(
                    f"reasoning {dimension} profile is missing required variants: "
                    + ", ".join(missing)
                )
            unexpected = sorted(set(variants).difference(accepted))
            if unexpected:
                raise ValueError(
                    f"reasoning {dimension} profile defines unsupported variants: "
                    + ", ".join(unexpected)
                )
        return self

    def validate_request_options(self, request_options: Mapping[str, object]) -> None:
        owned = self.profile.owned_fields if self.profile is not None else frozenset()
        collisions = sorted(owned.intersection(request_options))
        if collisions:
            raise ValueError(
                "request_options conflict with reasoning-owned fields: " + ", ".join(collisions)
            )

    def with_effort_preset(self, name: str) -> EndpointReasoningConfig:
        """Replace effort controls while preserving summary controls and preferences."""
        result = reasoning_effort_preset(name)
        result.capability.supported_summaries = self.capability.supported_summaries
        result.capability.default.summary = self.capability.default.summary
        if self.capability.supported_summaries:
            result.capability.output = self.capability.output
            if self.profile is not None:
                if result.profile is None:
                    result.profile = ReasoningProfileConfig(name=self.profile.name)
                result.profile.summary = self.profile.summary
        return EndpointReasoningConfig.model_validate(result.model_dump())

    def with_summary_preset(self, name: str) -> EndpointReasoningConfig:
        """Replace summary controls while preserving effort and compatible defaults."""
        preset = reasoning_summary_preset(name)
        result = self.model_copy(deep=True)
        summaries = preset.capability.supported_summaries
        result.capability.supported_summaries = summaries
        if result.profile is None:
            result.profile = preset.profile
        else:
            result.profile.summary = preset.profile.summary if preset.profile else {}
            if not result.profile.effort and not result.profile.summary:
                result.profile = None
        if not summaries:
            result.capability.default.summary = None
        else:
            if result.capability.default.summary not in ("provider_default", *summaries):
                result.capability.default.summary = preset.capability.default.summary
            result.capability.output = preset.capability.output
        return EndpointReasoningConfig.model_validate(result.model_dump())


def unsupported_reasoning() -> EndpointReasoningConfig:
    return EndpointReasoningConfig()


def mapped_reasoning(
    *,
    name: str,
    activation: ReasoningActivation,
    effort: Mapping[ReasoningEffortVariant, Mapping[str, object]],
    supported_efforts: tuple[ReasoningEffort, ...] = (),
    default: ReasoningRequest | None = None,
    supported_summaries: tuple[ReasoningSummary, ...] = (),
    summary: Mapping[ReasoningSummary, Mapping[str, object]] | None = None,
    output: ReasoningOutput = "none",
    continuation: ReasoningContinuation = "none",
) -> EndpointReasoningConfig:
    """Build a validated profile for templates and custom endpoint config."""

    capability = ReasoningCapability.model_validate(
        {
            "activation": activation,
            "supported_efforts": supported_efforts,
            "default": default or ReasoningRequest(),
            "supported_summaries": supported_summaries,
            "output": output,
            "continuation": continuation,
        }
    )
    profile = ReasoningProfileConfig.model_validate(
        {
            "name": name,
            "effort": {key: dict(value) for key, value in effort.items()},
            "summary": {key: dict(value) for key, value in (summary or {}).items()},
        }
    )
    return EndpointReasoningConfig(capability=capability, profile=profile)


def openai_chat_completions_effort_reasoning(
    *,
    supported_efforts: tuple[ReasoningEffort, ...] = REASONING_EFFORTS,
    default: ReasoningRequest | None = None,
    activation: ReasoningActivation = "toggle",
    output: ReasoningOutput = "none",
    continuation: ReasoningContinuation = "none",
) -> EndpointReasoningConfig:
    default = default or ReasoningRequest(mode="enabled")
    default = default.model_copy(update={"effort": default.effort or "medium"})
    effort_variants: dict[ReasoningEffortVariant, Mapping[str, object]] = {
        # "enabled" must be a concrete native instruction.  Leaving the
        # payload empty would merely inherit the provider default, which can
        # be "none" and therefore would not honor the canonical intent.
        "enabled": {"reasoning_effort": default.effort},
        **{effort: {"reasoning_effort": effort} for effort in supported_efforts},
    }
    if activation == "toggle":
        effort_variants["disabled"] = {"reasoning_effort": "none"}
    return mapped_reasoning(
        name="openai-chat-completions-effort",
        activation=activation,
        supported_efforts=supported_efforts,
        default=default,
        output=output,
        continuation=continuation,
        effort=effort_variants,
    )


def openai_responses_effort_reasoning(
    *,
    supported_efforts: tuple[ReasoningEffort, ...] = REASONING_EFFORTS,
    activation: ReasoningActivation = "toggle",
    output: ReasoningOutput = "none",
    supported_summaries: tuple[ReasoningSummary, ...] = (),
    default: ReasoningRequest | None = None,
) -> EndpointReasoningConfig:
    """Declare native Responses effort controls and preserve opaque reasoning."""
    default = default or ReasoningRequest()
    default = default.model_copy(update={"effort": default.effort or "medium"})
    effort_variants: dict[ReasoningEffortVariant, Mapping[str, object]] = {
        "enabled": {"reasoning": {"effort": default.effort}},
        **{effort: {"reasoning": {"effort": effort}} for effort in supported_efforts},
    }
    if activation == "toggle":
        effort_variants["disabled"] = {"reasoning": {"effort": "none"}}
    return mapped_reasoning(
        name="openai-responses-effort",
        activation=activation,
        supported_efforts=supported_efforts,
        output=output,
        supported_summaries=supported_summaries,
        summary={value: {"reasoning": {"summary": value}} for value in supported_summaries},
        default=default,
        continuation="opaque",
        effort=effort_variants,
    )


def reasoning_summary_preset(name: str) -> EndpointReasoningConfig:
    """Declare a standard summary capability and its native request mapping."""
    if name not in REASONING_SUMMARY_PRESETS:
        raise ValueError(f"unknown reasoning summary preset: {name}")
    summaries = REASONING_SUMMARY_PRESETS[name]
    if not summaries:
        return unsupported_reasoning()
    parent, field = (
        ("thinking", "display") if name == "anthropic-messages" else ("reasoning", "summary")
    )
    return mapped_reasoning(
        name=name,
        activation="unsupported",
        effort={},
        supported_summaries=summaries,
        summary={value: {parent: {field: value}} for value in summaries},
        default=ReasoningRequest(summary=summaries[0]),
        output="summary",
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
        effort={"enabled": enabled, "disabled": disabled},
    )


def reasoning_effort_preset(name: str) -> EndpointReasoningConfig:
    """Declare a standard effort capability and its native request mapping."""

    normalized = name.strip().casefold()
    if normalized == "none":
        return unsupported_reasoning()
    if normalized == "openai-chat-completions-effort":
        return openai_chat_completions_effort_reasoning()
    if normalized == "openai-responses-effort":
        return openai_responses_effort_reasoning(output="summary")
    if normalized == "reasoning-content-effort":
        return openai_chat_completions_effort_reasoning(output="text", continuation="text")
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
    if normalized == "anthropic-messages-adaptive-effort":
        efforts: tuple[ReasoningEffort, ...] = ("low", "medium", "high", "xhigh", "max")
        return mapped_reasoning(
            name="anthropic-messages-adaptive-effort",
            activation="toggle",
            supported_efforts=efforts,
            default=ReasoningRequest(mode="enabled", effort="high"),
            output="summary",
            continuation="opaque",
            effort={
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
    available = ", ".join(REASONING_EFFORT_PRESETS)
    raise ValueError(f"unknown reasoning effort preset: {name}; available: {available}")
