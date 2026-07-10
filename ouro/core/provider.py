from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.messages import MessageRecord

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class ModelCost(OuroModel):
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


class ModelMetadata(OuroModel):
    model_id: str
    provider_id: str
    endpoint_id: str | None = None
    display_name: str | None = None
    context_window: int = 128_000
    max_output_tokens: int = 8192
    temperature: float | None = None
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = False
    supports_reasoning: bool = False
    supported_thinking_levels: tuple[ThinkingLevel, ...] = ()
    default_thinking_level: ThinkingLevel | None = None
    cost: ModelCost | None = None
    metadata: Metadata = MetadataField


class Usage(OuroModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost: float | None = None


class ProviderCheckResult(OuroModel):
    provider_id: str
    ok: bool
    model_id: str | None = None
    message: str = ""
    metadata: Metadata = MetadataField


class ProviderStreamEvent(OuroModel):
    type: Literal["text_delta", "reasoning_delta", "tool_call", "done", "error"]
    text: str | None = None
    reasoning_content: str | None = None
    tool_call: dict[str, Any] | None = None
    usage: Usage | None = None
    error: str | None = None


class Provider(Protocol):
    provider_id: str

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult: ...

    def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[MessageRecord],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]: ...
