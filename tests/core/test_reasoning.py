from __future__ import annotations

import pytest

from ouro.core.reasoning import ReasoningCapability, ReasoningRequest, resolve_reasoning


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

    default = resolve_reasoning(capability)
    explicit = resolve_reasoning(capability, ReasoningRequest(effort="high"))

    assert default.requested == ReasoningRequest()
    assert default.effective == ReasoningRequest(mode="enabled", effort="low")
    assert default.variant == "low"
    assert explicit.effective == ReasoningRequest(mode="enabled", effort="high")
    assert explicit.variant == "high"


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
