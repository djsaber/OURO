import pytest

from ouro.core.provider import (
    ProviderRetryNotice,
    ProviderRetryPolicy,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)


def test_tool_call_delta_requires_at_least_one_fragment() -> None:
    with pytest.raises(ValueError, match="at least one fragment"):
        ToolCallDelta(index=0)


def test_provider_done_event_is_only_a_completion_boundary() -> None:
    event = ProviderStreamEvent(
        type="done",
        usage=Usage(input_tokens=1, output_tokens=2),
    )

    assert event.usage == Usage(input_tokens=1, output_tokens=2)
    with pytest.raises(ValueError, match="may only contain usage"):
        ProviderStreamEvent(type="done", text="unexpected")


def test_provider_retry_policy_is_bounded_and_retry_event_requires_payload() -> None:
    policy = ProviderRetryPolicy(
        max_attempts=4,
        initial_backoff_seconds=0.5,
        max_backoff_seconds=1.5,
        backoff_multiplier=2,
    )

    assert [policy.delay_before_attempt(attempt) for attempt in (2, 3, 4)] == [0.5, 1.0, 1.5]
    with pytest.raises(ValueError, match="missing its payload"):
        ProviderStreamEvent(type="retry_scheduled")

    event = ProviderStreamEvent(
        type="retry_scheduled",
        retry=ProviderRetryNotice(
            attempt=2,
            max_attempts=4,
            delay_seconds=0.5,
            reason="provider request timed out",
        ),
    )
    assert event.retry is not None and event.retry.attempt == 2
