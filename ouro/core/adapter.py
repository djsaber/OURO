from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Protocol

from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.errors import ConfigError
from ouro.core.provider import (
    AvailableModel,
    ModelMetadata,
    Provider,
    ProviderStreamEvent,
    model_route_key,
)


class RuntimeAdapter:
    def __init__(self, provider: Provider, model: ModelMetadata) -> None:
        self.provider = provider
        self.model = model

    async def stream(
        self,
        context: ContextView,
        *,
        signal: AbortSignal | None = None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        model = self.model
        if max_output_tokens is not None:
            if max_output_tokens < 1 or max_output_tokens > self.model.context_window:
                raise ValueError("max_output_tokens must be between 1 and the context window")
            model = self.model.model_copy(update={"max_output_tokens": max_output_tokens})
        provider_events = self.provider.stream(
            model=model,
            system_prompt=context.system_prompt,
            messages=list(context.model_messages),
            tools=[tool.model_dump() for tool in context.tool_definitions],
        )
        async for event in _interruptible(provider_events, signal=signal):
            yield event


class RuntimeAdapterCatalog(Protocol):
    """Resolve durable model route keys into request-local runtime adapters."""

    def list_models(self) -> list[AvailableModel]: ...

    def default_model(self) -> AvailableModel: ...

    def resolve(self, selector: str) -> AvailableModel: ...

    def adapter_for(self, selector: str) -> RuntimeAdapter: ...


class SingleRuntimeAdapterCatalog:
    """Compatibility catalog for hosts that inject one fixed adapter."""

    def __init__(self, adapter: RuntimeAdapter) -> None:
        self._adapter = adapter
        self._model = _available_model(adapter.model)

    def list_models(self) -> list[AvailableModel]:
        return [self._model]

    def default_model(self) -> AvailableModel:
        return self._model

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        aliases = {
            self._model.key,
            self._model.model_id,
            self._model.endpoint_id,
        }
        if normalized not in aliases:
            raise ConfigError(f"model not found: {selector}")
        return self._model

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        self.resolve(selector)
        return self._adapter


def _available_model(model: ModelMetadata) -> AvailableModel:
    return AvailableModel(
        key=model_route_key(model),
        provider_id=model.provider_id,
        endpoint_id=model.endpoint_id or model.model_id,
        model_id=model.model_id,
        display_name=model.display_name,
    )


async def _interruptible(
    events: AsyncIterator[ProviderStreamEvent],
    *,
    signal: AbortSignal | None,
    poll_interval: float = 0.05,
) -> AsyncIterator[ProviderStreamEvent]:
    if signal is None:
        async for event in events:
            yield event
        return

    try:
        while not signal.is_aborted():
            next_event = asyncio.ensure_future(anext(events))
            abort_wait = asyncio.create_task(_wait_for_abort(signal, poll_interval))
            done, _ = await asyncio.wait(
                {next_event, abort_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if abort_wait in done:
                next_event.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_event
                break

            abort_wait.cancel()
            with suppress(asyncio.CancelledError):
                await abort_wait

            if signal.is_aborted():
                break

            try:
                yield next_event.result()
            except StopAsyncIteration:
                break
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            with suppress(Exception):
                await close()


async def _wait_for_abort(signal: AbortSignal, poll_interval: float) -> None:
    while not signal.is_aborted():
        await asyncio.sleep(poll_interval)
