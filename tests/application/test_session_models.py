from __future__ import annotations

import pytest

from ouro.application.models import AvailableModel
from ouro.application.runtime import RuntimeService
from ouro.cli.interactive import run_interactive
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError, SessionBusyError
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


class _RespondingProvider:
    def __init__(self, provider_id: str, text: str) -> None:
        self.provider_id = provider_id
        self.text = text
        self.models: list[str] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del system_prompt, messages, tools
        self.models.append(model.model_id)
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
        aliases = [
            self._model(key, adapter)
            for key, adapter in self.adapters.items()
            if normalized in {adapter.model.model_id, adapter.model.endpoint_id}
        ]
        if len(aliases) == 1:
            return aliases[0]
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
            ),
        ),
        "beta/fast": RuntimeAdapter(
            providers["beta"],
            ModelMetadata(
                model_id="beta-model",
                provider_id="beta",
                endpoint_id="fast",
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
async def test_legacy_unique_model_id_is_canonicalized_in_current_model(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="beta-model")

    result = await runtime.run("hello", session_id=session.session_id)

    assert result.session.current_model == "beta/fast"
    assert runtime.store.load_session(session.session_id).session.current_model == "beta/fast"


def test_session_model_switch_rejects_busy_and_unknown_models(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)
    session = runtime.sessions.create(current_model="alpha/default")

    with pytest.raises(ConfigError, match="model not found"):
        runtime.switch_session_model(session.session_id, "missing/model")

    lock = runtime.store.acquire_session_lock(session.session_id, "run_busy")
    assert lock is not None
    try:
        with pytest.raises(SessionBusyError, match="active run"):
            runtime.switch_session_model(session.session_id, "beta/fast")
    finally:
        lock.release()


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
