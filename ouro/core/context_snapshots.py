from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from pydantic import Field, model_validator

from ouro.core.base import Metadata, MetadataField, VersionedOuroModel, new_id, utc_now
from ouro.core.context import ContextMetrics, ContextView
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.tools import ToolDefinition

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ContextSnapshotRecord(VersionedOuroModel):
    """Lossless latest-request snapshot retaining the exact text and image bytes sent."""

    schema_version: int = 3

    context_id: str = Field(default_factory=lambda: new_id("ctx"))
    session_id: str
    run_id: str
    turn_id: str
    model_round: int = Field(ge=1)
    provider_id: str
    endpoint_id: str | None = None
    model_id: str
    supports_vision: bool = False
    prepared_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    system_prompt: str = ""
    injected_messages: list[ModelMessage] = Field(default_factory=list)
    source_messages: list[MessageRecord] = Field(default_factory=list)
    tool_definitions: list[ToolDefinition] = Field(default_factory=list)
    metrics: ContextMetrics
    context_metadata: Metadata = MetadataField
    context_digest: str
    provider_reported_input_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_identity_and_digest(self) -> ContextSnapshotRecord:
        if not all(
            value.strip()
            for value in (
                self.context_id,
                self.session_id,
                self.run_id,
                self.turn_id,
                self.provider_id,
                self.model_id,
            )
        ):
            raise ValueError("context snapshot identity fields must not be empty")
        if not _DIGEST.fullmatch(self.context_digest):
            raise ValueError("context snapshot digest must be a lowercase sha256 digest")
        if self.completed_at is None and self.provider_reported_input_tokens is not None:
            raise ValueError("provider input tokens require a completed_at timestamp")
        return self


def context_view_digest(view: ContextView) -> str:
    """Digest the provider-neutral arguments represented by one context view."""

    payload = view.provider_context().model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = ["ContextSnapshotRecord", "context_view_digest"]
