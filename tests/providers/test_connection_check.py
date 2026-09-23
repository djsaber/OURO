import pytest

from ouro.core.provider import ModelMetadata, ProviderCompletion, ProviderStreamEvent
from ouro.providers.connection_check import check_connection_events


@pytest.mark.parametrize(
    ("reason", "thinking", "ok", "code"),
    [
        ("stop", False, False, "model_empty_response"),
        ("stop", True, True, None),
        ("length", False, True, None),
        ("content_filter", False, False, "model_content_filtered"),
        ("other", False, False, "model_incomplete"),
    ],
)
async def test_probe_distinguishes_connectivity_from_a_completed_agent_answer(
    reason, thinking, ok, code
) -> None:
    closed = []

    async def events():
        try:
            yield ProviderStreamEvent(
                type="done",
                completion=ProviderCompletion(finish_reason=reason, has_reasoning=thinking),
            )
        finally:
            closed.append(True)

    result = await check_connection_events(
        events(), provider_id="test", model=ModelMetadata(model_id="test", provider_id="test")
    )
    assert result.ok is ok
    assert result.error_code == code
    assert closed == [True]


@pytest.mark.parametrize("done", [False, True])
async def test_probe_preserves_errors_and_closes_the_stream(done) -> None:
    closed = []

    async def events():
        try:
            if done:
                yield ProviderStreamEvent(type="done")
            yield ProviderStreamEvent(
                type="error", error="service failed", error_code="service_error"
            )
            raise AssertionError("probe must stop after the error")
        finally:
            closed.append(True)

    result = await check_connection_events(
        events(), provider_id="test", model=ModelMetadata(model_id="test", provider_id="test")
    )
    assert not result.ok
    assert result.message == "service failed"
    assert result.error_code == "service_error"
    assert closed == [True]
