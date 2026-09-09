from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from ouro.billing import LocalUsageLedger
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.errors import MigrationRequiredError, PersistenceError


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


def test_local_usage_ledger_rejects_event_without_explicit_schema_version(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(_entry(0))
    raw = json.loads(ledger.journal_path.read_text("utf-8"))
    raw.pop("schema_version")
    ledger.journal_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(MigrationRequiredError, match="found missing, expected 1"):
        LocalUsageLedger(tmp_path).list_events()


def test_local_usage_ledger_rejects_conflicting_ids(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(_entry(0))

    with pytest.raises(PersistenceError, match="conflicts"):
        ledger.record(_entry(0).model_copy(update={"input_tokens": 99}))


def test_local_usage_ledger_preserves_unicode_line_separators_in_notes(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    note = "first\u2028second\u2029third"

    ledger.reset(UsageFilter(), note=note)

    assert LocalUsageLedger(tmp_path).list_events()[0].note == note


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


def test_local_usage_ledger_does_not_expose_mutable_cache_aliases(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    source = _entry(0)

    recorded = ledger.record(source)
    source.metadata["token_source"] = "mutated-source"
    recorded.metadata["token_source"] = "mutated-record-result"
    listed = ledger.list_entries()
    listed[0].metadata["token_source"] = "mutated-list-result"
    events = ledger.list_events()
    assert events[0].entry is not None
    events[0].entry.metadata["token_source"] = "mutated-event-result"

    assert ledger.get(source.entry_id).metadata == {"token_source": "provider_response"}
    assert LocalUsageLedger(tmp_path).get(source.entry_id).metadata == {
        "token_source": "provider_response"
    }


def test_scope_totals_follow_effective_entries_across_writers_and_corrections(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    first = _entry(0).model_copy(
        update={"metadata": {"harness_scopes": {"owner-a": ["goal", "goal"], "owner-b": ["goal"]}}}
    )
    ledger.record(first)
    ledger.record(first)
    ledger.record(_entry(1))  # Historical entries without attribution remain unassociated.
    assert ledger.session_scope_tokens("ses_a") == {"owner-a": {"goal": 3}, "owner-b": {"goal": 3}}
    assert ledger.session_scope_tokens("another-session") == {}

    other = LocalUsageLedger(tmp_path)
    assert other.session_scope_tokens("ses_a") == ledger.session_scope_tokens("ses_a")
    corrected = first.model_copy(update={"revision": 2, "input_tokens": 8})
    other.replace(corrected, note="provider correction")
    assert ledger.session_scope_tokens("ses_a") == {
        "owner-a": {"goal": 10},
        "owner-b": {"goal": 10},
    }
    returned = ledger.session_scope_tokens("ses_a")
    returned["owner-a"]["goal"] = 0
    assert ledger.session_scope_tokens("ses_a")["owner-a"]["goal"] == 10

    other.reset(UsageFilter(run_ids=frozenset({first.run_id})), note="explicit billing reset")
    assert ledger.session_scope_tokens("ses_a") == {}
    assert LocalUsageLedger(tmp_path).session_scope_tokens("ses_a") == {}
    assert len(ledger.list_entries()) == 1


def test_scope_reads_and_local_appends_do_not_replay_the_journal(tmp_path, monkeypatch):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(_entry(0))
    apply = LocalUsageLedger._apply_event
    applied = []

    def observe(entries, event, totals):
        applied.append(event.event_type)
        return apply(entries, event, totals)

    monkeypatch.setattr(LocalUsageLedger, "_apply_event", staticmethod(observe))
    for index in range(1, 21):
        ledger.record(
            _entry(index).model_copy(update={"metadata": {"harness_scopes": {"owner": ["goal"]}}})
        )
        assert ledger.session_scope_tokens("ses_a")["owner"]["goal"] > 0
    assert applied == ["record"] * 20
