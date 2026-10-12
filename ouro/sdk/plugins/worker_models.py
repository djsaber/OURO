"""Register output as models.output to consume generation chunks with backpressure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .models import ModelOutput, ModelRequest, ModelResult, ModelUsage
from .run_drivers import ModelResponse
from .worker import WorkerPeer
from .worker_drivers import WorkerRunPorts


class WorkerModels:
    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer
        self._run = WorkerRunPorts(peer)
        self.handlers: dict[str, Callable[[ModelOutput], Awaitable[None]]] = {}

    async def request(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> ModelResponse:
        return await self._run.request(prompt=prompt, tools=tools)

    async def generate(
        self,
        request: ModelRequest,
        *,
        on_output: Callable[[ModelOutput], Awaitable[None]] | None = None,
    ) -> ModelResult:
        identity = uuid4().hex if on_output is not None else None
        if identity is not None and on_output is not None:
            self.handlers[identity] = on_output
        try:
            raw = await self.peer.request(
                "models.generate",
                {"request": asdict(request), "handler_id": identity},
                timeout=0,
            )
            return ModelResult(
                raw["request_id"],
                raw["model"],
                raw["text"],
                raw["reasoning"],
                ModelUsage(**raw["usage"]) if raw["usage"] is not None else None,
            )
        finally:
            if identity is not None:
                del self.handlers[identity]

    async def output(self, params: Mapping[str, Any]) -> None:
        await self.handlers[params["handler_id"]](ModelOutput(**params["output"]))
