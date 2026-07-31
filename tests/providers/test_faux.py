import pytest

from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.providers import FauxProvider


@pytest.mark.asyncio
async def test_faux_provider_streams_scripted_events() -> None:
    provider = FauxProvider(
        [
            ProviderStreamEvent(type="text_delta", text="A"),
            ProviderStreamEvent(type="text_delta", text="B"),
            ProviderStreamEvent(type="done"),
        ]
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="faux", provider_id="faux"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="go")],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "text_delta", "done"]
    assert "".join(event.text or "" for event in events) == "AB"


@pytest.mark.asyncio
async def test_faux_provider_streams_event_batches_per_call() -> None:
    provider = FauxProvider(
        event_batches=[
            [ProviderStreamEvent.from_tool_call({"name": "Read"})],
            [ProviderStreamEvent(type="text_delta", text="done"), ProviderStreamEvent(type="done")],
        ]
    )

    first = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="faux", provider_id="faux"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="go")],
        )
    ]
    second = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="faux", provider_id="faux"),
            messages=[MessageRecord.text(session_id="ses_1", role="tool_result", text="ok")],
        )
    ]
    exhausted = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="faux", provider_id="faux"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="again")],
        )
    ]

    assert [event.type for event in first] == ["tool_call_delta"]
    assert [event.type for event in second] == ["text_delta", "done"]
    assert [event.type for event in exhausted] == ["done"]


@pytest.mark.asyncio
async def test_faux_provider_connection_check_reports_ok() -> None:
    provider = FauxProvider()

    result = await provider.check_connection(ModelMetadata(model_id="faux", provider_id="faux"))

    assert result.ok is True
    assert result.provider_id == "faux"
    assert result.model_id == "faux"
    assert result.message == "ok"
