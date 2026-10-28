from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ouro.core.errors import PersistenceError, UnsupportedDataVersionError
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.session_controls import SessionControlActivation, SessionControlState
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
        ({"schema_version": 2, "session_id": "ses_previous"}, "2"),
        ({"schema_version": 3, "session_id": "ses_previous_selection"}, "3"),
        ({"schema_version": 4, "session_id": "ses_previous_counts"}, "4"),
        ({"schema_version": 6, "session_id": "ses_future"}, "6"),
    ],
)
def test_versioned_decoder_requires_explicit_current_version(
    payload: dict[str, object],
    found: str,
) -> None:
    with pytest.raises(
        UnsupportedDataVersionError,
        match=rf"found {found}, expected 5",
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


def test_versioned_decoder_returns_current_typed_record_without_intermediate_json(
    monkeypatch,
) -> None:
    def reject_intermediate(*args, **kwargs):
        pytest.fail("valid persisted records must decode directly to their typed model")

    monkeypatch.setattr(json, "loads", reject_intermediate)
    session = decode_versioned_record_json(
        SessionRecord,
        SessionRecord(session_id="ses_current").model_dump_json(),
        label="session record",
    )

    assert session.session_id == "ses_current"
    assert session.schema_version == 5


def _activation(**changes):
    return SessionControlActivation(
        controller_id="example.plugin/session_controllers/plan",
        plugin_id="example.plugin",
        controller_local_id="plan",
        plugin_class="user",
        release_id="release",
        generation_id="generation",
        execution_contract_hash="contract",
        **changes,
    )


@pytest.mark.parametrize("phase", ["activating", "deactivating", "failed"])
def test_session_controls_reject_non_committed_lifecycle_phases(phase):
    with pytest.raises(ValidationError, match="phase"):
        _activation(phase=phase)


def test_session_controls_reject_duplicate_controller_identity():
    with pytest.raises(ValidationError, match="duplicate active session controller"):
        SessionControlState(activations=[_activation(), _activation()])
