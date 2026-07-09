from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class OuroModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Metadata = dict[str, Any]


def metadata_field() -> Metadata:
    return {}


MetadataField = Field(default_factory=metadata_field)
