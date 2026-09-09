from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True)
class PluginSessionState:
    """One plugin-owned business value from the current call's state view."""

    state_schema_version: int
    payload: JsonValue


@dataclass(frozen=True)
class PluginSessionControl:
    """An active controller owned by this plugin, read from the current draft."""

    controller_id: str
    activation_id: str
    data: dict[str, JsonValue] = field(default_factory=dict)


class PluginSessionStateView(Protocol):
    """Invocation-bound access; replacement and deletion only stage proposals."""

    @property
    def writable(self) -> bool: ...

    def read(self) -> PluginSessionState | None: ...

    def controls(self) -> tuple[PluginSessionControl, ...]: ...

    def replace(
        self,
        *,
        state_schema_version: int,
        payload: JsonValue,
        branch_policy: Literal["copy", "reset", "reject"] = "reset",
        projection_semantic_tags: tuple[str, ...] = (),
    ) -> None: ...

    def delete(self) -> None: ...
