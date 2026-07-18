from __future__ import annotations

from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel

RunBudgetReason = Literal[
    "model_rounds",
    "input_tokens",
    "output_tokens",
    "elapsed_time",
]
RunPauseReason = RunBudgetReason | Literal["context_blocked", "turn_policy_blocked"]


class RunBudgetPolicy(OuroModel):
    """Optional soft limits checked only at durable model-round boundaries."""

    max_model_rounds: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_elapsed_seconds: float | None = Field(default=None, gt=0)

    def exceeded_reason(
        self,
        *,
        next_model_round: int,
        input_tokens: int,
        output_tokens: int,
        elapsed_seconds: float,
    ) -> RunBudgetReason | None:
        if self.max_model_rounds is not None and next_model_round > self.max_model_rounds:
            return "model_rounds"
        if self.max_input_tokens is not None and input_tokens >= self.max_input_tokens:
            return "input_tokens"
        if self.max_output_tokens is not None and output_tokens >= self.max_output_tokens:
            return "output_tokens"
        if self.max_elapsed_seconds is not None and elapsed_seconds >= self.max_elapsed_seconds:
            return "elapsed_time"
        return None
