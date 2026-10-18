import asyncio
import threading

import pytest

from ouro.application.model_generation import ModelGenerationService
from ouro.application.models import FixedRuntimeAdapterCatalog
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.billing import UsageFilter
from ouro.core.errors import ConfigError
from ouro.core.model_generation import ModelGenerationRequest, ModelInput
from ouro.core.provider import ModelMetadata, ProviderCompletion, ProviderStreamEvent, Usage
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.providers import FauxProvider

PLUGIN = "dev.ouro.test"
REQUEST = ModelGenerationRequest(
    messages=(ModelInput(role="user", text="Review this."),), model="faux/faux"
)


def generation(tmp_path, provider, **model_options):
    adapter = RuntimeAdapter(
        provider, ModelMetadata(provider_id="faux", model_id="faux", **model_options)
    )
    usage = UsageService(LocalUsageLedger(tmp_path))
    usage.ledger.initialize()
    return ModelGenerationService(FixedRuntimeAdapterCatalog(adapter), usage), adapter


async def generate(service, request=REQUEST, **kwargs):
    return await service.generate(
        request, plugin_id=PLUGIN, session_id=None, workspace=None, **kwargs
    )


@pytest.mark.parametrize("limit", [None, 128])
async def test_independent_request_limit_does_not_inherit_or_mutate_shared_adapter(tmp_path, limit):
    seen = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            seen.append(kwargs["model"].max_output_tokens)
            yield ProviderStreamEvent(type="text_delta", text="answer")
            yield ProviderStreamEvent(type="done")

    service, adapter = generation(tmp_path, Provider(), max_output_tokens=32)
    await asyncio.gather(
        generate(service, REQUEST.model_copy(update={"max_output_tokens": limit})),
        generate(service, REQUEST.model_copy(update={"max_output_tokens": 64})),
    )
    assert seen.count(limit) == 1 and seen.count(64) == 1
    assert adapter.model.max_output_tokens == 32


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_unlimited_long_reasoning_requires_completed_response_and_preserves_usage(
    tmp_path, finish
):
    reasoning = "思考" * 40_000
    service, _ = generation(
        tmp_path,
        FauxProvider(
            [
                ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning),
                ProviderStreamEvent(type="text_delta", text="answer"),
                ProviderStreamEvent(
                    type="done",
                    completion=ProviderCompletion(finish_reason=finish),
                    usage=Usage(input_tokens=100, output_tokens=40_001),
                ),
            ]
        ),
    )
    if finish == "stop":
        result = await generate(service)
        assert result.reasoning == reasoning and result.text == "answer"
    else:
        with pytest.raises(RuntimeError, match="output limit"):
            await generate(service)
    assert service.usage.summarize(UsageFilter()).totals.output_tokens == 40_001


async def test_independent_generation_streams_and_records_only_real_request_identity(tmp_path):
    calls, output = [], []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            calls.append(kwargs)
            async for event in super().stream(**kwargs):
                yield event

    provider = Provider(
        [
            ProviderStreamEvent(type="reasoning_delta", reasoning_content="consider"),
            ProviderStreamEvent(type="text_delta", text="answer"),
            ProviderStreamEvent(
                type="done",
                usage=Usage(
                    input_tokens=8, output_tokens=3, cache_read_tokens=2, estimated_cost=0.02
                ),
            ),
        ]
    )
    service, adapter = generation(tmp_path, provider, billing_mode="usage_based")

    async def receive(delta):
        output.append(delta)

    result = await generate(
        service, REQUEST.model_copy(update={"max_output_tokens": 64}), on_output=receive
    )
    assert result.text == "answer" and result.reasoning == "consider"
    assert [(item.sequence, item.kind, item.text) for item in output] == [
        (1, "reasoning", "consider"),
        (2, "text", "answer"),
    ]
    assert calls[0]["model"].max_output_tokens == 64
    assert adapter.model.max_output_tokens is None
    assert calls[0]["tools"] == []
    assert calls[0]["request_context"].values == {
        "request.id": result.request_id,
        "request.purpose": "plugin",
        "plugin.id": PLUGIN,
    }
    (entry,) = service.usage.list_entries(UsageFilter()).entries
    assert entry.request_id == result.request_id and entry.plugin_id == PLUGIN
    assert entry.purpose == "plugin" and entry.billing_mode == "usage_based"
    assert entry.session_id is None and entry.run_id is None and entry.run_kind is None
    assert entry.input_tokens == 8 and entry.output_tokens == 3 and entry.cache_read_tokens == 2
    assert entry.estimated_cost == 0.02 and not entry.harness_scopes


