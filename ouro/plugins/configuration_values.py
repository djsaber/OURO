from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType


def copy_json(value: object) -> object:
    """Copy JSON data without coercing keys, tuples, or non-finite numbers."""

    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Configuration object keys must be strings")
        return {key: copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_json(item) for item in value]
    raise ValueError("Configuration must contain only finite JSON values")


def copy_configuration_values(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Plugin configuration must be an object")
    try:
        copied = copy_json(value)
    except RecursionError:
        raise ValueError("Configuration must be a finite JSON document") from None
    assert isinstance(copied, dict)
    return copied


def freeze_configuration(values: Mapping[str, object]) -> Mapping[str, object]:
    """Detach a validated snapshot using read-only mappings and tuples."""

    return MappingProxyType({key: _freeze(item) for key, item in values.items()})


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return freeze_configuration(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def configuration_dict(values: Mapping[str, object]) -> dict[str, object]:
    """Export an immutable snapshot as an independent JSON object."""

    return {key: _thaw(item) for key, item in values.items()}


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return configuration_dict(value)
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value
