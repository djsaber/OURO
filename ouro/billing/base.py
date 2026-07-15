from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsageLedgerEvent


class UsageLedger(Protocol):
    """Persistence boundary for the effective global provider usage ledger."""

    root: Path

    def initialize(self) -> None: ...

    def record(self, entry: ProviderUsageEntry) -> ProviderUsageEntry: ...

    def get(self, entry_id: str) -> ProviderUsageEntry: ...

    def list_entries(self) -> list[ProviderUsageEntry]: ...

    def replace(self, entry: ProviderUsageEntry, *, note: str) -> ProviderUsageEntry: ...

    def reset(self, usage_filter: UsageFilter, *, note: str) -> list[ProviderUsageEntry]: ...

    def list_events(self) -> list[UsageLedgerEvent]: ...