@pytest.mark.parametrize("reported", [False, True])
async def test_generation_attributes_actual_session_without_creating_run(tmp_path, reported):
    provider = FauxProvider(
        [
            ProviderStreamEvent(type="text_delta", text="answer"),
            ProviderStreamEvent(
                type="done", usage=Usage(input_tokens=4, output_tokens=2) if reported else None
            ),
        ]
    )
    service, _ = generation(tmp_path, provider)
    result = await service.generate(
        REQUEST, plugin_id=PLUGIN, session_id="ses_existing", workspace="workspace"
    )
    entries = service.usage.list_entries(UsageFilter(session_ids={"ses_existing"})).entries
    assert len(entries) == int(reported)
    if reported:
        assert entries[0].run_id is None and entries[0].workspace == "workspace"
    else:
        assert result.usage is None


@pytest.mark.parametrize(
    ("updates", "error", "message"),
    [
        ({"max_output_tokens": 64}, ValueError, "context_window"),
        (
            {"messages": (ModelInput(role="user", text="large input " * 500),)},
            ValueError,
            "context budget",
        ),
        ({"model": "default"}, ConfigError, "default endpoint"),
    ],
)
async def test_generation_rejects_invalid_route_or_budget_before_provider(
    tmp_path, updates, error, message
):
    provider = FauxProvider()
    service, _ = generation(tmp_path, provider, context_window=64, max_output_tokens=16)
    with pytest.raises(error, match=message):
        await generate(service, REQUEST.model_copy(update=updates))
    assert provider._stream_count == 0
    assert not service.usage.list_entries(UsageFilter()).entries


@pytest.mark.parametrize("ending", ["cancel", "timeout", "callback_error"])
async def test_generation_failure_closes_provider_and_releases_callback(tmp_path, ending):
    started, closed, callback_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            try:
                started.set()
                yield ProviderStreamEvent(type="text_delta", text="partial")
                await asyncio.Event().wait()
            finally:
                closed.set()

    async def output(delta):
        callback_entered.set()
        if ending == "callback_error":
            raise ValueError("consumer failed")
        await asyncio.Event().wait()

    service, _ = generation(tmp_path, Provider())
    request = REQUEST.model_copy(update={"timeout_seconds": 0.1 if ending == "timeout" else 10})
    task = asyncio.create_task(generate(service, request, on_output=output))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            await callback_entered.wait()
        if ending == "cancel":
            task.cancel()
        error = {
            "cancel": asyncio.CancelledError,
            "timeout": TimeoutError,
            "callback_error": RuntimeError,
        }[ending]
        with pytest.raises(error):
            await task
        assert closed.is_set()
        assert not service.usage.list_entries(UsageFilter()).entries
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_during_usage_commit_waits_for_persistence(tmp_path):
    started, release = threading.Event(), threading.Event()

    class Ledger(LocalUsageLedger):
        def record(self, entry):
            started.set()
            assert release.wait(5)
            return super().record(entry)

    service, _ = generation(tmp_path, FauxProvider())
    service.usage = UsageService(Ledger(tmp_path))
    task = asyncio.create_task(generate(service))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.usage.summarize(UsageFilter(plugin_ids={PLUGIN})).totals.request_count == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stream_completion_error_keeps_reported_usage(tmp_path):
    service, _ = generation(
        tmp_path, FauxProvider([ProviderStreamEvent(type="done", usage=Usage(input_tokens=4))])
    )
    with pytest.raises(RuntimeError):
        await generate(service)
    assert service.usage.summarize(UsageFilter()).totals.input_tokens == 4


async def test_generation_preserves_endpoint_reasoning_and_isolates_request_override(tmp_path):
    calls = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            calls.append(kwargs["model"].reasoning_request)
            async for event in super().stream(**kwargs):
                yield event

    service, adapter = generation(
        tmp_path,
        Provider(),
        reasoning=ReasoningCapability(activation="toggle"),
        reasoning_request=ReasoningRequest(mode="enabled"),
    )
    await generate(service)
    await generate(
        service, REQUEST.model_copy(update={"reasoning": ReasoningRequest(mode="disabled")})
    )
    await generate(service)
    assert [request.mode for request in calls] == ["enabled", "disabled", "enabled"]
    assert adapter.model.reasoning_request.mode == "enabled"


async def test_output_consumer_is_awaited_before_next_delta_and_completion(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    output = []
    service, _ = generation(
        tmp_path,
        FauxProvider(
            [
                ProviderStreamEvent(type="reasoning_delta", reasoning_content="reason"),
                ProviderStreamEvent(type="text_delta", text="answer"),
                ProviderStreamEvent(type="done", usage=Usage(input_tokens=4)),
            ]
        ),
    )

    async def receive(delta):
        output.append(delta.kind)
        entered.set()
        await release.wait()

    task = asyncio.create_task(generate(service, on_output=receive))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        await asyncio.sleep(0)
        assert output == ["reasoning"] and not task.done()
        release.set()
        assert (await task).text == "answer"
        assert output == ["reasoning", "text"]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
