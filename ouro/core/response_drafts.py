from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ouro.core.base import OuroModel


class ResponseDraftChunk(OuroModel):
    """One recoverable output fragment; tool arguments are never a Draft channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["text", "reasoning"]
    content: str = Field(min_length=1)


class ResponseDraftFragment(ResponseDraftChunk):
    """Read position, materialized with its content inside the read boundary."""

    cursor: int = Field(ge=1)


class ResponseStreamPosition(OuroModel):
    """Current request cursor; None marks a live-only tool argument observation."""

    context_id: str
    after_cursor: int = Field(default=0, ge=0)
    cursor: int | None = Field(default=None, ge=1)


class ResponseDraftPage(OuroModel):
    """Materialized fragments and a backend-local cursor for this exact request."""

    context_id: str
    chunks: tuple[ResponseDraftFragment, ...] = ()
    cursor: int = Field(default=0, ge=0)
