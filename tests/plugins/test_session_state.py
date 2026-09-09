from __future__ import annotations

import json

import pytest

from ouro.builtin_plugins.session_state_migration import migrate_builtin_session_state
from ouro.core.errors import MigrationRequiredError
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import SessionRecord
from ouro.sdk.plugins.v2 import PluginContext
from ouro.sessions import LocalSessionStore


def test_plugin_instance_context_exposes_no_session_writer() -> None:
    assert "session_state" not in PluginContext.__dataclass_fields__


def test_legacy_builtin_state_migrates_and_orphans_move_to_trash(tmp_path) -> None:
    home = tmp_path / "home"
    repository = LocalSessionStore(home)
    repository.initialize()
    plan_session = SessionRecord(session_id="ses_plan")
    todo_session = SessionRecord(session_id="ses_todo")
    for session in (plan_session, todo_session):
        repository.create_session(session)

    legacy = {
        "dev.ouro.plan/sessions/ses_plan.json": {
            "schema_version": 1,
            "state": {},
        },
        "dev.ouro.toolkit/todos/ses_todo.json": {
            "schema_version": 1,
            "todos": [{"title": "verify", "status": "pending"}],
        },
        "dev.ouro.toolkit/todos/ses_missing.json": {
            "schema_version": 1,
            "todos": [{"title": "orphan", "status": "pending"}],
        },
    }
    for relative, payload in legacy.items():
        path = home / "plugins" / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    report = migrate_builtin_session_state(home, repository)

    assert report.migrated == 2
    assert report.orphaned_to_trash == 1
    assert not any((home / "plugins" / "data").glob("**/*.json"))
    assert (
        home / "trash" / "plugin-session-state" / "dev.ouro.toolkit" / "ses_missing.json"
    ).is_file()
    plan = repository.load_session_extension_states(plan_session.session_id)[0]
    todo = repository.load_session_extension_states(todo_session.session_id)[0]
    assert plan.projection_semantic_tags == ("plan-context",)
    # Import retains the source schema; the explicit Harness migration converts its references.
    assert plan.state_schema_version == 1
    assert plan.payload == {}
    assert todo.payload == {"todos": [{"title": "verify", "status": "pending"}]}


def test_legacy_migration_refuses_to_overwrite_conflicting_canonical_state(tmp_path) -> None:
    home = tmp_path / "home"
    repository = LocalSessionStore(home)
    repository.initialize()
    session = SessionRecord(session_id="ses_conflict")
    repository.create_session(session)
    repository.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=session.session_id,
            owner_id="dev.ouro.plan",
            state_schema_version=1,
            payload={"phase": "active", "canonical": True},
        )
    )
    path = home / "plugins" / "data" / "dev.ouro.plan" / "sessions" / f"{session.session_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationRequiredError, match="conflicts with canonical state"):
        migrate_builtin_session_state(home, repository)

    assert path.is_file()
