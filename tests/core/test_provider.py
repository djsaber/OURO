import pytest

from ouro.core.provider import ProviderStreamEvent, ToolCallDelta, Usage


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
