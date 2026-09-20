"""Finite provider header declarations and their safe public projection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.request_context import REQUEST_CONTEXT_KEYS

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Wire framing and content negotiation belong to the transport/protocol.
_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "te",
        "trailer",
        "upgrade",
        "keep-alive",
        "proxy-connection",
        "content-type",
        "accept",
        "accept-encoding",
        "content-encoding",
        "anthropic-version",
    }
)


def validate_header_value(value: str) -> str:
    # httpx's string header API uses ASCII. Reject unsafe/unsupported bytes here
    # so downstream encoding errors cannot echo values into public diagnostics.
    if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("header value must be nonempty printable ASCII")
    if value != value.strip():
        raise ValueError("header value must not have surrounding whitespace")
    return value


class _HeaderSource(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class LiteralHeaderValue(_HeaderSource):
    source: Literal["literal"] = "literal"
    value: str = Field(repr=False, strict=True)

    _validate_value = field_validator("value")(validate_header_value)


class EnvironmentHeaderValue(_HeaderSource):
    source: Literal["env"] = "env"
    name: str = Field(strict=True)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("invalid environment variable name")
        return value


class ContextHeaderValue(_HeaderSource):
    source: Literal["context"] = "context"
    key: str = Field(strict=True)

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        if value not in REQUEST_CONTEXT_KEYS:
            raise ValueError("unregistered request context key")
        return value


ProviderHeaderValue = Annotated[
    LiteralHeaderValue | EnvironmentHeaderValue | ContextHeaderValue,
    Field(discriminator="source"),
]
_HEADERS = TypeAdapter(dict[str, ProviderHeaderValue])


def parse_provider_headers(raw: object) -> dict[str, ProviderHeaderValue]:
    if not isinstance(raw, Mapping):
        raise ValueError("provider headers must be an object")
    normalized: dict[str, object] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("invalid provider header name")
        key = name.lower()
        if key in normalized:
            raise ValueError("duplicate provider header name (case insensitive)")
        if key in _RESERVED_HEADERS:
            raise ValueError(f"provider header is owned by the transport or protocol: {key}")
        normalized[key] = value
    try:
        return _HEADERS.validate_python(normalized)
    except ValidationError as exc:
        # TypeAdapter errors can contain raw values, including unknown source
        # tags and extra field names. Keep only a safe schema-level explanation.
        error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        field = str(error["loc"][-1])
        reason = {
            "value": "literal value must be printable ASCII, nonempty and without padding",
            "name": "env source requires a valid environment variable name",
            "key": "context source requires a registered request context key",
        }.get(field, "expected exactly one source: literal/value, env/name or context/key")
        raise ValueError(f"invalid provider header declaration: {reason}") from None


def configured_provider_headers(raw: object) -> dict[str, ProviderHeaderValue]:
    """Application/constructor boundary with a safe domain error."""
    try:
        return parse_provider_headers(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def public_provider_headers(
    headers: Mapping[str, ProviderHeaderValue],
) -> dict[str, dict[str, str]]:
    return {
        name: (
            {"source": "literal", "value": "***"} if item.source == "literal" else item.model_dump()
        )
        for name, item in headers.items()
    }
