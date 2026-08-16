from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ouro.builtin_plugins.plan_mode.state import SessionPlanState
from ouro.builtin_plugins.toolkit.todos import TodoItem
from ouro.core.errors import MigrationRequiredError, PersistenceError
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.sdk.plugins.v1 import JsonValue
from ouro.sessions.base import SessionRepository

_PLAN_OWNER = "dev.ouro.plan_mode"
_TODO_OWNER = "dev.ouro.toolkit"


@dataclass(frozen=True)
class BuiltinSessionStateMigrationReport:
    migrated: int = 0
    discarded_duplicate: int = 0
    orphaned_to_trash: int = 0


def migrate_builtin_session_state(
    home: Path,
    repository: SessionRepository,
) -> BuiltinSessionStateMigrationReport:
    """Move legacy builtin files into canonical state; remove after layout-v7 migration."""

    report = BuiltinSessionStateMigrationReport()
    roots = (
        (home / "plugins" / "data" / _PLAN_OWNER / "sessions", _PLAN_OWNER),
        (home / "plugins" / "data" / _TODO_OWNER / "todos", _TODO_OWNER),
    )
    for root, owner_id in roots:
        for source in sorted(root.glob("*.json")):
            session_id = source.stem
            try:
                repository.load_session_record(session_id)
            except FileNotFoundError:
                _move_orphan_to_trash(home, owner_id, source)
                report = BuiltinSessionStateMigrationReport(
                    migrated=report.migrated,
                    discarded_duplicate=report.discarded_duplicate,
                    orphaned_to_trash=report.orphaned_to_trash + 1,
                )
                continue

            record = _load_legacy_record(source, session_id=session_id, owner_id=owner_id)
            existing = next(
                (
                    state
                    for state in repository.load_session_extension_states(session_id)
                    if state.owner_id == owner_id
                ),
                None,
            )
            if existing is not None and (
                existing.state_schema_version != record.state_schema_version
                or existing.payload != record.payload
            ):
                raise MigrationRequiredError(
                    f"legacy builtin Session state conflicts with canonical state: {source}"
                )
            repository.save_session_extension_state(record)
            try:
                source.unlink()
            except OSError as exc:
                raise PersistenceError(
                    f"could not remove migrated Session state {source}: {exc}"
                ) from exc
            report = BuiltinSessionStateMigrationReport(
                migrated=report.migrated + (existing is None),
                discarded_duplicate=report.discarded_duplicate + (existing is not None),
                orphaned_to_trash=report.orphaned_to_trash,
            )
    return report


def _load_legacy_record(
    path: Path,
    *,
    session_id: str,
    owner_id: str,
) -> SessionExtensionStateRecord:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("root must be an object")
        projection_tags: tuple[str, ...]
        if owner_id == _PLAN_OWNER:
            if raw.get("schema_version") != 1:
                raise ValueError("unsupported Plan state schema")
            payload = cast(JsonValue, SessionPlanState.from_json(raw.get("state")).to_json())
            state_schema_version = 1
            projection_tags = ("plan-context",)
        else:
            if raw.get("schema_version") != 1 or not isinstance(raw.get("todos"), list):
                raise ValueError("unsupported TodoList state schema")
            payload = cast(
                JsonValue,
                {"todos": [TodoItem.from_json(item).to_json() for item in raw["todos"]]},
            )
            state_schema_version = 1
            projection_tags = ()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MigrationRequiredError(
            f"could not migrate builtin Session state {path}: {exc}"
        ) from exc
    return SessionExtensionStateRecord(
        session_id=session_id,
        owner_id=owner_id,
        state_schema_version=state_schema_version,
        branch_policy="reset",
        projection_semantic_tags=projection_tags,
        payload=payload,
    )


def _move_orphan_to_trash(home: Path, owner_id: str, source: Path) -> None:
    target = home / "trash" / "plugin-session-state" / owner_id / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if target.read_bytes() == source.read_bytes():
                source.unlink()
                return
        except OSError as exc:
            raise PersistenceError(
                f"could not compare orphan Session state {source}: {exc}"
            ) from exc
        raise MigrationRequiredError(
            f"orphan Session state trash target already contains different data: {target}"
        )
    try:
        os.replace(source, target)
    except OSError as exc:
        raise PersistenceError(f"could not trash orphan Session state {source}: {exc}") from exc


__all__ = ["BuiltinSessionStateMigrationReport", "migrate_builtin_session_state"]
