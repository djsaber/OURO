from __future__ import annotations

import asyncio

import pytest

from ouro.application.models import AvailableModel
from ouro.application.runtime import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


class _RespondingProvider:
    def __init__(self, provider_id: str, text: str) -> None:
        self.provider_id = provider_id
        self.text = text
        self.models: list[str] = []
        self.reasoning_requests: list[ReasoningRequest] = []
        self.request_contexts = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del system_prompt, messages, tools
        self.request_contexts.append(request_context)
        self.models.append(model.model_id)
        self.reasoning_requests.append(model.reasoning_request)
        yield ProviderStreamEvent(type="text_delta", text=self.text)
        yield ProviderStreamEvent(type="done")


class _BlockingToolProvider:
    provider_id = "alpha"

    def __init__(self) -> None:
        self.request_started = asyncio.Event()
        self.release = asyncio.Event()
        self.models: list[str] = []
        self.reasoning_requests: list[ReasoningRequest] = []
        self.request_contexts = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        del system_prompt, messages, tools
        self.request_contexts.append(request_context)
        self.models.append(model.model_id)
        self.reasoning_requests.append(model.reasoning_request)
        if len(self.models) > 1:
            yield ProviderStreamEvent(type="text_delta", text="alpha-response")
            yield ProviderStreamEvent(type="done")
            return
        self.request_started.set()
        await self.release.wait()
        yield ProviderStreamEvent.from_tool_call(
            {"id": "call_1", "name": "Continue", "arguments": {}},
        )
        yield ProviderStreamEvent(type="done")


class _StaticCatalog:
    def __init__(self, adapters: dict[str, RuntimeAdapter], default: str) -> None:
        self.adapters = adapters
        self.default = default

    def list_models(self) -> list[AvailableModel]:
        return [self._model(key, adapter) for key, adapter in sorted(self.adapters.items())]

    def default_model(self) -> AvailableModel:
        return self._model(self.default, self.adapters[self.default])

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if normalized in self.adapters:
            return self._model(normalized, self.adapters[normalized])
        raise ConfigError(f"model not found: {selector}")

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        return self.adapters[self.resolve(selector).key]

    async def close(self) -> None:
        pass

    @staticmethod
    def _model(key: str, adapter: RuntimeAdapter) -> AvailableModel:
        provider_id, endpoint_id = key.split("/", 1)
        return AvailableModel(
            key=key,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=adapter.model.model_id,
            context_window=adapter.model.context_window,
            max_output_tokens=adapter.model.max_output_tokens,
        )


def _runtime(tmp_path) -> tuple[RuntimeService, _StaticCatalog, dict[str, _RespondingProvider]]:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    providers = {
        "alpha": _RespondingProvider("alpha", "alpha-response"),
        "beta": _RespondingProvider("beta", "beta-response"),
    }
    adapters = {
        "alpha/default": RuntimeAdapter(
            providers["alpha"],
            ModelMetadata(
                model_id="alpha-model",
                provider_id="alpha",
                endpoint_id="default",
                context_window=128_000,
                max_output_tokens=8_000,
                reasoning=ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="enabled", effort="low"),
                ),
            ),
        ),
        "beta/fast": RuntimeAdapter(
            providers["beta"],
            ModelMetadata(
                model_id="beta-model",
                provider_id="beta",
                endpoint_id="fast",
                context_window=256_000,
                max_output_tokens=16_000,
                reasoning=ReasoningCapability(
                    activation="always_on",
                    supported_efforts=("low",),
                    default=ReasoningRequest(mode="enabled", effort="low"),
                ),
            ),
        ),
    }
    catalog = _StaticCatalog(adapters, "alpha/default")
    return (
        RuntimeService(
            store=store,
            adapter=adapters["alpha/default"],
            model_catalog=catalog,
            tools=ToolRegistry(),
        ),
        catalog,
        providers,
    )


