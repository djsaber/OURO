"""Registration lifecycle and controller scoping shared by callback policies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, Self


class _PolicyRegistration(Protocol):
    @property
    def plugin_id(self) -> str: ...
    @property
    def policy_id(self) -> str: ...
    @property
    def priority(self) -> int: ...
    @property
    def timeout_seconds(self) -> float: ...
    @property
    def owner(self) -> str | None: ...
    @property
    def active_when_controller_id(self) -> str | None: ...
    def __replace__(self, *, owner: str) -> Self: ...


class PolicyRegistry[Registration: _PolicyRegistration]:
    _policy_name: str

    @property
    def _registry_name(self) -> str:
        return self._policy_name

    def __init__(self) -> None:
        self._registrations: list[Registration] = []
        self._frozen = False
        self._active_controller_ids: Callable[[], set[str]] | None = None

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError(f"{self._registry_name} registry structure is frozen")

    def replace_owner(
        self,
        owner: str,
        registrations: tuple[Registration, ...],
    ) -> None:
        self._require_mutable()
        identities = [(item.plugin_id, item.policy_id) for item in registrations]
        if len(identities) != len(set(identities)):
            raise ValueError(f"replacement contains duplicate {self._policy_name} identities")
        if any(item.timeout_seconds <= 0 for item in registrations):
            raise ValueError(f"{self._policy_name} timeout must be positive")
        retained = [item for item in self._registrations if item.owner != owner]
        self._registrations = [
            *retained,
            *(item.__replace__(owner=owner) for item in registrations),
        ]

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> list[Registration]:
        return list(self._registrations)

    def restore(self, snapshot: list[Registration]) -> None:
        self._require_mutable()
        self._registrations = list(snapshot)

    def registered(self) -> list[Registration]:
        active = self._active_controller_ids() if self._active_controller_ids is not None else None
        return sorted(
            (
                item
                for item in self._registrations
                if active is None
                or item.active_when_controller_id is None
                or item.active_when_controller_id in active
            ),
            key=lambda item: (item.priority, item.plugin_id, item.policy_id, item.owner or ""),
        )

    def scoped(
        self,
        active_controller_ids: Callable[[], set[str]],
    ) -> Self:
        scoped = type(self)()
        scoped._registrations = self.snapshot()
        scoped._active_controller_ids = active_controller_ids
        scoped.freeze()
        return scoped
