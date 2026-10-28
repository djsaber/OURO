from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.sessions.service import SessionService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.context import ContextMetrics, ProviderContext
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, Usage
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.context_facts import save_context


def make_store(root, backend):
    store = (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )
    store.initialize()
    return store


@pytest.fixture(params=["files", "sqlite"])
def prepared(tmp_path, request):
    store = make_store(tmp_path, request.param)
    session = SessionRecord(session_id="ses_current")
    run = RunRecord(
        session_id=session.session_id,
        run_id="run_current",
        status="running",
        model_round_count=1,
        current_request_id="ctx_current",
    )
    store.create_session(session)
    store.save_run(run)
    context = ContextSnapshotRecord(
        context_id="ctx_current",
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="request_current",
        model_round=1,
        provider_id="test",
        model_id="test",
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
        provider_context=ProviderContext(system_prompt="exact prepared input"),
    )
    save_context(store, context)
    return store, run, context, UsageService(LocalUsageLedger(tmp_path))


def report(usage, run, context):
    return usage.record_provider_usage(
        run=run,
        request_id=context.turn_id,
        purpose="agent",
        model=ModelMetadata(
            provider_id=context.provider_id,
            model_id=context.model_id,
            endpoint_id=context.endpoint_id,
        ),
        usage=Usage(input_tokens=7, output_tokens=3),
    )


def forbid_input_reads(store, monkeypatch):
    if isinstance(store, LocalSessionStore):
        original = Path.open

        def open_path(path, *args, **kwargs):
            if path.parent.name == "request-content":
                pytest.fail("settlement accessed the immutable request input")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_path)
    else:
        connect = store._connect

        def guarded_connect():
            connection = connect()

            def authorize(action, table, column, database, source):
                return (
                    sqlite3.SQLITE_DENY
                    if table == "latest_contexts"
                    and (column == "payload" or action != sqlite3.SQLITE_READ)
                    else sqlite3.SQLITE_OK
                )

            connection.set_authorizer(authorize)
            return connection

        monkeypatch.setattr(store, "_connect", guarded_connect)


def test_query_never_settles_and_recovery_only_references_original_usage(prepared, monkeypatch):
    store, run, context, usage = prepared
    original = report(usage, run, context)
    usage.replace_entry(original.entry_id, input_tokens=0, note="invoice correction")
    usage.reset(note="hide invoice")
    service = SessionService(store, usage_service=usage)
    before = store.load_run(run.session_id, run.run_id)
    forbid_input_reads(store, monkeypatch)
    status = service.context_status(run.session_id)
    assert status.usage_entry_id is None and status.provider_reported_input_tokens is None
    assert store.load_latest_context_header(run.session_id) == context.header()
    recovery = SessionRecoveryService(store, usage_service=usage)
    recovery.settle_request(run.session_id)
    header = store.load_latest_context_header(run.session_id)
    assert header.provider_done_at is None and header.interrupted_at is not None
    assert header.usage_entry_id == original.entry_id
    assert "provider_reported_input_tokens" not in header.model_dump()
    assert store.load_run(run.session_id, run.run_id) == before
    status = service.context_status(run.session_id)
    assert status.provider_reported_input_tokens == 7 and status.completed_at is None
    assert status.interrupted_at == header.interrupted_at
    recovery.settle_request(run.session_id)
    assert store.load_latest_context_header(run.session_id) == header
    assert usage.run_usage(run.session_id, run.run_id).request_count == 1
    assert usage.list_entries().entries == ()


def test_missing_usage_is_interrupted_unknown_and_late_report_does_not_rewrite_done(prepared):
    store, run, context, usage = prepared
    recovery = SessionRecoveryService(store, usage_service=usage)
    recovery.settle_request(run.session_id)
    unknown = store.load_latest_context_header(run.session_id)
    assert unknown.interrupted_at is not None and unknown.usage_entry_id is None
    assert usage.run_usage(run.session_id, run.run_id) is None
    original = report(usage, run, context)
    with pytest.raises(PersistenceError, match="conflicts"):
        store.settle_request_header(
            run.session_id,
            context.context_id,
            settled_at=utc_now(),
            provider_done=True,
            usage_entry_id=original.entry_id,
        )
    recovery.settle_request(run.session_id)
    known = store.load_latest_context_header(run.session_id)
    assert known.interrupted_at == unknown.interrupted_at and known.provider_done_at is None
    assert known.usage_entry_id == original.entry_id


