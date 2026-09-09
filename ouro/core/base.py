from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION: Final = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class OuroModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class VersionedOuroModel(OuroModel):
    """Current core record shape written as an independent persisted artifact."""

    schema_version: int = SCHEMA_VERSION

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_current_schema(cls, value: object) -> int:
        expected = cls.model_fields["schema_version"].default
        if type(value) is not int or value != expected:
            raise ValueError(f"schema_version must be {expected}")
        return value


Metadata = dict[str, Any]


def metadata_field() -> Metadata:
    return {}


MetadataField = Field(default_factory=metadata_field)
