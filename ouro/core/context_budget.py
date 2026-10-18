from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from ouro.core.base import OuroModel


class ContextBudget(OuroModel):
    """Local input capacity for one request, reserving its configured output limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_window: int = Field(ge=1, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def _require_input_capacity(self) -> ContextBudget:
        if self.reserved_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens must be lower than context_window")
        return self

    @property
    def reserved_output_tokens(self) -> int:
        return self.max_output_tokens or 0

    @property
    def input_tokens(self) -> int:
        return self.context_window - self.reserved_output_tokens
