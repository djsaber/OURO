from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ouro.billing import UsageLedger
from ouro.core.base import utc_now
from ouro.core.billing import ProviderUsageEntry, UsageFilter, UsagePurpose, provider_usage_entry_id
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.errors import PersistenceError
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord
from ouro.core.usage_queries import (
    RunUsageTotals,
    UsageAuditLog,
    UsageEntryCursor,
    UsageEntryList,
    UsageGroupBy,
    UsageMutationResult,
    UsageSummary,
    UsageTotals,
)
from ouro.sessions.base import SessionRepository


@dataclass(frozen=True)
class SummaryRequestRecorder:
    """Bind attribution once; commit each dispatch and settle its original report."""

    store: SessionRepository
    run: RunRecord
    workspace: str | None
    usage: UsageService | None

    def start(self, request_id: str, *, model: ModelMetadata, metrics: ContextMetrics) -> None:
        with self.store.session_mutation(self.run.session_id):
            session = self.store.load_session_record(self.run.session_id)
            latest = self.store.load_run(self.run.session_id, self.run.run_id)
            if (
                session.history_epoch != self.run.history_epoch
                or session.active_run_id != self.run.run_id
                or latest.status not in ACTIVE_RUN_STATUSES
                or latest.abort_requested
            ):
                raise SessionCommitConflict("Run no longer owns summary dispatch")
            updated = latest.model_copy(
                update={
                    "current_request_id": request_id,
                    "summary_request_count": latest.summary_request_count + 1,
                }
            )
            self.store.commit_session(
                SessionCommit(
                    before=session,
                    runs=(updated,),
                    request_header=ModelRequestHeader(
                        context_id=request_id,
                        session_id=session.session_id,
                        run_id=latest.run_id,
                        purpose="compaction",
                        turn_id=request_id,
                        model_round=updated.summary_request_count,
                        provider_id=model.provider_id,
                        endpoint_id=model.endpoint_id,
                        model_id=model.model_id,
                        started_at=utc_now(),
                        metrics=metrics,
                    ),
                )
            )

    def settle(
        self,
        request_id: str,
        *,
        model: ModelMetadata,
        provider_done: bool,
        usage: Usage | None,
    ) -> None:
        entry = (
            self.usage.record_provider_usage(
                run=self.run,
                workspace=self.workspace,
                model=model,
                request_id=request_id,
                purpose="compaction",
                usage=usage,
            )
            if self.usage is not None and usage is not None
            else None
        )
        self.store.settle_request_header(
            self.run.session_id,
            request_id,
            settled_at=utc_now(),
            provider_done=provider_done,
            usage_entry_id=entry.entry_id if entry is not None else None,
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

    def get_raw_entry(self, entry_id: str) -> ProviderUsageEntry | None:
        return self.ledger.get_raw(entry_id)

    def request_usage(self, request: ModelRequestHeader) -> ProviderUsageEntry | None:
        """Point-read and verify the original fact for this exact prepared request."""
        entry = self.get_raw_entry(request.expected_usage_entry_id)
        if entry is None:
            return None
        if (
            entry.session_id != request.session_id
            or entry.run_id != request.run_id
            or entry.request_id != request.turn_id
            or entry.purpose != request.purpose
            or entry.provider_id != request.provider_id
            or entry.endpoint_id != request.endpoint_id
            or entry.model_id != request.model_id
        ):
            raise PersistenceError("original usage does not belong to the prepared request")
        return entry

    def run_usage(self, session_id: str, run_id: str) -> RunUsageTotals | None:
        """Original Run consumption, independent of invoice correction and reset."""
        return self.ledger.run_usage(session_id, run_id)

    def run_values(self, session_id: str, run_id: str) -> dict[str, int | float] | None:
        """Bounded provider-value projection; absent reports remain unknown."""
        return self._values(self.run_usage(session_id, run_id))

    def session_values(self, session_id: str) -> dict[str, int | float] | None:
        """Project all raw Run consumption in one Session, without Session history reads."""
        return self._values(self.ledger.session_usage(session_id))

    @staticmethod
    def _values(totals: RunUsageTotals | None) -> dict[str, int | float] | None:
        if totals is None:
            return None
        values: dict[str, int | float] = {
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_tokens": totals.cache_read_tokens,
            "cache_write_tokens": totals.cache_write_tokens,
        }
        if totals.priced_request_count:
            values["estimated_cost"] = totals.estimated_cost
        return values

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
            affected_totals=removed,
        )

    def audit(self, *, limit: int = 100, before: int | None = None) -> UsageAuditLog:
        return self.ledger.audit(limit=limit, before=before)


def _entry_totals(entry: ProviderUsageEntry) -> UsageTotals:
    return UsageTotals(
        request_count=1,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        cache_read_tokens=entry.cache_read_tokens,
        cache_write_tokens=entry.cache_write_tokens,
        estimated_cost=entry.estimated_cost or 0.0,
        priced_request_count=int(entry.estimated_cost is not None),
        billing_modes=(entry.billing_mode,),
    )
