from __future__ import annotations

import asyncio

import pytest

import ouro.application.summary_compaction as compaction_module
from ouro.application.configuration import ConfigurationFacade
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_configuration import RuntimeConfigurationResolver
from ouro.application.summary_compaction import LLMSummaryCompactionStrategy
from ouro.config.compaction import CompactionConfig, CompactionConfigManager, CompactionModelConfig
from ouro.config.prompts import PromptConfigManager, PromptDraft
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import (
    CompactionAbortedError,
    CompactionArtifact,
    CompactionPolicy,
    CompactionRequest,
    CompactionStrategyError,
    CompactionStrategyRegistry,
)
from ouro.core.context import ContextBuilder
from ouro.core.context_budget import ContextBudget
from ouro.core.errors import CompactionError
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderCompletion,
    ProviderStreamEvent,
    Usage,
)
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


class RecordingProvider:
    provider_id = "recording"

    def __init__(self) -> None:
        self.calls: list[list[MessageRecord]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, tools
        self.calls.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text="ok")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
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

    def __init__(
        self,
        *,
        error: str | None = None,
        response: str = "<summary>routed summary</summary>",
        reasoning: str = "",
    ) -> None:
        self.error = error
        self.response = response
        self.reasoning = reasoning
        self.models: list[ModelMetadata] = []
        self.system_prompts: list[str] = []
        self.request_contexts = []
        self.messages: list[list[MessageRecord]] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del tools
        self.models.append(model)
        self.request_contexts.append(request_context)
        self.system_prompts.append(system_prompt)
        self.messages.append(list(messages))
        if self.error is not None:
            yield ProviderStreamEvent(type="error", error=self.error)
            return
        if self.reasoning:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=self.reasoning)
        yield ProviderStreamEvent(type="text_delta", text=self.response)
        yield ProviderStreamEvent(
            type="done",
            completion=ProviderCompletion(finish_reason="stop"),
        )

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


class BlockingSummaryProvider:
    provider_id = "blocking-summary"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del model, system_prompt, messages, tools
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        if False:  # pragma: no cover - keeps this provider an async generator.
            yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(provider_id=self.provider_id, ok=True, model_id=model.model_id)


class AbortAwareFailingStrategy:
    strategy_id = "test.abort-aware"
    version = "1"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def compact(self, request, *, signal=None):
        del request
        assert signal is not None
        self.started.set()
        await signal.wait()
        raise CompactionStrategyError("strategy observed cancellation")


class CountingFallbackStrategy:
    strategy_id = "test.fallback"
    version = "1"

    def __init__(self) -> None:
        self.call_count = 0

    async def compact(self, request, *, signal=None):
        del request, signal
        self.call_count += 1
        return CompactionArtifact(summary="fallback summary")


class ForgedAbortStrategy:
    strategy_id = "test.forged-abort"
    version = "1"

    async def compact(self, request, *, signal=None):
        del request, signal
        raise CompactionAbortedError("strategy cannot abort its owning Run")


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
            ),
        ),
    )


def _builtin_compaction_runtime(
    store: LocalSessionStore,
    provider: BlockingSummaryProvider,
) -> RuntimeService:
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="blocking-summary",
                provider_id=provider.provider_id,
                context_window=3_000,
                max_output_tokens=500,
            ),
        ),
        tools=ToolRegistry(),
        system_prompt="",
        compaction_config=CompactionConfig(
            policy=CompactionPolicy(trigger_ratio=0.3, target_ratio=0.2),
        ),
    )


def _append_turns(
    store: LocalSessionStore,
    session_id: str,
    count: int,
    *,
    offset: int = 0,
) -> None:
    snapshot = store.load_session(session_id)
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


