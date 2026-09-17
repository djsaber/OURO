from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.errors import PersistenceError
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.state import RunRecord


def _stored_entry(
    entry_id: str,
    *,
    occurred_at: datetime,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost: float | None = None,
) -> ProviderUsageEntry:
    return ProviderUsageEntry(
        entry_id=entry_id,
        occurred_at=occurred_at,
        provider_id=provider,
        endpoint_id=f"{model}-endpoint",
        model_id=model,
        session_id="ses_a",
        run_id=f"run_{entry_id}",
        run_kind="agent",
        purpose="agent",
        request_id=f"request_{entry_id}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=cost,
        metadata={"token_source": "provider_response"},
    )


def test_usage_service_filters_and_groups_provider_entries(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(
        _stored_entry(
            "use_a",
            occurred_at=datetime(2026, 7, 1, 10, tzinfo=UTC),
            provider="provider-a",
            model="model-a",
            input_tokens=10,
            output_tokens=4,
            cost=0.2,
        )
    )
    ledger.record(
        _stored_entry(
            "use_b",
            occurred_at=datetime(2026, 7, 2, 10, tzinfo=UTC),
            provider="provider-b",
            model="model-b",
            input_tokens=20,
            output_tokens=5,
        )
    )
    service = UsageService(ledger)

    summary = service.summarize(
        UsageFilter(
            occurred_from=datetime(2026, 7, 1, tzinfo=UTC),
            occurred_to=datetime(2026, 7, 3, tzinfo=UTC),
        ),
        group_by="model",
    )

    assert summary.totals.request_count == 2
    assert summary.totals.input_tokens == 30
    assert summary.totals.total_tokens == 39
    assert summary.totals.estimated_cost == 0.2
    assert summary.totals.unpriced_request_count == 1
    assert [group.key for group in summary.groups] == ["model-a", "model-b"]


def test_usage_service_records_only_supplied_provider_usage(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    service = UsageService(
        ledger,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    )
    run = RunRecord(session_id="ses_a", run_id="run_a")
    model = ModelMetadata(
        provider_id="provider-a",
        endpoint_id="endpoint-a",
        model_id="model-a",
        context_window=1_000_000,
    )

    first = service.record_provider_usage(
        run=run,
        usage=Usage(input_tokens=7, output_tokens=3, cache_read_tokens=2),
        model=model,
        request_id="request-a",
        purpose="agent",
    )
    repeated = service.record_provider_usage(
        run=run,
        usage=Usage(input_tokens=7, output_tokens=3, cache_read_tokens=2),
        model=model,
        request_id="request-a",
        purpose="agent",
    )

    assert repeated == first
    assert first.input_tokens == 7
    assert first.output_tokens == 3
    assert first.metadata == {"token_source": "provider_response", "billing_mode": "unknown"}
    assert len(ledger.list_events()) == 1

    with pytest.raises(PersistenceError, match="conflicts"):
        service.record_provider_usage(
            run=run,
            usage=Usage(input_tokens=999, output_tokens=999),
            model=model,
            request_id="request-a",
            purpose="agent",
        )


def test_usage_service_modification_and_reset_are_audited(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(
        _stored_entry(
            "use_a",
            occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
            provider="provider-a",
            model="model-a",
            input_tokens=10,
            output_tokens=4,
        )
    )
    service = UsageService(
        ledger,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    )

    modified = service.replace_entry(
        "use_a",
        input_tokens=12,
        estimated_cost=0.5,
        note="match provider invoice",
    )
    reset = service.reset(
        UsageFilter(provider_ids=frozenset({"provider-a"})),
        note="new accounting period",
    )

    assert modified.entry is not None
    assert modified.entry.revision == 2
    assert modified.entry.input_tokens == 12
    assert reset.affected_count == 1
    assert service.summarize().totals.request_count == 0
    assert [event.event_type for event in service.audit().events] == [
        "reset",
        "replace",
        "record",
    ]


def test_usage_service_requires_values_and_notes_for_manual_changes(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(
        _stored_entry(
            "use_a",
            occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
            provider="provider-a",
            model="model-a",
            input_tokens=10,
            output_tokens=4,
        )
    )
    service = UsageService(ledger)

    with pytest.raises(ValueError, match="at least one"):
        service.replace_entry("use_a", note="no values")
    with pytest.raises(ValueError, match="note"):
        service.replace_entry("use_a", input_tokens=1, note=" ")
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        service.replace_entry("use_a", input_tokens=-1, note="invalid correction")
    with pytest.raises(ValueError, match="note"):
        service.reset(note=" ")
