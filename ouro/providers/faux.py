from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence

from ouro.core.messages import ModelMessage
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent, Usage


class FauxProvider:
    provider_id = "faux"

    def __init__(
        self,
        events: Iterable[ProviderStreamEvent] | None = None,
        *,
        event_batches: Sequence[Iterable[ProviderStreamEvent]] | None = None,
    ) -> None:
        self._event_batches = (
            [list(batch) for batch in event_batches] if event_batches is not None else None
        )
        self._stream_count = 0
        self._events = (
            list(events)
            if events is not None
            else [
                ProviderStreamEvent(type="text_delta", text="Hello from OURO."),
                ProviderStreamEvent(type="done", usage=Usage(input_tokens=1, output_tokens=4)),
            ]
        )

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        events = self._next_events()
        for event in events:
            yield event

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
            metadata={"endpoint_id": model.endpoint_id} if model.endpoint_id else {},
        )

    def _next_events(self) -> list[ProviderStreamEvent]:
        if self._event_batches is None:
            return self._events
        if self._stream_count >= len(self._event_batches):
            return [ProviderStreamEvent(type="done")]
        events = self._event_batches[self._stream_count]
        self._stream_count += 1
        return events
