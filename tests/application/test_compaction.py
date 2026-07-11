from __future__ import annotations

import pytest

from ouro.application.compaction import LLMSummaryCompactionStrategy
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.config.compaction import CompactionConfig, CompactionModelConfig
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


class SummaryRouteProvider:
    provider_id = "summary-route"

    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.models: list[ModelMetadata] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del system_prompt, messages, tools
        self.models.append(model)
        if self.error is not None:
            yield ProviderStreamEvent(type="error", error=self.error)
            return
        yield ProviderStreamEvent(type="text_delta", text="<summary1>routed summary</summary1>")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


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


def _summary_request() -> CompactionRequest:
    return CompactionRequest(
        session_id="ses_route",
        strategy_id="builtin.llm_summary",
        trigger="manual",
        messages=[
            MessageRecord.text(session_id="ses_route", role="user", text="old question"),
            MessageRecord.text(session_id="ses_route", role="assistant", text="old answer"),
        ],
        input_token_budget=20_000,
        summary_output_tokens=1024,
    )


def _summary_adapter(
    provider: SummaryRouteProvider,
    *,
    provider_id: str,
    endpoint_id: str,
    context_window: int = 20_000,
) -> RuntimeAdapter:
    return RuntimeAdapter(
        provider,
        ModelMetadata(
            model_id=f"{provider_id}-model",
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            context_window=context_window,
            max_output_tokens=min(2048, context_window),
            temperature=0.8,
            supports_reasoning=True,
            supported_thinking_levels=("low", "high"),
            default_thinking_level="low",
        ),
    )


@pytest.mark.asyncio
async def test_dedicated_compaction_endpoint_uses_isolated_model_parameters() -> None:
    runtime_provider = SummaryRouteProvider()
    configured_provider = SummaryRouteProvider()
    runtime_adapter = _summary_adapter(
        runtime_provider,
        provider_id="runtime",
        endpoint_id="chat",
    )
    configured_adapter = _summary_adapter(
        configured_provider,
        provider_id="summary",
        endpoint_id="compact",
    )
    strategy = LLMSummaryCompactionStrategy(
        runtime_adapter,
        configured_adapter=configured_adapter,
        model_config=CompactionModelConfig(
            provider_id="summary",
            endpoint_id="compact",
            temperature=0.1,
            max_output_tokens=512,
            thinking_enabled=True,
            thinking_level="high",
        ),
    )

    artifact = await strategy.compact(_summary_request())

    assert runtime_provider.models == []
    assert len(configured_provider.models) == 1
    request_model = configured_provider.models[0]
    assert request_model.temperature == 0.1
    assert request_model.max_output_tokens == 512
    assert request_model.default_thinking_level == "high"
    assert configured_adapter.model.temperature == 0.8
    assert configured_adapter.model.max_output_tokens == 2048
    assert configured_adapter.model.default_thinking_level == "low"
    assert artifact.metadata["model_route"] == "configured"
    assert artifact.metadata["model_endpoint_id"] == "compact"


@pytest.mark.asyncio
async def test_compaction_provider_error_retries_runtime_route_with_same_parameters() -> None:
    configured_provider = SummaryRouteProvider(error="dedicated unavailable")
    runtime_provider = SummaryRouteProvider()
    runtime_adapter = _summary_adapter(
        runtime_provider,
        provider_id="runtime",
        endpoint_id="chat",
    )
    strategy = LLMSummaryCompactionStrategy(
        runtime_adapter,
        configured_adapter=_summary_adapter(
            configured_provider,
            provider_id="summary",
            endpoint_id="compact",
        ),
        model_config=CompactionModelConfig(
            provider_id="summary",
            endpoint_id="compact",
            temperature=0.2,
            max_output_tokens=700,
            thinking_enabled=False,
            thinking_level="off",
        ),
    )

    artifact = await strategy.compact(_summary_request())

    assert len(configured_provider.models) == 1
    assert len(runtime_provider.models) == 1
    fallback_model = runtime_provider.models[0]
    assert fallback_model.temperature == 0.2
    assert fallback_model.max_output_tokens == 700
    assert fallback_model.default_thinking_level == "off"
    assert runtime_adapter.model.temperature == 0.8
    assert runtime_adapter.model.default_thinking_level == "low"
    assert artifact.metadata["model_route"] == "runtime_fallback"
    assert "dedicated unavailable" in str(artifact.metadata["model_fallback_reason"])


@pytest.mark.asyncio
async def test_compaction_context_overflow_and_resolution_error_fall_back_to_runtime() -> None:
    too_small_provider = SummaryRouteProvider()
    runtime_provider = SummaryRouteProvider()
    runtime_adapter = _summary_adapter(
        runtime_provider,
        provider_id="runtime",
        endpoint_id="chat",
    )
    target = CompactionModelConfig(provider_id="summary", endpoint_id="tiny")
    too_small = LLMSummaryCompactionStrategy(
        runtime_adapter,
        configured_adapter=_summary_adapter(
            too_small_provider,
            provider_id="summary",
            endpoint_id="tiny",
            context_window=256,
        ),
        model_config=target,
    )

    overflow_artifact = await too_small.compact(_summary_request())
    unresolved = LLMSummaryCompactionStrategy(
        runtime_adapter,
        configured_adapter_error="configured endpoint unavailable: endpoint not found",
        model_config=target,
    )
    unresolved_artifact = await unresolved.compact(_summary_request())

    assert too_small_provider.models == []
    assert overflow_artifact.metadata["model_route"] == "runtime_fallback"
    assert "ContextOverflowError" in str(overflow_artifact.metadata["model_fallback_reason"])
    assert unresolved_artifact.metadata["model_route"] == "runtime_fallback"
    assert "endpoint not found" in str(unresolved_artifact.metadata["model_fallback_reason"])


@pytest.mark.asyncio
async def test_runtime_persists_compaction_endpoint_and_route_metadata(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Dedicated summary")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    runtime_adapter = _summary_adapter(
        SummaryRouteProvider(),
        provider_id="runtime",
        endpoint_id="chat",
    )
    configured_adapter = _summary_adapter(
        SummaryRouteProvider(),
        provider_id="summary",
        endpoint_id="compact",
    )
    runtime = RuntimeService(
        store=store,
        adapter=runtime_adapter,
        tools=ToolRegistry(),
        system_prompt="",
        compaction_adapter=configured_adapter,
        compaction_config=CompactionConfig(
            model=CompactionModelConfig(
                provider_id="summary",
                endpoint_id="compact",
                max_output_tokens=512,
            )
        ),
    )

    result = await runtime.compact(session_id=session.session_id)
    restored = store.load_session(session.session_id).active_compaction

    assert result.compaction is not None
    assert restored is not None
    assert restored.model_provider == "summary"
    assert restored.model_endpoint_id == "compact"
    assert restored.model_id == "summary-model"
    assert restored.metadata["model_route"] == "configured"


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
        snapshot.active_compaction.previous_compaction_id == snapshot.compactions[0].compaction_id
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
