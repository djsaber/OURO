from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from threading import Event, Thread

import pytest

from ouro.application.models.requests import ModelRequestPorts, RunModelRequests
from ouro.application.runtime.service import RuntimeService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.model_requests import ModelRequestBlocked, RunModelRequest
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, Usage
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    store_type = request.param
    result = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "home"
    )
    result.initialize()
    return result


@asynccontextmanager
async def request_host(tmp_path, store, *, reports=None, with_ledger=True):
    if reports is None:
        reports = (Usage(input_tokens=5, output_tokens=2),)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text="answer"),
                        ProviderStreamEvent(type="done", usage=usage),
                    ]
                    for usage in reports
                ]
            ),
            ModelMetadata(model_id="test", provider_id="test"),
        ),
        tools=ToolRegistry(),
        usage_service=UsageService(LocalUsageLedger(tmp_path / "usage")) if with_ledger else None,
    )
    session = SessionRecord()
    store.create_session(session)
    session, run, _ = runtime.run_lifecycle.start(
        session=session,
        run=RunRecord(session_id=session.session_id, status="running"),
        snapshot=store.load_runtime_session(session.session_id),
    )
    try:
        yield runtime, RunModelRequest(session, run, 1, "first", 0, AbortSignal())
    finally:
        await runtime.close()


def requests(runtime, budget=None):
    return RunModelRequests(
        adapter=runtime.adapter,
        tool_definitions=runtime.tools.definitions,
        run_budget=budget,
        ports=ModelRequestPorts(
            prepare_context=runtime.context_preparation.prepare,
            record_settlement=runtime.context_preparation.record_request_settlement,
            record_usage=runtime.run_lifecycle.record_usage,
            commit_model_request=runtime._commit_model_request,
            publish_committed=runtime.event_recorder.publish_committed,
            append_draft=runtime.store.append_response_draft,
            emit=runtime.event_recorder.emit,
            run_usage=runtime.usage.run_usage if runtime.usage is not None else None,
        ),
    )


async def test_missing_ledger_has_no_run_copy_and_rejects_token_budget(tmp_path, store):
    async with request_host(tmp_path, store, with_ledger=False) as (runtime, request):
        with pytest.raises(ValueError, match="require a usage ledger"):
            requests(runtime, RunBudgetPolicy(max_input_tokens=100))
        assert store.load_run(request.session.session_id, request.run.run_id).model_round_count == 0
        port = requests(runtime, RunBudgetPolicy(max_model_rounds=1))
        first = await port.execute(request)
        assert isinstance(first.outcome, ModelExecutionResult)
        assert first.outcome.error is None
        blocked = await port.execute(replace(request, run=first.run, model_round=2, turn_id="next"))
        assert blocked.outcome == ModelRequestBlocked("model_rounds")
        assert runtime.run_lifecycle.usage_for_run(first.run) is None
        saved = store.load_run(request.session.session_id, request.run.run_id).model_dump()
        assert "usage" not in saved and "usage_entry_ids" not in saved


