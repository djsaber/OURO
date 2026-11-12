from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from pydantic import ConfigDict, JsonValue, TypeAdapter, ValidationError

_VALUES = TypeAdapter(dict[str, JsonValue], config=ConfigDict(strict=True, allow_inf_nan=False))
_JSON: TypeAdapter[JsonValue] = TypeAdapter(
    JsonValue, config=ConfigDict(strict=True, allow_inf_nan=False)
)


def copy_json(value: object) -> object:
    """Validate and detach one finite JSON value."""
    try:
        return _JSON.validate_python(value)
    except ValidationError:
        raise ValueError("Configuration must contain only finite JSON values") from None


def copy_configuration_values(value: object) -> dict[str, object]:
    try:
        return cast(dict[str, object], _VALUES.validate_python(value))
    except ValidationError:
        raise ValueError(
            "Configuration must be an object containing only finite JSON values"
        ) from None


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
