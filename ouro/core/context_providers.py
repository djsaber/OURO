from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.redaction import redact_text

ContextProviderFailureMode = Literal["omit", "block"]
ContextFragmentPersistence = Literal["ephemeral", "message"]


class ContextProviderCall(OuroModel):
    session_id: str
    run_id: str
    message_count: int = Field(ge=0)
    model_round: int = Field(ge=1)


class ContextFragment(OuroModel):
    text: str = Field(min_length=1)
    persistence: ContextFragmentPersistence = "ephemeral"


class AttributedContextFragment(OuroModel):
    plugin_id: str
    provider_id: str
    generation_id: str | None = None
    fragment_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    persistence: ContextFragmentPersistence = "ephemeral"


class ContextProviderResult(OuroModel):
    fragments: list[ContextFragment] = Field(default_factory=list)


ContextProviderHandler = Callable[
    [ContextProviderCall],
    ContextProviderResult | Awaitable[ContextProviderResult],
]


@dataclass(frozen=True)
class ContextProviderRegistration:
    plugin_id: str
    provider_id: str
    handler: ContextProviderHandler
    priority: int = 100
    timeout_seconds: float = 10.0
    failure_mode: ContextProviderFailureMode = "omit"
    owner: str | None = None
    generation_id: str | None = None


class ContextProviderFailure(OuroModel):
    plugin_id: str
    provider_id: str
    session_id: str
    run_id: str
    failure_mode: ContextProviderFailureMode
    error_type: str
    message: str


@dataclass
class ContextProviderEvaluation:
    fragments: list[AttributedContextFragment] = field(default_factory=list)
    failures: list[ContextProviderFailure] = field(default_factory=list)
    blocked: bool = False


class ContextProviderRegistry:
    """Deterministic registry for context that participates in model semantics."""

    def __init__(self) -> None:
        self._registrations: list[ContextProviderRegistration] = []
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("context provider registry structure is frozen")

    def replace_owner(
        self,
        owner: str,
        registrations: tuple[ContextProviderRegistration, ...],
    ) -> None:
        self._require_mutable()
        identities = [(item.plugin_id, item.provider_id) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate context provider identities")
        if any(item.timeout_seconds <= 0 for item in registrations):
            raise ValueError("context provider timeout must be positive")
        retained = [item for item in self._registrations if item.owner != owner]
        self._registrations = [
            *retained,
            *(replace(item, owner=owner) for item in registrations),
        ]

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> list[ContextProviderRegistration]:
        return list(self._registrations)

    def restore(self, snapshot: list[ContextProviderRegistration]) -> None:
        self._require_mutable()
        self._registrations = list(snapshot)

    def registered(self) -> list[ContextProviderRegistration]:
        return sorted(
            self._registrations,
            key=lambda item: (item.priority, item.plugin_id, item.provider_id, item.owner or ""),
        )

    async def evaluate(self, call: ContextProviderCall) -> ContextProviderEvaluation:
        evaluation = ContextProviderEvaluation()
        for registration in self.registered():
            try:
                result = await invoke_bounded_callback(
                    registration.handler,
                    call,
                    timeout_seconds=registration.timeout_seconds,
                )
                if not isinstance(result, ContextProviderResult):
                    raise TypeError("context provider must return ContextProviderResult")
                evaluation.fragments.extend(
                    AttributedContextFragment(
                        plugin_id=registration.plugin_id,
                        provider_id=registration.provider_id,
                        generation_id=registration.generation_id,
                        fragment_index=index,
                        text=fragment.text,
                        persistence=fragment.persistence,
                    )
                    for index, fragment in enumerate(result.fragments)
                )
            except TimeoutError:
                failure = ContextProviderFailure(
                    plugin_id=registration.plugin_id,
                    provider_id=registration.provider_id,
                    session_id=call.session_id,
                    run_id=call.run_id,
                    failure_mode=registration.failure_mode,
                    error_type="TimeoutError",
                    message=(
                        f"context provider timed out after {registration.timeout_seconds:g} seconds"
                    ),
                )
                evaluation.failures.append(failure)
                if registration.failure_mode == "block":
                    evaluation.blocked = True
                    return evaluation
            except Exception as exc:  # noqa: BLE001 - extension isolation boundary.
                failure = ContextProviderFailure(
                    plugin_id=registration.plugin_id,
                    provider_id=registration.provider_id,
                    session_id=call.session_id,
                    run_id=call.run_id,
                    failure_mode=registration.failure_mode,
                    error_type=type(exc).__name__,
                    message=redact_text(str(exc)),
                )
                evaluation.failures.append(failure)
                if registration.failure_mode == "block":
                    evaluation.blocked = True
                    return evaluation
        return evaluation


__all__ = [
    "AttributedContextFragment",
    "ContextFragment",
    "ContextFragmentPersistence",
    "ContextProviderCall",
    "ContextProviderEvaluation",
    "ContextProviderFailure",
    "ContextProviderFailureMode",
    "ContextProviderRegistration",
    "ContextProviderRegistry",
    "ContextProviderResult",
]
