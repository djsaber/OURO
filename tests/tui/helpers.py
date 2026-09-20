"""Shared TUI test fixtures built outside the single-adapter faux default."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from ouro.application.models import AvailableModel, close_runtime_adapters
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


@asynccontextmanager
async def run_ready_app(app, **options):
    """Start a test Host before interacting with its surface.

    Textual's mount/idle boundary does not await application startup I/O. Only
    wait for that worker; live session observers deliberately remain running.
    Tests of startup itself should continue to use App.run_test directly.
    """

    async with app.run_test(**options) as pilot:
        for worker in tuple(app.workers):
            if worker.name == "tui-start":
                await worker.wait()
        await pilot.pause()
        yield pilot


class StaticRouteCatalog:
    """Protocol-compliant catalog over fixed faux routes for interface tests."""

    def __init__(
        self,
        adapters: dict[str, RuntimeAdapter],
        *,
        default_key: str,
    ) -> None:
        self._adapters = adapters
        self._default_key = default_key
        self._models = {
            key: AvailableModel(
                key=key,
                provider_id=adapter.model.provider_id,
                endpoint_id=adapter.model.endpoint_id or adapter.model.model_id,
                model_id=adapter.model.model_id,
                context_window=adapter.model.context_window,
                max_output_tokens=adapter.model.max_output_tokens,
                reasoning=adapter.model.reasoning,
            )
            for key, adapter in adapters.items()
        }

    def list_models(self) -> list[AvailableModel]:
        return sorted(self._models.values(), key=lambda item: item.key)

    def default_model(self) -> AvailableModel:
        return self._models[self._default_key]

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if normalized not in self._models:
            raise ConfigError(f"model not found: {selector}")
        return self._models[normalized]

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        return self._adapters[self.resolve(selector).key]

    async def close(self) -> None:
        await close_runtime_adapters(tuple(self._adapters.values()))


def two_route_application(tmp_path: Path, *, provider_id: str = "faux") -> LocalApplication:
    """One faux runtime exposing a default and a separately selectable route."""

    default_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="default-model", provider_id=provider_id, endpoint_id="default"),
    )
    recent_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="recent-model", provider_id=provider_id, endpoint_id="recent"),
    )
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=default_adapter,
        model_catalog=StaticRouteCatalog(
            {
                f"{provider_id}/default": default_adapter,
                f"{provider_id}/recent": recent_adapter,
            },
            default_key=f"{provider_id}/default",
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)