@pytest.mark.parametrize("change", ["replace", "withdraw"])
def test_ledger_lookup_releases_session_lock_and_rechecks_current_request(
    prepared, monkeypatch, change
):
    store, run, context, usage = prepared
    report(usage, run, context)
    arrived, release = Event(), Event()
    read = usage.ledger.get_raw

    def delayed(entry_id):
        arrived.set()
        assert release.wait(10)
        return read(entry_id)

    monkeypatch.setattr(usage.ledger, "get_raw", delayed)
    recovery = SessionRecoveryService(store, usage_service=usage)
    replacement = context.model_copy(update={"context_id": "ctx_next", "turn_id": "request_next"})
    message = MessageRecord.text(session_id=run.session_id, role="assistant", text="withdrawn")
    if change == "withdraw":
        store.append_message(message)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(recovery.settle_request, run.session_id)
        try:
            assert arrived.wait(10)

            def change_current():
                with store.session_mutation(run.session_id):
                    if change == "replace":
                        save_context(store, replacement)
                    else:
                        store.commit_session(
                            SessionCommit(
                                before=store.load_session_record(run.session_id),
                                before_message_id=message.message_id,
                            )
                        )

            pool.submit(change_current).result(timeout=10)
        finally:
            release.set()
        pending.result(timeout=10)
    expected = replacement.header() if change == "replace" else None
    assert store.load_latest_context_header(run.session_id) == expected


def test_live_owner_prevents_request_recovery(prepared):
    store, run, context, usage = prepared
    report(usage, run, context)
    lock = store.acquire_session_lock(run.session_id, run.run_id)
    assert lock is not None
    try:
        SessionRecoveryService(store, usage_service=usage).settle_request(run.session_id)
        assert store.load_latest_context_header(run.session_id) == context.header()
    finally:
        lock.release()


def test_recovery_native_write_failure_keeps_request_pending(prepared):
    store, run, context, usage = prepared
    original = report(usage, run, context)
    recovery = SessionRecoveryService(store, usage_service=usage)
    database = (
        store.sessions_dir / run.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_settlement AFTER UPDATE ON request_headers "
            "BEGIN SELECT RAISE(ABORT, 'settlement failed'); END"
        )
    try:
        with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="settlement failed"):
            recovery.settle_request(run.session_id)
    finally:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_settlement")
    assert store.load_latest_context_header(run.session_id) == context.header()
    assert usage.request_usage(context) == original
    recovery.settle_request(run.session_id)
    assert store.load_latest_context_header(run.session_id).usage_entry_id == original.entry_id
    assert usage.run_usage(run.session_id, run.run_id).request_count == 1


def test_foreign_usage_reference_and_fact_are_rejected(prepared, monkeypatch):
    store, run, context, usage = prepared
    original = report(usage, run, context)
    with pytest.raises(ValueError, match="conflicts"):
        store.settle_request_header(
            run.session_id,
            context.context_id,
            settled_at=utc_now(),
            provider_done=True,
            usage_entry_id="use_foreign",
        )
    monkeypatch.setattr(
        usage.ledger, "get_raw", lambda _: original.model_copy(update={"session_id": "ses_foreign"})
    )
    with pytest.raises(PersistenceError, match="does not belong"):
        SessionRecoveryService(store, usage_service=usage).settle_request(run.session_id)
    assert store.load_latest_context_header(run.session_id) == context.header()


