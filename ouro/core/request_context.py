"""Explicit, immutable facts supplied for one model invocation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

# Registration defines availability, not extraction. Each calling use case owns
# the value's lifecycle and must supply it explicitly at the request boundary.
REQUEST_CONTEXT_KEYS = frozenset(
    {
        "session.id",
        "run.id",
        "provider.id",
        "endpoint.id",
        "model.id",
        "request.purpose",
        "request.id",
        "plugin.id",
    }
)


@dataclass(frozen=True)
class ProviderRequestContext:
    values: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if any(key not in REQUEST_CONTEXT_KEYS for key in self.values):
            raise ValueError("unregistered request context key")
        if any(not isinstance(value, str) or not value for value in self.values.values()):
            raise ValueError("request context values must be nonempty strings")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
