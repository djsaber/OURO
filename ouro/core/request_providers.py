"""Ordering, ownership and bounded execution for request-scoped providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Container, Iterable
from dataclasses import dataclass, field, replace
from typing import Literal, Self

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.redaction import redact_text


@dataclass(frozen=True)
class RequestProviderRegistration[Call, Result]:
    plugin_id: str
    provider_id: str
    handler: Callable[[Call], Result | Awaitable[Result]]
    priority: int = 100
    timeout_seconds: float = 10.0
    failure_mode: Literal["omit", "block"] = "omit"
    owner: str | None = None
    generation_id: str | None = None
    active_when_controller_id: str | None = None


class RequestProviderFailure(OuroModel):
    plugin_id: str
    provider_id: str
    failure_mode: Literal["omit", "block"]
    error_type: str
    message: str


@dataclass
class ProviderEvaluation[T]:
    fragments: list[T] = field(default_factory=list)
    failures: list[RequestProviderFailure] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.failures and self.failures[-1].failure_mode == "block")


class OrderedProviderRegistry[R: RequestProviderRegistration]:
    def __init__(self) -> None:
        self._registrations: tuple[R, ...] = ()
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("provider registry structure is frozen")

    def replace_owner(self, owner: str, registrations: tuple[R, ...]) -> None:
        self._require_mutable()
        identities = [(item.plugin_id, item.provider_id) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate provider identities")
        if any(item.timeout_seconds <= 0 for item in registrations):
            raise ValueError("provider timeout must be positive")
        updated = [
            *(item for item in self._registrations if item.owner != owner),
            *(replace(item, owner=owner) for item in registrations),
        ]
        self._registrations = tuple(
            sorted(updated, key=lambda p: (p.priority, p.plugin_id, p.provider_id, p.owner or ""))
        )

    def snapshot(self) -> tuple[R, ...]:
        return self._registrations

    def restore(self, snapshot: tuple[R, ...]) -> None:
        self._require_mutable()
        self._registrations = snapshot

    def scoped(self, active_controller_ids: Container[str]) -> Self:
        scoped = type(self)()
        scoped._registrations = tuple(
            item
            for item in self._registrations
            if item.active_when_controller_id is None
            or item.active_when_controller_id in active_controller_ids
        )
        scoped.freeze()
        return scoped

    async def _evaluate[T, V](
        self,
        call: object,
        result_type: type[T],
        fragments: Callable[[R, T], Iterable[V]],
    ) -> ProviderEvaluation[V]:
        evaluation: ProviderEvaluation[V] = ProviderEvaluation()
        for provider in self._registrations:
            try:
                result = await invoke_bounded_callback(
                    provider.handler, call, timeout_seconds=provider.timeout_seconds
                )
                if not isinstance(result, result_type):
                    raise TypeError(f"provider must return {result_type.__name__}")
                evaluation.fragments.extend(fragments(provider, result))
            except Exception as exc:  # noqa: BLE001 - isolate extension failures.
                evaluation.failures.append(
                    RequestProviderFailure(
                        plugin_id=provider.plugin_id,
                        provider_id=provider.provider_id,
                        failure_mode=provider.failure_mode,
                        error_type=type(exc).__name__,
                        message=f"provider timed out after {provider.timeout_seconds:g} seconds"
                        if isinstance(exc, TimeoutError)
                        else redact_text(str(exc)),
                    )
                )
                if provider.failure_mode == "block":
                    break
        return evaluation
