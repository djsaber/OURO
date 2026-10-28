from __future__ import annotations

import asyncio
import multiprocessing
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from threading import Event, Thread

import pytest

from ouro.application.context.summary import LLMSummaryCompactionStrategy
from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionAbortedError, CompactionRequest, CompactionStrategyError
from ouro.core.context import ContextMetrics, ProviderContext
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderCompletion, ProviderStreamEvent, Usage
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.context_facts import save_context


class SummaryProvider:
    provider_id = "summary-test"

    def __init__(self, *, responses=None, gated=False, before_done=None):
        self.responses = responses or [("<summary>summary</summary>", "stop")]
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gated = gated
        self.before_done = before_done
        self.calls = 0

    async def stream(self, **kwargs):
        text, finish_reason = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        yield ProviderStreamEvent(type="text_delta", text=text)
        if self.gated and self.calls == 1:
            self.started.set()
            await self.release.wait()
        if self.before_done is not None:
            self.before_done()
        yield ProviderStreamEvent(
            type="done",
            usage=Usage(input_tokens=7, output_tokens=2),
            completion=ProviderCompletion(finish_reason=finish_reason),
        )


def adapter(provider, endpoint):
    return RuntimeAdapter(
        provider,
        ModelMetadata(
            model_id="summary-model", provider_id=provider.provider_id, endpoint_id=endpoint
        ),
    )


def control_metadata(scope):
    return {
        "session_control_snapshot": {
            "activations": [
                {
                    "controller_id": "owner/session_controllers/control",
                    "activation_id": "activation",
                    "usage_scope": scope,
                }
            ]
        }
    }


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    store_type = request.param
    result = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "home"
    )
    result.initialize()
    return result


@asynccontextmanager
async def summary_host(tmp_path, store, provider, *, configured=None, with_ledger=True):
    usage = UsageService(LocalUsageLedger(tmp_path / "usage")) if with_ledger else None
    runtime = RuntimeService(
        store=store,
        adapter=adapter(provider, "main"),
        compaction_adapter=adapter(configured, "dedicated") if configured is not None else None,
        tools=ToolRegistry(),
        usage_service=usage,
    )
    session = SessionRecord(workspace=str(tmp_path / "original"))
    store.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        kind="compaction",
        status="running",
        metadata=control_metadata("original"),
    )
    store.save_run(run)
    session = session.model_copy(
        update={"active_run_id": run.run_id, "last_run_id": run.run_id, "status": "running"}
    )
    store.save_session(session)
    request = CompactionRequest(
        session_id=session.session_id,
        run_id=run.run_id,
        strategy_id=LLMSummaryCompactionStrategy.strategy_id,
        trigger="manual",
        messages=[
            MessageRecord.text(session_id=session.session_id, role=role, text=f"{role} {index}")
            for index in range(2)
            for role in ("user", "assistant")
        ],
    )
    try:
        yield runtime, request, session, run
    finally:
        await runtime.close()


@pytest.mark.parametrize("mode", ["single", "fallback", "retry_chunks"])
async def test_summary_binds_attribution_before_all_requests(tmp_path, store, monkeypatch, mode):
    provider = SummaryProvider(
        gated=mode != "fallback",
        responses=[
            ("<summary>truncated", "length"),
            ("<summary>first chunk</summary>", "stop"),
            ("<summary>final</summary>", "stop"),
        ]
        if mode == "retry_chunks"
        else None,
    )
    configured = (
        SummaryProvider(gated=True, responses=[("invalid summary", "stop")])
        if mode == "fallback"
        else None
    )
    gated = configured or provider
    async with summary_host(tmp_path, store, provider, configured=configured) as host:
        runtime, request, session, run = host
        strategy = runtime.compaction_strategies.get(request.strategy_id)
        task = asyncio.create_task(strategy.compact(request))
        try:
            await asyncio.wait_for(gated.started.wait(), 10)
            with store.session_mutation(session.session_id):
                store.save_session(session.model_copy(update={"workspace": str(tmp_path / "new")}))
                store.save_run(
                    store.load_run(session.session_id, run.run_id).model_copy(
                        update={"metadata": control_metadata("new")}
                    )
                )

            def forbidden(*args, **kwargs):
                pytest.fail("summary usage must not read or write Session/Run after preparation")

            record_usage = runtime.usage.record_provider_usage

            def record_bound_usage(**kwargs):
                with monkeypatch.context() as patch:
                    for name in ("load_session_record", "load_run", "save_run"):
                        patch.setattr(store, name, forbidden)
                    return record_usage(**kwargs)

            monkeypatch.setattr(runtime.usage, "record_provider_usage", record_bound_usage)
            gated.release.set()
            artifact = await asyncio.wait_for(task, 10)
        finally:
            gated.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        expected = {"single": 1, "fallback": 2, "retry_chunks": 3}[mode]
        entries = runtime.usage.list_entries().entries
        assert len(entries) == len(artifact.model_outputs) == expected
        assert {entry.request_id for entry in entries} == {
            output.request_id for output in artifact.model_outputs
        }
        assert all(entry.workspace == session.workspace for entry in entries)
        assert all(entry.metadata["harness_scopes"] == {"owner": ["original"]} for entry in entries)
        assert all(
            entry.run_id == run.run_id and entry.run_kind == "compaction" for entry in entries
        )
        assert all(entry.purpose == "compaction" for entry in entries)
        totals = runtime.usage.run_usage(session.session_id, run.run_id)
        assert totals.request_count == expected and totals.agent_request_count == 0
        assert totals.total_tokens == 9 * expected
        current = store.load_run(session.session_id, run.run_id)
        assert current.summary_request_count == expected and current.model_round_count == 0
        for entry in entries:
            header = store.load_request_header(session.session_id, entry.request_id)
            assert header.purpose == "compaction" and header.started_at is not None
            assert header.provider_done_at is not None and header.usage_entry_id == entry.entry_id
        assert store.load_latest_context_header(session.session_id) is None
        if mode == "fallback":
            assert {entry.endpoint_id for entry in entries} == {"main", "dedicated"}


