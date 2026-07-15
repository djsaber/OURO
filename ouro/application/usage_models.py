from __future__ import annotations

from typing import Literal

from ouro.application.dto import ApplicationDTO
from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsageLedgerEvent

UsageGroupBy = Literal["date", "provider", "endpoint", "model", "purpose"]


class UsageTotals(ApplicationDTO):
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost: float = 0.0
    priced_request_count: int = 0
    unpriced_request_count: int = 0


class UsageSummaryGroup(ApplicationDTO):
    key: str
    totals: UsageTotals


class UsageSummary(ApplicationDTO):
    usage_filter: UsageFilter
    totals: UsageTotals
    group_by: UsageGroupBy | None = None
    groups: tuple[UsageSummaryGroup, ...] = ()


class UsageEntryList(ApplicationDTO):
    usage_filter: UsageFilter
    entries: tuple[ProviderUsageEntry, ...] = ()
    total_matching: int = 0
    truncated: bool = False


class UsageMutationResult(ApplicationDTO):
    action: Literal["replace", "reset"]
    affected_count: int
    affected_totals: UsageTotals
    entry: ProviderUsageEntry | None = None


class UsageAuditLog(ApplicationDTO):
    events: tuple[UsageLedgerEvent, ...] = ()
    total_events: int = 0
    truncated: bool = False
