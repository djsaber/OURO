from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from pydantic import TypeAdapter

from ouro.plugins.worker_services import WorkerRequests
from ouro.sdk.plugins.models import ModelOutput, ModelRequest, PluginModels

_REQUEST = TypeAdapter(ModelRequest)


class WorkerModelBridge:
    def __init__(self, client: WorkerRequests, models: PluginModels) -> None:
        self.client = client
        self.models = models

    async def generate(self, params: Mapping[str, object]) -> object:
        request = _REQUEST.validate_python(params["request"])
        handler_id = params.get("handler_id")
        if handler_id is not None and (not isinstance(handler_id, str) or not handler_id):
            raise ValueError("Model output handler id must be a nonempty string")

        async def output(item: ModelOutput) -> None:
            await self.client.request(
                "models.output",
                {"handler_id": handler_id, "output": asdict(item)},
                timeout=0,
            )

        return asdict(
            await self.models.generate(
                request, on_output=output if handler_id is not None else None
            )
        )