def test_default_compaction_route_tracks_explicit_system_selection(tmp_path) -> None:
    facade = ConfigurationFacade(tmp_path)
    primary = facade.add_provider(display_name="Primary", provider_type="openai", make_default=True)
    assert primary.changed_provider_id is not None
    facade.update_compaction_model(use_default_model=True)
    resolver = RuntimeConfigurationResolver(tmp_path)
    config = CompactionConfigManager(tmp_path).snapshot()

    first = resolver.compaction_provider_options(config)
    assert first is not None
    assert first.provider_id == primary.changed_provider_id

    secondary = facade.add_provider(display_name="Secondary", provider_type="openai")
    assert secondary.changed_provider_id is not None
    facade.use_provider(secondary.changed_provider_id)
    second = resolver.compaction_provider_options(config)
    assert second is not None
    assert second.provider_id == secondary.changed_provider_id


@pytest.mark.parametrize("manual", [False, True])
async def test_prompt_snapshot_survives_config_update_during_compaction(tmp_path, manual):
    manager = PromptConfigManager(tmp_path)
    manager.save_entry("replacement", PromptDraft(text="Before", enabled=True), expected_revision=0)
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Prompt snapshot")
    store.create_session(session)
    _append_turns(store, session.session_id, 5)
    resolutions = []
    resolver = RuntimeConfigurationResolver(tmp_path)

    def resolve_prompt():
        prompt = resolver.prompt_context()
        resolutions.append(prompt)
        return prompt

    class UpdatingStrategy(RecordingStrategy):
        async def compact(self, request, *, signal=None):
            manager.save_entry(
                "replacement",
                PromptDraft(text="Oversized instruction " * 5_000, enabled=True),
                expected_revision=1,
            )
            return await super().compact(request, signal=signal)

    strategy = UpdatingStrategy()
    provider = RecordingProvider()
    runtime = _runtime(store, provider, strategy, strategy_id=strategy.strategy_id)
    runtime.context_preparation.prompt_context_resolver = resolve_prompt
    async with runtime:
        if manual:
            result = await runtime.compact(session_id=session.session_id)
        else:
            result = await runtime.run("new", session_id=session.session_id)
            context = store.load_latest_context(session.session_id)
            assert context.system_prompt == "Before"
            assert context.context_metadata["personal_prompt"]["revision"] == 1
        assert result.run.status == "completed"
        record = store.load_active_compaction(session.session_id)
        assert record is not None
        assert record.context_metrics_after.estimated_fixed_tokens < 100
        assert len(strategy.requests) == 1
        assert len(resolutions) == 1
        assert resolutions[0].system_prompt == "Before"
        sent = len(provider.calls)
        # A fresh request budgets the new instructions instead of reusing the old snapshot.
        blocked = await runtime.run("next", session_id=session.session_id)
        assert blocked.run.status == "paused"
        assert len(provider.calls) == sent
        assert len(resolutions) == 2
        assert resolutions[-1].revision == 2


def _summary_request() -> CompactionRequest:
    return CompactionRequest(
        session_id="ses_route",
        strategy_id="builtin.llm_summary",
        trigger="manual",
        messages=[
            MessageRecord.text(session_id="ses_route", role="user", text="old question"),
            MessageRecord.text(session_id="ses_route", role="assistant", text="old answer"),
        ],
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
            max_output_tokens=min(2048, context_window - 1),
            temperature=0.8,
            reasoning=ReasoningCapability(
                activation="toggle",
                supported_efforts=("low", "high"),
                default=ReasoningRequest(mode="enabled", effort="low"),
            ),
            reasoning_request=ReasoningRequest(mode="enabled", effort="low"),
        ),
    )


@pytest.mark.parametrize("main_limit", [None, 256, 4096])
@pytest.mark.parametrize("summary_limit", [None, 512])
@pytest.mark.parametrize("route", ["runtime", "configured", "runtime_fallback"])
async def test_summary_uses_its_own_limit_on_every_route(main_limit, summary_limit, route):
    runtime_provider = SummaryRouteProvider()
    configured_provider = SummaryRouteProvider(
        error="unavailable" if route == "runtime_fallback" else None
    )
    runtime = _summary_adapter(
        runtime_provider, provider_id="runtime", endpoint_id="main"
    ).with_output_limit(main_limit)
    configured = _summary_adapter(configured_provider, provider_id="summary", endpoint_id="summary")
    strategy = LLMSummaryCompactionStrategy(
        runtime,
        configured_adapter=configured if route != "runtime" else None,
        model_config=CompactionModelConfig(
            provider_id="summary" if route != "runtime" else None,
            endpoint_id="summary" if route != "runtime" else None,
            max_output_tokens=summary_limit,
        ),
    )
    artifact = await strategy.compact(_summary_request())
    used = configured_provider if route == "configured" else runtime_provider
    assert used.models[-1].max_output_tokens == summary_limit
    assert artifact.model_outputs[-1].requested_output_tokens == summary_limit
    assert runtime.model.max_output_tokens == main_limit
    assert configured.model.max_output_tokens == 2048


