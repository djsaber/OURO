from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ouro.core.base import OuroModel, VersionedOuroModel, utc_now

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z")

ResourceSourceKind = Literal["import", "tool"]


class ResourceSource(OuroModel):
    """Non-path provenance safe to expose through application DTOs."""

    kind: ResourceSourceKind = "import"
    name: str
    source_id: str | None = None

    @model_validator(mode="after")
    def validate_name(self) -> Self:
        if not self.name.strip():
            raise ValueError("resource source name cannot be empty")
        if "/" in self.name or "\\" in self.name:
            raise ValueError("resource source name must not contain a path")
        if self.source_id is not None and ("/" in self.source_id or "\\" in self.source_id):
            raise ValueError("resource source_id must not contain a path")
        return self


class ResourceRecord(VersionedOuroModel):
    """Canonical identity and metadata for one immutable content blob."""

    resource_id: str
    digest: str
    media_type: str
    size: int = Field(ge=0)
    source: ResourceSource
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("resource digest must be a lowercase sha256 digest")
        if self.resource_id != f"res_{self.digest.removeprefix('sha256:')}":
            raise ValueError("resource id must match its sha256 digest")
        if not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("resource media_type must use type/subtype syntax")
        return self


class SessionResourceReference(OuroModel):
    """Canonical relationship between a Session and a shared resource identity."""

    resource_id: str
    attached_at: datetime = Field(default_factory=utc_now)
    copied_from_session_id: str | None = None


class SessionResourceReferences(OuroModel):
    resources: list[SessionResourceReference] = Field(default_factory=list)
