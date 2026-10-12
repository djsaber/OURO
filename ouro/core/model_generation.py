from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from ouro.core.base import OuroModel
from ouro.core.provider import Usage
from ouro.core.reasoning import ReasoningRequest


class ModelInput(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["system", "user", "assistant"]
    text: str


class ModelGenerationRequest(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    messages: tuple[ModelInput, ...] = Field(min_length=1)
    model: str = Field(default="default", min_length=1)
    system_prompt: str = ""
    reasoning: ReasoningRequest = Field(default_factory=ReasoningRequest)
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)


@dataclass(frozen=True)
class ModelTextDelta:
    sequence: int
    kind: Literal["text", "reasoning"]
    text: str


@dataclass(frozen=True)
class ModelGenerationResult:
    request_id: str
    model: str
    text: str
    reasoning: str
    usage: Usage | None


class ModelGenerationPort(Protocol):
    async def generate(
        self,
        request: ModelGenerationRequest,
        *,
        plugin_id: str,
        session_id: str | None,
        workspace: str | None,
        on_output: Callable[[ModelTextDelta], Awaitable[None]] | None = None,
    ) -> ModelGenerationResult: ...
