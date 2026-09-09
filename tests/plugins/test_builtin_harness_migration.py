from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.builtin_plugins.goal.state import SessionGoalState
from ouro.builtin_plugins.session_state_migration import migrate_builtin_session_state
from ouro.core.billing import ProviderUsageEntry
from ouro.core.errors import MigrationRequiredError, PersistenceError, SessionBusyError
from ouro.core.harness import HarnessState
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import SessionControlActivation, SessionControlState
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import SessionRecord
from ouro.local_application import migrate_builtin_harness_state
from ouro.plugins.api import adapt_sdk_contributions
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sdk.plugins import v2 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.file_commits import FileSessionCommits
from ouro.sessions.sqlite_commits import SQLiteSessionCommits

GOAL, PLAN = "dev.ouro.goal", "dev.ouro.plan"


@pytest.fixture(params=["files", "sqlite"])
def legacy(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    ledger = LocalUsageLedger(tmp_path / "home")
    ledger.initialize()
    registry = PluginSessionControllerRegistry()
    for owner in (GOAL, PLAN):

        def forbidden_callback(call):
            raise AssertionError("migration must not execute a plugin callback")

        controller = adapt_sdk_contributions(
            sdk.ContributionSet(
                session_controllers=[
                    sdk.SessionControllerContribution(
                        id="mode",
                        description="mode",
                        claims=(
                            sdk.SessionControlClaim("ouro.session.primary_behavior", "exclusive"),
                        ),
                        activate=forbidden_callback,
                        deactivate=forbidden_callback,
                    )
                ]
            ),
            owner_id=owner,
        ).session_controllers[0]
        registry.replace_owner(
            owner,
            [
                replace(
                    controller,
                    plugin_id=owner,
                    release_id="new",
                    generation_id="new-generation",
                    execution_contract_hash="n" * 64,
                )
            ],
        )
    return store, session, UsageService(ledger), registry


def publish_legacy(
    legacy, *, owner=GOAL, tokens=5, run_id="old-run", created="2026-09-01T00:00:00Z"
):
    store, session, _, _ = legacy
    active = SessionControlActivation(
        controller_id=f"{owner}/session_controllers/mode",
        controller_local_id="mode",
        plugin_id=owner,
        plugin_class="builtin",
        release_id="old",
        generation_id="old-generation",
        execution_contract_hash="o" * 64,
        data={"obsolete": True},
    )
    goal = {
        "objective": "Old work",
        "status": "active",
        "token_budget": 50,
        "tokens_used": tokens,
        "created_at": created,
        "updated_at": created,
        "report": None,
        "activation_id": active.activation_id,
        "revision": 2,
        "last_accounted_run_id": run_id,
    }
    record = SessionExtensionStateRecord(
        session_id=session.session_id,
        owner_id=owner,
        state_schema_version=3 if owner == GOAL else 1,
        payload={
            "goal": goal,
            "run_binding": {
                "run_id": run_id,
                "activation_id": active.activation_id,
                "goal_revision": 2,
            },
        }
        if owner == GOAL
        else {"phase": "active", "plan_path": "plan.md", "revision": 8},
    )
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            steps=(
                SessionCommitStep(
                    harness=HarnessState(
                        controls=SessionControlState(activations=[active]),
                        states=(record,),
                    )
                ),
            ),
        )
    )
    version = store.load_harness(session.session_id)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="boundary")
    store.append_message(message)
    return version, message


def usage_entry(legacy, *, tokens=5, run_id="old-run"):
    _, session, usage, _ = legacy
    entry = ProviderUsageEntry(
        entry_id=run_id,
        provider_id="test",
        model_id="model",
        session_id=session.session_id,
        run_id=run_id,
        run_kind="agent",
        purpose="agent",
        request_id=run_id,
        input_tokens=tokens,
    )
    usage.ledger.record(entry)
    return entry


def migrate(legacy):
    store, session, usage, controllers = legacy
    return migrate_builtin_harness_state(
        store,
        session.session_id,
        controllers=controllers,
        usage_service=usage,
    )


