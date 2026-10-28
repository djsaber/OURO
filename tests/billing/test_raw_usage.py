from __future__ import annotations

import multiprocessing
import sqlite3
from contextlib import closing, contextmanager

import pytest

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.billing import UsageFilter
from ouro.core.errors import PersistenceError
from tests.billing.test_local import _entry


def test_session_consumption_uses_raw_run_totals_after_invoice_reset(tmp_path, monkeypatch):
    ledger = LocalUsageLedger(tmp_path)
    service = UsageService(ledger)
    assert service.session_values("ses_a") is None
    first = _entry(0).model_copy(update={"estimated_cost": 0.25})
    second = _entry(1).model_copy(update={"purpose": "compaction"})
    ledger.record(first)
    ledger.record(second)
    ledger.record(_entry(2).model_copy(update={"session_id": "ses_other"}))
    service.replace_entry(first.entry_id, input_tokens=0, note="invoice correction")
    service.reset(note="hide invoices")
    connect = ledger._connect

    @contextmanager
    def only_totals():
        with connect() as connection:
            connection.set_authorizer(
                lambda action, table, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ
                    and table in {"entries", "entry_records", "events", "raw_requests"}
                    else sqlite3.SQLITE_OK
                )
            )
            yield connection

    monkeypatch.setattr(ledger, "_connect", only_totals)
    totals = ledger.session_usage("ses_a")
    assert totals.request_count == 2
    assert totals.agent_request_count == totals.compaction_request_count == 1
    assert totals.total_tokens == 7
    assert totals.priced_request_count == totals.unpriced_request_count == 1
    assert service.session_values("ses_a") == {
        "input_tokens": 3,
        "output_tokens": 4,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "estimated_cost": 0.25,
    }
    assert ledger.session_usage("ses_other").request_count == 1
    assert service.session_values("ses_missing") is None


