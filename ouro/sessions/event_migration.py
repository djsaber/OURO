"""One-time conversion of event provenance for the versioned storage layouts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ouro.core.errors import MigrationRequiredError
from ouro.core.events import EventRecord


def migrate_event(
    raw: dict[str, Any], run_epochs: Mapping[str, int], *, history_epoch: int
) -> EventRecord:
    version = raw.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise MigrationRequiredError(f"unsupported Event schema: {version!r}")
    if version == 1:
        raw = {
            **raw,
            "schema_version": 2,
            "history_epoch": run_epochs.get(raw.get("run_id") or "", history_epoch),
        }
    return EventRecord.model_validate(raw)


def migrate_event_journal(payloads: list[str], run_epochs: Mapping[str, int]) -> list[EventRecord]:
    epoch = 0
    events = []
    for payload in payloads:
        raw = json.loads(payload)
        if raw["event_type"] == "session_history_withdrawn":
            epoch = raw["payload"]["history_epoch"]
        event = migrate_event(raw, run_epochs, history_epoch=epoch)
        epoch = max(epoch, event.history_epoch)
        events.append(event)
    return events


def migrate_result_events(result: dict[str, Any], run_epochs: Mapping[str, int]) -> None:
    epoch = result["session"]["history_epoch"]
    result["events"] = [
        migrate_event(event, run_epochs, history_epoch=epoch).model_dump(mode="json")
        for event in result["events"]
    ]
