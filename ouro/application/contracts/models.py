"""Model availability and reasoning views for application consumers."""

from __future__ import annotations

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.context_budget import ContextBudget
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest


class AvailableModel(OuroModel):
    """Stable model route plus the capabilities needed by interface controls."""

    key: str
    provider_id: str
    provider_display_name: str | None = None
    endpoint_id: str
    model_id: str
    display_name: str | None = None
    temperature: float | None = None
    context_window: int = Field(default=128_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    reasoning: ReasoningCapability = Field(default_factory=ReasoningCapability)

    @property
    def context_budget(self) -> ContextBudget:
        return ContextBudget(
            context_window=self.context_window, max_output_tokens=self.max_output_tokens
        )


class ModelReasoningState(OuroModel):
    """Endpoint capability and resolved Session intent exposed to interface layers."""

    model: AvailableModel
    capability: ReasoningCapability
    requested: ReasoningRequest
    effective: ReasoningRequest