@pytest.mark.asyncio
async def test_session_context_metrics_keep_usage_and_follow_selected_model_limits(
    tmp_path,
) -> None:
    runtime, _catalog, _providers = _runtime(tmp_path)
    result = await runtime.run("first")
    session_id = result.session.session_id
    durable = runtime.sessions.effective_context_metrics(session_id)
    assert durable is not None

    runtime.switch_session_model(session_id, "beta/fast")
    projected = runtime.session_context_metrics(session_id)

    assert projected is not None
    assert projected.estimated_input_tokens == durable.estimated_input_tokens
    assert projected.estimated_fixed_tokens == durable.estimated_fixed_tokens
    assert projected.estimator_id == durable.estimator_id
    assert projected.context_window_tokens == 256_000
    assert projected.reserved_output_tokens == 16_000
    assert projected.input_budget_tokens == 240_000
    assert durable.context_window_tokens == 128_000
    assert durable.reserved_output_tokens == 8_000


@pytest.mark.asyncio
async def test_session_context_metrics_preserve_history_when_model_is_unavailable(tmp_path) -> None:
    runtime, catalog, _providers = _runtime(tmp_path)
    result = await runtime.run("first", initial_model="beta/fast")
    session_id = result.session.session_id
    durable = runtime.sessions.effective_context_metrics(session_id)
    assert durable is not None
    del catalog.adapters["beta/fast"]

    assert runtime.session_context_metrics(session_id) == durable
    assert runtime.sessions.open(session_id).session.current_model == "beta/fast"
    with pytest.raises(ConfigError, match="model not found"):
        runtime.resolve_model("beta/fast")


@pytest.mark.asyncio
async def test_session_model_switch_persists_and_is_used_after_runtime_restart(tmp_path) -> None:
    runtime, catalog, providers = _runtime(tmp_path)

    first = await runtime.run("first")
    runtime.switch_session_model(first.session.session_id, "beta/fast")
    second = await runtime.run("second", session_id=first.session.session_id)

    assert first.session.current_model == "alpha/default"
    assert second.session.current_model == "beta/fast"
    assert second.final_text == "beta-response"
    assert second.run.model == "beta-model"
    assert runtime.store.load_session(first.session.session_id).session.current_model == "beta/fast"

    restarted = RuntimeService(
        store=runtime.store,
        adapter=catalog.adapters["alpha/default"],
        model_catalog=catalog,
        tools=ToolRegistry(),
    )
    third = await restarted.run("third", session_id=first.session.session_id)

    assert third.final_text == "beta-response"
    assert providers["alpha"].models == ["alpha-model"]
    assert providers["beta"].models == ["beta-model", "beta-model"]
    assert providers["alpha"].request_contexts[0].values == {
        "session.id": first.session.session_id,
        "run.id": first.run.run_id,
        "request.purpose": "chat",
    }
    assert [context.values["session.id"] for context in providers["beta"].request_contexts] == [
        first.session.session_id,
        first.session.session_id,
    ]
    assert [context.values["run.id"] for context in providers["beta"].request_contexts] == [
        second.run.run_id,
        third.run.run_id,
    ]


@pytest.mark.parametrize("budget_exhausted", [False, True])
async def test_continue_binds_selected_model_only_when_a_request_can_start(
    tmp_path, monkeypatch, budget_exhausted
):
    runtime, catalog, providers = _runtime(tmp_path)
    try:
        first = await runtime.run("original task")
        metadata = dict(first.run.metadata)
        if budget_exhausted:
            metadata["run_budget"] = {"max_model_rounds": 1}
        runtime.store.save_run(
            first.run.model_copy(update={"status": "paused", "metadata": metadata})
        )
        runtime.switch_session_model(first.session.session_id, "beta/fast")
        bindings = []
        adapter_for = catalog.adapter_for

        def bind(selector):
            bindings.append(selector)
            return adapter_for(selector)

        monkeypatch.setattr(catalog, "adapter_for", bind)
        resumed = await runtime.continue_run(session_id=first.session.session_id)
        assert resumed.run.run_id == first.run.run_id
        assert resumed.run.status == ("paused" if budget_exhausted else "completed")
        assert bindings == ([] if budget_exhausted else ["beta/fast"])
        assert providers["beta"].models == ([] if budget_exhausted else ["beta-model"])
        assert [
            m.text_content
            for m in runtime.store.load_runtime_session(first.session.session_id).messages
            if m.role == "user"
        ] == ["original task"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_active_session_model_switch_applies_to_the_next_model_request(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    alpha = _BlockingToolProvider()
    beta = _RespondingProvider("beta", "beta-response")
    adapters = {
        "alpha/default": RuntimeAdapter(
            alpha,
            ModelMetadata(model_id="alpha-model", provider_id="alpha", endpoint_id="default"),
        ),
        "beta/fast": RuntimeAdapter(
            beta,
            ModelMetadata(model_id="beta-model", provider_id="beta", endpoint_id="fast"),
        ),
    }
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Continue",
                description="Continue to the next model round.",
            ),
            handler=lambda invocation, context: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="continued",
            ),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=adapters["alpha/default"],
        model_catalog=_StaticCatalog(adapters, "alpha/default"),
        tools=tools,
    )

    running = asyncio.create_task(runtime.run("start"))
    await asyncio.wait_for(alpha.request_started.wait(), timeout=2)
    session_id = next(entry.session_id for entry in runtime.sessions.catalog().entries)

    switched = runtime.switch_session_model(session_id, "beta/fast")
    alpha.release.set()
    result = await asyncio.wait_for(running, timeout=2)

    requests = [event for event in result.events if event.event_type == "model_request_started"]
    assert switched.current_model == "beta/fast"
    assert [event.payload["model"] for event in requests] == ["alpha-model", "beta-model"]
    assert result.final_text == "beta-response"
    assert result.run.model == "beta-model"
    assert result.run.model_endpoint_id == "fast"
    assert alpha.request_contexts[0].values == beta.request_contexts[0].values
    assert beta.request_contexts[0].values["run.id"] == result.run.run_id


