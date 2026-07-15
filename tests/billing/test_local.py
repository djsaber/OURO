from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from ouro.billing import LocalUsageLedger
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.errors import PersistenceError


def _entry(index: int, *, model: str = "model-a") -> ProviderUsageEntry:
    return ProviderUsageEntry(
        entry_id=f"use_{index}",
        occurred_at=datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=index),
        provider_id="provider-a",
        endpoint_id="endpoint-a",
        model_id=model,
        session_id="ses_a",
        run_id=f"run_{index}",
        run_kind="agent",
        purpose="agent",
        request_id=f"request_{index}",
        input_tokens=index + 1,
        output_tokens=2,
        metadata={"token_source": "provider_response"},
    )


def test_local_usage_ledger_persists_revisions_and_filtered_resets(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    first = ledger.record(_entry(0))
    ledger.record(_entry(1, model="model-b"))

    replacement = first.model_copy(
        update={
            "revision": 2,
            "input_tokens": 9,
            "modified_at": datetime(2026, 7, 10, tzinfo=UTC),
            "modification_note": "provider invoice correction",
        }
    )
    ledger.replace(replacement, note="provider invoice correction")
    removed = ledger.reset(
        UsageFilter(model_ids=frozenset({"model-b"})),
        note="clear model-b",
    )

    reopened = LocalUsageLedger(tmp_path)
    reopened.initialize()
    assert removed == [_entry(1, model="model-b")]
    assert reopened.list_entries() == [replacement]
    assert [event.event_type for event in reopened.list_events()] == [
        "record",
        "record",
        "replace",
        "reset",
    ]


def test_local_usage_ledger_rejects_conflicting_ids(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(_entry(0))

    with pytest.raises(PersistenceError, match="conflicts"):
        ledger.record(_entry(0).model_copy(update={"input_tokens": 99}))


def test_local_usage_ledger_serializes_concurrent_writers(tmp_path) -> None:
    def write(index: int) -> None:
        ledger = LocalUsageLedger(tmp_path)
        ledger.initialize()
        ledger.record(_entry(index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    assert len(ledger.list_entries()) == 40
    assert len(ledger.list_events()) == 40
