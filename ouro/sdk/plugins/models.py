"""Independent model generation and model calls belonging to the current Run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .run_drivers import RunModels


@dataclass(frozen=True)
class ModelInput:
    role: Literal["system", "user", "assistant"]
    text: str


@dataclass(frozen=True)
class ModelReasoning:
    mode: Literal["default", "enabled", "disabled"] = "default"
    effort: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelInput, ...]
    model: str = "default"
    system_prompt: str = ""
    reasoning: ModelReasoning = field(default_factory=ModelReasoning)
    max_output_tokens: int | None = None
    timeout_seconds: float = 120


@dataclass(frozen=True)
class ModelOutput:
    sequence: int
    kind: Literal["text", "reasoning"]
    text: str


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost: float | None = None


@dataclass(frozen=True)
class ModelResult:
    request_id: str
    model: str
    text: str
    reasoning: str
    usage: ModelUsage | None


class PluginModels(RunModels, Protocol):
    async def generate(
        self,
        request: ModelRequest,
        *,
        on_output: Callable[[ModelOutput], Awaitable[None]] | None = None,
    ) -> ModelResult: ...
