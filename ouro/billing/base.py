from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.usage_queries import (
    UsageAuditLog,
    UsageEntryCursor,
    UsageEntryList,
    UsageGroupBy,
    UsageSummary,
    UsageTotals,
)


class UsageLedger(Protocol):
    """Persistence boundary for the effective global provider usage ledger."""

    root: Path

    def initialize(self) -> None: ...

    def record(self, entry: ProviderUsageEntry) -> ProviderUsageEntry: ...

    def get(self, entry_id: str) -> ProviderUsageEntry: ...

    def list_entries(
        self,
        usage_filter: UsageFilter,
        *,
        limit: int = 100,
        before: UsageEntryCursor | None = None,
    ) -> UsageEntryList: ...

    def summarize(
        self,
        usage_filter: UsageFilter,
        *,
        group_by: tuple[UsageGroupBy, ...] = (),
    ) -> UsageSummary: ...

    def session_scope_tokens(self, session_id: str) -> dict[str, dict[str, int]]: ...

    def replace(self, entry: ProviderUsageEntry, *, note: str) -> ProviderUsageEntry: ...

    def reset(self, usage_filter: UsageFilter, *, note: str) -> UsageTotals: ...

    def audit(self, *, limit: int = 100, before: int | None = None) -> UsageAuditLog: ...
