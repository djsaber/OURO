from __future__ import annotations

import pytest

from ouro.config import (
    EndpointReasoningConfig,
    ProviderConfig,
    ProviderEndpointConfig,
    ReasoningProfileConfig,
    mapped_reasoning,
    openai_responses_effort_reasoning,
    reasoning_preset,
)
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.providers.reasoning import ReasoningPayloadEncoder


def test_reasoning_profile_requires_every_controllable_variant() -> None:
    with pytest.raises(ValueError, match="missing required variants: disabled, high"):
        EndpointReasoningConfig(
            capability=ReasoningCapability(
                activation="toggle",
                supported_efforts=("high",),
                default_effort="high",
                default=ReasoningRequest(mode="enabled", effort="high"),
            ),
            profile=ReasoningProfileConfig(
                name="incomplete",
                variants={"enabled": {"thinking": True}},
            ),
        )

    with pytest.raises(ValueError, match="unsupported reasoning cannot define"):
        EndpointReasoningConfig(
            profile=ReasoningProfileConfig(name="unused", variants={}),
        )


def test_reasoning_profile_protects_core_fields_and_request_option_ownership() -> None:
    with pytest.raises(ValueError, match="reserved request fields: messages"):
        ReasoningProfileConfig(
            name="unsafe",
            variants={"enabled": {"messages": []}},
        )

    with pytest.raises(ValueError, match="reserved request fields: temperature"):
        ReasoningProfileConfig(
            name="unsafe-temperature",
            variants={"enabled": {"temperature": 1.0}},
        )

    reasoning = mapped_reasoning(
        name="native-toggle",
        activation="toggle",
        default=ReasoningRequest(mode="disabled"),
        variants={
            "enabled": {"thinking": {"type": "enabled"}},
            "disabled": {"thinking": {"type": "disabled"}},
        },
    )
    with pytest.raises(ValueError, match="reasoning-owned fields: thinking"):
        reasoning.validate_request_options({"thinking": {"type": "disabled"}})


def test_reasoning_encoder_owns_exact_native_payload_variant() -> None:
    reasoning = mapped_reasoning(
        name="native-effort",
        activation="toggle",
        supported_efforts=("high", "max"),
        default_effort="high",
        default=ReasoningRequest(mode="enabled", effort="high"),
        variants={
            "enabled": {"thinking": {"type": "enabled"}},
            "disabled": {"thinking": {"type": "disabled"}},
            "high": {"thinking": {"type": "enabled"}, "native_effort": "h"},
            "max": {"thinking": {"type": "enabled"}, "native_effort": "m"},
        },
    )
    model = ModelMetadata(
        model_id="custom",
        provider_id="custom",
        reasoning=reasoning.capability,
        reasoning_request=ReasoningRequest(mode="enabled", effort="max"),
    )
    payload: dict[str, object] = {"model": "custom"}

    ReasoningPayloadEncoder(reasoning.profile).apply(payload, model)

    assert payload == {
        "model": "custom",
        "thinking": {"type": "enabled"},
        "native_effort": "m",
    }


def test_enabled_reasoning_encodes_the_default_effort_variant() -> None:
    reasoning = mapped_reasoning(
        name="native-effort",
        activation="toggle",
        supported_efforts=("high", "max"),
        default_effort="high",
        default=ReasoningRequest(mode="disabled"),
        variants={
            "enabled": {"thinking": {"type": "enabled"}},
            "disabled": {"thinking": {"type": "disabled"}},
            "high": {"thinking": {"type": "enabled"}, "native_effort": "h"},
            "max": {"thinking": {"type": "enabled"}, "native_effort": "m"},
        },
    )
    model = ModelMetadata(
        model_id="custom",
        provider_id="custom",
        reasoning=reasoning.capability,
        reasoning_request=ReasoningRequest(mode="enabled"),
    )
    payload: dict[str, object] = {"model": "custom"}

    ReasoningPayloadEncoder(reasoning.profile).apply(payload, model)

    assert payload == {
        "model": "custom",
        "thinking": {"type": "enabled"},
        "native_effort": "h",
    }


