from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .processes import ProcessOutput, ProcessRequest, ProcessResult
from .worker import WorkerPeer


class WorkerProcesses:
    """Register output as processes.output to receive ordered, bounded process chunks."""

    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer
        self.handlers: dict[str, Callable[[ProcessOutput], Awaitable[None]]] = {}

    async def run(
        self,
        request: ProcessRequest,
        *,
        on_output: Callable[[ProcessOutput], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        identity = uuid4().hex if on_output is not None else None
        if identity is not None and on_output is not None:
            self.handlers[identity] = on_output
        try:
            result = await self.peer.request(
                "processes.run",
                {"request": asdict(request), "handler_id": identity},
                timeout=request.timeout_seconds + 10,
            )
            return ProcessResult(**result)
        finally:
            if identity is not None:
                del self.handlers[identity]

    async def output(self, params: Mapping[str, Any]) -> None:
        await self.handlers[params["handler_id"]](ProcessOutput(**params["output"]))
