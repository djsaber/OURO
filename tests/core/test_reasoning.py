from __future__ import annotations

from typing import cast

import pytest

from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, Provider
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest, resolve_reasoning


class _Provider:
    provider_id = "test"


def _toggle_capability() -> ReasoningCapability:
    return ReasoningCapability(
        activation="toggle",
        supported_efforts=("low", "high"),
        default=ReasoningRequest(mode="enabled", effort="low"),
        output="text",
        continuation="text",
    )


def test_reasoning_resolver_uses_endpoint_default_and_promotes_explicit_effort() -> None:
    capability = _toggle_capability()
    capability.supported_summaries = ("auto", "detailed")
    capability.default.summary = "auto"

    default = resolve_reasoning(capability)
    explicit = resolve_reasoning(capability, ReasoningRequest(effort="high"))

    assert default.requested == ReasoningRequest()
    assert default.effective == ReasoningRequest(mode="enabled", effort="low", summary="auto")
    assert default.effort_variant == "low"
    assert explicit.effective == ReasoningRequest(mode="enabled", effort="high", summary="auto")
    assert explicit.effort_variant == "high"
    summary_only = resolve_reasoning(capability, ReasoningRequest(summary="provider_default"))
    assert summary_only.effective == ReasoningRequest(
        mode="enabled", effort="low", summary="provider_default"
    )
    detailed = resolve_reasoning(capability, ReasoningRequest(summary="detailed"))
    assert detailed.effective == ReasoningRequest(mode="enabled", effort="low", summary="detailed")


def test_enabled_reasoning_resolves_the_endpoint_default_effort() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        supported_efforts=("low", "high"),
        default=ReasoningRequest(mode="disabled", effort="high"),
    )

    endpoint_default = resolve_reasoning(capability)
    enabled = resolve_reasoning(capability, ReasoningRequest(mode="enabled"))

    assert endpoint_default.effective == ReasoningRequest(mode="disabled")
    assert endpoint_default.effort_variant == "disabled"
    assert enabled.effective == ReasoningRequest(mode="enabled", effort="high")
    assert enabled.effort_variant == "high"


def test_single_supported_effort_is_the_implicit_enabled_default() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        supported_efforts=("max",),
        default=ReasoningRequest(mode="disabled"),
    )

    assert capability.default.effort == "max"
    assert resolve_reasoning(
        capability,
        ReasoningRequest(mode="enabled"),
    ).effective == ReasoningRequest(mode="enabled", effort="max")


def test_reasoning_defaults_roundtrip_as_one_preference_object() -> None:
    capability = ReasoningCapability.model_validate(
        {
            "activation": "toggle",
            "supported_efforts": ["low", "high"],
            "default": {"mode": "enabled", "effort": "high"},
        }
    )

    assert capability.default.effort == "high"
    encoded = capability.model_dump(mode="json")
    assert encoded["default"] == {"mode": "enabled", "effort": "high"}
    assert "default_effort" not in encoded


def test_reasoning_without_effort_controls_keeps_plain_enabled_semantics() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        default=ReasoningRequest(mode="disabled"),
    )

    enabled = resolve_reasoning(capability, ReasoningRequest(mode="enabled"))

    assert enabled.effective == ReasoningRequest(mode="enabled")
    assert enabled.effort_variant == "enabled"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {
                "activation": "toggle",
                "supported_efforts": ("low", "high"),
                "default": ReasoningRequest(mode="disabled"),
            },
            "multiple efforts must declare a default effort",
        ),
        (
            {
                "activation": "toggle",
                "default": ReasoningRequest(mode="enabled", effort="high"),
            },
            "default reasoning effort must be supported",
        ),
        (
            {
                "activation": "toggle",
                "supported_efforts": ("low", "high"),
                "default": ReasoningRequest(mode="disabled", effort="max"),
            },
            "default reasoning effort must be supported",
        ),
        (
            {
                "activation": "unsupported",
                "default": ReasoningRequest(effort="high"),
            },
            "unsupported reasoning cannot declare a non-default intent",
        ),
    ],
)
def test_reasoning_capability_rejects_ambiguous_enabled_effort(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ReasoningCapability.model_validate(values)


@pytest.mark.parametrize(
    ("capability", "intent", "message"),
    [
        (
            ReasoningCapability(),
            ReasoningRequest(mode="enabled"),
            "does not support reasoning controls",
        ),
        (
            ReasoningCapability(),
            ReasoningRequest(summary="auto"),
            "is not supported by endpoint",
        ),
        (
            ReasoningCapability(
                activation="always_on",
                default=ReasoningRequest(mode="enabled"),
            ),
            ReasoningRequest(mode="disabled"),
            "always on",
        ),
        (
            _toggle_capability(),
            ReasoningRequest(mode="enabled", effort="medium"),
            "is not supported",
        ),
    ],
)
def test_reasoning_resolver_rejects_unfulfillable_intent(
    capability: ReasoningCapability,
    intent: ReasoningRequest,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_reasoning(capability, intent)


def test_reasoning_continuation_is_independent_of_visible_output() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        default=ReasoningRequest(mode="disabled"),
        output="none",
        continuation="text",
    )
    assert capability.output == "none"
    assert capability.continuation == "text"


def test_disabled_reasoning_preserves_but_does_not_apply_effort_preference() -> None:
    request = ReasoningRequest(mode="disabled", effort="high")
    resolution = resolve_reasoning(_toggle_capability(), request)
    assert resolution.requested == request
    assert resolution.effective == ReasoningRequest(mode="disabled")
    assert resolution.effort_variant == "disabled"


def test_runtime_adapter_binds_validated_reasoning_without_mutating_shared_model() -> None:
    adapter = RuntimeAdapter(
        cast(Provider, _Provider()),
        ModelMetadata(
            model_id="reasoning-model",
            provider_id="test",
            reasoning=_toggle_capability(),
        ),
    )
    request = ReasoningRequest(mode="enabled", effort="high")

    bound = adapter.with_reasoning(request)

    assert bound.provider is adapter.provider
    assert bound.model.reasoning_request == request
    assert adapter.model.reasoning_request == ReasoningRequest()
    with pytest.raises(ValueError, match="not supported"):
        adapter.with_reasoning(ReasoningRequest(mode="enabled", effort="medium"))