@pytest.mark.parametrize(
    "name",
    (
        "none",
        "openai-chat-completions-effort",
        "openai-responses-effort",
        "openai-responses-effort-summary",
        "reasoning-content-effort",
        "thinking-toggle",
        "enable-thinking-toggle",
        "anthropic-messages-adaptive-effort",
    ),
)
def test_reasoning_presets_are_valid_endpoint_contracts(name: str) -> None:
    assert isinstance(reasoning_preset(name), EndpointReasoningConfig)


def test_unknown_reasoning_preset_lists_available_names() -> None:
    with pytest.raises(ValueError, match="available: none, openai-chat-completions-effort"):
        reasoning_preset("unknown")


def test_responses_preset_preserves_native_defaults_and_owns_reasoning_object() -> None:
    reasoning = reasoning_preset("openai-responses-effort")
    assert reasoning.capability.default == ReasoningRequest()
    assert reasoning.capability.default_effort == "medium"
    assert reasoning.capability.continuation == "opaque"
    assert reasoning.capability.output == "summary"
    assert reasoning.profile is not None
    assert reasoning.profile.variants["enabled"] == {"reasoning": {"effort": "medium"}}
    assert reasoning.profile.variants["disabled"] == {"reasoning": {"effort": "none"}}
    with pytest.raises(ValueError, match="reasoning-owned fields: reasoning"):
        reasoning.validate_request_options({"reasoning": {"effort": "high"}})


def test_responses_summary_request_is_explicit_and_applies_to_provider_default() -> None:
    native = reasoning_preset("openai-responses-effort")
    requested = reasoning_preset("openai-responses-effort-summary")
    assert native.profile is not None and requested.profile is not None
    assert "default" not in native.profile.variants
    assert all(
        "summary" not in fragment["reasoning"] for fragment in native.profile.variants.values()
    )
    assert requested.capability.output == "summary"
    assert requested.profile.variants["default"] == {"reasoning": {"summary": "auto"}}
    assert requested.profile.variants["high"] == {
        "reasoning": {"effort": "high", "summary": "auto"}
    }
    assert requested.profile.variants["disabled"] == {"reasoning": {"effort": "none"}}


def test_responses_profile_supports_endpoint_specific_default_effort() -> None:
    reasoning = openai_responses_effort_reasoning(
        activation="always_on", supported_efforts=("high", "max"), default_effort="high"
    )
    model = ModelMetadata(model_id="custom", provider_id="custom", reasoning=reasoning.capability)
    payload: dict[str, object] = {}
    ReasoningPayloadEncoder(reasoning.profile).apply(payload, model)
    assert payload == {"reasoning": {"effort": "high"}}
    assert reasoning.profile is not None
    assert "disabled" not in reasoning.profile.variants
    with pytest.raises(ValueError, match="default effort must be supported"):
        openai_responses_effort_reasoning(supported_efforts=("high",), default_effort="medium")


@pytest.mark.parametrize(
    ("protocol", "continuation"),
    [
        ("openai-chat-completions", "opaque"),
        ("anthropic-messages", "text"),
    ],
)
def test_provider_rejects_reasoning_continuation_unsupported_by_protocol(
    protocol,
    continuation,
) -> None:
    reasoning = mapped_reasoning(
        name="incompatible-continuation",
        activation="toggle",
        default=ReasoningRequest(mode="enabled"),
        output="text",
        continuation=continuation,
        variants={
            "enabled": {"thinking": {"type": "enabled"}},
            "disabled": {"thinking": {"type": "disabled"}},
        },
    )

    with pytest.raises(ValueError, match="is not supported by protocol"):
        ProviderConfig(
            provider_id="custom",
            provider_type="custom",
            protocol=protocol,
            base_url="https://example.test",
            default_endpoint="chat",
            endpoints=(
                ProviderEndpointConfig(
                    endpoint_id="chat",
                    model_id="custom-model",
                    reasoning=reasoning,
                ),
            ),
        )
