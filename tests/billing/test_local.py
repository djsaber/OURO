from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from ouro.billing import LocalUsageLedger
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.errors import PersistenceError, UnsupportedDataVersionError


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
    assert removed.request_count == 1 and removed.total_tokens == 4
    assert reopened.list_entries(UsageFilter()).entries == (replacement,)
    assert [event.event_type for event in reversed(reopened.audit().events)] == [
        "record",
        "record",
        "replace",
        "reset",
    ]


def test_local_usage_ledger_rejects_event_without_explicit_schema_version(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(_entry(0))
    with closing(sqlite3.connect(ledger.path)) as connection:
        raw = json.loads(connection.execute("SELECT payload FROM events").fetchone()[0])
        raw.pop("schema_version")
        connection.execute("UPDATE events SET payload=?", (json.dumps(raw),))
        connection.commit()

    with pytest.raises(UnsupportedDataVersionError, match="found missing, expected 1"):
        LocalUsageLedger(tmp_path).audit()


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

    assert LocalUsageLedger(tmp_path).audit().events[0].note == note


def test_local_usage_ledger_serializes_concurrent_writers(tmp_path) -> None:
    def write(index: int) -> None:
        ledger = LocalUsageLedger(tmp_path)
        ledger.initialize()
        ledger.record(_entry(index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(40)))

    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    assert len(ledger.list_entries(UsageFilter()).entries) == 40
    assert len(ledger.audit().events) == 40


def test_local_usage_ledger_returns_detached_records(tmp_path) -> None:
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    source = _entry(0)

    recorded = ledger.record(source)
    source.metadata["token_source"] = "mutated-source"
    recorded.metadata["token_source"] = "mutated-record-result"
    listed = ledger.list_entries(UsageFilter()).entries
    listed[0].metadata["token_source"] = "mutated-list-result"
    events = ledger.audit().events
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
    ledger.record(_entry(1))  # Entries without attribution remain unassociated.
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
    assert len(ledger.list_entries(UsageFilter()).entries) == 1


def test_combined_filters_grouping_and_utc_boundaries(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    instant = datetime.fromisoformat("2026-07-02T01:00:00+08:00")
    for index, workspace, provider, mode, cost in (
        (0, "/one", "a", "usage_based", 0.25),
        (1, "/one", "a", "subscription", None),
        (2, "/two", "a", "usage_based", 0.0),
        (3, "/one", "b", "usage_based", 0.5),
        (4, None, "a", "unknown", None),
    ):
        ledger.record(
            _entry(index).model_copy(
                update={
                    "occurred_at": instant,
                    "workspace": workspace,
                    "provider_id": provider,
                    "estimated_cost": cost,
                    "metadata": {"billing_mode": mode},
                    "endpoint_id": None if index == 4 else "endpoint-a",
                }
            )
        )
    selected = UsageFilter(
        occurred_from=datetime(2026, 7, 1, tzinfo=UTC),
        occurred_to=datetime(2026, 7, 2, tzinfo=UTC),
        workspaces=frozenset({"/one"}),
        provider_ids=frozenset({"a"}),
        billing_modes=frozenset({"usage_based", "subscription"}),
    )
    summary = ledger.summarize(selected, group_by=("date", "workspace", "billing_mode"))
    assert [group.key for group in summary.groups] == [
        ("2026-07-01", "/one", "subscription"),
        ("2026-07-01", "/one", "usage_based"),
    ]
    assert summary.totals == ledger.summarize(selected).totals
    assert summary.totals.request_count == 2
    assert summary.totals.total_tokens == 7
    assert summary.totals.estimated_cost == 0.25
    assert summary.totals.unpriced_request_count == 1
    unknown = UsageFilter(workspaces=frozenset({None}), endpoint_ids=frozenset({None}))
    assert ledger.summarize(unknown).totals.request_count == 1
    assert ledger.summarize(unknown, group_by=("endpoint", "workspace")).groups[0].key == (
        None,
        None,
    )
    exclusive = UsageFilter(occurred_to=instant)
    assert ledger.summarize(exclusive).totals.request_count == 0
    assert ledger.summarize(UsageFilter(occurred_from=instant)).totals.request_count == 5


def test_query_pages_have_stable_cursors_at_equal_timestamps(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    instant = datetime(2026, 7, 1, tzinfo=UTC)
    for index in range(5):
        ledger.record(_entry(index).model_copy(update={"occurred_at": instant}))
    first = ledger.list_entries(UsageFilter(), limit=2)
    assert [entry.entry_id for entry in first.entries] == ["use_4", "use_3"]
    assert first.total_matching == 5 and first.truncated
    # A newly inserted earlier page does not shift the continuation boundary.
    ledger.record(_entry(8))
    second = ledger.list_entries(UsageFilter(), limit=2, before=first.next_cursor)
    third = ledger.list_entries(UsageFilter(), limit=2, before=second.next_cursor)
    assert [entry.entry_id for entry in second.entries + third.entries] == [
        "use_2",
        "use_1",
        "use_0",
    ]
    assert third.next_cursor is None and not third.truncated
    events = []
    before = None
    while True:
        page = ledger.audit(limit=2, before=before)
        events.extend(event.entry_id for event in page.events)
        before = page.next_cursor
        if before is None:
            break
    assert events == ["use_8", "use_4", "use_3", "use_2", "use_1", "use_0"]


def test_corrections_preserve_source_identity_and_reject_stale_revisions(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    source = ledger.record(_entry(0))
    corrected = source.model_copy(update={"revision": 2, "input_tokens": 99})
    ledger.replace(corrected, note="invoice")
    assert LocalUsageLedger(tmp_path).record(source) == corrected
    with pytest.raises(PersistenceError, match="conflicts"):
        ledger.record(source.model_copy(update={"output_tokens": 100}))
    with pytest.raises(PersistenceError, match="stale"):
        ledger.replace(corrected, note="stale writer")
    with pytest.raises(PersistenceError, match="attribution"):
        ledger.replace(
            corrected.model_copy(update={"revision": 3, "workspace": "/changed"}), note="invalid"
        )
    assert ledger.audit().total_events == 2


@pytest.mark.parametrize("operation", ["record", "replace", "reset"])
def test_failed_audit_rolls_back_facts_and_scope_totals(tmp_path, monkeypatch, operation):
    ledger = LocalUsageLedger(tmp_path)
    first = _entry(0).model_copy(update={"metadata": {"harness_scopes": {"owner": ["goal"]}}})
    ledger.record(first)
    summary = ledger.summarize(UsageFilter())
    append = LocalUsageLedger._append_event

    def fail(connection, event):
        append(connection, event)
        raise sqlite3.IntegrityError("injected write failure")

    monkeypatch.setattr(LocalUsageLedger, "_append_event", staticmethod(fail))
    with pytest.raises(PersistenceError, match="storage failed"):
        if operation == "record":
            ledger.record(_entry(1))
        elif operation == "replace":
            ledger.replace(
                first.model_copy(update={"revision": 2, "input_tokens": 50}), note="invoice"
            )
        else:
            ledger.reset(UsageFilter(), note="reset")
    assert ledger.summarize(UsageFilter()) == summary
    assert ledger.get(first.entry_id) == first
    assert ledger.session_scope_tokens("ses_a") == {"owner": {"goal": 3}}
    assert ledger.audit().total_events == 1


def test_new_reader_sees_committed_snapshot_without_waiting_for_writer(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    ledger.record(_entry(0))
    with closing(sqlite3.connect(ledger.path)) as writer, ThreadPoolExecutor() as executor:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE entries SET input_tokens=100")
        try:
            query = executor.submit(LocalUsageLedger(tmp_path).summarize, UsageFilter())
            assert query.result(timeout=2).totals.input_tokens == 1
        finally:
            writer.rollback()


def test_concurrent_repeated_requests_create_one_fact_and_event(tmp_path):
    LocalUsageLedger(tmp_path).initialize()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(lambda _: LocalUsageLedger(tmp_path).record(_entry(0)), range(24))
        )
    assert all(entry == _entry(0) for entry in results)
    ledger = LocalUsageLedger(tmp_path)
    assert ledger.summarize(UsageFilter()).totals.request_count == 1
    assert ledger.audit().total_events == 1


def test_summary_scope_and_reset_never_decode_request_or_audit_payloads(tmp_path, monkeypatch):
    ledger = LocalUsageLedger(tmp_path)
    ledger.record(
        _entry(0).model_copy(update={"metadata": {"harness_scopes": {"owner": ["goal"]}}})
    )

    def fail(*args, **kwargs):
        pytest.fail("aggregate query decoded a persisted payload")

    monkeypatch.setattr("ouro.billing.local.decode_versioned_record_json", fail)
    assert ledger.summarize(UsageFilter(), group_by=("date",)).totals.total_tokens == 3
    assert ledger.session_scope_tokens("ses_a") == {"owner": {"goal": 3}}
    assert ledger.reset(UsageFilter(), note="clear").total_tokens == 3
    assert ledger.session_scope_tokens("ses_a") == {}


def test_database_version_is_required(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(UnsupportedDataVersionError, match="found 999"):
        LocalUsageLedger(tmp_path).summarize(UsageFilter())


def test_initialization_preserves_other_files_and_reuses_database(tmp_path):
    billing = tmp_path / "billing"
    billing.mkdir()
    (billing / "unrecognized-data").write_text("accounting data", encoding="utf-8")
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    ledger.record(_entry(0))
    reopened = LocalUsageLedger(tmp_path)
    reopened.initialize()
    assert reopened.get("use_0") == _entry(0)
    assert reopened.audit().total_events == 1
    assert (billing / "unrecognized-data").read_text("utf-8") == "accounting data"


def test_paged_reads_decode_only_the_requested_page(tmp_path, monkeypatch):
    import ouro.billing.local as storage

    ledger = LocalUsageLedger(tmp_path)
    for index in range(12):
        ledger.record(_entry(index))
    decoded = []
    decode = storage.decode_versioned_record_json

    def observe(model, payload, *, label):
        decoded.append(model)
        return decode(model, payload, label=label)

    monkeypatch.setattr(storage, "decode_versioned_record_json", observe)
    assert len(ledger.list_entries(UsageFilter(), limit=2).entries) == 2
    assert len(decoded) == 2
    decoded.clear()
    assert len(ledger.audit(limit=3).events) == 3
    assert len(decoded) == 3


def test_combined_run_filter_does_not_scan_unrelated_requests(tmp_path, monkeypatch):
    ledger = LocalUsageLedger(tmp_path)
    for index in range(512):
        ledger.record(_entry(index))
    connect = ledger._connect

    @contextmanager
    def limited_work():
        with connect() as connection:
            # An indexed point query needs far fewer VM instructions than examining
            # every request with the same billing mode. This is independent of timing.
            connection.set_progress_handler(lambda: 1, 1000)
            yield connection

    monkeypatch.setattr(ledger, "_connect", limited_work)
    summary = ledger.summarize(UsageFilter(run_ids={"run_3"}, billing_modes={"unknown"}))
    assert summary.totals.request_count == 1 and summary.totals.total_tokens == 6


@pytest.mark.parametrize("group_by", [("date", "date"), ("invalid",)])
def test_group_dimensions_are_validated(tmp_path, group_by):
    with pytest.raises(ValueError, match="dimensions"):
        LocalUsageLedger(tmp_path).summarize(UsageFilter(), group_by=group_by)