async def test_summary_reporting_precedes_session_settlement(tmp_path, store, monkeypatch):
    provider = SummaryProvider(gated=True)
    async with summary_host(tmp_path, store, provider) as (runtime, request, session, _):
        task = asyncio.create_task(
            runtime.compaction_strategies.get(request.strategy_id).compact(request)
        )
        acquired, release, reported = Event(), Event(), Event()
        record = runtime.usage.ledger.record

        def record_report(entry):
            result = record(entry)
            reported.set()
            return result

        monkeypatch.setattr(runtime.usage.ledger, "record", record_report)

        def hold_mutation():
            with store.session_mutation(session.session_id):
                acquired.set()
                assert release.wait(10)

        worker = Thread(target=hold_mutation)
        try:
            await asyncio.wait_for(provider.started.wait(), 10)
            worker.start()
            assert await asyncio.to_thread(acquired.wait, 10)
            provider.release.set()
            assert await asyncio.to_thread(reported.wait, 10)
            assert runtime.usage.list_entries().entries[0].input_tokens == 7
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, 10)
        finally:
            release.set()
            provider.release.set()
            if worker.ident is not None:
                await asyncio.to_thread(worker.join, 10)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not worker.is_alive()


async def test_usage_failure_is_terminal_before_route_fallback(tmp_path, store):
    provider, configured = SummaryProvider(), SummaryProvider()
    async with summary_host(tmp_path, store, provider, configured=configured) as host:
        runtime, request, session, run = host
        ledger = runtime.usage.ledger
        ledger.initialize()
        with closing(sqlite3.connect(ledger.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_raw AFTER INSERT ON raw_run_totals "
                "BEGIN SELECT RAISE(ABORT, 'usage commit failed'); END"
            )
        with pytest.raises(CompactionStrategyError, match="usage storage failed") as raised:
            await runtime.compaction_strategies.get(request.strategy_id).compact(request)
        assert raised.value.terminal
        assert configured.calls == 1 and provider.calls == 0
        assert runtime.usage.run_usage(session.session_id, run.run_id) is None
        assert runtime.usage.list_entries().entries == ()


async def test_missing_run_fails_before_any_summary_provider(tmp_path, store):
    provider, configured = SummaryProvider(), SummaryProvider()
    async with summary_host(tmp_path, store, provider, configured=configured) as host:
        runtime, request, _, _ = host
        request = request.model_copy(update={"run_id": "run_missing"})
        with pytest.raises(CompactionStrategyError, match="cannot bind summary usage") as raised:
            await runtime.compaction_strategies.get(request.strategy_id).compact(request)
        assert raised.value.terminal
        assert configured.calls == provider.calls == 0
        assert runtime.usage.list_entries().entries == ()


@pytest.mark.parametrize("outcome", ["invalid", "cancelled", "buffered_cancel"])
async def test_unadopted_summary_keeps_reported_usage(tmp_path, store, monkeypatch, outcome):
    signal = AbortSignal()
    provider = SummaryProvider(
        responses=[("invalid summary", "stop")] if outcome == "invalid" else None,
        before_done=(lambda: signal.abort("cancelled before output consumption"))
        if outcome == "buffered_cancel"
        else None,
    )
    async with summary_host(tmp_path, store, provider) as (runtime, request, session, run):
        if outcome == "cancelled":
            record = runtime.usage.ledger.record

            def record_then_cancel(entry):
                result = record(entry)
                signal.abort("cancelled after reported usage")
                return result

            monkeypatch.setattr(runtime.usage.ledger, "record", record_then_cancel)
        error = CompactionStrategyError if outcome == "invalid" else CompactionAbortedError
        with pytest.raises(error):
            await runtime.compaction_strategies.get(request.strategy_id).compact(
                request, signal=signal
            )
        assert provider.calls == 1
        assert runtime.usage.run_usage(session.session_id, run.run_id).total_tokens == 9
        assert store.load_active_compaction(session.session_id) is None


async def test_unmetered_summary_keeps_request_identity_without_inventing_usage(tmp_path, store):
    provider = SummaryProvider()
    async with summary_host(tmp_path, store, provider, with_ledger=False) as host:
        runtime, request, session, run = host
        artifact = await runtime.compaction_strategies.get(request.strategy_id).compact(request)
        current = store.load_run(session.session_id, run.run_id)
        header = store.load_request_header(session.session_id, current.current_request_id)
        assert current.summary_request_count == 1
        assert header.provider_done_at is not None and header.usage_entry_id is None
        assert artifact.summary == "summary" and provider.calls == 1
        assert store.load_latest_context_header(session.session_id) is None


async def test_summary_ledger_commit_survives_header_write_failure(tmp_path, store):
    provider = SummaryProvider()
    async with summary_host(tmp_path, store, provider) as (runtime, request, session, run):
        database = (
            store.sessions_dir / session.session_id / "session-data.sqlite3"
            if isinstance(store, LocalSessionStore)
            else store.database
        )
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_summary_settlement AFTER UPDATE ON request_headers "
                "BEGIN SELECT RAISE(ABORT, 'summary settlement failed'); END"
            )
        try:
            with pytest.raises(
                CompactionStrategyError, match="summary settlement failed"
            ) as raised:
                await runtime.compaction_strategies.get(request.strategy_id).compact(request)
            assert raised.value.terminal and provider.calls == 1
        finally:
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("DROP TRIGGER fail_summary_settlement")
        latest = store.load_run(session.session_id, run.run_id)
        header = store.load_request_header(session.session_id, latest.current_request_id)
        assert header.provider_done_at is None and header.usage_entry_id is None
        assert runtime.usage.run_usage(session.session_id, run.run_id).total_tokens == 9
        SessionRecoveryService(store, usage_service=runtime.usage).settle_request(
            session.session_id
        )
        recovered = store.load_request_header(session.session_id, header.context_id)
        assert recovered.usage_entry_id == recovered.expected_usage_entry_id
        assert recovered.interrupted_at is not None and recovered.provider_done_at is None
        assert (
            recovered.response_compaction_id is None
            and store.load_active_compaction(session.session_id) is None
        )
        assert provider.calls == 1