async def test_closed_summary_with_length_finish_reason_is_still_rejected():
    class TruncatedProvider(SummaryRouteProvider):
        async def stream(self, **kwargs):
            yield ProviderStreamEvent(type="text_delta", text="<summary>closed</summary>")
            yield ProviderStreamEvent(
                type="done", completion=ProviderCompletion(finish_reason="length")
            )

    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(TruncatedProvider(), provider_id="runtime", endpoint_id="chat")
    )
    with pytest.raises(CompactionStrategyError, match="output limit") as raised:
        await strategy.compact(_summary_request())
    assert len(raised.value.model_outputs) == 1
    assert raised.value.model_outputs[0].raw_text == "<summary>closed</summary>"
    assert raised.value.model_outputs[0].status == "failed"


def test_summary_chunk_selection_serializes_each_message_once_and_preserves_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SummaryRouteProvider()
    adapter = _summary_adapter(
        provider,
        provider_id="runtime",
        endpoint_id="chat",
        context_window=8_000,
    )
    strategy = LLMSummaryCompactionStrategy(adapter)
    frames = [
        [
            MessageRecord.text(
                session_id="ses_linear",
                role="user",
                text=f"message-{index}-" + ("x" * (index % 7 + 1)),
            )
        ]
        for index in range(200)
    ]
    previous_summary = "earlier summary"
    summary_output_tokens = 512
    adapter = adapter.with_output_limit(summary_output_tokens)
    capacity = adapter.model.context_budget.input_tokens
    expected: list[MessageRecord] = []
    expected_frames = 0
    for frame in frames:
        candidate = [*expected, *frame]
        view = ContextBuilder().build(
            system_prompt=strategy.prompt,
            messages=[
                ModelMessage.text(
                    role="user",
                    text=compaction_module._summary_payload(previous_summary, candidate),
                    semantic_tag="conversation-to-summarize",
                )
            ],
            tools=[],
            budget=ContextBudget(context_window=capacity),
        )
        if view.metrics.over_budget:
            break
        expected = candidate
        expected_frames += 1

    visible_calls = 0
    model_visible_message = compaction_module.model_visible_message

    def count_model_visible_message(message):
        nonlocal visible_calls
        visible_calls += 1
        return model_visible_message(message)

    monkeypatch.setattr(
        compaction_module,
        "model_visible_message",
        count_model_visible_message,
    )

    selected, consumed_frames = strategy._next_chunk(
        frames,
        adapter=adapter,
        previous_summary=previous_summary,
    )

    assert selected == expected
    assert consumed_frames == expected_frames
    assert visible_calls <= sum(len(frame) for frame in frames[: expected_frames + 1])


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
            reasoning=ReasoningRequest(mode="enabled", effort="high"),
        ),
    )

    artifact = await strategy.compact(_summary_request())

    assert runtime_provider.models == []
    assert len(configured_provider.models) == 1
    assert configured_provider.request_contexts[0].values == {
        "session.id": "ses_route",
        "request.purpose": "compaction",
    }
    request_model = configured_provider.models[0]
    assert request_model.temperature == 0.1
    assert request_model.max_output_tokens == 512
    assert request_model.reasoning_request == ReasoningRequest(mode="enabled", effort="high")
    assert configured_adapter.model.temperature == 0.8
    assert configured_adapter.model.max_output_tokens == 2048
    assert configured_adapter.model.reasoning_request == ReasoningRequest(
        mode="enabled", effort="low"
    )
    assert artifact.metadata["model_route"] == "configured"
    assert artifact.metadata["model_endpoint_id"] == "compact"
    assert len(artifact.model_outputs) == 1
    assert artifact.model_outputs[0].raw_text == "<summary>routed summary</summary>"
    assert artifact.model_outputs[0].status == "completed"


