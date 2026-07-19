from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.messages import ModelMessage, ProviderContinuationArtifact
from ouro.core.reasoning import (
    ReasoningCapability,
    ReasoningRequest,
    ReasoningResolution,
    resolve_reasoning,
)
from ouro.core.tools import ToolCallRequest


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
    context_window: int = Field(default=128_000, ge=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    temperature: float | None = None
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = False
    reasoning: ReasoningCapability = Field(default_factory=ReasoningCapability)
    reasoning_request: ReasoningRequest = Field(default_factory=ReasoningRequest)
    cost: ModelCost | None = None
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def _validate_token_limits(self) -> ModelMetadata:
        if self.max_output_tokens > self.context_window:
            raise ValueError("max_output_tokens must not exceed context_window")
        resolve_reasoning(self.reasoning, self.reasoning_request)
        return self

    @property
    def reasoning_resolution(self) -> ReasoningResolution:
        return resolve_reasoning(self.reasoning, self.reasoning_request)


class Usage(OuroModel):
    """Canonical token usage decoded from a provider response.

    This type must never be populated from OURO's context-size estimator.
    ``estimated_cost``, when supplied by an adapter, is denominated in USD.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)


class ProviderRetryPolicy(OuroModel):
    """Bounded retry policy for transient provider transport failures."""

    max_attempts: int = Field(default=1, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=0.5, ge=0, le=60)
    max_backoff_seconds: float = Field(default=8.0, ge=0, le=300)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)

    @model_validator(mode="after")
    def _validate_backoff(self) -> ProviderRetryPolicy:
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("initial_backoff_seconds must not exceed max_backoff_seconds")
        return self

    def delay_before_attempt(self, attempt: int) -> float:
        if attempt < 2:
            raise ValueError("retry attempt must be at least 2")
        delay = self.initial_backoff_seconds * self.backoff_multiplier ** (attempt - 2)
        return min(delay, self.max_backoff_seconds)


class ProviderRetryNotice(OuroModel):
    attempt: int = Field(ge=2)
    max_attempts: int = Field(ge=2)
    delay_seconds: float = Field(ge=0)
    reason: str


class ProviderCheckResult(OuroModel):
    provider_id: str
    ok: bool
    model_id: str | None = None
    message: str = ""
    metadata: Metadata = MetadataField


class ToolCallDelta(OuroModel):
    """Provider-neutral fragment of one streamed tool call."""

    index: int = Field(ge=0)
    id_delta: str = ""
    name_delta: str = ""
    arguments_delta: str = ""

    @model_validator(mode="after")
    def _require_content(self) -> ToolCallDelta:
        if not (self.id_delta or self.name_delta or self.arguments_delta):
            raise ValueError("tool call delta must contain at least one fragment")
        return self


class ProviderStreamEvent(OuroModel):
    type: Literal[
        "text_delta",
        "reasoning_delta",
        "continuation",
        "tool_call_delta",
        "tool_call",
        "retry_scheduled",
        "done",
        "error",
    ]
    text: str | None = None
    reasoning_content: str | None = None
    continuation: ProviderContinuationArtifact | None = None
    tool_call_delta: ToolCallDelta | None = None
    tool_call: ToolCallRequest | None = None
    usage: Usage | None = None
    retry: ProviderRetryNotice | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> ProviderStreamEvent:
        required_payloads = {
            "text_delta": self.text,
            "reasoning_delta": self.reasoning_content,
            "continuation": self.continuation,
            "tool_call_delta": self.tool_call_delta,
            "tool_call": self.tool_call,
            "retry_scheduled": self.retry,
            "error": self.error,
        }
        if self.type in required_payloads and required_payloads[self.type] is None:
            raise ValueError(f"provider stream event {self.type!r} is missing its payload")
        if self.type == "done" and any(
            value is not None
            for value in (
                self.text,
                self.reasoning_content,
                self.continuation,
                self.tool_call_delta,
                self.tool_call,
                self.retry,
                self.error,
            )
        ):
            raise ValueError("provider stream done event may only contain usage")
        if self.type != "done" and self.usage is not None:
            raise ValueError("provider usage may only be emitted on a done event")
        return self


class Provider(Protocol):
    provider_id: str

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult: ...

    def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]: ...
