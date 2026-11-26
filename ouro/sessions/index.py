from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.session_queries import SessionHealth, SessionIndexEntry
from ouro.sessions.layout import DATA_LAYOUT_VERSION


def invalid_session_index_entry(
    payload: object,
    *,
    error: str,
    fallback_session_id: str,
    health: SessionHealth = "corrupt",
    error_scope: Literal["canonical", "catalog"] = "canonical",
) -> SessionIndexEntry:
    """Retain readable identity while keeping catalog and canonical errors distinct."""

    if isinstance(payload, dict):
        raw_session_kind = payload.get("session_kind")
        session_kind = raw_session_kind if raw_session_kind in ("primary", "subagent") else None
        candidate = {
            **payload,
            "session_kind": session_kind,
            "health": None if error_scope == "catalog" else health,
            "error_detail": error if error_scope == "canonical" else None,
            "catalog_error": error if error_scope == "catalog" else None,
        }
        if error_scope == "canonical":
            candidate["status"] = "failed"
        try:
            return SessionIndexEntry.model_validate(candidate)
        except ValidationError:
            pass
    else:
        payload = {}
        session_kind = None

    def identifier(field: str) -> str | None:
        value = payload.get(field)
        return value if isinstance(value, str) and value else None

    now = utc_now()
    identity = identifier("session_id") or fallback_session_id
    title = payload.get("title")
    return SessionIndexEntry(
        session_id=identity,
        session_kind=session_kind,
        root_session_id=identifier("root_session_id") or identity,
        parent_session_id=identifier("parent_session_id"),
        context_source_session_id=identifier("context_source_session_id"),
        title=(
            title
            if isinstance(title, str) and title
            else "Invalid session index entry"
            if error_scope == "catalog"
            else "Unsupported data version"
            if health == "unsupported_version"
            else "Corrupt session"
        ),
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        status="failed",
        health=None if error_scope == "catalog" else health,
        error_detail=error if error_scope == "canonical" else None,
        catalog_error=error if error_scope == "catalog" else None,
    )


class SessionIndex(OuroModel):
    """Versioned, disposable index document stored under ``sessions/index.json``."""

    data_layout_version: int = DATA_LAYOUT_VERSION
    generation: str = Field(default_factory=lambda: new_id("idx"))
    generated_at: datetime = Field(default_factory=utc_now)
    sessions: list[SessionIndexEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_current_layout(self) -> SessionIndex:
        if self.data_layout_version != DATA_LAYOUT_VERSION:
            raise ValueError(
                "session index layout version "
                f"{self.data_layout_version} is not supported; expected {DATA_LAYOUT_VERSION}"
            )
        return self