@pytest.mark.asyncio
async def test_dedicated_compaction_endpoint_uses_unlimited_output_by_default() -> None:
    runtime_provider = SummaryRouteProvider()
    configured_provider = SummaryRouteProvider()
    configured_adapter = _summary_adapter(
        configured_provider,
        provider_id="summary",
        endpoint_id="compact",
    )
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(runtime_provider, provider_id="runtime", endpoint_id="chat"),
        configured_adapter=configured_adapter,
        model_config=CompactionModelConfig(
            provider_id="summary",
            endpoint_id="compact",
        ),
    )

    await strategy.compact(_summary_request())

    assert runtime_provider.models == []
    assert configured_provider.models[0] == configured_adapter.with_output_limit(None).model
    assert configured_provider.models[0].reasoning_request == ReasoningRequest(
        mode="enabled", effort="low"
    )


@pytest.mark.asyncio
async def test_invalid_compaction_response_retains_raw_model_output() -> None:
    provider = SummaryRouteProvider(
        response="summary without the required wrapper",
        reasoning="I should summarize the conversation.",
    )
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(provider, provider_id="runtime", endpoint_id="chat")
    )

    with pytest.raises(CompactionStrategyError) as raised:
        await strategy.compact(_summary_request())

    assert len(raised.value.model_outputs) == 1
    output = raised.value.model_outputs[0]
    assert output.status == "failed"
    assert output.raw_text == "summary without the required wrapper"
    assert output.raw_reasoning == "I should summarize the conversation."
    assert output.error_type == "CompactionError"
    assert "non-empty <summary> block" in str(output.error)


@pytest.mark.asyncio
async def test_runtime_compaction_reuses_route_without_inheriting_output_limit() -> None:
    provider = SummaryRouteProvider()
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(provider, provider_id="runtime", endpoint_id="chat")
    )

    artifact = await strategy.compact(_summary_request())

    assert provider.models[0].temperature == 0.8
    assert provider.models[0].max_output_tokens is None
    assert provider.models[0].reasoning_request == ReasoningRequest(mode="enabled", effort="low")
    assert artifact.metadata["model_reasoning"] == {
        "mode": "enabled",
        "effort": "low",
    }
    assert "{{ max_output_tokens }}" not in provider.system_prompts[0]
    assert "<example>" not in provider.system_prompts[0]


@pytest.mark.asyncio
async def test_compaction_reasoning_rejects_explicit_enable_on_unsupported_endpoint() -> None:
    configured_provider = SummaryRouteProvider()
    runtime_provider = SummaryRouteProvider(error="runtime unavailable")
    configured_adapter = RuntimeAdapter(
        configured_provider,
        ModelMetadata(
            model_id="plain-model",
            provider_id="plain",
            endpoint_id="chat",
            context_window=20_000,
            max_output_tokens=2048,
        ),
    )
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(runtime_provider, provider_id="runtime", endpoint_id="chat"),
        configured_adapter=configured_adapter,
        model_config=CompactionModelConfig(
            provider_id="plain",
            endpoint_id="chat",
            reasoning=ReasoningRequest(mode="enabled"),
        ),
    )

    with pytest.raises(CompactionError, match="does not support reasoning controls"):
        await strategy.compact(_summary_request())

    assert configured_provider.models == []
    assert len(runtime_provider.models) == 1


@pytest.mark.asyncio
async def test_runtime_compaction_inherits_always_on_session_reasoning() -> None:
    provider = SummaryRouteProvider()
    adapter = RuntimeAdapter(
        provider,
        ModelMetadata(
            model_id="reasoning-model",
            provider_id="reasoning",
            endpoint_id="chat",
            context_window=20_000,
            max_output_tokens=2048,
            reasoning=ReasoningCapability(
                activation="always_on",
                default=ReasoningRequest(mode="enabled"),
            ),
            reasoning_request=ReasoningRequest(mode="enabled"),
        ),
    )
    strategy = LLMSummaryCompactionStrategy(adapter)

    artifact = await strategy.compact(_summary_request())

    assert provider.models[0].reasoning_request == ReasoningRequest(mode="enabled")
    assert artifact.metadata["model_reasoning"] == {"mode": "enabled", "effort": None}


