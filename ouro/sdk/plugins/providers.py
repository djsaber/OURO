"""Protocol implementations for configured model routes, independent of the Host."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from .content import ToolAudioContent, ToolImageContent, ToolTextContent, ToolVideoContent
from .models import ModelReasoning, ModelUsage
from .state import JsonValue


@dataclass(frozen=True)
class ProviderContinuation:
    provider_id: str
    protocol: str
    model_id: str
    blocks: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True)
class ProviderSkillContent:
    name: str
    source: Literal["user", "workspace", "plugin"]
    location: str
    base_directory: str
    instructions: str
    content_digest: str
    type: Literal["skill_activation"] = "skill_activation"


@dataclass(frozen=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, JsonValue]
    source_index: int = 0


@dataclass(frozen=True)
class ProviderToolProducer:
    kind: Literal["model", "driver"] = "model"
    driver_id: str | None = None


@dataclass(frozen=True)
class ProviderToolResult:
    tool_call_id: str
    tool_name: str
    is_error: bool = False
    producer: ProviderToolProducer = ProviderToolProducer()


@dataclass(frozen=True)
class ProviderMessage:
    role: Literal["system", "user", "assistant", "tool_result"]
    content: tuple[
        ToolTextContent
        | ToolImageContent
        | ToolAudioContent
        | ToolVideoContent
        | ProviderSkillContent,
        ...,
    ] = ()
    semantic_tag: str | None = None
    tool_calls: tuple[ProviderToolCall, ...] = ()
    tool_result: ProviderToolResult | None = None
    provider_continuation: ProviderContinuation | None = None


@dataclass(frozen=True)
class ProviderModel:
    provider_id: str
    model_id: str
    endpoint_id: str | None
    context_window: int
    max_output_tokens: int | None
    temperature: float | None
    supports_tools: bool
    supports_streaming: bool
    supports_structured_output: bool
    input_modalities: tuple[Literal["image", "audio", "video"], ...]
    reasoning: ModelReasoning
    reasoning_output: Literal["none", "summary", "text"]
    reasoning_continuation: Literal["none", "text", "opaque"]


@dataclass(frozen=True)
class ProviderRetryPolicy:
    max_attempts: int = 5
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    backoff_multiplier: float = 2.0


@dataclass(frozen=True)
class ProviderConnection:
    protocol: str
    base_url: str | None = None
    api_path: str | None = None
    api_key: str | None = field(default=None, repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    timeout_seconds: float = 60.0
    retry: ProviderRetryPolicy = ProviderRetryPolicy()
    request_options: dict[str, JsonValue] = field(default_factory=dict)
    reasoning_profile: dict[str, JsonValue] | None = None


@dataclass(frozen=True)
class ProviderRequest:
    connection: ProviderConnection
    model: ProviderModel
    system_prompt: str = ""
    messages: tuple[ProviderMessage, ...] = ()
    tools: tuple[dict[str, JsonValue], ...] = ()
    context: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderToolDelta:
    index: int
    id_delta: str = ""
    name_delta: str = ""
    arguments_delta: str = ""


@dataclass(frozen=True)
class ProviderCompletion:
    finish_reason: Literal["stop", "length", "tool_call", "content_filter", "other", "unknown"] = (
        "unknown"
    )
    provider_finish_reason: str | None = None
    has_reasoning: bool = False


@dataclass(frozen=True)
class ProviderRetryNotice:
    attempt: int
    max_attempts: int
    delay_seconds: float
    reason: str


@dataclass(frozen=True)
class ProviderEvent:
    type: Literal[
        "text_delta",
        "reasoning_delta",
        "continuation",
        "tool_call_delta",
        "retry_scheduled",
        "done",
        "error",
    ]
    text: str | None = None
    reasoning_content: str | None = None
    continuation: ProviderContinuation | None = None
    tool_call_delta: ProviderToolDelta | None = None
    usage: ModelUsage | None = None
    completion: ProviderCompletion | None = None
    retry: ProviderRetryNotice | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ProviderCheck:
    ok: bool
    message: str = ""
    error_code: str | None = None


ProviderOutput = Callable[[ProviderEvent], Awaitable[None]]
ProviderStream = Callable[[ProviderRequest, ProviderOutput], Awaitable[None]]
ProviderCheckHandler = Callable[[ProviderRequest], Awaitable[ProviderCheck]]


@dataclass(frozen=True)
class ProviderContribution:
    id: str
    stream: ProviderStream
    check: ProviderCheckHandler
