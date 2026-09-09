from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True)
class PluginSessionState:
    """One plugin-owned business value from the current call's state view."""

    state_schema_version: int
    payload: JsonValue


class PluginSessionStateView(Protocol):
    """Invocation-bound access; replacement and deletion only stage proposals."""

    @property
    def writable(self) -> bool: ...

    def read(self) -> PluginSessionState | None: ...

    def replace(
        self,
        *,
        state_schema_version: int,
        payload: JsonValue,
        branch_policy: Literal["copy", "reset", "reject"] = "reset",
        projection_semantic_tags: tuple[str, ...] = (),
    ) -> None: ...

    def delete(self) -> None: ...
