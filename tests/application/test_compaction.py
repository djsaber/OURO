from __future__ import annotations

import pytest

from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.config.compaction import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionPolicy,
    CompactionRequest,
    CompactionStrategyRegistry,
)
from ouro.core.errors import CompactionError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


class RecordingProvider:
    provider_id = "recording"

    def __init__(self) -> None:
        self.calls: list[list[MessageRecord]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, tools
        self.calls.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text="ok")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


class RecordingStrategy:
    strategy_id = "test.summary"
    version = "1"

    def __init__(self) -> None:
        self.requests: list[CompactionRequest] = []

    async def compact(self, request, *, signal=None):
        del signal
        self.requests.append(request)
        return CompactionArtifact(
            summary=f"summary-{len(self.requests)}",
            usage={"input_tokens": 10, "output_tokens": 2},
            metadata={"model_provider": "test", "model_id": "summary"},
        )


class FailingStrategy:
    strategy_id = "test.failure"
    version = "1"

    async def compact(self, request, *, signal=None):
        del request, signal
        raise RuntimeError("summary unavailable")


def _runtime(
    store: LocalSessionStore,
    provider: RecordingProvider,
    strategy,
    *,
    strategy_id: str,
) -> RuntimeService:
    registry = CompactionStrategyRegistry()
    registry.register(strategy)
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="recording",
                provider_id="recording",
                context_window=3_000,
                max_output_tokens=500,
            ),
        ),
        tools=ToolRegistry(),
        system_prompt="",
        compaction_strategies=registry,
        compaction_config=CompactionConfig(
            strategy_id=strategy_id,
            policy=CompactionPolicy(
                trigger_ratio=0.3,
                target_ratio=0.2,
                summary_output_tokens=256,
            ),
        ),
    )


def _append_turns(
    store: LocalSessionStore,
    session_id: str,
    count: int,
    *,
    offset: int = 0,
) -> None:
    snapshot = store.load_session(session_id, recover=False)
    sequence = len(snapshot.messages)
    for index in range(offset, offset + count):
        sequence += 1
        store.append_message(
            MessageRecord.text(
                session_id=session_id,
                role="user",
                text=f"old user {index} " + ("x" * 1_000),
                sequence=sequence,
            )
        )
        sequence += 1
        store.append_message(
            MessageRecord.text(
                session_id=session_id,
                role="assistant",
                text=f"old answer {index} " + ("y" * 1_000),
                sequence=sequence,
            )
        )


@pytest.mark.asyncio
async def test_auto_compaction_persists_and_is_reused_by_new_runtime(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Persistent summary")
    store.create_session(session)
    _append_turns(store, session.session_id, 5)
    strategy = RecordingStrategy()
    first_provider = RecordingProvider()
    first_runtime = _runtime(
        store,
        first_provider,
        strategy,
        strategy_id=strategy.strategy_id,
    )

    first_result = await first_runtime.run("new", session_id=session.session_id)
    first_snapshot = store.load_session(session.session_id)
    canonical_after_first = list(first_snapshot.messages)
    active_id = first_snapshot.session.active_compaction_id

    second_provider = RecordingProvider()
    second_runtime = _runtime(
        store,
        second_provider,
        strategy,
        strategy_id=strategy.strategy_id,
    )
    second_result = await second_runtime.run("again", session_id=session.session_id)

    assert first_result.run.status == "completed"
    assert second_result.run.status == "completed"
    assert len(strategy.requests) == 1
    assert active_id is not None
    assert store.load_session(session.session_id).session.active_compaction_id == active_id
    assert canonical_after_first[:10] == first_snapshot.messages[:10]
    assert second_provider.calls[0][0].role == "system_synthetic"
    assert second_provider.calls[0][0].text_content == "summary-1"
    assert second_provider.calls[0][-1].text_content == "again"


@pytest.mark.asyncio
async def test_later_auto_compaction_is_incremental(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Incremental summary")
    store.create_session(session)
    _append_turns(store, session.session_id, 5)
    strategy = RecordingStrategy()
    runtime = _runtime(store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id)

    await runtime.run("first", session_id=session.session_id)
    first_request_ids = {message.message_id for message in strategy.requests[0].messages}
    _append_turns(store, session.session_id, 5, offset=100)
    await runtime.run("second", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)

    assert len(strategy.requests) == 2
    assert strategy.requests[1].previous_summary == "summary-1"
    assert first_request_ids.isdisjoint(
        {message.message_id for message in strategy.requests[1].messages}
    )
    assert snapshot.active_compaction is not None
    assert (
        snapshot.active_compaction.previous_compaction_id
        == snapshot.compactions[0].compaction_id
    )
    assert snapshot.active_compaction.summary == "summary-2"


@pytest.mark.asyncio
async def test_manual_compaction_uses_same_pipeline_without_adding_messages(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Manual summary")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    strategy = RecordingStrategy()
    runtime = _runtime(store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id)
    before = store.load_session(session.session_id)

    result = await runtime.compact(session_id=session.session_id)
    after = store.load_session(session.session_id)

    assert result.run.kind == "compaction"
    assert result.run.status == "completed"
    assert result.compaction is not None
    assert result.compaction.trigger == "manual"
    assert before.messages == after.messages
    assert after.active_compaction == result.compaction
    assert [event.event_type for event in result.events] == [
        "run_started",
        "compaction_started",
        "compaction_progress",
        "compaction_completed",
        "run_completed",
    ]


@pytest.mark.asyncio
async def test_compaction_failure_is_visible_and_never_truncates_history(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Failure")
    store.create_session(session)
    _append_turns(store, session.session_id, 5)
    provider = RecordingProvider()
    runtime = _runtime(store, provider, FailingStrategy(), strategy_id="test.failure")

    result = await runtime.run("new", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "failed"
    assert provider.calls == []
    assert snapshot.session.active_compaction_id is None
    assert snapshot.compactions == []
    assert len(snapshot.messages) == 11
    assert any(event.event_type == "compaction_failed" for event in snapshot.events)
    assert "context preparation failed" in (result.run.error or "")


@pytest.mark.asyncio
async def test_manual_compaction_failure_leaves_session_usable(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Manual failure")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    runtime = _runtime(
        store,
        RecordingProvider(),
        FailingStrategy(),
        strategy_id="test.failure",
    )

    with pytest.raises(CompactionError, match="summary unavailable"):
        await runtime.compact(session_id=session.session_id)

    snapshot = store.load_session(session.session_id)
    assert snapshot.session.status == "idle"
    assert snapshot.session.active_compaction_id is None
    assert snapshot.compactions == []
    assert snapshot.runs[-1].kind == "compaction"
    assert snapshot.runs[-1].status == "failed"
    assert any(event.event_type == "compaction_failed" for event in snapshot.events)


@pytest.mark.asyncio
async def test_interactive_compact_command_displays_progress_and_completion(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interactive summary")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    strategy = RecordingStrategy()
    runtime = _runtime(store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id)
    inputs = iter([f"/open {session.session_id}", "/compact", "/exit"])
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "Context manual compaction in progress..." in outputs
    assert any(output.startswith("Context compaction completed") for output in outputs)
    assert any(output.startswith("Active compaction: cmp_") for output in outputs)
