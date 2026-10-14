from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import (
    ConfigDict,
    Field,
    FieldSerializationInfo,
    SecretStr,
    TypeAdapter,
    field_serializer,
)

from ouro.core.base import OuroModel


class InlinePluginCredential(OuroModel):
    """A persisted key, redacted unless explicitly serialized for storage or export."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    type: Literal["inline"] = "inline"
    value: SecretStr = Field(repr=False)

    @field_serializer("value", when_used="json")
    def _serialize_value(self, value: SecretStr, info: FieldSerializationInfo) -> str:
        if info.context and info.context.get("include_secrets"):
            return value.get_secret_value()
        return str(value)


PluginCredentialBinding = str | InlinePluginCredential
_BINDINGS = TypeAdapter(dict[str, PluginCredentialBinding])


def parse_credential_bindings(value: object) -> dict[str, PluginCredentialBinding]:
    try:
        return _BINDINGS.validate_python(value, strict=True)
    except ValueError:
        raise ValueError(
            "Credential bindings must map slots to env: references or inline key objects"
        ) from None


def credential_bindings_payload(bindings: Mapping[str, PluginCredentialBinding]) -> object:
    """Serialize exact binding contents for the immutable configuration digest."""

    return _BINDINGS.dump_python(dict(bindings), mode="json", context={"include_secrets": True})


def resolve_credential_binding(
    binding: PluginCredentialBinding,
    resolve_reference: Callable[[str], str | None] | None,
) -> str | None:
    if isinstance(binding, InlinePluginCredential):
        return binding.value.get_secret_value()
    if resolve_reference is None:
        raise RuntimeError("credential resolver is unavailable")
    return resolve_reference(binding)
