from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ouro.core.base import VersionedOuroModel, utc_now
from ouro.core.message_semantics import SemanticTag

SessionExtensionBranchPolicy = Literal["copy", "reset", "reject"]

_OWNER_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")


class SessionExtensionStateRecord(VersionedOuroModel):
    """One owner-scoped state value governed by the Session lifecycle."""

    session_id: str
    owner_id: str
    state_schema_version: int = Field(ge=1)
    branch_policy: SessionExtensionBranchPolicy = "reset"
    projection_semantic_tags: tuple[SemanticTag, ...] = ()
    payload: JsonValue
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("owner_id")
    @classmethod
    def validate_owner_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _OWNER_ID.fullmatch(normalized) is None:
            raise ValueError("Session extension owner_id must be a stable dotted identifier")
        return normalized

    @model_validator(mode="after")
    def validate_projection_tags(self) -> Self:
        if len(set(self.projection_semantic_tags)) != len(self.projection_semantic_tags):
            raise ValueError("Session extension projection semantic tags must be unique")
        return self


class SessionExtensionStateCollection(VersionedOuroModel):
    """Versioned local container for all extension states owned by one Session."""

    states: list[SessionExtensionStateRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_owner_uniqueness(self) -> Self:
        owners = [state.owner_id for state in self.states]
        if len(set(owners)) != len(owners):
            raise ValueError("Session extension state owners must be unique")
        session_ids = {state.session_id for state in self.states}
        if len(session_ids) > 1:
            raise ValueError("Session extension state collection cannot mix Sessions")
        return self


__all__ = [
    "SessionExtensionBranchPolicy",
    "SessionExtensionStateCollection",
    "SessionExtensionStateRecord",
]