def test_request_query_without_original_ledger_keeps_input_and_reports_unknown(prepared, tmp_path):
    store, run, context, usage = prepared
    original = report(usage, run, context)
    store.settle_request_header(
        run.session_id,
        context.context_id,
        settled_at=utc_now(),
        provider_done=True,
        usage_entry_id=original.entry_id,
    )
    # A Session-only transfer does not also transfer the global billing database.
    query = SessionService(
        store, usage_service=UsageService(LocalUsageLedger(tmp_path / "other-ledger"))
    )
    result = query.context(run.session_id)
    assert result.provider_context == context.provider_context
    assert result.status.usage_entry_id == original.entry_id
    assert result.status.provider_reported_input_tokens is None
    assert result.status.completed_at is not None


def _die_between_stores(root, backend, ready, release):
    root = Path(root)

    class CountedProvider(FauxProvider):
        async def stream(self, **kwargs):
            with (root / "provider-calls.txt").open("a", encoding="utf-8") as output:
                output.write("request\n")
                output.flush()
            async for event in super().stream(**kwargs):
                yield event

    async def run():
        store = make_store(root, backend)
        runtime = RuntimeService(
            store=store,
            tools=ToolRegistry(),
            usage_service=UsageService(LocalUsageLedger(root)),
            adapter=RuntimeAdapter(
                CountedProvider(
                    [
                        ProviderStreamEvent(type="text_delta", text="partial before commit"),
                        ProviderStreamEvent(
                            type="done", usage=Usage(input_tokens=7, output_tokens=3)
                        ),
                    ]
                ),
                ModelMetadata(provider_id="test", model_id="test"),
            ),
        )

        def before_session_settlement(**kwargs):
            ready.put(kwargs["session_id"])
            assert release.wait(30)

        runtime.context_preparation.record_request_settlement = before_session_settlement
        await runtime.run("request")

    asyncio.run(run())


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_process_death_after_ledger_commit_recovers_only_local_settlement(tmp_path, backend):
    context = multiprocessing.get_context("spawn")
    ready, release = context.Queue(), context.Event()
    process = context.Process(
        target=_die_between_stores, args=(str(tmp_path), backend, ready, release)
    )
    process.start()
    try:
        session_id = ready.get(timeout=30)
        store = make_store(tmp_path, backend)
        usage = UsageService(LocalUsageLedger(tmp_path))
        request = store.load_latest_context_header(session_id)
        original = usage.request_usage(request)
        assert original is not None and request.usage_entry_id is None
        usage.replace_entry(original.entry_id, input_tokens=0, note="invoice correction")
        usage.reset(note="hide before recovery")
        process.terminate()
        process.join(timeout=15)
        assert not process.is_alive() and process.exitcode != 0
        service = SessionService(store, usage_service=usage)
        assert service.context_status(session_id).usage_entry_id is None

        class NoReplayProvider(FauxProvider):
            async def stream(self, **kwargs):
                pytest.fail("request settlement replayed the Provider")
                yield

        async def recover():
            runtime = RuntimeService(
                store=store,
                usage_service=usage,
                tools=ToolRegistry(),
                adapter=RuntimeAdapter(
                    NoReplayProvider(), ModelMetadata(provider_id="test", model_id="test")
                ),
            )
            try:
                return await runtime.reconcile_execution_session(session_id)
            finally:
                await runtime.close()

        recovered = asyncio.run(recover())
        header = store.load_latest_context_header(session_id)
        assert header.usage_entry_id == original.entry_id
        assert header.interrupted_at is not None and header.provider_done_at is None
        assert recovered.runs[0].status == "recoverable"
        messages = store.load_run_messages(session_id, request.run_id)
        assistants = [message for message in messages if message.role == "assistant"]
        assert len(assistants) == 1 and assistants[0].metadata["incomplete"] is True
        asyncio.run(recover())
        assert store.load_run_messages(session_id, request.run_id) == messages
        assert (tmp_path / "provider-calls.txt").read_text(encoding="utf-8") == "request\n"
        assert usage.run_usage(session_id, request.run_id).request_count == 1
        assert service.context_status(session_id).provider_reported_input_tokens == 7
        assert usage.list_entries().entries == ()
    finally:
        # A terminated Event waiter must not be notified: Windows can wait for its acknowledgement.
        if process.is_alive():
            process.terminate()
        process.join(timeout=15)
        ready.close()
