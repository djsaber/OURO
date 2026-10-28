from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, field_validator

from ouro.core.base import OuroModel
from ouro.core.billing import BillingMode, ProviderUsageEntry, UsageFilter, UsageLedgerEvent

UsageGroupBy = Literal[
    "date",
    "provider",
    "endpoint",
    "model",
    "workspace",
    "session",
    "run",
    "purpose",
    "plugin",
    "billing_mode",
]


class UsageQueryResult(OuroModel):
    """Host-neutral billing query values shared by storage and application services."""

    model_config = ConfigDict(frozen=True)


class UsageTotals(UsageQueryResult):
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost: float = 0.0
    priced_request_count: int = 0
    unpriced_request_count: int = 0
    billing_modes: tuple[BillingMode, ...] = ()


class RunUsageTotals(UsageTotals):
    """Raw consumption and report counts by purpose, independent of billing edits."""

    agent_request_count: int = 0
    compaction_request_count: int = 0


class UsageSummaryGroup(UsageQueryResult):
    key: tuple[str | None, ...]
    totals: UsageTotals


class UsageSummary(UsageQueryResult):
    usage_filter: UsageFilter
    totals: UsageTotals
    group_by: tuple[UsageGroupBy, ...] = ()
    groups: tuple[UsageSummaryGroup, ...] = ()


class UsageEntryCursor(UsageQueryResult):
    occurred_at: datetime
    entry_id: str

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("usage cursor timestamps must include a timezone")
        return value


class UsageEntryList(UsageQueryResult):
    usage_filter: UsageFilter
    entries: tuple[ProviderUsageEntry, ...] = ()
    next_cursor: UsageEntryCursor | None = None
    total_matching: int = 0
    truncated: bool = False


class UsageMutationResult(UsageQueryResult):
    action: Literal["replace", "reset"]
    affected_count: int
    affected_totals: UsageTotals
    entry: ProviderUsageEntry | None = None


class UsageAuditLog(UsageQueryResult):
    events: tuple[UsageLedgerEvent, ...] = ()
    next_cursor: int | None = None
    total_events: int = 0
    truncated: bool = False