def test_raw_request_and_run_consumption_survive_correction_reset_and_repeat(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    service = UsageService(ledger)
    assert service.get_raw_entry("not-reported") is None
    assert service.run_usage("ses_a", "run_0") is None
    source = _entry(0).model_copy(
        update={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 2,
            "cache_write_tokens": 3,
            "estimated_cost": 0.5,
            "metadata": {"billing_mode": "usage_based", "harness_scopes": {"owner": ["scope"]}},
        }
    )
    ledger.record(source)
    other = _entry(1).model_copy(
        update={
            "run_id": source.run_id,
            "purpose": "compaction",
            "input_tokens": 7,
            "output_tokens": 4,
            "cache_read_tokens": 1,
            "cache_write_tokens": 2,
            "metadata": {"billing_mode": "subscription"},
        }
    )
    ledger.record(other)
    totals = service.run_usage("ses_a", "run_0")
    assert totals is not None
    assert (
        totals.request_count,
        totals.input_tokens,
        totals.output_tokens,
        totals.total_tokens,
    ) == (2, 17, 9, 26)
    assert (totals.cache_read_tokens, totals.cache_write_tokens) == (3, 5)
    assert (totals.estimated_cost, totals.priced_request_count, totals.unpriced_request_count) == (
        0.5,
        1,
        1,
    )
    assert totals.billing_modes == ("subscription", "usage_based")
    assert service.run_usage("different-session", "run_0") is None
    service.replace_entry(source.entry_id, input_tokens=0, output_tokens=0, note="invoice")
    assert ledger.session_scope_tokens("ses_a") == {}
    assert service.run_usage("ses_a", "run_0") == totals
    assert service.get_raw_entry(source.entry_id) == source
    service.reset(note="hide effective bill")
    assert service.summarize().totals.request_count == 0
    cold = LocalUsageLedger(tmp_path)
    assert cold.record(source) == source
    assert cold.record(other) == other
    assert cold.run_usage("ses_a", "run_0") == totals
    assert cold.get_raw(source.entry_id) == source
    with pytest.raises(KeyError):
        cold.get(source.entry_id)
    assert cold.session_scope_tokens("ses_a") == {}
    assert cold.summarize(UsageFilter()).totals.request_count == 0
    assert cold.audit().total_events == 4
    with pytest.raises(PersistenceError, match="conflicts"):
        cold.record(source.model_copy(update={"input_tokens": 100}))
    assert cold.run_usage("ses_a", "run_0") == totals


def test_zero_report_is_known_and_independent_plugin_has_no_run_summary(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    zero = _entry(0).model_copy(
        update={"input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0}
    )
    ledger.record(zero)
    totals = ledger.run_usage("ses_a", "run_0")
    assert totals is not None and totals.request_count == 1
    assert totals.total_tokens == 0
    assert totals.priced_request_count == 1
    plugin = _entry(1).model_copy(
        update={"purpose": "plugin", "plugin_id": "example", "run_id": None, "run_kind": None}
    )
    ledger.record(plugin)
    ledger.reset(UsageFilter(), note="hide")
    assert ledger.record(plugin) == plugin
    assert ledger.get_raw(plugin.entry_id) == plugin
    assert ledger.run_usage("ses_a", "run_0") == totals
    assert ledger.summarize(UsageFilter()).totals.request_count == 0
    assert ledger.audit().total_events == 3


@pytest.mark.parametrize(
    ("table", "operation"),
    [
        ("raw_requests", "INSERT"),
        ("raw_run_totals", "INSERT"),
        ("raw_run_totals", "UPDATE"),
        ("entry_records", "INSERT"),
        ("scopes", "INSERT"),
    ],
)
def test_native_failure_rolls_back_raw_identity_totals_bill_and_audit(tmp_path, table, operation):
    ledger = LocalUsageLedger(tmp_path)
    first = ledger.record(_entry(0))
    before = ledger.run_usage("ses_a", "run_0")
    candidate = _entry(1).model_copy(
        update={
            "run_id": first.run_id if operation == "UPDATE" else "run_1",
            "metadata": {"harness_scopes": {"owner": ["scope"]}},
        }
    )
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute(
            f"CREATE TRIGGER fail_raw AFTER {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'injected native usage failure'); END"
        )
        connection.commit()
    with pytest.raises(PersistenceError, match="storage failed"):
        ledger.record(candidate)
    cold = LocalUsageLedger(tmp_path)
    assert cold.get_raw(candidate.entry_id) is None
    assert cold.run_usage("ses_a", "run_0") == before
    assert cold.run_usage("ses_a", "run_1") is None
    assert cold.summarize(UsageFilter()).totals.request_count == 1
    assert cold.audit().total_events == 1
    assert cold.session_scope_tokens("ses_a") == {}
    with closing(sqlite3.connect(ledger.path)) as connection:
        connection.execute("DROP TRIGGER fail_raw")
        connection.commit()
    cold.record(candidate)
    assert cold.get_raw(candidate.entry_id) == candidate
    assert cold.run_usage("ses_a", candidate.run_id).request_count == (
        2 if operation == "UPDATE" else 1
    )


def _record_in_process(root, index, ready, release):
    ledger = LocalUsageLedger(root)
    ready.put(index)
    if not release.wait(20):
        raise TimeoutError("recording processes were not released")
    common = _entry(0)
    ledger.record(common)
    ledger.record(_entry(index + 1).model_copy(update={"run_id": common.run_id}))


@pytest.mark.parametrize("hidden", [False, True])
def test_processes_share_one_raw_request_and_accumulate_distinct_reports_once(tmp_path, hidden):
    ledger = LocalUsageLedger(tmp_path)
    ledger.initialize()
    if hidden:
        ledger.record(_entry(0))
        ledger.reset(UsageFilter(), note="hide before competing repeats")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Queue(), context.Event()
    processes = [
        context.Process(target=_record_in_process, args=(tmp_path, index, ready, release))
        for index in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=20), ready.get(timeout=20)} == {0, 1}
        release.set()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        totals = ledger.run_usage("ses_a", "run_0")
        assert totals.request_count == 3
        assert totals.input_tokens == 6
        assert totals.output_tokens == 6
        assert ledger.audit().total_events == (4 if hidden else 3)
        assert ledger.summarize(UsageFilter()).totals.request_count == (2 if hidden else 3)
        assert ledger.get_raw("use_0") == _entry(0)
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=10)
        ready.close()
        ready.join_thread()


@pytest.mark.parametrize("same_run", [False, True])
def test_raw_queries_do_not_decode_or_scan_request_history(tmp_path, monkeypatch, same_run):
    ledger = LocalUsageLedger(tmp_path)
    source = ledger.record(_entry(0))
    connect = ledger._connect
    steps = [0]

    def step():
        steps[0] += 1
        return 0

    @contextmanager
    def counted():
        with connect() as connection:
            connection.set_progress_handler(step, 1)
            yield connection

    monkeypatch.setattr(ledger, "_connect", counted)
    assert ledger.get_raw(source.entry_id) == source
    assert ledger.run_usage("ses_a", "run_0").request_count == 1
    baseline = steps[0]
    for index in range(1, 401):
        entry = _entry(index)
        ledger.record(entry.model_copy(update={"run_id": "run_0"}) if same_run else entry)
    ledger.reset(UsageFilter(), note="raw history remains")
    steps[0] = 0
    assert ledger.get_raw(source.entry_id) == source
    assert ledger.run_usage("ses_a", "run_0").request_count == (401 if same_run else 1)
    assert steps[0] <= baseline + 10

    def no_decode(*args, **kwargs):
        raise AssertionError("Run raw totals must not decode request or audit bodies")

    monkeypatch.setattr("ouro.billing.local.decode_versioned_record_json", no_decode)
    assert ledger.run_usage("ses_a", "run_0").request_count == (401 if same_run else 1)


def _record_until_summary(root, entered, release):
    original = LocalUsageLedger._add_run_usage

    def hold(connection, entry):
        original(connection, entry)
        entered.set()
        if not release.wait(30):
            raise TimeoutError("parent did not settle the injected transaction boundary")

    LocalUsageLedger._add_run_usage = staticmethod(hold)
    LocalUsageLedger(root).record(_entry(1))


def test_process_exit_inside_native_record_leaves_no_partial_raw_or_effective_state(tmp_path):
    ledger = LocalUsageLedger(tmp_path)
    original = ledger.record(_entry(0))
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_record_until_summary, args=(tmp_path, entered, release))
    process.start()
    try:
        assert entered.wait(20)
        # A live reader sees the last committed state while the child holds its write transaction.
        assert ledger.get_raw("use_1") is None
        assert ledger.run_usage("ses_a", "run_1") is None
        process.terminate()
        process.join(timeout=20)
        assert process.exitcode is not None and process.exitcode != 0
        cold = LocalUsageLedger(tmp_path)
        assert cold.get_raw(original.entry_id) == original
        assert cold.get_raw("use_1") is None
        assert cold.run_usage("ses_a", "run_1") is None
        assert cold.summarize(UsageFilter()).totals.request_count == 1
        assert cold.audit().total_events == 1
        cold.record(_entry(1))
        assert cold.run_usage("ses_a", "run_1").request_count == 1
        assert cold.audit().total_events == 2
    finally:
        # A killed Event waiter cannot acknowledge Condition notifications.
        # Terminate this isolated child without signalling its abandoned wait.
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
