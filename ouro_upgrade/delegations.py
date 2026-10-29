"""Resolve historical delegation parents with bounded, reference-driven lookups."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ouro.core.agents import DelegationRecord
from ouro.sessions.backends.files.delegations import LookupKind, lookup_path, record_path
from ouro_upgrade.errors import require
from ouro_upgrade.files import decode, read_json
from ouro_upgrade.records import validate
from ouro_upgrade.workspace import Workspace


def bind_parent(value: dict[str, Any], batch_id: str | None) -> None:
    value["parent_batch_id"] = batch_id
    if batch_id is None:
        value.update(parent_tool_call_id=None, delivery_policy="notify_only")
        if value.get("result"):
            result = value["result"]
            value["result"] = {
                **result,
                "delivery": {**result["delivery"], "policy": "notify_only"},
            }


def resolve_file_parents(work: Workspace, root: str, records: list[dict[str, Any]]) -> None:
    parents: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if (
            record.get("parent_tool_call_id")
            and record["parent_session_id"] != root
            and record.get("parent_batch_id") is None
        ):
            parents.setdefault(record["parent_session_id"], []).append(record)
    for parent, children in parents.items():
        wanted = {(item["parent_run_id"], item["parent_tool_call_id"]) for item in children}
        found = {}
        database = work.path(f"sessions/{parent}/session-data.sqlite3")
        if database.exists():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                for run, call in wanted:
                    row = connection.execute(
                        "SELECT batch_id FROM tool_calls WHERE session_id=? "
                        "AND run_id=? AND tool_call_id=? ORDER BY rowid DESC LIMIT 1",
                        (parent, run, call),
                    ).fetchone()
                    if row:
                        found[(run, call)] = row[0]
        else:
            for run in {key[0] for key in wanted}:
                path = f"sessions/{parent}/runs/{run}.json"
                if not work.path(path).exists():
                    continue
                batch = read_json(work, path).get("active_tool_batch")
                if batch:
                    for call in batch["calls"]:
                        key = (run, call["request"]["id"])
                        if key in wanted:
                            found[key] = (
                                batch.get("batch_id") or batch.get("assistant_message_id") or run
                            )
            missing = wanted - found.keys()
            journal = f"sessions/{parent}/messages.jsonl"
            if missing and work.path(journal).exists():
                checksum = hashlib.sha256()
                with work.path(journal).open("rb") as stream:
                    for line in stream:
                        checksum.update(line)
                        if not line.strip():
                            continue
                        message = decode(line)
                        for call in message.get("tool_calls", ()):
                            key = (message.get("run_id"), call["id"])
                            if key in missing:
                                found[key] = message["message_id"]
                work.observed[journal] = checksum.hexdigest()
        for record in children:
            bind_parent(record, found.get((record["parent_run_id"], record["parent_tool_call_id"])))


def save_delegation(
    connection: sqlite3.Connection, raw: dict[str, Any], *, directory: Path | None = None
) -> None:
    require(raw.get("schema_version") in (1, 2), "delegation", "unsupported version")
    value = dict(raw)
    if value["schema_version"] == 1:
        batch_id = value.get("parent_batch_id")
        if value.get("parent_tool_call_id") is not None and batch_id is None:
            row = connection.execute(
                "SELECT batch_id FROM tool_calls "
                "WHERE session_id=? AND run_id=? AND tool_call_id=? ORDER BY rowid DESC LIMIT 1",
                (value["parent_session_id"], value["parent_run_id"], value["parent_tool_call_id"]),
            ).fetchone()
            batch_id = row[0] if row else None
            bind_parent(value, batch_id)
        value.update(schema_version=2, parent_batch_id=batch_id)
    record = validate(DelegationRecord, value)
    if directory is not None:
        path = record_path(directory, record.delegation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record.model_dump_json(), "utf-8")
        lookups: tuple[tuple[LookupKind, str | None], ...] = (
            ("requests", record.idempotency_key),
            ("children", record.child_session_id),
            ("parents", record.parent_run_id),
        )
        for kind, key in lookups:
            if key is None:
                continue
            path = lookup_path(directory, kind, key)
            if kind == "parents":
                path /= record.delegation_id
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("" if kind == "parents" else record.delegation_id, "utf-8")
    else:
        connection.execute(
            "INSERT INTO delegations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.delegation_id,
                record.root_session_id,
                record.parent_session_id,
                record.parent_run_id,
                record.child_session_id,
                record.idempotency_key,
                record.spawn_state,
                record.result.delivery.status if record.result else None,
                record.revision,
                record.created_at.isoformat(),
                record.model_dump_json(),
            ),
        )
