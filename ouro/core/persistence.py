from __future__ import annotations

import json

from pydantic import ValidationError

from ouro.core.base import SCHEMA_VERSION, VersionedOuroModel
from ouro.core.errors import MigrationRequiredError, PersistenceError


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
    if type(version) is not int or version != SCHEMA_VERSION:
        found = "missing" if "schema_version" not in raw else repr(version)
        raise MigrationRequiredError(
            f"{label} schema migration required: found {found}, expected {SCHEMA_VERSION}"
        )

    try:
        return model_type.model_validate(raw)
    except ValidationError as exc:
        raise PersistenceError(f"could not validate {label}: {exc}") from exc


__all__ = ["decode_versioned_record_json"]