@pytest.mark.asyncio
async def test_active_session_reasoning_switch_applies_to_the_next_model_request(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = _BlockingToolProvider()
    adapter = RuntimeAdapter(
        provider,
        ModelMetadata(
            model_id="alpha-model",
            provider_id="alpha",
            endpoint_id="default",
            reasoning=ReasoningCapability(
                activation="toggle",
                supported_efforts=("low", "high"),
                default=ReasoningRequest(mode="enabled", effort="low"),
            ),
        ),
    )
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Continue",
                description="Continue to the next model round.",
            ),
            handler=lambda invocation, context: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content="continued",
            ),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=adapter,
        model_catalog=_StaticCatalog({"alpha/default": adapter}, "alpha/default"),
        tools=tools,
    )

    running = asyncio.create_task(runtime.run("start"))
    await asyncio.wait_for(provider.request_started.wait(), timeout=2)
    session_id = next(entry.session_id for entry in runtime.sessions.catalog().entries)
    high = ReasoningRequest(mode="enabled", effort="high")

    switched = runtime.set_session_reasoning(session_id, high)
    provider.release.set()
    result = await asyncio.wait_for(running, timeout=2)

    assert switched.reasoning_request == high
    assert provider.reasoning_requests == [ReasoningRequest(), high]
    assert result.final_text == "alpha-response"


@pytest.mark.asyncio
async def test_initial_model_creates_first_session_on_selected_route(tmp_path) -> None:
    runtime, _, providers = _runtime(tmp_path)

    result = await runtime.run("first", initial_model="beta/fast")

    assert result.session.current_model == "beta/fast"
    assert result.run.model == "beta-model"
    assert result.final_text == "beta-response"
    assert providers["alpha"].models == []
    assert providers["beta"].models == ["beta-model"]


@pytest.mark.asyncio
async def test_initial_model_is_rejected_for_an_existing_session(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="alpha/default").session

    with pytest.raises(ValueError, match="only be set when creating a session"):
        await runtime.run(
            "hello",
            session_id=session.session_id,
            initial_model="beta/fast",
        )