async def test_ledger_failure_cannot_complete_request_header(tmp_path, store):
    async with request_host(tmp_path, store) as (runtime, request):
        ledger = runtime.usage.ledger
        ledger.initialize()
        with closing(sqlite3.connect(ledger.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_raw AFTER INSERT ON raw_run_totals "
                "BEGIN SELECT RAISE(ABORT, 'usage commit failed'); END"
            )
        result = await requests(runtime).execute(request)
        assert isinstance(result.outcome, ModelExecutionResult)
        assert "usage storage failed (IntegrityError)" in result.outcome.error
        header = store.load_latest_context_header(request.session.session_id)
        assert header is not None and header.provider_done_at is None
        assert runtime.usage.run_usage(request.session.session_id, request.run.run_id) is None
        assert runtime.usage.list_entries().entries == ()


async def test_usage_keeps_request_workspace_when_session_changes(tmp_path, store, monkeypatch):
    async with request_host(tmp_path, store) as (runtime, request):
        provider = runtime.adapter.provider
        stream = provider.stream

        async def change_workspace_before_done(**kwargs):
            async for event in stream(**kwargs):
                if event.type == "done":
                    with store.session_mutation(request.session.session_id):
                        current = store.load_session_record(request.session.session_id)
                        store.save_session(current.model_copy(update={"workspace": str(tmp_path)}))
                yield event

        monkeypatch.setattr(provider, "stream", change_workspace_before_done)
        result = await requests(runtime).execute(request)
        assert isinstance(result.outcome, ModelExecutionResult) and result.outcome.error is None
        assert store.load_session_record(request.session.session_id).workspace == str(tmp_path)
        entry = runtime.usage.list_entries().entries[0]
        assert entry.workspace == request.session.workspace is None


async def test_received_report_survives_cancellation_before_output_consumption(
    tmp_path, store, monkeypatch
):
    async with request_host(tmp_path, store) as (runtime, request):
        provider = runtime.adapter.provider
        stream = provider.stream

        async def abort_before_buffering_done(**kwargs):
            async for event in stream(**kwargs):
                if event.type == "done":
                    request.abort_signal.abort("cancel before output consumption")
                yield event

        monkeypatch.setattr(provider, "stream", abort_before_buffering_done)
        result = await requests(runtime).execute(request)
        assert isinstance(result.outcome, ModelExecutionResult) and result.outcome.aborted
        totals = runtime.usage.run_usage(request.session.session_id, request.run.run_id)
        assert totals.request_count == totals.agent_request_count == 1
        assert totals.total_tokens == 7
        assert len(runtime.usage.list_entries().entries) == 1


async def test_context_completion_failure_keeps_original_usage(tmp_path, store):
    async with request_host(tmp_path, store) as (runtime, request):
        database = (
            store.sessions_dir / request.session.session_id / "session-data.sqlite3"
            if isinstance(store, LocalSessionStore)
            else store.database
        )
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_completion AFTER UPDATE ON request_headers "
                "WHEN json_extract(NEW.payload, '$.provider_done_at') IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, 'context completion failed'); END"
            )
        result = await requests(runtime).execute(request)
        assert isinstance(result.outcome, ModelExecutionResult)
        assert "context completion failed" in result.outcome.error
        header = store.load_latest_context_header(request.session.session_id)
        assert header is not None and header.provider_done_at is None
        cold = LocalUsageLedger(runtime.usage.ledger.root)
        totals = cold.run_usage(request.session.session_id, request.run.run_id)
        assert (totals.request_count, totals.input_tokens, totals.output_tokens) == (1, 5, 2)
        entry = runtime.usage.list_entries().entries[0]
        runtime.usage.reset(note="hide invoice after failed Session settlement")
        assert cold.record(entry) == entry
        assert cold.run_usage(request.session.session_id, request.run.run_id) == totals
        assert runtime.usage.list_entries().entries == ()


async def test_usage_recording_holds_no_session_lock_and_never_rewrites_run(
    tmp_path, store, monkeypatch
):
    async with request_host(tmp_path, store) as (runtime, request):
        original_record = runtime.usage.ledger.record
        workers = []
        failures = []

        def record(entry):
            acquired = Event()

            def mutate():
                try:
                    with store.session_mutation(request.session.session_id):
                        acquired.set()
                except Exception as error:
                    failures.append(error)

            worker = Thread(target=mutate)
            workers.append(worker)
            worker.start()
            assert acquired.wait(10), "usage recording held the Session mutation lock"
            return original_record(entry)

        def unexpected_run_write(*args, **kwargs):
            pytest.fail("raw usage must not rewrite Run")

        before = store.load_run(request.session.session_id, request.run.run_id)
        monkeypatch.setattr(runtime.usage.ledger, "record", record)
        monkeypatch.setattr(store, "save_run", unexpected_run_write)
        try:
            for _ in range(2):
                runtime.run_lifecycle.record_usage(
                    request.run,
                    Usage(input_tokens=5, output_tokens=2),
                    model=runtime.adapter.model,
                    request_id="same",
                    purpose="agent",
                )
        finally:
            for worker in workers:
                worker.join(timeout=10)
                assert not worker.is_alive()
        assert not failures
        assert store.load_run(request.session.session_id, request.run.run_id) == before
        assert (
            runtime.usage.run_usage(request.session.session_id, request.run.run_id).request_count
            == 1
        )