def test_migration_remaps_all_reachable_versions_and_preserves_usage_and_immutable_sources(legacy):
    store, session, usage, _ = legacy
    old_plan, plan_boundary = publish_legacy(legacy, owner=PLAN)
    old_goal, goal_boundary = publish_legacy(legacy)
    original_usage = usage_entry(legacy)
    todo = SessionExtensionStateRecord(
        session_id=session.session_id,
        owner_id="dev.ouro.toolkit",
        state_schema_version=1,
        payload={"todos": [{"title": "Retain this", "status": "pending"}]},
    )
    store.save_session_extension_state(todo)
    before = store.load_session(session.session_id)
    result = migrate(legacy)
    assert result is not None
    after = store.load_session(session.session_id)
    assert [m.message_id for m in after.messages] == [m.message_id for m in before.messages]
    assert after.session.history_epoch == before.session.history_epoch + 1
    assert store.load_harness(session.session_id, old_plan.version_id) == old_plan
    assert store.load_harness(session.session_id, old_goal.version_id) == old_goal
    assert all(
        m.before_harness_version not in {old_plan.version_id, old_goal.version_id}
        for m in after.messages
    )
    current = store.load_harness(session.session_id)
    goal = SessionGoalState.from_json(current.state.states[0].payload).goal
    assert current.state.states[0].state_schema_version == 4
    assert current.state.states[1] == todo
    assert current.state.controls.activations[0].usage_scope == goal.goal_id
    assert current.state.controls.activations[0].release_id == "new"
    assert current.state.controls.activations[0].data == {}
    entry = usage.ledger.get(original_usage.entry_id)
    assert entry.total_tokens == original_usage.total_tokens
    assert entry.harness_scopes == {GOAL: (goal.goal_id,)}
    assert usage.session_scope_tokens(session.session_id) == {GOAL: {goal.goal_id: 5}}
    assert migrate(legacy) is None
    assert usage.ledger.get(entry.entry_id) == entry
    store.commit_session(
        SessionCommit(before=after.session, before_message_id=goal_boundary.message_id)
    )
    assert (
        store.load_harness(session.session_id).state.states[0].payload
        == current.state.states[0].payload
    )
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            before_message_id=plan_boundary.message_id,
        )
    )
    restored_plan = store.load_harness(session.session_id).state
    assert restored_plan.states[0].state_schema_version == 2
    assert restored_plan.states[0].payload == {"plan_path": "plan.md"}
    assert usage.ledger.get(entry.entry_id) == entry


def test_migration_retry_after_usage_attribution_does_not_rebill(legacy, monkeypatch):
    store, session, usage, _ = legacy
    old, _ = publish_legacy(legacy)
    entry = usage_entry(legacy)
    original = store.migrate_harness

    def fail(request):
        raise OSError("before state publication")

    monkeypatch.setattr(store, "migrate_harness", fail)
    with pytest.raises(OSError, match="before state publication"):
        migrate(legacy)
    assert store.load_harness(session.session_id) == old
    attributed = usage.ledger.get(entry.entry_id)
    assert attributed.revision == 2
    monkeypatch.setattr(store, "migrate_harness", original)
    assert migrate(legacy) is not None
    assert usage.ledger.get(entry.entry_id) == attributed


@pytest.mark.parametrize("problem", ["missing", "mismatch", "ambiguous", "unsettled"])
def test_unprovable_usage_rejects_migration_without_partial_state_or_usage(legacy, problem):
    store, session, usage, _ = legacy
    publish_legacy(legacy, tokens=10 if problem == "mismatch" else 5)
    if problem != "missing":
        usage_entry(legacy)
    if problem == "ambiguous":
        publish_legacy(legacy, created="2026-09-02T00:00:00Z")
    if problem == "unsettled":
        current = store.load_session_extension_states(session.session_id)[0]
        current.payload["run_binding"]["run_id"] = "unsettled-run"
        store.save_session_extension_state(current)
        usage_entry(legacy, run_id="unsettled-run")
    before, entries = store.load_session(session.session_id), usage.ledger.list_entries()
    with pytest.raises(MigrationRequiredError):
        migrate(legacy)
    assert store.load_session(session.session_id) == before
    assert usage.ledger.list_entries() == entries


