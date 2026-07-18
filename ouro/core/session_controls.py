from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ouro.core.base import OuroModel

SessionInputMode = Literal["follow_up", "steer"]


class SessionInputRequest(OuroModel):
    """A request for the application layer to add one session input.

    Extensions describe the requested input, while the application runtime
    remains the only owner of persistence, locking, and execution.
    """

    prompt: str = Field(min_length=1)
    mode: SessionInputMode = "follow_up"
    title: str | None = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session input prompt cannot be blank")
        return value


class AttributedSessionInput(SessionInputRequest):
    """A request bound to the extension generation that produced it."""

    plugin_id: str
    contribution_id: str
    generation_id: str
    execution_contract_hash: str


class HostSessionInput(SessionInputRequest):
    """A synthetic input produced by an OURO Host/Application component."""

    host_source_id: str = Field(min_length=1)

    @field_validator("host_source_id")
    @classmethod
    def validate_host_source_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("host session input source id cannot be blank")
        return normalized


__all__ = [
    "AttributedSessionInput",
    "HostSessionInput",
    "SessionInputMode",
    "SessionInputRequest",
]
