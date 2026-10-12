import asyncio
from dataclasses import replace

import pytest

from ouro.application.model_requests import ModelRequestPorts, RunModelRequests
from ouro.application.runtime import RuntimeService
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


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.asyncio
async def test_model_port_records_requests_and_usage_without_driving_or_finishing_run(
    tmp_path, store_type
) -> None:
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    provider_calls = []

    class RecordingProvider(FauxProvider):
        async def stream(self, **kwargs):
            provider_calls.append(kwargs["request_context"])
            async for event in super().stream(**kwargs):
                yield event

    adapter = RuntimeAdapter(
        RecordingProvider(
            [
                ProviderStreamEvent(type="text_delta", text="answer"),
                ProviderStreamEvent(type="done", usage=Usage(input_tokens=5, output_tokens=2)),
            ]
        ),
        ModelMetadata(model_id="test", provider_id="test"),
    )
    runtime = RuntimeService(store=store, adapter=adapter, tools=ToolRegistry())
    session, run = runtime.run_lifecycle.start(
        session=session, run=run, snapshot=store.load_runtime_session(session.session_id)
    )
    requests = RunModelRequests(
        adapter=adapter,
        tool_definitions=runtime.tools.definitions,
        run_budget=RunBudgetPolicy(max_model_rounds=2),
        ports=ModelRequestPorts(
            prepare_context=runtime.context_preparation.prepare,
            record_completion=runtime.context_preparation.record_provider_completion,
            record_usage=runtime.run_lifecycle.record_usage,
            commit_model_request=runtime._commit_model_request,
            emit=runtime.event_recorder.emit,
        ),
    )
    request = RunModelRequest(session, run, [], 1, "first", 0, [], AbortSignal())
    try:
        first = await requests.execute(request)
        second = await requests.execute(
            replace(
                request, run=first.run, sequence=first.sequence, model_round=2, turn_id="second"
            )
        )
        # A caller cannot bypass the durable request budget by reusing a round number.
        blocked = await requests.execute(
            replace(request, run=second.run, sequence=second.sequence, turn_id="third")
        )

        assert isinstance(first.outcome, ModelExecutionResult)
        assert first.outcome.text == "answer"
        assert isinstance(second.outcome, ModelExecutionResult)
        assert second.outcome.error is None
        assert blocked.outcome == ModelRequestBlocked("model_rounds")
        assert len(provider_calls) == 2
        assert all(context.values["run.id"] == run.run_id for context in provider_calls)
        persisted = store.load_run(session.session_id, run.run_id)
        assert persisted.status == "running"
        assert persisted.model_round_count == 2
        assert persisted.usage["input_tokens"] == 10
        assert persisted.usage["output_tokens"] == 4
        assert len(persisted.usage_entry_ids) == 2
        latest_context = store.load_latest_context(session.session_id)
        assert latest_context is not None
        assert latest_context.completed_at is not None
        assert latest_context.provider_reported_input_tokens == 5
        snapshot = store.load_session(session.session_id)
        assert snapshot.messages == []
        assert [event.event_type for event in snapshot.events].count("model_request_started") == 2
        assert [event.event_type for event in snapshot.events].count("model_message_completed") == 2
        assert not any(event.event_type == "run_completed" for event in snapshot.events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_abort_during_usage_commit_keeps_usage_and_closes_stream(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    signal = AbortSignal()
    closed = asyncio.Event()

    class OpenProvider(FauxProvider):
        async def stream(self, **kwargs):
            try:
                yield ProviderStreamEvent(type="text_delta", text="partial")
                yield ProviderStreamEvent(type="done", usage=Usage(input_tokens=3, output_tokens=7))
                await asyncio.Event().wait()
            finally:
                closed.set()

    adapter = RuntimeAdapter(OpenProvider(), ModelMetadata(model_id="test", provider_id="test"))
    runtime = RuntimeService(store=store, adapter=adapter, tools=ToolRegistry())
    session, run = runtime.run_lifecycle.start(
        session=session, run=run, snapshot=store.load_runtime_session(session.session_id)
    )

    def record_usage(*args, **kwargs):
        recorded = runtime.run_lifecycle.record_usage(*args, **kwargs)
        signal.abort()
        return recorded

    requests = RunModelRequests(
        adapter=adapter,
        tool_definitions=runtime.tools.definitions,
        ports=ModelRequestPorts(
            prepare_context=runtime.context_preparation.prepare,
            record_completion=runtime.context_preparation.record_provider_completion,
            record_usage=record_usage,
            commit_model_request=runtime._commit_model_request,
            emit=runtime.event_recorder.emit,
        ),
    )
    try:
        result = await requests.execute(RunModelRequest(session, run, [], 1, "turn", 0, [], signal))

        assert isinstance(result.outcome, ModelExecutionResult)
        assert result.outcome.aborted
        assert result.outcome.text == "partial"
        assert result.run.usage["output_tokens"] == 7
        assert closed.is_set()
        assert not any(
            event.event_type == "model_message_completed"
            for event in store.load_session(session.session_id).events
        )
    finally:
        await runtime.close()
