from __future__ import annotations

from typing import Literal, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from ouro.core.base import OuroModel
from ouro.core.plugin_contracts import PluginImplementation


class PluginTaskTarget(OuroModel):
    """A scheduled call to an explicitly schedulable plugin service method."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal["plugin"] = "plugin"
    plugin_id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    method: str = Field(min_length=1)
    arguments: JsonValue = None


class PluginTaskCall(OuroModel):
    """Accepted arguments and exact implementation closure for one occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal["plugin"] = "plugin"
    request: PluginTaskTarget
    implementation: PluginImplementation
    dependencies: tuple[PluginImplementation, ...] = ()

    @model_validator(mode="after")
    def valid_binding(self) -> Self:
        if self.request.plugin_id != self.implementation.plugin_id:
            raise ValueError("Task implementation does not own the requested service")
        ids = [self.implementation.plugin_id, *(item.plugin_id for item in self.dependencies)]
        if len(set(ids)) != len(ids):
            raise ValueError("Task implementation identities must be unique")
        return self
