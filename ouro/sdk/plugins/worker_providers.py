from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from .content import ToolAudioContent, ToolImageContent, ToolTextContent, ToolVideoContent
from .models import ModelReasoning
from .providers import (
    ProviderConnection,
    ProviderContinuation,
    ProviderContribution,
    ProviderEvent,
    ProviderMessage,
    ProviderModel,
    ProviderRequest,
    ProviderRetryPolicy,
    ProviderSkillContent,
    ProviderToolCall,
    ProviderToolProducer,
    ProviderToolResult,
)
from .worker import WorkerPeer


class WorkerProviders:
    """Expose protocol handlers through the same worker peer as other contributions."""

    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer
        self._providers: dict[str, ProviderContribution] = {}
        peer.handlers["providers.stream"] = self._stream
        peer.handlers["providers.check"] = self._check

    def register(self, contributions: Sequence[ProviderContribution]) -> list[str]:
        for item in contributions:
            if item.id in self._providers:
                raise ValueError(f"duplicate Provider: {item.id}")
            self._providers[item.id] = item
        return list(self._providers)

    async def _stream(self, params: Mapping[str, Any]) -> None:
        async def output(event: ProviderEvent) -> None:
            await self.peer.request("providers.output", {"event": asdict(event)}, timeout=0)

        await self._providers[params["id"]].stream(_request(params["request"]), output)

    async def _check(self, params: Mapping[str, Any]) -> object:
        return asdict(await self._providers[params["id"]].check(_request(params["request"])))


def _request(raw: Mapping[str, Any]) -> ProviderRequest:
    connection = dict(raw["connection"])
    connection["retry"] = ProviderRetryPolicy(**connection["retry"])
    model = dict(raw["model"])
    model["reasoning"] = ModelReasoning(**model["reasoning"])
    model["input_modalities"] = tuple(model["input_modalities"])
    return ProviderRequest(
        connection=ProviderConnection(**connection),
        model=ProviderModel(**model),
        system_prompt=raw["system_prompt"],
        messages=tuple(_message(item) for item in raw["messages"]),
        tools=tuple(raw["tools"]),
        context=dict(raw["context"]),
    )


def _message(raw: Mapping[str, Any]) -> ProviderMessage:
    blocks: list[
        ToolTextContent
        | ToolImageContent
        | ToolAudioContent
        | ToolVideoContent
        | ProviderSkillContent
    ] = []
    for value in raw["content"]:
        match value["type"]:
            case "text":
                blocks.append(ToolTextContent(**value))
            case "image":
                blocks.append(ToolImageContent(**value))
            case "audio":
                blocks.append(ToolAudioContent(**value))
            case "video":
                blocks.append(ToolVideoContent(**value))
            case "skill_activation":
                blocks.append(ProviderSkillContent(**value))
            case _:
                raise ValueError("unknown Provider content type")
    result = raw["tool_result"]
    continuation = raw["provider_continuation"]
    return ProviderMessage(
        role=raw["role"],
        content=tuple(blocks),
        semantic_tag=raw["semantic_tag"],
        tool_calls=tuple(ProviderToolCall(**item) for item in raw["tool_calls"]),
        tool_result=ProviderToolResult(
            result["tool_call_id"],
            result["tool_name"],
            result["is_error"],
            ProviderToolProducer(**result["producer"]),
        )
        if result is not None
        else None,
        provider_continuation=ProviderContinuation(
            continuation["provider_id"],
            continuation["protocol"],
            continuation["model_id"],
            tuple(continuation["blocks"]),
        )
        if continuation is not None
        else None,
    )