@pytest.mark.asyncio
async def test_workspace_is_applied_only_while_creating_a_session(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await runtime.run("hello", workspace=workspace)

    assert result.session.workspace == str(workspace.resolve())
    with pytest.raises(ValueError, match="only be set when creating a session"):
        await runtime.run(
            "continue",
            session_id=result.session.session_id,
            workspace=tmp_path,
        )


@pytest.mark.asyncio
async def test_invalid_initial_model_does_not_create_an_empty_session(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)

    with pytest.raises(ConfigError, match="model not found"):
        await runtime.run("hello", initial_model="missing/model")

    assert runtime.sessions.catalog().entries == ()


@pytest.mark.asyncio
async def test_noncanonical_persisted_model_route_is_rejected(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="beta-model").session

    with pytest.raises(ConfigError, match="model not found"):
        await runtime.run("hello", session_id=session.session_id)


def test_session_model_switch_accepts_busy_sessions_and_rejects_unknown_models(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="alpha/default").session

    with pytest.raises(ConfigError, match="model not found"):
        runtime.switch_session_model(session.session_id, "missing/model")

    lock = runtime.store.acquire_session_lock(session.session_id, "run_busy")
    assert lock is not None
    try:
        switched = runtime.switch_session_model(session.session_id, "beta/fast")
    finally:
        lock.release()

    assert switched.current_model == "beta/fast"


def test_model_and_reasoning_updates_do_not_load_message_history(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="alpha/default").session

    def reject_runtime_aggregate(*_args, **_kwargs):
        raise AssertionError("model settings must inspect only Session and Run state")

    monkeypatch.setattr(runtime.store, "load_runtime_session", reject_runtime_aggregate)

    switched = runtime.switch_session_model(session.session_id, "beta/fast")
    updated = runtime.set_session_reasoning(
        session.session_id,
        ReasoningRequest(mode="enabled", effort="low"),
    )

    assert switched.current_model == "beta/fast"
    assert updated.reasoning_request == ReasoningRequest(mode="enabled", effort="low")


@pytest.mark.asyncio
async def test_session_reasoning_is_persisted_and_bound_per_model_request(tmp_path) -> None:
    runtime, catalog, providers = _runtime(tmp_path)
    high = ReasoningRequest(mode="enabled", effort="high")
    low = ReasoningRequest(mode="enabled", effort="low")
    first = runtime.create_session(title="High", reasoning_request=high)
    second = runtime.create_session(title="Low", reasoning_request=low)

    await runtime.run("first", session_id=first.session_id)
    await runtime.run("second", session_id=second.session_id)

    assert providers["alpha"].reasoning_requests == [high, low]
    assert catalog.adapters["alpha/default"].model.reasoning_request == ReasoningRequest()

    restarted = RuntimeService(
        store=runtime.store,
        adapter=catalog.adapters["alpha/default"],
        model_catalog=catalog,
        tools=ToolRegistry(),
    )
    await restarted.run("again", session_id=first.session_id)

    assert providers["alpha"].reasoning_requests[-1] == high
    assert restarted.session_reasoning(first.session_id).requested == high
    assert restarted.session_reasoning(second.session_id).requested == low


def test_model_and_reasoning_changes_reject_incompatible_sessions(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    high = ReasoningRequest(mode="enabled", effort="high")
    session = runtime.create_session(title="Strict", reasoning_request=high)
    limited = runtime.create_session(title="Limited", initial_model="beta/fast")

    with pytest.raises(ConfigError, match="not supported by beta/fast"):
        runtime.set_session_reasoning(limited.session_id, high)
    assert runtime.store.load_session_record(limited.session_id).reasoning_request == (
        ReasoningRequest()
    )

    with pytest.raises(ConfigError, match="not supported by beta/fast"):
        runtime.switch_session_model(session.session_id, "beta/fast")

    persisted = runtime.store.load_session_record(session.session_id)
    assert persisted.current_model == "alpha/default"
    assert persisted.reasoning_request == high

    lock = runtime.store.acquire_session_lock(session.session_id, "run_busy")
    assert lock is not None
    try:
        updated = runtime.set_session_reasoning(
            session.session_id,
            ReasoningRequest(mode="disabled"),
        )
        assert updated.reasoning_request == ReasoningRequest(mode="disabled")
    finally:
        lock.release()


def test_session_model_switch_atomically_replaces_incompatible_reasoning(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    high = ReasoningRequest(mode="enabled", effort="high")
    session = runtime.create_session(title="Strict", reasoning_request=high)

    switched = runtime.switch_session_model(
        session.session_id,
        "beta/fast",
        reasoning_request=ReasoningRequest(),
    )

    assert switched.current_model == "beta/fast"
    assert switched.reasoning_request == ReasoningRequest()
    state = runtime.session_reasoning(session.session_id)
    assert state.model.key == "beta/fast"
    assert state.effective == ReasoningRequest(mode="enabled", effort="low")
