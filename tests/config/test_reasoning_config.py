from __future__ import annotations

import pytest

from ouro.config import (
    EndpointReasoningConfig,
    ProviderConfig,
    ProviderEndpointConfig,
    ReasoningProfileConfig,
    mapped_reasoning,
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


@pytest.mark.parametrize(
    "name",
    (
        "none",
        "openai-effort",
        "reasoning-content-effort",
        "thinking-toggle",
        "enable-thinking-toggle",
        "anthropic-adaptive-effort",
    ),
)
def test_reasoning_presets_are_valid_endpoint_contracts(name: str) -> None:
    assert isinstance(reasoning_preset(name), EndpointReasoningConfig)


def test_unknown_reasoning_preset_lists_available_names() -> None:
    with pytest.raises(ValueError, match="available: none, openai-effort"):
        reasoning_preset("unknown")


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
            provider_type=(
                "openai-compatible"
                if protocol == "openai-chat-completions"
                else "anthropic-compatible"
            ),
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
