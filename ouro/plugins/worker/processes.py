from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from pydantic import TypeAdapter

from ouro.plugins.worker.services import WorkerRequests
from ouro.sdk.plugins.processes import PluginProcesses, ProcessOutput, ProcessRequest

_REQUEST = TypeAdapter(ProcessRequest)


class WorkerProcessBridge:
    def __init__(self, client: WorkerRequests, processes: PluginProcesses) -> None:
        self.client = client
        self.processes = processes

    async def run(self, params: Mapping[str, object]) -> object:
        request = _REQUEST.validate_python(params["request"])
        handler_id = params.get("handler_id")
        if handler_id is not None and (not isinstance(handler_id, str) or not handler_id):
            raise ValueError("process output handler id must be a nonempty string")

        async def output(item: ProcessOutput) -> None:
            await self.client.request(
                "processes.output", {"handler_id": handler_id, "output": asdict(item)}
            )

        return asdict(
            await self.processes.run(request, on_output=output if handler_id is not None else None)
        )
