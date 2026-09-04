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
        default_effort="low",
        default=ReasoningRequest(mode="enabled", effort="low"),
        output="text",
        continuation="text",
    )


def test_reasoning_resolver_uses_endpoint_default_and_promotes_explicit_effort() -> None:
    capability = _toggle_capability()

    default = resolve_reasoning(capability)
    explicit = resolve_reasoning(capability, ReasoningRequest(effort="high"))

    assert default.requested == ReasoningRequest()
    assert default.effective == ReasoningRequest(mode="enabled", effort="low")
    assert default.variant == "low"
    assert explicit.effective == ReasoningRequest(mode="enabled", effort="high")
    assert explicit.variant == "high"


def test_enabled_reasoning_resolves_the_endpoint_default_effort() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        supported_efforts=("low", "high"),
        default_effort="high",
        default=ReasoningRequest(mode="disabled"),
    )

    endpoint_default = resolve_reasoning(capability)
    enabled = resolve_reasoning(capability, ReasoningRequest(mode="enabled"))

    assert endpoint_default.effective == ReasoningRequest(mode="disabled")
    assert endpoint_default.variant == "disabled"
    assert enabled.effective == ReasoningRequest(mode="enabled", effort="high")
    assert enabled.variant == "high"


def test_single_supported_effort_is_the_implicit_enabled_default() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        supported_efforts=("max",),
        default=ReasoningRequest(mode="disabled"),
    )

    assert capability.default_effort == "max"
    assert resolve_reasoning(
        capability,
        ReasoningRequest(mode="enabled"),
    ).effective == ReasoningRequest(mode="enabled", effort="max")


def test_enabled_default_effort_is_derived_from_the_endpoint_default() -> None:
    capability = ReasoningCapability.model_validate(
        {
            "activation": "toggle",
            "supported_efforts": ["low", "high"],
            "default": {"mode": "enabled", "effort": "high"},
        }
    )

    assert capability.default_effort == "high"
    assert capability.model_dump(mode="json")["default_effort"] == "high"


def test_reasoning_without_effort_controls_keeps_plain_enabled_semantics() -> None:
    capability = ReasoningCapability(
        activation="toggle",
        default=ReasoningRequest(mode="disabled"),
    )

    enabled = resolve_reasoning(capability, ReasoningRequest(mode="enabled"))

    assert enabled.effective == ReasoningRequest(mode="enabled")
    assert enabled.variant == "enabled"


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
                "default_effort": "high",
                "default": ReasoningRequest(mode="enabled"),
            },
            "without effort controls cannot declare a default effort",
        ),
        (
            {
                "activation": "toggle",
                "supported_efforts": ("low", "high"),
                "default_effort": "max",
                "default": ReasoningRequest(mode="disabled"),
            },
            "enabled default effort must be supported",
        ),
        (
            {
                "activation": "unsupported",
                "default_effort": "high",
            },
            "unsupported reasoning cannot declare a default effort",
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
            ReasoningRequest(mode="disabled"),
            "does not support reasoning controls",
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


def test_reasoning_capability_rejects_invalid_continuation_contract() -> None:
    with pytest.raises(ValueError, match="text reasoning continuation"):
        ReasoningCapability(
            activation="toggle",
            default=ReasoningRequest(mode="disabled"),
            output="none",
            continuation="text",
        )


def test_disabled_reasoning_cannot_carry_an_effort() -> None:
    with pytest.raises(ValueError, match="cannot specify an effort"):
        ReasoningRequest(mode="disabled", effort="high")


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
