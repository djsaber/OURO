"""Model availability queries and reasoning selection."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ouro.application.contracts.models import AvailableModel, ModelReasoningState
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningRequest

if TYPE_CHECKING:
    from ouro.application.contracts.sessions import SessionView
    from ouro.application.sessions.service import SessionService
    from ouro.core.session_ports import SessionStore
    from ouro.core.state import SessionRecord


class RuntimeAdapterCatalog(Protocol):
    """Resolve durable model route keys into request-local runtime adapters."""

    def list_models(self) -> list[AvailableModel]: ...

    def default_model(self) -> AvailableModel: ...

    def configured_default_model(self) -> AvailableModel: ...

    def resolve(self, selector: str) -> AvailableModel: ...

    def adapter_for(self, selector: str) -> RuntimeAdapter: ...

    async def close(self) -> None: ...


class FixedRuntimeAdapterCatalog:
    """Expose one injected adapter through the same route contract as configured models."""

    def __init__(self, adapter: RuntimeAdapter) -> None:
        self._adapter = adapter
        self._model = available_model(adapter.model)

    def list_models(self) -> list[AvailableModel]:
        return [self._model]

    def default_model(self) -> AvailableModel:
        return self._model

    def configured_default_model(self) -> AvailableModel:
        raise ConfigError("system default endpoint is not configured")

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if normalized != self._model.key:
            raise ConfigError(f"model not found: {selector}")
        return self._model

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        self.resolve(selector)
        return self._adapter

    async def close(self) -> None:
        await close_runtime_adapters((self._adapter,))


def model_route_key(model: ModelMetadata) -> str:
    """Return the durable provider/endpoint key stored on a session."""

    endpoint_id = model.endpoint_id or model.model_id
    return f"{model.provider_id}/{endpoint_id}"


def available_model(model: ModelMetadata) -> AvailableModel:
    return AvailableModel(
        key=model_route_key(model),
        provider_id=model.provider_id,
        endpoint_id=model.endpoint_id or model.model_id,
        model_id=model.model_id,
        display_name=model.display_name,
        temperature=model.temperature,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        reasoning=model.reasoning,
    )


async def close_runtime_adapters(adapters: tuple[RuntimeAdapter, ...]) -> None:
    seen: set[int] = set()
    errors: list[Exception] = []
    for adapter in adapters:
        provider = adapter.provider
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        close = getattr(provider, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - close every owned provider before reporting.
            errors.append(exc)
    if errors:
        raise ExceptionGroup("runtime provider shutdown failed", errors)


@dataclass
class SessionModels:
    """Resolve executable routes and reasoning for drafts, Sessions and model requests."""

    catalog: RuntimeAdapterCatalog
    store: SessionStore
    sessions: SessionService

    def available_models(self) -> list[AvailableModel]:
        """Return enabled model endpoints from the current system configuration."""

        return self.catalog.list_models()

    def current_model(self, session_id: str | None = None) -> AvailableModel:
        if session_id is None:
            return self.catalog.default_model()
        session = self.store.load_session_record(session_id)
        return self.catalog.resolve(session.current_model or self.catalog.default_model().key)

    def resolve_model(self, selector: str) -> AvailableModel:
        """Resolve and validate a model route before it is attached to a session."""

        model = self.catalog.resolve(selector)
        self.catalog.adapter_for(model.key)
        return model

    def configured_default_model(self) -> AvailableModel:
        model = self.catalog.configured_default_model()
        self.catalog.adapter_for(model.key)
        return model

    def model_reasoning(
        self,
        selector: str | None,
        request: ReasoningRequest | None = None,
    ) -> ModelReasoningState:
        """Resolve reasoning against an explicit route or the configured default."""
        model = (
            self.catalog.configured_default_model()
            if selector is None
            else self.catalog.resolve(selector)
        )
        return self.reasoning_state(model, request or ReasoningRequest())

    def session_reasoning(self, session_id: str) -> ModelReasoningState:
        """Return the persisted and effective reasoning state for one Session."""

        return self.reasoning_for_session(self.sessions.view(session_id))

    def reasoning_for_session(self, session: SessionView) -> ModelReasoningState:
        """Project reasoning from the supplied Session settings without rereading its record."""
        model = self.catalog.resolve(session.current_model or self.catalog.default_model().key)
        return self.reasoning_state(model, session.reasoning_request)

    def switch_session_model(
        self,
        session_id: str,
        selector: str,
        *,
        reasoning_request: ReasoningRequest | None = None,
    ) -> SessionRecord:
        """Atomically persist an executable model route and reasoning pair.

        Omitting ``reasoning_request`` preserves the Session intent and therefore
        rejects an incompatible route. Passing an explicit request replaces both
        settings in one mutation, which lets interfaces reset or stage reasoning
        when the user deliberately changes model capabilities.
        """

        model = self.resolve_model(selector)

        def validate(session: SessionRecord) -> None:
            selected_reasoning = (
                session.reasoning_request if reasoning_request is None else reasoning_request
            )
            self.reasoning_state(model, selected_reasoning)

        updates: dict[str, object] = {"current_model": model.key}
        if reasoning_request is not None:
            updates["reasoning_request"] = reasoning_request

        return self.sessions.update_settings(
            session_id,
            updates,
            validate=validate,
        )

    def set_session_reasoning(
        self,
        session_id: str,
        reasoning_request: ReasoningRequest,
    ) -> SessionRecord:
        """Persist reasoning intent for the next model request boundary."""

        def validate(session: SessionRecord) -> None:
            model = self.catalog.resolve(session.current_model or self.catalog.default_model().key)
            self.reasoning_state(model, reasoning_request)

        return self.sessions.update_settings(
            session_id,
            {"reasoning_request": reasoning_request},
            validate=validate,
        )

    def reasoning_state(
        self,
        model: AvailableModel,
        request: ReasoningRequest,
    ) -> ModelReasoningState:
        adapter = self.catalog.adapter_for(model.key)
        bound = self._bind_reasoning(adapter, request, model.key)
        return ModelReasoningState(
            model=model,
            capability=adapter.model.reasoning,
            requested=request,
            effective=bound.model.reasoning_resolution.effective,
        )

    @staticmethod
    def _bind_reasoning(
        adapter: RuntimeAdapter,
        request: ReasoningRequest,
        model_key: str,
    ) -> RuntimeAdapter:
        selected = adapter.model.reasoning_request if request == ReasoningRequest() else request
        try:
            return adapter.with_reasoning(selected)
        except ValueError as exc:
            raise ConfigError(f"reasoning setting is not supported by {model_key}: {exc}") from exc

    def bind_session(
        self,
        session_id: str,
    ) -> tuple[SessionRecord, RuntimeAdapter]:
        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            selection = self.catalog.resolve(
                latest.current_model or self.catalog.default_model().key
            )
            adapter = self._bind_reasoning(
                self.catalog.adapter_for(selection.key),
                latest.reasoning_request,
                selection.key,
            )
            if latest.current_model == selection.key:
                return latest, adapter
            session = latest.model_copy(
                update={
                    "current_model": selection.key,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                }
            )
            self.store.save_session(session)
        self.store.refresh_catalog(session.session_id)
        return session, adapter

    def adapter_for_session_id(self, session_id: str) -> RuntimeAdapter:
        _, adapter = self.bind_session(session_id)
        return adapter