def test_migration_requires_quiescence_and_does_not_guess_unknown_schema(legacy):
    store, session, _, _ = legacy
    publish_legacy(legacy, owner=PLAN)
    lock = store.acquire_session_lock(session.session_id, "running")
    try:
        with pytest.raises(SessionBusyError):
            migrate(legacy)
    finally:
        lock.release()
    record = store.load_session_extension_states(session.session_id)[0]
    store.save_session_extension_state(record.model_copy(update={"state_schema_version": 99}))
    before = store.load_session(session.session_id)
    with pytest.raises(MigrationRequiredError, match="unsupported builtin state"):
        migrate(legacy)
    assert store.load_session(session.session_id) == before


def test_backend_failure_recovers_one_complete_migration_and_preserves_usage(legacy, monkeypatch):
    store, session, usage, _ = legacy
    old, boundary = publish_legacy(legacy)
    entry = usage_entry(legacy)
    if isinstance(store, LocalSessionStore):
        original = FileSessionCommits.save_session_record

        def fail(directory, record):
            raise OSError("after durable decision")

        monkeypatch.setattr(FileSessionCommits, "save_session_record", staticmethod(fail))
        with pytest.raises(PersistenceError, match="after durable decision"):
            migrate(legacy)
        monkeypatch.setattr(FileSessionCommits, "save_session_record", staticmethod(original))
        # Every canonical read finishes the decided reference rewrite before returning.
        migrated = store.load_session_record(session.session_id)
        assert migrated.harness_version != old.version_id
        assert (
            store.load_message(session.session_id, boundary.message_id).before_harness_version
            == migrated.harness_version
        )
        assert migrate(legacy) is None
    else:
        original = SQLiteSessionCommits.save_version

        def fail(self, version):
            original(self, version)
            raise OSError("inside transaction")

        monkeypatch.setattr(SQLiteSessionCommits, "save_version", fail)
        with pytest.raises(OSError, match="inside transaction"):
            migrate(legacy)
        monkeypatch.setattr(SQLiteSessionCommits, "save_version", original)
        assert store.load_harness(session.session_id) == old
        assert (
            store.load_message(session.session_id, boundary.message_id).before_harness_version
            == old.version_id
        )
        assert migrate(legacy) is not None
    assert store.load_session_record(session.session_id).history_epoch == 1
    assert usage.ledger.get(entry.entry_id).revision == 2
    assert usage.ledger.get(entry.entry_id).total_tokens == entry.total_tokens


def test_legacy_plan_file_import_then_native_conversion_is_repeatable(legacy, tmp_path):
    store, session, _, _ = legacy
    home = tmp_path / "import-home"
    source = home / "plugins/data/dev.ouro.plan/sessions" / f"{session.session_id}.json"
    source.parent.mkdir(parents=True)
    raw = {"phase": "inactive", "plan_path": "keep-file.md", "revision": 4}
    source.write_text(json.dumps({"schema_version": 1, "state": raw}), encoding="utf-8")
    lock = store.acquire_session_lock(session.session_id, "running")
    try:
        with pytest.raises(MigrationRequiredError, match="stop Session execution"):
            migrate_builtin_session_state(home, store)
        assert source.is_file()
        assert not store.load_session_extension_states(session.session_id)
    finally:
        lock.release()
    assert migrate_builtin_session_state(home, store).migrated == 1
    imported = store.load_harness(session.session_id)
    assert imported.state.states[0].state_schema_version == 1
    assert imported.state.states[0].payload == raw
    assert not source.exists()
    assert migrate(legacy) is not None
    current = store.load_harness(session.session_id).state.states[0]
    assert current.state_schema_version == 2
    assert current.payload == {"plan_path": "keep-file.md"}
    assert store.load_harness(session.session_id, imported.version_id) == imported
    assert migrate(legacy) is None
