from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from pydantic import JsonValue, TypeAdapter

from ouro.plugins.worker_services import WorkerRequests
from ouro.sdk.plugins import run_drivers as sdk

_CHECKPOINT: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_CALLS: TypeAdapter[tuple[sdk.RunToolCall, ...]] = TypeAdapter(tuple[sdk.RunToolCall, ...])


class WorkerDriverBridge:
    """Bind reverse RPC to the inherited Run, never a peer-supplied Run identity."""

    def __init__(
        self,
        client: WorkerRequests,
        *,
        models: sdk.RunModels | None = None,
        tools: sdk.RunTools | None = None,
        inputs: sdk.RunInputs | None = None,
    ) -> None:
        self.client = client
        self.models, self.tools = models, tools
        self.inputs = inputs

    def handler(self, driver_id: str) -> sdk.RunDriverHandler:
        async def invoke(
            context: sdk.RunDriverContext, resume: sdk.DriverResume | None
        ) -> sdk.DriverFinished | sdk.DriverSuspended:
            result = await self.client.request(
                "drivers.call",
                {
                    "id": driver_id,
                    "session_id": context.session_id,
                    "run_id": context.run_id,
                    "resume": asdict(resume) if resume is not None else None,
                },
                timeout=0,
            )
            if not isinstance(result, dict):
                raise ValueError("worker Driver result must be an object")
            if result.get("status") == "finished" and isinstance(result.get("text"), str):
                response_id = result.get("response_id")
                if response_id is not None and not isinstance(response_id, str):
                    raise ValueError("response_id must be a string")
                return sdk.DriverFinished(result["text"], response_id=response_id)
            if result.get("status") == "suspended":
                return sdk.DriverSuspended()
            raise ValueError("invalid worker Driver result")

        return invoke

    async def dispatch(self, method: str, params: Mapping[str, object]) -> object:
        if method == "run.inputs.consume":
            if self.inputs is None:
                raise RuntimeError("native Run inputs are unavailable")
            return [asdict(item) for item in await self.inputs.consume()]
        if self.models is None or self.tools is None:
            raise RuntimeError("native Run ports are unavailable")
        if method == "run.models.request":
            prompt = params.get("prompt", "")
            tools = params.get("tools")
            if not isinstance(prompt, str) or (
                tools is not None
                and (
                    not isinstance(tools, list) or not all(isinstance(item, str) for item in tools)
                )
            ):
                raise ValueError("invalid model request")
            return asdict(
                await self.models.request(
                    prompt=prompt, tools=tuple(tools) if tools is not None else None
                )
            )
        if method == "run.tools.execute":
            return asdict(
                await self.tools.execute(
                    _CALLS.validate_python(params["calls"]),
                    checkpoint=_CHECKPOINT.validate_python(params["checkpoint"]),
                )
            )
        if method == "run.tools.execute_model_response":
            identity = params.get("response_id")
            if not isinstance(identity, str):
                raise ValueError("response_id must be a string")
            return asdict(
                await self.tools.execute_model_response(
                    identity, checkpoint=_CHECKPOINT.validate_python(params["checkpoint"])
                )
            )
        raise ValueError(f"unknown native Run method: {method}")
