from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from typing import cast

from .content import ToolAudioContent, ToolImageContent, ToolTextContent, ToolVideoContent
from .run_drivers import (
    DriverFinished,
    DriverResume,
    DriverSuspended,
    ModelResponse,
    ModelToolCall,
    NativeToolResult,
    RunAgents,
    RunDriverContribution,
    RunInput,
    RunInputs,
    RunModels,
    RunToolCall,
    RunTools,
    ToolBatchResult,
)
from .state import JsonValue
from .worker import WorkerPeer


class WorkerRunPorts:
    """Use native ports attached by the Host to the current incoming call."""

    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer

    @property
    def models(self) -> RunModels:
        return self

    @property
    def tools(self) -> RunTools:
        return self

    @property
    def agents(self) -> RunAgents:
        return RunAgents(self)

    @property
    def inputs(self) -> RunInputs:
        return self

    async def consume(self) -> tuple[RunInput, ...]:
        raw = cast(list[dict], await self.peer.request("run.inputs.consume", {}, timeout=0))
        return tuple(
            RunInput(
                item["message_id"],
                item["role"],
                tuple(item["content"]),
                item["scaffold"],
                item["semantic_tag"],
                item["metadata"],
                tuple(item["resource_refs"]),
            )
            for item in raw
        )

    async def request(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> ModelResponse:
        raw = cast(
            dict,
            await self.peer.request(
                "run.models.request",
                {"prompt": prompt, "tools": list(tools) if tools is not None else None},
                timeout=0,
            ),
        )
        return ModelResponse(
            raw["response_id"],
            raw["text"],
            raw["reasoning"],
            tuple(ModelToolCall(**item) for item in raw["tool_calls"]),
        )

    async def execute(
        self, calls: tuple[RunToolCall, ...], *, checkpoint: JsonValue
    ) -> ToolBatchResult:
        raw = cast(
            dict,
            await self.peer.request(
                "run.tools.execute",
                {"calls": [asdict(item) for item in calls], "checkpoint": checkpoint},
                timeout=0,
            ),
        )
        return ToolBatchResult(
            raw["suspended"], tuple(_tool_result(item) for item in raw["results"])
        )

    async def execute_model_response(
        self, response_id: str, *, checkpoint: JsonValue
    ) -> ToolBatchResult:
        raw = cast(
            dict,
            await self.peer.request(
                "run.tools.execute_model_response",
                {"response_id": response_id, "checkpoint": checkpoint},
                timeout=0,
            ),
        )
        return ToolBatchResult(
            raw["suspended"], tuple(_tool_result(item) for item in raw["results"])
        )


class WorkerRunContext(WorkerRunPorts):
    def __init__(self, peer: WorkerPeer, session_id: str, run_id: str) -> None:
        super().__init__(peer)
        self.session_id = session_id
        self.run_id = run_id

    @property
    def is_aborted(self) -> bool:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0


class WorkerRunDrivers:
    def __init__(self, peer: WorkerPeer, drivers: list[RunDriverContribution]) -> None:
        self.peer = peer
        self.drivers = {item.id: item.handler for item in drivers}

    async def dispatch(self, method: str, params: Mapping[str, object]) -> object:
        if method != "drivers.call":
            raise ValueError(f"unknown Driver method: {method}")
        raw = cast(dict | None, params.get("resume"))
        resume = (
            DriverResume(raw["checkpoint"], tuple(_tool_result(item) for item in raw["results"]))
            if raw is not None
            else None
        )
        context = WorkerRunContext(self.peer, str(params["session_id"]), str(params["run_id"]))
        result = await self.drivers[str(params["id"])](context, resume)
        if isinstance(result, DriverFinished):
            return {"status": "finished", "text": result.text, "response_id": result.response_id}
        if isinstance(result, DriverSuspended):
            return {"status": "suspended"}
        raise TypeError("Run driver must return DriverFinished or DriverSuspended")


def _tool_result(raw: dict) -> NativeToolResult:
    content = raw["content"]
    if not isinstance(content, str):
        content = tuple(
            ToolTextContent(text=item["text"])
            if item["type"] == "text"
            else {
                "image": ToolImageContent,
                "audio": ToolAudioContent,
                "video": ToolVideoContent,
            }[item["type"]](uri=item["uri"], media_type=item["media_type"])
            for item in content
        )
    return NativeToolResult(
        raw["tool_call_id"],
        raw["tool_name"],
        content,
        raw["is_error"],
        raw["structured"],
        tuple(raw["resource_refs"]),
    )
