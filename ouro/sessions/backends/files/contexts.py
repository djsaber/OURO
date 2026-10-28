"""Latest request input publication and its small mutable header.

All functions run under the existing Session lock. Replacement uses commit.json,
the same native decision slot as other Session commits, and never executes code
outside storage. Readers materialize the selected input before releasing the lock.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ouro.core.context_snapshots import (
    ModelRequestHeader,
    RequestManifestRecord,
)
from ouro.core.errors import PersistenceError
from ouro.core.persistence import decode_versioned_record_json
from ouro.local_io import read_text_atomic
from ouro.sessions import sqlite_requests


def input_path(directory: Path, context_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", context_id) is None:
        raise PersistenceError("invalid context identity")
    return directory / "request-content" / f"{context_id}.json"


def read_request(
    connection: sqlite3.Connection, directory: Path
) -> tuple[ModelRequestHeader, RequestManifestRecord] | None:
    header = sqlite_requests.read_latest_header(connection, directory.name)
    if header is None:
        return None
    try:
        body = decode_versioned_record_json(
            RequestManifestRecord,
            read_text_atomic(input_path(directory, header.context_id)),
            label="context input",
        )
    except FileNotFoundError as exc:
        raise PersistenceError("context header references missing input") from exc
    if header.context_id != body.context_id:
        raise PersistenceError("context header and input have different identities")
    return header, body
