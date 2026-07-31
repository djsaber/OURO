from __future__ import annotations

import json

import pytest

from ouro.builtin_plugins.session_state_migration import migrate_builtin_session_state
from ouro.core.errors import MigrationRequiredError
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import SessionRecord
from ouro.plugins.api import BoundPluginSessionStateStore
from ouro.sessions import LocalSessionStore


def test_candidate_generation_cannot_mutate_canonical_session_state(tmp_path) -> None:
    repository = LocalSessionStore(tmp_path / "home")
    repository.initialize()
    session = SessionRecord(title="Generation gate")
    repository.create_session(session)
    state = BoundPluginSessionStateStore(repository, "dev.example.plugin")

    with pytest.raises(RuntimeError, match="candidate plugin Generation"):
        state.save(
            session.session_id,
            state_schema_version=1,
            payload={"phase": "candidate"},
        )

    state.activate()
    state.save(
        session.session_id,
        state_schema_version=1,
        payload={"phase": "active"},
        branch_policy="copy",
    )

    assert repository.load_session_extension_states(session.session_id)[0].payload == {
        "phase": "active"
    }


def test_legacy_builtin_state_migrates_and_orphans_move_to_trash(tmp_path) -> None:
    home = tmp_path / "home"
    repository = LocalSessionStore(home)
    repository.initialize()
    goal_session = SessionRecord(session_id="ses_goal")
    plan_session = SessionRecord(session_id="ses_plan")
    todo_session = SessionRecord(session_id="ses_todo")
    for session in (goal_session, plan_session, todo_session):
        repository.create_session(session)

    legacy = {
        "dev.ouro.goal_mode/sessions/ses_goal.json": {
            "schema_version": 2,
            "state": {"goal": None, "run_binding": None},
        },
        "dev.ouro.plan_mode/sessions/ses_plan.json": {
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

    assert report.migrated == 3
    assert report.orphaned_to_trash == 1
    assert not any((home / "plugins" / "data").glob("**/*.json"))
    assert (
        home / "trash" / "plugin-session-state" / "dev.ouro.toolkit" / "ses_missing.json"
    ).is_file()
    goal = repository.load_session_extension_states(goal_session.session_id)[0]
    plan = repository.load_session_extension_states(plan_session.session_id)[0]
    todo = repository.load_session_extension_states(todo_session.session_id)[0]
    assert goal.projection_semantic_tags == ("goal-contract",)
    assert plan.projection_semantic_tags == ("plan-context",)
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
            owner_id="dev.ouro.goal_mode",
            state_schema_version=2,
            payload={"goal": None, "run_binding": None, "canonical": True},
        )
    )
    path = (
        home / "plugins" / "data" / "dev.ouro.goal_mode" / "sessions" / f"{session.session_id}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state": {"goal": None, "run_binding": None},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MigrationRequiredError, match="conflicts with canonical state"):
        migrate_builtin_session_state(home, repository)

    assert path.is_file()
