from __future__ import annotations

import pytest

from ouro.application.models import AvailableModel
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError, SessionBusyError
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


class _RespondingProvider:
    def __init__(self, provider_id: str, text: str) -> None:
        self.provider_id = provider_id
        self.text = text
        self.models: list[str] = []
        self.reasoning_requests: list[ReasoningRequest] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del system_prompt, messages, tools
        self.models.append(model.model_id)
        self.reasoning_requests.append(model.reasoning_request)
        yield ProviderStreamEvent(type="text_delta", text=self.text)
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

    @staticmethod
    def _model(key: str, adapter: RuntimeAdapter) -> AvailableModel:
        provider_id, endpoint_id = key.split("/", 1)
        return AvailableModel(
            key=key,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=adapter.model.model_id,
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


def test_session_model_switch_rejects_busy_and_unknown_models(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="alpha/default").session

    with pytest.raises(ConfigError, match="model not found"):
        runtime.switch_session_model(session.session_id, "missing/model")

    lock = runtime.store.acquire_session_lock(session.session_id, "run_busy")
    assert lock is not None
    try:
        with pytest.raises(SessionBusyError, match="active run"):
            runtime.switch_session_model(session.session_id, "beta/fast")
    finally:
        lock.release()


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
async def test_interactive_lists_switches_and_reports_session_model(tmp_path) -> None:
    runtime, _, providers = _runtime(tmp_path)
    inputs = iter(
        [
            "hello",
            "/models",
            "/model beta/fast",
            "again",
            "/model",
            "/exit",
        ]
    )
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert any("Available models:" in output for output in outputs)
    assert "Switched model: beta/fast (beta-model)" in outputs
    assert "beta/fast" in outputs
    assert providers["alpha"].models == ["alpha-model"]
    assert providers["beta"].models == ["beta-model"]


@pytest.mark.asyncio
async def test_interactive_selects_model_before_creating_first_session(tmp_path) -> None:
    runtime, _, providers = _runtime(tmp_path)
    inputs = iter(
        [
            "/models",
            "/model beta/fast",
            "/models",
            "/model",
            "hello",
            "/exit",
        ]
    )
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "Selected model for new session: beta/fast (beta-model)" in outputs
    assert "beta/fast" in outputs
    model_lists = [output for output in outputs if output.startswith("Available models:")]
    assert model_lists[0].splitlines()[1].startswith("*")
    assert any(line.startswith("* beta/fast") for line in model_lists[1].splitlines())
    assert providers["alpha"].models == []
    assert providers["beta"].models == ["beta-model"]


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


def test_model_and_reasoning_changes_reject_incompatible_or_busy_sessions(tmp_path) -> None:
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
        with pytest.raises(SessionBusyError, match="active run"):
            runtime.set_session_reasoning(
                session.session_id,
                ReasoningRequest(mode="disabled"),
            )
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_interactive_thinking_command_keeps_new_sessions_independent(tmp_path) -> None:
    runtime, _, providers = _runtime(tmp_path)
    inputs = iter(
        [
            "/thinking high",
            "first",
            "/new",
            "/thinking low",
            "second",
            "/thinking",
            "/exit",
        ]
    )
    outputs: list[str] = []

    code = await run_interactive(
        runtime=runtime,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert "Thinking: high (effective: high; available: default, enabled, disabled, low, high)" in (
        outputs
    )
    assert "Thinking: low (effective: low; available: default, enabled, disabled, low, high)" in (
        outputs
    )
    assert providers["alpha"].reasoning_requests == [
        ReasoningRequest(mode="enabled", effort="high"),
        ReasoningRequest(mode="enabled", effort="low"),
    ]
    sessions = runtime.sessions.catalog().entries
    persisted = [
        runtime.store.load_session_record(entry.session_id).reasoning_request for entry in sessions
    ]
    assert set(request.effort for request in persisted) == {"low", "high"}