@pytest.mark.asyncio
async def test_unclosed_length_summary_retries_once_with_half_the_frames() -> None:
    class BinaryBackoffProvider(SummaryRouteProvider):
        def __init__(self) -> None:
            super().__init__()
            self.responses = [
                ("<analysis>long</analysis><summary>truncated", "length", 1024),
                ("<summary>first frame</summary>", "stop", 10),
                ("<summary>complete rolling summary</summary>", "stop", 10),
            ]

        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            del tools
            self.models.append(model)
            self.system_prompts.append(system_prompt)
            self.messages.append(list(messages))
            text, finish_reason, output_tokens = self.responses[len(self.models) - 1]
            yield ProviderStreamEvent(type="text_delta", text=text)
            yield ProviderStreamEvent(
                type="done",
                usage=Usage(input_tokens=10, output_tokens=output_tokens),
                completion=ProviderCompletion(finish_reason=finish_reason),
            )

    provider = BinaryBackoffProvider()
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(provider, provider_id="runtime", endpoint_id="chat")
    )
    request = _summary_request().model_copy(
        update={
            "messages": [
                MessageRecord.text(session_id="ses_route", role="user", text="question 1"),
                MessageRecord.text(session_id="ses_route", role="assistant", text="answer 1"),
                MessageRecord.text(session_id="ses_route", role="user", text="question 2"),
                MessageRecord.text(session_id="ses_route", role="assistant", text="answer 2"),
            ]
        }
    )

    artifact = await strategy.compact(request)

    assert artifact.summary == "complete rolling summary"
    assert len(provider.models) == 3
    assert [output.status for output in artifact.model_outputs] == [
        "failed",
        "completed",
        "completed",
    ]
    assert [output.chunk_index for output in artifact.model_outputs] == [1, 1, 2]
    assert [output.attempt_index for output in artifact.model_outputs] == [1, 2, 1]
    assert [output.input_frame_count for output in artifact.model_outputs] == [2, 1, 1]
    assert [output.input_message_count for output in artifact.model_outputs] == [4, 2, 2]
    first = artifact.model_outputs[0]
    assert first.finish_reason == "length"
    assert "<summary>" in first.raw_text and "</summary>" not in first.raw_text
    assert first.requested_output_tokens is None
    assert first.estimated_input_tokens > 0
    assert artifact.usage["input_tokens"] == 30
    assert artifact.usage["output_tokens"] == 1044
    assert artifact.metadata["model_request_count"] == 3


@pytest.mark.asyncio
async def test_unclosed_length_summary_stops_after_one_binary_retry() -> None:
    class RepeatedTruncationProvider(SummaryRouteProvider):
        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            del tools
            self.models.append(model)
            self.system_prompts.append(system_prompt)
            self.messages.append(list(messages))
            yield ProviderStreamEvent(type="text_delta", text="<summary>still truncated")
            yield ProviderStreamEvent(
                type="done",
                completion=ProviderCompletion(finish_reason="length"),
            )

    provider = RepeatedTruncationProvider()
    runtime_provider = SummaryRouteProvider()
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(runtime_provider, provider_id="runtime", endpoint_id="chat"),
        configured_adapter=_summary_adapter(
            provider,
            provider_id="summary",
            endpoint_id="compact",
        ),
        model_config=CompactionModelConfig(
            provider_id="summary",
            endpoint_id="compact",
        ),
    )
    request = _summary_request().model_copy(
        update={
            "messages": [
                MessageRecord.text(session_id="ses_route", role="user", text="question 1"),
                MessageRecord.text(session_id="ses_route", role="assistant", text="answer 1"),
                MessageRecord.text(session_id="ses_route", role="user", text="question 2"),
                MessageRecord.text(session_id="ses_route", role="assistant", text="answer 2"),
            ]
        }
    )

    with pytest.raises(CompactionStrategyError) as raised:
        await strategy.compact(request)

    assert len(provider.models) == 2
    assert runtime_provider.models == []
    assert raised.value.terminal is True
    assert [output.attempt_index for output in raised.value.model_outputs] == [1, 2]
    assert [output.input_frame_count for output in raised.value.model_outputs] == [2, 1]


