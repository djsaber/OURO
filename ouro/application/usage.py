from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime

from ouro.billing import UsageLedger
from ouro.core.base import utc_now
from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsagePurpose
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.state import RunRecord
from ouro.core.usage_queries import (
    UsageAuditLog,
    UsageEntryCursor,
    UsageEntryList,
    UsageGroupBy,
    UsageMutationResult,
    UsageSummary,
    UsageTotals,
)


class UsageService:
    """Record and query provider-reported usage for Runs and independent plugins."""

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
        workspace: str | None = None,
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
        scopes: dict[str, set[str]] = {}
        for binding in run.control_bindings:
            if binding.usage_scope is not None:
                scopes.setdefault(binding.owner_id, set()).add(binding.usage_scope)
        metadata: dict[str, object] = {
            "token_source": "provider_response",
            "billing_mode": model.billing_mode,
        }
        if scopes:
            metadata["harness_scopes"] = {owner: sorted(values) for owner, values in scopes.items()}
        entry = ProviderUsageEntry(
            entry_id=entry_id,
            occurred_at=self._clock(),
            provider_id=model.provider_id,
            endpoint_id=model.endpoint_id,
            model_id=model.model_id,
            session_id=run.session_id,
            workspace=workspace,
            run_id=run.run_id,
            run_kind=run.kind,
            purpose=purpose,
            request_id=request_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            estimated_cost=usage.estimated_cost,
            metadata=metadata,
        )
        return self.ledger.record(entry)

    def session_scope_tokens(self, session_id: str) -> dict[str, dict[str, int]]:
        """Project the native ledger without scanning a Session's Run or message history."""
        return self.ledger.session_scope_tokens(session_id)

    def record_plugin_usage(
        self,
        *,
        plugin_id: str,
        request_id: str,
        usage: Usage,
        model: ModelMetadata,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> ProviderUsageEntry:
        """Record an independent request without inventing Session or Run facts."""
        return self.ledger.record(
            ProviderUsageEntry(
                entry_id=provider_usage_entry_id(
                    run_id=None,
                    plugin_id=plugin_id,
                    request_id=request_id,
                    purpose="plugin",
                    provider_id=model.provider_id,
                    endpoint_id=model.endpoint_id,
                    model_id=model.model_id,
                ),
                occurred_at=self._clock(),
                provider_id=model.provider_id,
                endpoint_id=model.endpoint_id,
                model_id=model.model_id,
                session_id=session_id,
                workspace=workspace,
                plugin_id=plugin_id,
                purpose="plugin",
                request_id=request_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                estimated_cost=usage.estimated_cost,
                metadata={"token_source": "provider_response", "billing_mode": model.billing_mode},
            )
        )

    def list_entries(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        limit: int = 100,
        before: UsageEntryCursor | None = None,
    ) -> UsageEntryList:
        return self.ledger.list_entries(usage_filter or UsageFilter(), limit=limit, before=before)

    def get_entry(self, entry_id: str) -> ProviderUsageEntry:
        return self.ledger.get(entry_id)

    def summarize(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        group_by: tuple[UsageGroupBy, ...] = (),
    ) -> UsageSummary:
        return self.ledger.summarize(usage_filter or UsageFilter(), group_by=group_by)

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
            affected_totals=_entry_totals(replaced),
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
            affected_count=removed.request_count,
            affected_totals=removed,
        )

    def audit(self, *, limit: int = 100, before: int | None = None) -> UsageAuditLog:
        return self.ledger.audit(limit=limit, before=before)


def provider_usage_entry_id(
    *,
    run_id: str | None,
    request_id: str,
    purpose: UsagePurpose,
    provider_id: str,
    endpoint_id: str | None,
    model_id: str,
    plugin_id: str | None = None,
) -> str:
    identity = "\0".join(
        (
            run_id or "",
            plugin_id or "",
            request_id,
            purpose,
            provider_id,
            endpoint_id or "",
            model_id,
        )
    )
    return f"use_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def _entry_totals(entry: ProviderUsageEntry) -> UsageTotals:
    return UsageTotals(
        request_count=1,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        total_tokens=entry.total_tokens,
        cache_read_tokens=entry.cache_read_tokens,
        cache_write_tokens=entry.cache_write_tokens,
        estimated_cost=entry.estimated_cost or 0.0,
        priced_request_count=int(entry.estimated_cost is not None),
        unpriced_request_count=int(entry.estimated_cost is None),
        billing_modes=(entry.billing_mode,),
    )
