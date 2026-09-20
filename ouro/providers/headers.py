"""Resolve declared headers without interpreting the facts they reference."""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType

from ouro import __version__
from ouro.config.provider_headers import (
    ProviderHeaderValue,
    configured_provider_headers,
    validate_header_value,
)
from ouro.core.errors import ConfigError
from ouro.core.request_context import ProviderRequestContext


class ProviderHeaderResolver:
    def __init__(
        self,
        headers: Mapping[str, ProviderHeaderValue] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._headers = MappingProxyType(configured_provider_headers(headers or {}))
        self._environ = os.environ if environ is None else environ

    def resolve(
        self,
        context: ProviderRequestContext | None,
        *,
        defaults: Mapping[str, str],
    ) -> dict[str, str]:
        resolved = {"user-agent": f"ouro/{__version__}"}
        resolved.update({name.lower(): value for name, value in defaults.items()})
        for name, source in self._headers.items():
            value: str | None
            if source.source == "literal":
                value = source.value
            elif source.source == "env":
                value = self._environ.get(source.name)
            else:
                value = context.values.get(source.key) if context is not None else None
            if value is None:
                raise ConfigError(f"provider header {name}: {source.source} value is unavailable")
            try:
                resolved[name] = validate_header_value(value)
            except ValueError:
                raise ConfigError(f"provider header {name}: invalid value") from None
        return resolved
