from __future__ import annotations

import json

from pydantic import ValidationError

from ouro.core.base import VersionedOuroModel
from ouro.core.errors import PersistenceError, UnsupportedDataVersionError


def decode_versioned_record_json[RecordT: VersionedOuroModel](
    model_type: type[RecordT],
    payload: str | bytes | bytearray,
    *,
    label: str,
) -> RecordT:
    """Decode one independent core record and enforce its persisted version boundary."""

    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise PersistenceError(f"could not decode {label}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PersistenceError(f"could not decode {label}: record must be a JSON object")

    version = raw.get("schema_version")
    expected = model_type.model_fields["schema_version"].default
    if type(version) is not int or version != expected:
        found = "missing" if "schema_version" not in raw else repr(version)
        raise UnsupportedDataVersionError(
            f"unsupported {label} schema: found {found}, expected {expected}"
        )

    try:
        return model_type.model_validate(raw)
    except ValidationError as exc:
        raise PersistenceError(f"could not validate {label}: {exc}") from exc


__all__ = ["decode_versioned_record_json"]
