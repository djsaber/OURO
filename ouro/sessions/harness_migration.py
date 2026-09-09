from __future__ import annotations

import json
import os
from pathlib import Path

from ouro.core.errors import MigrationRequiredError, PersistenceError
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.messages import MessageRecord
from ouro.core.session_controls import SessionControlState
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_io import read_text_atomic, write_json_atomic, write_model_atomic, write_text_atomic


def migrate_session_record(
    payload: str, states: list[SessionExtensionStateRecord]
) -> tuple[SessionRecord, HarnessVersion | None]:
    raw = _record(payload, "Session")
    if raw["schema_version"] == 2:
        return SessionRecord.model_validate(raw), None
    _require_legacy(raw, "Session")
    state = HarnessState(
        controls=SessionControlState.model_validate(raw.get("controls", {})),
        states=tuple(states),
    )
    raw.update(schema_version=2, harness_version="harness_baseline", history_epoch=0)
    session = SessionRecord.model_validate(raw)
    return session, HarnessVersion(
        session_id=session.session_id, version_id=session.harness_version, state=state
    )


def migrate_message_record(payload: str) -> MessageRecord:
    raw = _record(payload, "Message")
    if raw["schema_version"] != 2:
        _require_legacy(raw, "Message")
        # The current state cannot prove the state before an old message.
        raw.update(schema_version=2, before_harness_version=None)
    return MessageRecord.model_validate(raw)


def migrate_run_record(payload: str) -> RunRecord:
    raw = _record(payload, "Run")
    if raw["schema_version"] != 2:
        _require_legacy(raw, "Run")
        raw.update(schema_version=2, history_epoch=0)
    return RunRecord.model_validate(raw)


def finish_legacy_history_cut(directory: Path) -> None:
    """Complete the old format's durable decision before establishing a baseline."""

    path = directory / "history-cut.json"
    if not path.exists():
        return
    raw = json.loads(read_text_atomic(path))
    if not isinstance(raw, dict) or not isinstance(raw.get("session"), dict):
        raise PersistenceError("invalid legacy history cut")
    session = raw["session"]
    offset = raw.get("offset")
    if session.get("session_id") != directory.name or type(offset) is not int or offset < 0:
        raise PersistenceError("invalid legacy history cut boundary")
    with (directory / "messages.jsonl").open("r+b") as stream:
        if stream.seek(0, 2) < offset:
            raise PersistenceError("legacy history cut offset exceeds journal size")
        stream.truncate(offset)
        stream.flush()
        os.fsync(stream.fileno())
    if raw.get("compactions") is not None:
        write_text_atomic(
            directory / "compactions.jsonl",
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in raw["compactions"]),
        )
    (directory / "context.json").unlink(missing_ok=True)
    write_json_atomic(directory / "session.json", session)
    path.unlink()


def migrate_file_records(directory: Path) -> None:
    path = directory / "messages.jsonl"
    messages = [
        migrate_message_record(line) for line in read_text_atomic(path).split("\n") if line.strip()
    ]
    if any(message.session_id != directory.name for message in messages):
        raise PersistenceError("legacy message belongs to another Session")
    write_text_atomic(path, "".join(f"{message.model_dump_json()}\n" for message in messages))
    for path in (directory / "runs").glob("*.json"):
        run = migrate_run_record(read_text_atomic(path))
        if run.session_id != directory.name:
            raise PersistenceError("legacy Run belongs to another Session")
        write_model_atomic(path, run)


def _record(payload: str, label: str) -> dict[str, object]:
    raw = json.loads(payload)
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
        raise MigrationRequiredError(f"{label} migration requires an explicit record version")
    return raw


def _require_legacy(raw: dict[str, object], label: str) -> None:
    if raw["schema_version"] != 1:
        raise MigrationRequiredError(f"unsupported {label} schema: {raw['schema_version']}")
