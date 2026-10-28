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
        record = model_type.model_validate_json(payload)
    except ValidationError as exc:
        # Parse invalid input only to preserve the persisted-version diagnostic
        # boundary. Valid records are decoded and validated in one pass.
        try:
            raw = json.loads(payload)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
            raise PersistenceError(f"could not decode {label}: {error}") from error
        if not isinstance(raw, dict):
            raise PersistenceError(
                f"could not decode {label}: record must be a JSON object"
            ) from exc
        version = raw.get("schema_version")
        expected = model_type.model_fields["schema_version"].default
        if type(version) is not int or version != expected:
            found = "missing" if "schema_version" not in raw else repr(version)
            raise UnsupportedDataVersionError(
                f"unsupported {label} schema: found {found}, expected {expected}"
            ) from exc
        raise PersistenceError(f"could not validate {label}: {exc}") from exc
    if "schema_version" not in record.model_fields_set:
        raise UnsupportedDataVersionError(
            f"unsupported {label} schema: found missing, expected {record.schema_version}"
        )
    return record


__all__ = ["decode_versioned_record_json"]
