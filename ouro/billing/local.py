from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsageLedgerEvent
from ouro.core.errors import PersistenceError
from ouro.sessions.locks import FileMutationLock


class LocalUsageLedger:
    """Durable append-only provider usage ledger under ``OURO_HOME``."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.billing_dir = self.root / "billing"
        self.journal_path = self.billing_dir / "usage.jsonl"
        self.lock_path = self.root / "locks" / "billing-mutations.lock"
        self._cache_size = -1
        self._cached_events: list[UsageLedgerEvent] = []
        self._cached_entries: dict[str, ProviderUsageEntry] = {}

    def initialize(self) -> None:
        try:
            self.billing_dir.mkdir(parents=True, exist_ok=True)
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.touch(exist_ok=True)
        except OSError as exc:
            raise PersistenceError(f"could not initialize usage ledger: {exc}") from exc

    def record(self, entry: ProviderUsageEntry) -> ProviderUsageEntry:
        with self._mutation():
            entries = self._read_entries()
            existing = entries.get(entry.entry_id)
            if existing is not None:
                comparable = entry.model_copy(update={"occurred_at": existing.occurred_at})
                if existing != comparable:
                    raise PersistenceError(
                        f"usage entry id conflicts with existing data: {entry.entry_id}"
                    )
                return existing
            self._append_event(UsageLedgerEvent(event_type="record", entry=entry))
            return entry

    def get(self, entry_id: str) -> ProviderUsageEntry:
        with self._mutation():
            entry = self._read_entries().get(entry_id)
            if entry is None:
                raise KeyError(entry_id)
            return entry

    def list_entries(self) -> list[ProviderUsageEntry]:
        with self._mutation():
            return list(self._read_entries().values())

    def replace(self, entry: ProviderUsageEntry, *, note: str) -> ProviderUsageEntry:
        with self._mutation():
            entries = self._read_entries()
            current = entries.get(entry.entry_id)
            if current is None:
                raise KeyError(entry.entry_id)
            if entry.revision != current.revision + 1:
                raise PersistenceError(
                    f"stale usage revision for {entry.entry_id}: "
                    f"expected {current.revision + 1}, got {entry.revision}"
                )
            self._append_event(
                UsageLedgerEvent(event_type="replace", entry=entry, note=note.strip())
            )
            return entry

    def reset(self, usage_filter: UsageFilter, *, note: str) -> list[ProviderUsageEntry]:
        with self._mutation():
            entries = self._read_entries()
            removed = [entry for entry in entries.values() if usage_filter.matches(entry)]
            self._append_event(
                UsageLedgerEvent(
                    event_type="reset",
                    usage_filter=usage_filter,
                    note=note.strip(),
                )
            )
            return removed

    def list_events(self) -> list[UsageLedgerEvent]:
        with self._mutation():
            return self._read_events()

    def _mutation(self) -> FileMutationLock:
        return FileMutationLock(self.lock_path)

    def _read_events(self) -> list[UsageLedgerEvent]:
        return list(self._read_state()[0])

    def _read_entries(self) -> dict[str, ProviderUsageEntry]:
        return self._read_state()[1]

    def _read_state(
        self,
    ) -> tuple[list[UsageLedgerEvent], dict[str, ProviderUsageEntry]]:
        if not self.journal_path.exists():
            return [], {}
        try:
            current_size = self.journal_path.stat().st_size
            if current_size == self._cache_size:
                return self._cached_events, self._cached_entries
            events: list[UsageLedgerEvent] = []
            for line_number, line in enumerate(
                self.journal_path.read_text("utf-8").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                try:
                    events.append(UsageLedgerEvent.model_validate_json(line))
                except (ValidationError, ValueError) as exc:
                    raise PersistenceError(
                        f"invalid usage ledger event at line {line_number}: {exc}"
                    ) from exc
            entries = self._effective_entries(events)
            self._cache_size = current_size
            self._cached_events = events
            self._cached_entries = entries
            return events, entries
        except OSError as exc:
            raise PersistenceError(f"could not read usage ledger: {exc}") from exc

    def _append_event(self, event: UsageLedgerEvent) -> None:
        try:
            with self.journal_path.open("a", encoding="utf-8") as file:
                file.write(event.model_dump_json())
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            size = self.journal_path.stat().st_size
            if self._cache_size >= 0:
                self._apply_event(self._cached_entries, event)
                self._cached_events.append(event)
                self._cache_size = size
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"could not append usage ledger event: {exc}") from exc

    @staticmethod
    def _effective_entries(
        events: list[UsageLedgerEvent],
    ) -> dict[str, ProviderUsageEntry]:
        entries: dict[str, ProviderUsageEntry] = {}
        for event in events:
            LocalUsageLedger._apply_event(entries, event)
        return entries

    @staticmethod
    def _apply_event(
        entries: dict[str, ProviderUsageEntry],
        event: UsageLedgerEvent,
    ) -> None:
        if event.event_type == "record":
            assert event.entry is not None
            if event.entry.entry_id in entries:
                raise PersistenceError(f"duplicate usage entry in ledger: {event.entry.entry_id}")
            entries[event.entry.entry_id] = event.entry
            return
        if event.event_type == "replace":
            assert event.entry is not None
            current = entries.get(event.entry.entry_id)
            if current is None:
                raise PersistenceError(
                    f"usage replacement has no active entry: {event.entry.entry_id}"
                )
            if event.entry.revision != current.revision + 1:
                raise PersistenceError(f"non-sequential usage revision for {event.entry.entry_id}")
            entries[event.entry.entry_id] = event.entry
            return
        assert event.usage_filter is not None
        removed_ids = [
            entry_id for entry_id, entry in entries.items() if event.usage_filter.matches(entry)
        ]
        for entry_id in removed_ids:
            entries.pop(entry_id)
