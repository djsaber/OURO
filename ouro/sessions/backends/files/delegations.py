"""Root-local Delegation facts and their bounded identity lookups."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from ouro.core.agents import DelegationRecord
from ouro.core.base import VersionedOuroModel
from ouro.core.errors import PersistenceError
from ouro.core.persistence import decode_versioned_record_json
from ouro.local_io import read_text_atomic, write_model_atomic, write_text_atomic

LookupKind = Literal["requests", "children", "parents"]


class FileDelegationDecision(VersionedOuroModel):
    kind: Literal["delegation"] = "delegation"
    record: DelegationRecord
    observation_revision: int
    old_parent_run_id: str | None = None
    old_child_session_id: str | None = None
    old_idempotency_key: str | None = None


def record_path(directory: Path, delegation_id: str) -> Path:
    return directory / "delegations" / f"{_identity(delegation_id)}.json"


def lookup_path(directory: Path, kind: LookupKind, key: str) -> Path:
    # Host request keys are opaque values, never filesystem paths or body hashes.
    name = hashlib.sha256(key.encode("utf-8")).hexdigest() if kind == "requests" else _identity(key)
    return directory / "delegation-index" / kind / name


def read_record(directory: Path, root_id: str, delegation_id: str) -> DelegationRecord:
    record = decode_versioned_record_json(
        DelegationRecord,
        read_text_atomic(record_path(directory, delegation_id)),
        label=f"delegation {delegation_id}",
    )
    if record.root_session_id != root_id or record.delegation_id != delegation_id:
        raise PersistenceError("Delegation record has a foreign identity")
    return record


def find_record(
    directory: Path, root_id: str, kind: Literal["requests", "children"], key: str
) -> DelegationRecord | None:
    try:
        identity = read_text_atomic(lookup_path(directory, kind, key))
    except FileNotFoundError:
        return None
    try:
        record = read_record(directory, root_id, identity)
    except FileNotFoundError as exc:
        raise PersistenceError("Delegation lookup references a missing record") from exc
    actual = record.idempotency_key if kind == "requests" else record.child_session_id
    if actual != key:
        raise PersistenceError("Delegation lookup disagrees with its record")
    return record


def write_decision(directory: Path, decision: FileDelegationDecision) -> None:
    record = decision.record
    old_lookups: tuple[tuple[LookupKind, str | None, str | None], ...] = (
        ("requests", decision.old_idempotency_key, record.idempotency_key),
        ("children", decision.old_child_session_id, record.child_session_id),
        ("parents", decision.old_parent_run_id, record.parent_run_id),
    )
    for kind, old_key, new_key in old_lookups:
        if old_key is not None and old_key != new_key:
            old_path = lookup_path(directory, kind, old_key)
            if kind == "parents":
                old_path /= _identity(record.delegation_id)
            old_path.unlink(missing_ok=True)
    write_model_atomic(record_path(directory, record.delegation_id), record)
    parent = lookup_path(directory, "parents", record.parent_run_id) / _identity(
        record.delegation_id
    )
    if not parent.exists():
        write_text_atomic(parent, "")
    lookups: tuple[tuple[LookupKind, str | None], ...] = (
        ("requests", record.idempotency_key),
        ("children", record.child_session_id),
    )
    for kind, key in lookups:
        if key is not None:
            path = lookup_path(directory, kind, key)
            if not path.exists() or read_text_atomic(path) != record.delegation_id:
                write_text_atomic(path, record.delegation_id)


def _identity(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None:
        raise PersistenceError("invalid Delegation lookup identity")
    return value