@pytest.mark.parametrize("mutation", ["replace", "reset"])
async def test_invoice_edits_do_not_replenish_run_budget(tmp_path, store, mutation):
    async with request_host(tmp_path, store) as (runtime, request):
        port = requests(runtime, RunBudgetPolicy(max_input_tokens=5))
        first = await port.execute(request)
        assert isinstance(first.outcome, ModelExecutionResult) and first.outcome.error is None
        if mutation == "reset":
            runtime.usage.reset(note="invoice hidden")
        else:
            entry = runtime.usage.list_entries().entries[0]
            runtime.usage.replace_entry(entry.entry_id, input_tokens=0, note="invoice correction")
        blocked = await port.execute(replace(request, run=first.run, model_round=2, turn_id="next"))
        assert blocked.outcome == ModelRequestBlocked("input_tokens")
        assert store.load_run(request.session.session_id, request.run.run_id).model_round_count == 1


@pytest.mark.parametrize("prefix", [(), (Usage(input_tokens=1),)])
async def test_missing_report_does_not_become_known_zero_after_prior_usage(tmp_path, store, prefix):
    async with request_host(tmp_path, store, reports=(*prefix, None)) as (runtime, request):
        port = requests(runtime, RunBudgetPolicy(max_input_tokens=100))
        for index in range(len(prefix) + 1):
            result = await port.execute(request)
            assert isinstance(result.outcome, ModelExecutionResult) and result.outcome.error is None
            request = replace(
                request,
                run=result.run,
                sequence=result.sequence,
                model_round=index + 2,
                turn_id=f"request-{index + 2}",
            )
        # Summary reports count toward tokens, but cannot stand in for a missing agent report.
        runtime.usage.record_provider_usage(
            run=request.run,
            usage=Usage(input_tokens=2),
            model=runtime.adapter.model,
            request_id="summary",
            purpose="compaction",
        )
        blocked = await port.execute(request)
        assert blocked.outcome == ModelRequestBlocked("usage_unavailable")


async def test_zero_report_is_known_and_compaction_consumption_counts_for_budget(tmp_path, store):
    async with request_host(tmp_path, store, reports=(Usage(),)) as (runtime, request):
        port = requests(runtime, RunBudgetPolicy(max_input_tokens=5))
        first = await port.execute(request)
        assert isinstance(first.outcome, ModelExecutionResult) and first.outcome.error is None
        totals = runtime.usage.run_usage(request.session.session_id, request.run.run_id)
        assert totals.agent_request_count == 1 and totals.input_tokens == 0
        runtime.usage.record_provider_usage(
            run=first.run,
            usage=Usage(input_tokens=5),
            model=runtime.adapter.model,
            request_id="summary",
            purpose="compaction",
        )
        blocked = await port.execute(replace(request, run=first.run, model_round=2, turn_id="next"))
        assert blocked.outcome == ModelRequestBlocked("input_tokens")


@pytest.mark.parametrize("reported", [False, True])
async def test_token_budget_requires_reports_for_summary_requests(tmp_path, store, reported):
    from ouro.core.context import ContextMetrics

    async with request_host(tmp_path, store) as (runtime, request):
        recorder = runtime.run_lifecycle.prepare_summary_requests(
            request.session.session_id, request.run.run_id
        )
        recorder.start(
            "req_summary",
            model=runtime.adapter.model,
            metrics=ContextMetrics(estimated_input_tokens=0, estimated_fixed_tokens=0),
        )
        recorder.settle(
            "req_summary",
            model=runtime.adapter.model,
            provider_done=True,
            usage=Usage(input_tokens=7, output_tokens=2) if reported else None,
        )
        latest = store.load_run(request.session.session_id, request.run.run_id)
        result = await requests(runtime, RunBudgetPolicy(max_input_tokens=100)).execute(
            replace(request, run=latest)
        )
        if reported:
            assert isinstance(result.outcome, ModelExecutionResult) and result.outcome.error is None
            assert runtime.usage.run_usage(latest.session_id, latest.run_id).request_count == 2
        else:
            assert result.outcome == ModelRequestBlocked("usage_unavailable")
            assert store.load_run(latest.session_id, latest.run_id).model_round_count == 0
