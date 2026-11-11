from __future__ import annotations

import pytest
from pydantic import ValidationError

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
from ouro.config.provider_models import validate_reasoning_protocol_compatibility
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest, resolve_reasoning
from ouro.providers.reasoning import ReasoningPayloadEncoder


@pytest.mark.parametrize("path", [(), ("capability",), ("profile",)])
def test_reasoning_contract_rejects_undeclared_fields(path):
    raw = reasoning_effort_preset("thinking-toggle").model_dump()
    target = raw[path[0]] if path else raw
    target["undeclared_field"] = None
    with pytest.raises(ValidationError) as error:
        EndpointReasoningConfig.model_validate(raw)
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        ((*path, "undeclared_field"), "extra_forbidden")
    ]


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_reasoning_text_field_is_independent_of_generation_controls(field):
    endpoint = ProviderEndpointConfig(model_id="model", reasoning_text_field=field)
    for preset in ("thinking-toggle", "none"):
        endpoint.reasoning = endpoint.reasoning.with_effort_preset(preset)
        assert endpoint.reasoning_text_field == field
    endpoint.reasoning = endpoint.reasoning.with_summary_preset("none")
    payload = {}
    ReasoningPayloadEncoder(endpoint.reasoning.profile).apply(
        payload, ModelMetadata(provider_id="p", model_id="m")
    )
    assert payload == {}
    validate_reasoning_protocol_compatibility("openai-chat-completions", endpoint.reasoning, field)
    for protocol in ("openai-responses", "anthropic-messages", "faux"):
        with pytest.raises(ValueError, match="reasoning_text_field"):
            validate_reasoning_protocol_compatibility(protocol, endpoint.reasoning, field)


def test_reasoning_text_field_accepts_only_supported_wire_spellings():
    with pytest.raises(ValueError):
        ProviderEndpointConfig(model_id="model", reasoning_text_field="private_field")


def test_history_field_roundtrips_and_runtime_override_can_clear_it(tmp_path):
    from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
    from ouro.config.providers.runtime import merge_runtime_options, runtime_options_from_config
    from ouro.providers.factory import build_runtime_adapter

    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        display_name="Gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test",
    )
    endpoint = manager.create_endpoint(
        provider.provider_id, model_id="model", reasoning_text_field="reasoning"
    )
    provider = manager.get_provider(provider.provider_id)
    options = runtime_options_from_config(provider, endpoint_id=endpoint.endpoint_id)
    assert options.reasoning_text_field == "reasoning"
    adapter = build_runtime_adapter(options)
    assert adapter.provider.reasoning_text_field == "reasoning"
    assert (
        merge_runtime_options(options, ProviderRuntimeOptions()).reasoning_text_field == "reasoning"
    )
    assert (
        merge_runtime_options(
            options, ProviderRuntimeOptions(reasoning_text_field=None)
        ).reasoning_text_field
        is None
    )


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
        "always-on",
        "thinking-toggle",
        "enable-thinking-toggle",
        "anthropic-messages-adaptive-effort",
    ),
)
def test_reasoning_effort_presets_are_valid_endpoint_contracts(name: str) -> None:
    assert isinstance(reasoning_effort_preset(name), EndpointReasoningConfig)


def test_always_on_preset_declares_no_level_or_toggle_parameters() -> None:
    reasoning = reasoning_effort_preset("always-on")
    model = ModelMetadata(provider_id="fixture", model_id="model", reasoning=reasoning.capability)
    assert model.reasoning_resolution.effective == ReasoningRequest(mode="enabled")
    payload = {}
    ReasoningPayloadEncoder(reasoning.profile).apply(payload, model)
    assert payload == {}
    with pytest.raises(ValueError, match="always on"):
        resolve_reasoning(reasoning.capability, ReasoningRequest(mode="disabled"))


@pytest.mark.parametrize("effort", ["none", "always-on", "openai-responses-effort"])
@pytest.mark.parametrize("summary", ["none", "openai-responses"])
def test_effort_and_summary_presets_are_independent(effort: str, summary: str) -> None:
    base = EndpointReasoningConfig()
    combined = base.with_effort_preset(effort).with_summary_preset(summary)
    assert combined == base.with_summary_preset(summary).with_effort_preset(effort)
    assert combined.with_summary_preset("none") == reasoning_effort_preset(effort)
    summary_only = combined.with_effort_preset("none")
    expected = reasoning_summary_preset(summary)
    assert summary_only.capability == expected.capability
    if expected.profile is not None:
        assert summary_only.profile.effort == {}
        assert summary_only.profile.summary == expected.profile.summary
    else:
        assert summary_only.profile is None
    assert base == EndpointReasoningConfig()


def test_serialized_reasoning_only_contains_request_controls() -> None:
    reasoning = reasoning_effort_preset("openai-chat-completions-effort")
    assert set(reasoning.capability.model_dump()) == {
        "activation",
        "supported_efforts",
        "supported_summaries",
        "default",
    }


def test_unknown_reasoning_effort_preset_lists_available_names() -> None:
    with pytest.raises(
        ValueError, match="available: none, always-on, openai-chat-completions-effort"
    ):
        reasoning_effort_preset("unknown")


def test_responses_preset_preserves_native_defaults_and_owns_reasoning_object() -> None:
    reasoning = reasoning_effort_preset("openai-responses-effort")
    assert reasoning.capability.default == ReasoningRequest(effort="medium")
    assert resolve_reasoning(reasoning.capability).effective == ReasoningRequest()
    assert reasoning.capability.default.effort == "medium"
    assert reasoning.profile is not None
    assert reasoning.profile.effort["enabled"] == {"reasoning": {"effort": "medium"}}
    assert reasoning.profile.effort["disabled"] == {"reasoning": {"effort": "none"}}
    with pytest.raises(ValueError, match="reasoning-owned fields: reasoning"):
        reasoning.validate_request_options({"reasoning": {"effort": "high"}})


def test_responses_summary_request_is_explicit_and_applies_to_provider_default() -> None:
    native = reasoning_effort_preset("openai-responses-effort")
    requested = openai_responses_effort_reasoning(
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
