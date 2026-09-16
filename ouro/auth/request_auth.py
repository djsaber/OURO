from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from ouro.auth.contracts import AuthError as AuthError


@dataclass(frozen=True)
class RequestAuthSnapshot:
    """One attempt's internal material; never serialize into events or public DTOs."""

    headers: Mapping[str, str] = field(repr=False)
    revision: int | None = None
    scope: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class RequestAuthenticator(Protocol):
    async def acquire(self, url: str) -> RequestAuthSnapshot:
        """Authorize the final target before reading managed material."""
        ...

    async def recover(self, snapshot: RequestAuthSnapshot) -> bool:
        """Recover a rejected revision without resending the model request."""
        ...


class StaticRequestAuthenticator:
    """Use already resolved static material, preserving custom endpoint semantics."""

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._snapshot = RequestAuthSnapshot(headers)

    async def acquire(self, url: str) -> RequestAuthSnapshot:
        return self._snapshot

    async def recover(self, snapshot: RequestAuthSnapshot) -> bool:
        return False
