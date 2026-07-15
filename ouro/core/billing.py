from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now

UsagePurpose = Literal["agent", "compaction"]
UsageLedgerEventType = Literal["record", "replace", "reset"]


class ProviderUsageEntry(OuroModel):
    """One provider-reported usage result for one completed model request.

    Context-size estimates never enter this model. Cache token fields preserve the
    provider adapter's canonical usage categories and are informational subsets or
    supplements of input usage according to that provider's wire contract.
    """

    schema_version: int = SCHEMA_VERSION
    entry_id: str
    occurred_at: datetime = Field(default_factory=utc_now)
    provider_id: str
    endpoint_id: str | None = None
    model_id: str
    session_id: str
    run_id: str
    run_kind: str
    purpose: UsagePurpose
    request_id: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    estimated_cost: float | None = Field(
        default=None,
        ge=0,
        description="Provider or manually supplied estimated cost in USD.",
    )
    revision: int = Field(default=1, ge=1)
    modified_at: datetime | None = None
    modification_note: str | None = None
    metadata: Metadata = MetadataField

    @field_validator(
        "entry_id",
        "provider_id",
        "model_id",
        "session_id",
        "run_id",
        "run_kind",
        "request_id",
    )
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("usage identifiers must not be empty")
        return normalized

    @field_validator("endpoint_id")
    @classmethod
    def _normalize_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("occurred_at", "modified_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("usage timestamps must include a timezone")
        return value

    @property
    def total_tokens(self) -> int:
        """Return primary input plus output tokens without double-counting cache detail."""

        return self.input_tokens + self.output_tokens


class UsageFilter(OuroModel):
    """Reusable inclusive/exclusive filter over effective provider usage entries."""

    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    provider_ids: frozenset[str] = Field(default_factory=frozenset)
    endpoint_ids: frozenset[str] = Field(default_factory=frozenset)
    model_ids: frozenset[str] = Field(default_factory=frozenset)
    session_ids: frozenset[str] = Field(default_factory=frozenset)
    run_ids: frozenset[str] = Field(default_factory=frozenset)
    purposes: frozenset[UsagePurpose] = Field(default_factory=frozenset)

    @field_validator("occurred_from", "occurred_to")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("usage filter timestamps must include a timezone")
        return value

    @field_validator(
        "provider_ids",
        "endpoint_ids",
        "model_ids",
        "session_ids",
        "run_ids",
        mode="before",
    )
    @classmethod
    def _normalize_values(cls, value: object) -> object:
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return frozenset({value.strip()}) if value.strip() else frozenset()
        if not isinstance(value, Iterable):
            return value
        return frozenset(str(item).strip() for item in value if str(item).strip())

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if (
            self.occurred_from is not None
            and self.occurred_to is not None
            and self.occurred_from >= self.occurred_to
        ):
            raise ValueError("occurred_from must be earlier than occurred_to")
        return self

    def matches(self, entry: ProviderUsageEntry) -> bool:
        if self.occurred_from is not None and entry.occurred_at < self.occurred_from:
            return False
        if self.occurred_to is not None and entry.occurred_at >= self.occurred_to:
            return False
        if self.provider_ids and entry.provider_id not in self.provider_ids:
            return False
        if self.endpoint_ids and entry.endpoint_id not in self.endpoint_ids:
            return False
        if self.model_ids and entry.model_id not in self.model_ids:
            return False
        if self.session_ids and entry.session_id not in self.session_ids:
            return False
        if self.run_ids and entry.run_id not in self.run_ids:
            return False
        return not self.purposes or entry.purpose in self.purposes


class UsageLedgerEvent(OuroModel):
    """Append-only mutation in the global provider usage ledger."""

    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: new_id("uev"))
    event_type: UsageLedgerEventType
    recorded_at: datetime = Field(default_factory=utc_now)
    entry_id: str | None = None
    entry: ProviderUsageEntry | None = None
    usage_filter: UsageFilter | None = None
    note: str | None = None

    @field_validator("recorded_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ledger timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_payload(self) -> Self:
        if self.event_type in {"record", "replace"}:
            if self.entry is None or self.usage_filter is not None:
                raise ValueError(f"{self.event_type} event requires only an entry")
            if self.entry_id is not None and self.entry_id != self.entry.entry_id:
                raise ValueError("ledger event entry_id does not match its entry")
            self.entry_id = self.entry.entry_id
        elif self.event_type == "reset":
            if self.usage_filter is None or self.entry is not None or self.entry_id is not None:
                raise ValueError("reset event requires only a usage_filter")
        return self
