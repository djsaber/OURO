from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ouro.core.errors import MigrationRequiredError, PersistenceError
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.state import SessionRecord


def test_versioned_record_model_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        SessionRecord(schema_version=999)


@pytest.mark.parametrize(
    "payload, found",
    [
        ({"session_id": "ses_missing"}, "missing"),
        ({"schema_version": "1", "session_id": "ses_string"}, "'1'"),
        ({"schema_version": 0, "session_id": "ses_old"}, "0"),
        ({"schema_version": 1, "session_id": "ses_legacy"}, "1"),
        ({"schema_version": 3, "session_id": "ses_future"}, "3"),
    ],
)
def test_versioned_decoder_requires_explicit_current_version(
    payload: dict[str, object],
    found: str,
) -> None:
    with pytest.raises(
        MigrationRequiredError,
        match=rf"found {found}, expected 2",
    ):
        decode_versioned_record_json(
            SessionRecord,
            json.dumps(payload),
            label="session record",
        )


@pytest.mark.parametrize("payload", ["[]", '"session"', "null", "{broken"])
def test_versioned_decoder_rejects_non_record_payload(payload: str) -> None:
    with pytest.raises(PersistenceError, match="could not decode session record"):
        decode_versioned_record_json(SessionRecord, payload, label="session record")


def test_versioned_decoder_returns_current_typed_record() -> None:
    session = decode_versioned_record_json(
        SessionRecord,
        SessionRecord(session_id="ses_current").model_dump_json(),
        label="session record",
    )

    assert session.session_id == "ses_current"
    assert session.schema_version == 2
