from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import asdict

from pydantic import TypeAdapter

from ouro.plugins.worker.services import WorkerRequests
from ouro.sdk.plugins.providers import (
    ProviderCheck,
    ProviderContribution,
    ProviderEvent,
    ProviderOutput,
    ProviderRequest,
)

_EVENT = TypeAdapter(ProviderEvent)
_CHECK = TypeAdapter(ProviderCheck)
_OUTPUT: ContextVar[ProviderOutput | None] = ContextVar("ouro_provider_output", default=None)


class WorkerProviderBridge:
    """Await each reverse output call to preserve order and bound transport buffering."""

    def __init__(self, client: WorkerRequests) -> None:
        self.client = client

    def contribution(self, identity: str) -> ProviderContribution:
        async def stream(request: ProviderRequest, output: ProviderOutput) -> None:
            token = _OUTPUT.set(output)
            try:
                await self.client.request(
                    "providers.stream", {"id": identity, "request": asdict(request)}, timeout=0
                )
            finally:
                _OUTPUT.reset(token)

        async def check(request: ProviderRequest) -> ProviderCheck:
            return _CHECK.validate_python(
                await self.client.request(
                    "providers.check", {"id": identity, "request": asdict(request)}, timeout=0
                )
            )

        return ProviderContribution(identity, stream, check)

    async def output(self, params: Mapping[str, object]) -> None:
        callback = _OUTPUT.get()
        if callback is None:
            raise RuntimeError("Provider output requires a live stream invocation")
        await callback(_EVENT.validate_python(params["event"]))
