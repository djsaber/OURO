from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ouro.application.usage_models import (
    UsageAuditLog,
    UsageEntryList,
    UsageGroupBy,
    UsageMutationResult,
    UsageSummary,
    UsageSummaryGroup,
    UsageTotals,
)
from ouro.billing import UsageLedger
from ouro.core.base import utc_now
from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsagePurpose
from ouro.core.errors import PersistenceError
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.state import RunRecord


class UsageService:
    """Record and query provider-reported model usage across all sessions."""

    def __init__(
        self,
        ledger: UsageLedger,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.ledger = ledger
        self._clock = clock

    def record_provider_usage(
        self,
        *,
        run: RunRecord,
        usage: Usage,
        model: ModelMetadata,
        request_id: str,
        purpose: UsagePurpose,
    ) -> ProviderUsageEntry:
        """Persist only canonical usage supplied by a provider response."""

        entry_id = provider_usage_entry_id(
            run_id=run.run_id,
            request_id=request_id,
            purpose=purpose,
            provider_id=model.provider_id,
            endpoint_id=model.endpoint_id,
            model_id=model.model_id,
        )
        try:
            existing = self.ledger.get(entry_id)
        except KeyError:
            pass
        else:
            expected = (
                run.session_id,
                run.run_id,
                purpose,
                request_id,
                model.provider_id,
                model.endpoint_id,
                model.model_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.estimated_cost,
            )
            actual = (
                existing.session_id,
                existing.run_id,
                existing.purpose,
                existing.request_id,
                existing.provider_id,
                existing.endpoint_id,
                existing.model_id,
                existing.input_tokens,
                existing.output_tokens,
                existing.cache_read_tokens,
                existing.cache_write_tokens,
                existing.estimated_cost,
            )
            if actual != expected and existing.revision == 1:
                raise PersistenceError(
                    f"provider usage request conflicts with ledger entry: {entry_id}"
                )
            return existing
        entry = ProviderUsageEntry(
            entry_id=entry_id,
            occurred_at=self._clock(),
            provider_id=model.provider_id,
            endpoint_id=model.endpoint_id,
            model_id=model.model_id,
            session_id=run.session_id,
            run_id=run.run_id,
            run_kind=run.kind,
            purpose=purpose,
            request_id=request_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            estimated_cost=usage.estimated_cost,
            metadata={"token_source": "provider_response"},
        )
        return self.ledger.record(entry)

    def list_entries(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        limit: int = 100,
    ) -> UsageEntryList:
        if not 1 <= limit <= 10_000:
            raise ValueError("usage list limit must be between 1 and 10000")
        active_filter = usage_filter or UsageFilter()
        matching = sorted(
            (entry for entry in self.ledger.list_entries() if active_filter.matches(entry)),
            key=lambda entry: (entry.occurred_at, entry.entry_id),
            reverse=True,
        )
        return UsageEntryList(
            usage_filter=active_filter,
            entries=tuple(matching[:limit]),
            total_matching=len(matching),
            truncated=len(matching) > limit,
        )

    def get_entry(self, entry_id: str) -> ProviderUsageEntry:
        return self.ledger.get(entry_id)

    def summarize(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        group_by: UsageGroupBy | None = None,
    ) -> UsageSummary:
        active_filter = usage_filter or UsageFilter()
        entries = [entry for entry in self.ledger.list_entries() if active_filter.matches(entry)]
        grouped: dict[str, list[ProviderUsageEntry]] = {}
        if group_by is not None:
            for entry in entries:
                grouped.setdefault(_group_key(entry, group_by), []).append(entry)
        return UsageSummary(
            usage_filter=active_filter,
            totals=usage_totals(entries),
            group_by=group_by,
            groups=tuple(
                UsageSummaryGroup(key=key, totals=usage_totals(grouped[key]))
                for key in sorted(grouped)
            ),
        )

    def replace_entry(
        self,
        entry_id: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        estimated_cost: float | None = None,
        clear_estimated_cost: bool = False,
        note: str,
    ) -> UsageMutationResult:
        supplied = (
            input_tokens is not None,
            output_tokens is not None,
            cache_read_tokens is not None,
            cache_write_tokens is not None,
            estimated_cost is not None,
            clear_estimated_cost,
        )
        if not any(supplied):
            raise ValueError("at least one usage value must be supplied")
        normalized_note = note.strip()
        if not normalized_note:
            raise ValueError("a modification note is required")
        if estimated_cost is not None and clear_estimated_cost:
            raise ValueError("estimated_cost and clear_estimated_cost are mutually exclusive")
        current = self.ledger.get(entry_id)
        updates: dict[str, object] = {
            "revision": current.revision + 1,
            "modified_at": self._clock(),
            "modification_note": normalized_note,
            "metadata": {**current.metadata, "manually_corrected": True},
        }
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_tokens", cache_read_tokens),
            ("cache_write_tokens", cache_write_tokens),
        ):
            if value is not None:
                updates[name] = value
        if estimated_cost is not None:
            updates["estimated_cost"] = estimated_cost
        elif clear_estimated_cost:
            updates["estimated_cost"] = None
        replacement_data = current.model_dump()
        replacement_data.update(updates)
        replaced = self.ledger.replace(
            ProviderUsageEntry.model_validate(replacement_data),
            note=normalized_note,
        )
        return UsageMutationResult(
            action="replace",
            affected_count=1,
            affected_totals=usage_totals([replaced]),
            entry=replaced,
        )

    def reset(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        note: str,
    ) -> UsageMutationResult:
        normalized_note = note.strip()
        if not normalized_note:
            raise ValueError("a reset note is required")
        removed = self.ledger.reset(usage_filter or UsageFilter(), note=normalized_note)
        return UsageMutationResult(
            action="reset",
            affected_count=len(removed),
            affected_totals=usage_totals(removed),
        )

    def audit(self, *, limit: int = 100) -> UsageAuditLog:
        if not 1 <= limit <= 10_000:
            raise ValueError("usage audit limit must be between 1 and 10000")
        events = self.ledger.list_events()
        selected = list(reversed(events[-limit:]))
        return UsageAuditLog(
            events=tuple(selected),
            total_events=len(events),
            truncated=len(events) > limit,
        )


def provider_usage_entry_id(
    *,
    run_id: str,
    request_id: str,
    purpose: UsagePurpose,
    provider_id: str,
    endpoint_id: str | None,
    model_id: str,
) -> str:
    identity = "\0".join((run_id, request_id, purpose, provider_id, endpoint_id or "", model_id))
    return f"use_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def usage_totals(entries: Iterable[ProviderUsageEntry]) -> UsageTotals:
    request_count = 0
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    estimated_cost = 0.0
    priced_request_count = 0
    for entry in entries:
        request_count += 1
        input_tokens += entry.input_tokens
        output_tokens += entry.output_tokens
        cache_read_tokens += entry.cache_read_tokens
        cache_write_tokens += entry.cache_write_tokens
        if entry.estimated_cost is not None:
            estimated_cost += entry.estimated_cost
            priced_request_count += 1
    return UsageTotals(
        request_count=request_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        estimated_cost=estimated_cost,
        priced_request_count=priced_request_count,
        unpriced_request_count=request_count - priced_request_count,
    )


def _group_key(entry: ProviderUsageEntry, group_by: UsageGroupBy) -> str:
    if group_by == "date":
        return entry.occurred_at.astimezone(UTC).date().isoformat()
    if group_by == "provider":
        return entry.provider_id
    if group_by == "endpoint":
        return entry.endpoint_id or "<none>"
    if group_by == "model":
        return entry.model_id
    return entry.purpose
