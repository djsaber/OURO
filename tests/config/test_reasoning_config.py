from __future__ import annotations

import pytest

from ouro.config import (
    EndpointReasoningConfig,
    ProviderConfig,
    ProviderEndpointConfig,
    ReasoningProfileConfig,
    mapped_reasoning,
    openai_responses_effort_reasoning,
    reasoning_effort_preset,
    reasoning_summary_preset,
)
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest, resolve_reasoning
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
                effort={"enabled": {"thinking": True}},
            ),
        )

    with pytest.raises(ValueError, match="unsupported reasoning cannot define"):
        EndpointReasoningConfig(
            profile=ReasoningProfileConfig(name="unused", effort={}),
        )
    with pytest.raises(ValueError, match="summary profile is missing required variants: auto"):
        EndpointReasoningConfig(
            capability=ReasoningCapability(supported_summaries=("auto",)),
            profile=ReasoningProfileConfig(name="incomplete-summary"),
        )
    with pytest.raises(ValueError, match="summary profile defines unsupported variants: detailed"):
        EndpointReasoningConfig(
            capability=ReasoningCapability(supported_summaries=("auto",)),
            profile=ReasoningProfileConfig(
                name="extra-summary", summary={"auto": {}, "detailed": {}}
            ),
        )


def test_reasoning_profile_protects_core_fields_and_request_option_ownership() -> None:
    with pytest.raises(ValueError, match="reserved request fields: messages"):
        ReasoningProfileConfig(
            name="unsafe",
            effort={"enabled": {"messages": []}},
        )

    with pytest.raises(ValueError, match="reserved request fields: temperature"):
        ReasoningProfileConfig(
            name="unsafe-temperature",
            effort={"enabled": {"temperature": 1.0}},
        )

    reasoning = mapped_reasoning(
        name="native-toggle",
        activation="toggle",
        default=ReasoningRequest(mode="disabled"),
        effort={
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
        supported_summaries=("auto", "detailed"),
        summary={
            "auto": {"thinking": {"explanation": "brief"}},
            "detailed": {"thinking": {"type": "enabled", "explanation": "verbose"}},
        },
        default=ReasoningRequest(mode="enabled", effort="high"),
        effort={
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
        reasoning_request=ReasoningRequest(mode="enabled", effort="max", summary="detailed"),
    )
    payload: dict[str, object] = {"model": "custom"}

    ReasoningPayloadEncoder(reasoning.profile).apply(payload, model)

    assert payload == {
        "model": "custom",
        "thinking": {"type": "enabled", "explanation": "verbose"},
        "native_effort": "m",
    }
    assert reasoning.profile.effort["max"]["thinking"] == {"type": "enabled"}
    with pytest.raises(ValueError, match="reasoning-owned fields: thinking"):
        ReasoningPayloadEncoder(reasoning.profile).apply({"thinking": {}}, model)
    conflicting = reasoning.profile.model_copy(deep=True)
    conflicting.summary["detailed"]["thinking"]["type"] = "disabled"
    with pytest.raises(ValueError, match="reasoning mappings conflict at thinking.type"):
        ReasoningPayloadEncoder(conflicting).apply({}, model)
    endpoint = ProviderEndpointConfig(endpoint_id="mapped", model_id="mapped", reasoning=reasoning)
    ProviderConfig(
        provider_id="custom",
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test",
        default_endpoint="mapped",
        endpoints=(endpoint,),
    )


def test_enabled_reasoning_encodes_the_default_effort_variant() -> None:
    reasoning = mapped_reasoning(
        name="native-effort",
        activation="toggle",
        supported_efforts=("high", "max"),
        default=ReasoningRequest(mode="disabled", effort="high"),
        effort={
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
        "reasoning-content-effort",
        "thinking-toggle",
        "enable-thinking-toggle",
        "anthropic-messages-adaptive-effort",
    ),
)
def test_reasoning_effort_presets_are_valid_endpoint_contracts(name: str) -> None:
    assert isinstance(reasoning_effort_preset(name), EndpointReasoningConfig)


def test_unknown_reasoning_effort_preset_lists_available_names() -> None:
    with pytest.raises(ValueError, match="available: none, openai-chat-completions-effort"):
        reasoning_effort_preset("unknown")


def test_responses_preset_preserves_native_defaults_and_owns_reasoning_object() -> None:
    reasoning = reasoning_effort_preset("openai-responses-effort")
    assert reasoning.capability.default == ReasoningRequest(effort="medium")
    assert resolve_reasoning(reasoning.capability).effective == ReasoningRequest()
    assert reasoning.capability.default.effort == "medium"
    assert reasoning.capability.continuation == "opaque"
    assert reasoning.capability.output == "summary"
    assert reasoning.profile is not None
    assert reasoning.profile.effort["enabled"] == {"reasoning": {"effort": "medium"}}
    assert reasoning.profile.effort["disabled"] == {"reasoning": {"effort": "none"}}
    with pytest.raises(ValueError, match="reasoning-owned fields: reasoning"):
        reasoning.validate_request_options({"reasoning": {"effort": "high"}})


def test_responses_summary_request_is_explicit_and_applies_to_provider_default() -> None:
    native = reasoning_effort_preset("openai-responses-effort")
    requested = openai_responses_effort_reasoning(
        output="summary",
        supported_summaries=("auto", "concise", "detailed"),
        default=ReasoningRequest(summary="auto"),
    )
    assert native.profile is not None and requested.profile is not None
    assert native.profile.effort == requested.profile.effort
    assert requested.profile.summary == {
        value: {"reasoning": {"summary": value}} for value in ("auto", "concise", "detailed")
    }
    assert all(
        "summary" not in fragment["reasoning"] for fragment in native.profile.effort.values()
    )
    assert requested.capability.output == "summary"
    assert resolve_reasoning(requested.capability).effective == ReasoningRequest(summary="auto")
    assert resolve_reasoning(
        requested.capability, ReasoningRequest(effort="high")
    ).effective == ReasoningRequest(mode="enabled", effort="high", summary="auto")
    assert requested.profile.effort["high"] == {"reasoning": {"effort": "high"}}
    assert requested.profile.effort["disabled"] == {"reasoning": {"effort": "none"}}
    summary_only = reasoning_summary_preset("openai-responses")
    assert summary_only.profile.effort == {}
    assert resolve_reasoning(summary_only.capability).effective.summary == "auto"
    payload = {}
    ReasoningPayloadEncoder(summary_only.profile).apply(
        payload,
        ModelMetadata(model_id="summary", provider_id="custom", reasoning=summary_only.capability),
    )
    assert payload == {"reasoning": {"summary": "auto"}}
    combined = summary_only.with_effort_preset("openai-responses-effort")
    assert combined == native.with_summary_preset("openai-responses")
    assert combined.profile.effort == native.profile.effort
    assert combined.profile.summary == summary_only.profile.summary
    cleared = combined.with_summary_preset("none")
    assert cleared == native
    assert combined.capability.default.summary == "auto"
    assert summary_only.profile.effort == {}
    assert native.capability.supported_summaries == ()


def test_responses_profile_supports_endpoint_specific_default_effort() -> None:
    reasoning = openai_responses_effort_reasoning(
        activation="always_on",
        supported_efforts=("high", "max"),
        default=ReasoningRequest(effort="high"),
    )
    model = ModelMetadata(model_id="custom", provider_id="custom", reasoning=reasoning.capability)
    payload: dict[str, object] = {}
    ReasoningPayloadEncoder(reasoning.profile).apply(payload, model)
    assert payload == {"reasoning": {"effort": "high"}}
    assert reasoning.profile is not None
    assert "disabled" not in reasoning.profile.effort
    with pytest.raises(ValueError, match="default reasoning effort must be supported"):
        openai_responses_effort_reasoning(
            supported_efforts=("high",), default=ReasoningRequest(effort="medium")
        )


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
        effort={
            "enabled": {"thinking": {"type": "enabled"}},
            "disabled": {"thinking": {"type": "disabled"}},
        },
    )

    with pytest.raises(ValueError, match="is not supported by protocol"):
        ProviderConfig(
            provider_id="custom",
            display_name="Custom",
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