def _summary_usage_process(root, store_path, sqlite, session_id, run_id):
    store = SQLiteSessionStore(store_path) if sqlite else LocalSessionStore(store_path)
    store.initialize()
    run = store.load_run(session_id, run_id)
    usage = UsageService(LocalUsageLedger(Path(root) / "usage"))
    usage.record_provider_usage(
        run=run,
        model=ModelMetadata(
            model_id="summary-model", provider_id="summary-test", endpoint_id="main"
        ),
        request_id="req_interrupted_summary",
        purpose="compaction",
        usage=Usage(input_tokens=7, output_tokens=2),
    )
    os._exit(29)


async def test_summary_process_exit_and_invoice_reset_preserve_raw_settlement(tmp_path, store):
    provider = SummaryProvider()
    async with summary_host(tmp_path, store, provider) as (runtime, request, session, run):
        ordinary = ContextSnapshotRecord(
            session_id=session.session_id,
            run_id="run_prior",
            turn_id="ordinary",
            model_round=1,
            provider_id="summary-test",
            model_id="summary-model",
            metrics=ContextMetrics(estimated_input_tokens=0, estimated_fixed_tokens=0),
            provider_context=ProviderContext(system_prompt="the exact ordinary request"),
        )
        save_context(store, ordinary)
        recorder = runtime.run_lifecycle.prepare_summary_requests(session.session_id, run.run_id)
        recorder.start(
            "req_interrupted_summary",
            model=runtime.adapter.model,
            metrics=ContextMetrics(estimated_input_tokens=0, estimated_fixed_tokens=0),
        )
        sqlite = isinstance(store, SQLiteSessionStore)
        store_path = store.database if sqlite else store.sessions_dir.parent
        process = multiprocessing.get_context("spawn").Process(
            target=_summary_usage_process,
            args=(str(tmp_path), str(store_path), sqlite, session.session_id, run.run_id),
        )
        process.start()
        try:
            await asyncio.to_thread(process.join, 15)
            assert process.exitcode == 29
        finally:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        pending = store.load_request_header(session.session_id, "req_interrupted_summary")
        assert pending.started_at is not None and pending.usage_entry_id is None
        runtime.usage.reset(note="invoice reset after original report")
        recovery = SessionRecoveryService(store, usage_service=runtime.usage)
        recovery.recover_runtime(session.session_id)
        recovery.recover_runtime(session.session_id)
        final = store.load_request_header(session.session_id, pending.context_id)
        assert final.usage_entry_id == pending.expected_usage_entry_id
        assert final.provider_done_at is None and final.interrupted_at is not None
        assert final.response_compaction_id is None
        assert store.load_latest_context(session.session_id) == ordinary
        assert runtime.usage.run_usage(session.session_id, run.run_id).total_tokens == 9
        assert runtime.usage.list_entries().entries == ()
        assert provider.calls == 0 and store.load_active_compaction(session.session_id) is None