@pytest.mark.asyncio
async def test_compaction_provider_error_applies_summary_parameters_to_fallback() -> None:
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
            reasoning=ReasoningRequest(mode="disabled"),
        ),
    )

    artifact = await strategy.compact(_summary_request())

    assert len(configured_provider.models) == 1
    assert len(runtime_provider.models) == 1
    fallback_model = runtime_provider.models[0]
    assert fallback_model.temperature == 0.2
    assert fallback_model.max_output_tokens == 700
    assert fallback_model.reasoning_request == ReasoningRequest(mode="disabled")
    assert runtime_adapter.model.temperature == 0.8
    assert runtime_adapter.model.reasoning_request == ReasoningRequest(mode="enabled", effort="low")
    assert artifact.metadata["model_route"] == "runtime_fallback"
    assert "dedicated unavailable" in str(artifact.metadata["model_fallback_reason"])
    assert [output.status for output in artifact.model_outputs] == ["failed", "completed"]
    assert [output.route for output in artifact.model_outputs] == [
        "configured",
        "runtime_fallback",
    ]


@pytest.mark.parametrize(
    "code",
    [
        "authentication_required",
        "access_denied",
        "quota_exhausted",
        "provider_request_failed",
        "provider_response_failed",
    ],
)
async def test_compaction_classified_service_failure_does_not_switch_accounts(code):
    class DeniedProvider(SummaryRouteProvider):
        async def stream(self, **kwargs):
            yield ProviderStreamEvent(type="error", error="Request denied.", error_code=code)

    fallback = SummaryRouteProvider()
    strategy = LLMSummaryCompactionStrategy(
        _summary_adapter(fallback, provider_id="runtime", endpoint_id="chat"),
        configured_adapter=_summary_adapter(
            DeniedProvider(), provider_id="subscription", endpoint_id="summary"
        ),
        model_config=CompactionModelConfig(provider_id="subscription", endpoint_id="summary"),
    )
    with pytest.raises(CompactionStrategyError) as failure:
        await strategy.compact(_summary_request())
    assert failure.value.terminal
    assert fallback.models == []
    assert len(failure.value.model_outputs) == 1


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
    snapshot = store.load_session(session.session_id)
    restored = snapshot.active_compaction

    assert result.compaction is not None
    assert restored is not None
    assert restored.model_provider == "summary"
    assert restored.model_endpoint_id == "compact"
    assert restored.model_id == "summary-model"
    assert restored.metadata["model_route"] == "configured"
    output_events = [
        event for event in snapshot.events if event.event_type == "compaction_model_output"
    ]
    assert len(output_events) == 1
    assert output_events[0].payload["compaction_id"] == restored.compaction_id
    assert output_events[0].payload["status"] == "completed"
    assert output_events[0].payload["raw_text"] == "<summary>routed summary</summary>"
    assert output_events[0].payload["attempt_index"] == 1
    assert output_events[0].payload["requested_output_tokens"] == 512
    assert output_events[0].payload["finish_reason"] == "stop"
    assert output_events[0].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_runtime_persists_raw_compaction_output_when_validation_fails(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid summary output")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    raw_output = "A summary emitted without the required XML block."
    runtime = RuntimeService(
        store=store,
        adapter=_summary_adapter(
            SummaryRouteProvider(response=raw_output),
            provider_id="runtime",
            endpoint_id="chat",
        ),
        tools=ToolRegistry(),
        system_prompt="",
    )

    with pytest.raises(CompactionError, match="non-empty <summary> block"):
        await runtime.compact(session_id=session.session_id)

    snapshot = store.load_session(session.session_id)
    output_events = [
        event for event in snapshot.events if event.event_type == "compaction_model_output"
    ]
    failure = next(event for event in snapshot.events if event.event_type == "compaction_failed")
    assert len(output_events) == 1
    output = output_events[0]
    assert output.sequence < failure.sequence
    assert output.payload["status"] == "failed"
    assert output.payload["raw_text"] == raw_output
    assert output.payload["error_type"] == "CompactionError"
    assert snapshot.compactions == []


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
    assert second_provider.calls[0][0].role == "system"
    assert second_provider.calls[0][0].text_content == (
        "<conversation-summary>\nsummary-1\n</conversation-summary>"
    )
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
    assert result.compaction.context_metrics_after is not None
    assert result.compaction.duration_seconds is not None
    assert result.compaction.duration_seconds >= 0
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
    completed = next(event for event in result.events if event.event_type == "compaction_completed")
    assert completed.payload["context_metrics_after"] == (
        result.compaction.context_metrics_after.model_dump(mode="json")
    )
    assert completed.payload["duration_seconds"] == result.compaction.duration_seconds


@pytest.mark.parametrize("summary", ["   ", "oversized " * 10_000], ids=["empty", "oversized"])
async def test_rejected_summary_preserves_active_record_and_canonical_history(tmp_path, summary):
    class SummaryStrategy(RecordingStrategy):
        text = "## Continuation\nA plain Markdown summary."

        async def compact(self, request, *, signal=None):
            return CompactionArtifact(summary=self.text)

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Summary acceptance")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    strategy = SummaryStrategy()
    runtime = _runtime(store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id)
    first = await runtime.compact(session_id=session.session_id)
    assert first.compaction is not None
    assert first.compaction.summary == strategy.text
    _append_turns(store, session.session_id, 3, offset=100)
    before = store.load_session(session.session_id)
    strategy.text = summary

    with pytest.raises(CompactionError, match="empty summary|exceed the model input budget"):
        await runtime.compact(session_id=session.session_id)

    after = store.load_session(session.session_id)
    assert after.active_compaction == before.active_compaction
    assert after.compactions == before.compactions
    assert after.messages == before.messages


@pytest.mark.asyncio
async def test_manual_compaction_abort_returns_aborted_result_without_activating_summary(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = BlockingSummaryProvider()
    runtime = _builtin_compaction_runtime(store, provider)
    session = SessionRecord(title="Interrupted manual compaction")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)

    compact_task = asyncio.create_task(runtime.compact(session_id=session.session_id))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    active_run = store.load_runs(session.session_id)[-1]
    abort_task = asyncio.create_task(
        runtime.abort_run(
            session_id=session.session_id,
            run_id=active_run.run_id,
            reason="Interrupt manual compaction",
        )
    )

    compact_result, abort_result = await asyncio.gather(compact_task, abort_task)
    snapshot = store.load_runtime_session(session.session_id)
    event_types = [event.event_type for event in store.load_session(session.session_id).events]

    assert compact_result.run.status == "aborted"
    assert abort_result.run.status == "aborted"
    assert compact_result.compaction is None
    assert snapshot.session.active_compaction_id is None
    assert snapshot.compactions == []
    assert provider.cancelled.is_set()
    assert "run_aborted" in event_types
    assert "run_failed" not in event_types
    assert "compaction_failed" not in event_types


@pytest.mark.asyncio
async def test_auto_compaction_abort_terminates_agent_run_before_model_request(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = BlockingSummaryProvider()
    runtime = _builtin_compaction_runtime(store, provider)
    session = SessionRecord(title="Interrupted auto compaction")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)

    run_task = asyncio.create_task(runtime.run("new request", session_id=session.session_id))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    active_run = store.load_runs(session.session_id)[-1]
    abort_task = asyncio.create_task(
        runtime.abort_run(
            session_id=session.session_id,
            run_id=active_run.run_id,
            reason="Interrupt automatic compaction",
        )
    )

    run_result, abort_result = await asyncio.gather(run_task, abort_task)
    snapshot = store.load_runtime_session(session.session_id)
    event_types = [event.event_type for event in store.load_session(session.session_id).events]

    assert run_result.run.status == "aborted"
    assert abort_result.run.status == "aborted"
    assert snapshot.session.active_compaction_id is None
    assert snapshot.compactions == []
    assert event_types.count("compaction_started") == 1
    assert "model_request_started" not in event_types
    assert "compaction_failed" not in event_types


@pytest.mark.asyncio
async def test_compaction_abort_is_terminal_and_does_not_invoke_fallback(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Cancelled strategy fallback")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    primary = AbortAwareFailingStrategy()
    fallback = CountingFallbackStrategy()
    strategies = CompactionStrategyRegistry()
    strategies.register(primary)
    strategies.register(fallback)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            RecordingProvider(),
            ModelMetadata(
                model_id="recording",
                provider_id="recording",
                context_window=3_000,
                max_output_tokens=500,
            ),
        ),
        tools=ToolRegistry(),
        system_prompt="",
        compaction_strategies=strategies,
        compaction_config=CompactionConfig(
            strategy_id=primary.strategy_id,
            fallback_strategy_id=fallback.strategy_id,
            policy=CompactionPolicy(trigger_ratio=0.3, target_ratio=0.2),
        ),
    )

    compact_task = asyncio.create_task(runtime.compact(session_id=session.session_id))
    await asyncio.wait_for(primary.started.wait(), timeout=2)
    active_run = store.load_runs(session.session_id)[-1]
    abort_task = asyncio.create_task(
        runtime.abort_run(
            session_id=session.session_id,
            run_id=active_run.run_id,
            reason="Stop before fallback",
        )
    )

    compact_result, abort_result = await asyncio.gather(compact_task, abort_task)

    assert compact_result.run.status == "aborted"
    assert abort_result.run.status == "aborted"
    assert fallback.call_count == 0
    assert store.load_session_record(session.session_id).active_compaction_id is None


