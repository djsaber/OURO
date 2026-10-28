from __future__ import annotations

import hashlib
from pathlib import Path

from ouro.core.errors import PersistenceError
from ouro.core.inputs import InputRecord, InputState
from ouro.core.persistence import decode_versioned_record_json
from ouro.local_io import read_text_atomic


def input_key(input_id: str) -> str:
    """Encode an opaque Host identity as a fixed filename, never a path."""
    if not input_id:
        raise ValueError("invalid input identity")
    return hashlib.sha256(input_id.encode("utf-8")).hexdigest()


def input_path(directory: Path, input_id: str, *, state: bool = False) -> Path:
    return directory / "inputs" / ("states" if state else "records") / f"{input_key(input_id)}.json"


def read_input(directory: Path, session_id: str, input_id: str) -> InputRecord | None:
    try:
        payload = read_text_atomic(input_path(directory, input_id))
    except FileNotFoundError:
        return None
    record = decode_versioned_record_json(InputRecord, payload, label=f"input {input_id}")
    if record.session_id != session_id or record.item.queue_id != input_id:
        raise PersistenceError("accepted input has a foreign identity")
    return record


def read_state(directory: Path, session_id: str, input_id: str) -> InputState | None:
    try:
        payload = read_text_atomic(input_path(directory, input_id, state=True))
    except FileNotFoundError:
        return None
    record = decode_versioned_record_json(InputState, payload, label=f"input state {input_id}")
    if record.session_id != session_id or record.input_id != input_id:
        raise PersistenceError("input state has a foreign identity")
    return record