@pytest.mark.asyncio
async def test_compaction_strategy_cannot_forge_host_cancellation(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Host-owned cancellation")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    strategy = ForgedAbortStrategy()
    runtime = _runtime(
        store,
        RecordingProvider(),
        strategy,
        strategy_id=strategy.strategy_id,
    )

    with pytest.raises(CompactionStrategyError, match="cannot abort"):
        await runtime.compact(session_id=session.session_id)

    run = store.load_runs(session.session_id)[-1]
    event_types = [event.event_type for event in store.load_session(session.session_id).events]
    assert run.status == "failed"
    assert "run_failed" in event_types
    assert "run_aborted" not in event_types


@pytest.mark.asyncio
async def test_manual_compaction_does_not_rewrite_latest_provider_context(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Current context")
    store.create_session(session)
    strategy = RecordingStrategy()
    runtime = _runtime(store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id)
    await runtime.run("seed", session_id=session.session_id)
    _append_turns(store, session.session_id, 3)

    last_request_before = runtime.sessions.context(session.session_id)
    result = await runtime.compact(session_id=session.session_id)
    last_request_after = runtime.sessions.context(session.session_id)

    assert result.compaction is not None
    assert last_request_before is not None
    assert last_request_after is not None
    assert last_request_after == last_request_before
    assert result.compaction.context_metrics_after is not None
    assert runtime.sessions.effective_context_metrics(session.session_id) == (
        result.compaction.context_metrics_after
    )
    assert store.load_session_record(session.session_id).active_compaction_id == (
        result.compaction.compaction_id
    )


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

    assert result.run.status == "paused"
    assert provider.calls == []
    assert snapshot.session.active_compaction_id is None
    assert snapshot.compactions == []
    assert len(snapshot.messages) == 11
    assert any(event.event_type == "compaction_failed" for event in snapshot.events)
    assert result.run.error is None
    assert result.run.metadata["pause"]["reason"] == "context_blocked"


@pytest.mark.asyncio
async def test_auto_compaction_failure_defers_while_context_still_fits(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Deferred compaction")
    store.create_session(session)
    _append_turns(store, session.session_id, 2)
    provider = RecordingProvider()
    runtime = _runtime(store, provider, FailingStrategy(), strategy_id="test.failure")

    result = await runtime.run("new", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "completed"
    assert len(provider.calls) == 1
    assert snapshot.compactions == []
    assert any(event.event_type == "compaction_failed" for event in snapshot.events)


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
