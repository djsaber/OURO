from __future__ import annotations

from unittest.mock import Mock

import pytest

from ouro.application.harness import capture_harness
from ouro.application.runtime import RuntimeService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.billing import UsageFilter
from ouro.core.harness import HarnessState
from ouro.core.provider import ModelMetadata, Usage
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import (
    SessionControlActivation,
    SessionControlClaim,
    SessionControlState,
)
from ouro.core.state import RunRecord, SessionRecord
from ouro.plugins.catalog import ExtensionCatalogBase, build_extension_catalog
from ouro.plugins.harness_calls import PluginStateView
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerOutput,
    PluginSessionControllerRegistry,
)
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry

OWNER = "example.goal"
MODEL = ModelMetadata(model_id="faux", provider_id="faux")


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "sessions")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    result.initialize()
    return result


def goal_controls(scope: str) -> SessionControlState:
    return SessionControlState(
        activations=[
            SessionControlActivation(
                controller_id=f"{OWNER}/session_controllers/mode",
                plugin_id=OWNER,
                controller_local_id="mode",
                plugin_class="user",
                release_id="release",
                generation_id="generation",
                execution_contract_hash="c" * 64,
                usage_scope=scope,
            )
        ]
    )


async def test_native_usage_survives_undo_and_keeps_request_attribution(store, tmp_path):
    ledger = LocalUsageLedger(tmp_path / "home")
    ledger.initialize()
    usage = UsageService(ledger)
    app = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(FauxProvider(), MODEL),
        usage_service=usage,
    )
    controllers = PluginSessionControllerRegistry()
    controllers.replace_owner(
        OWNER,
        (
            PluginSessionController(
                controller_id="mode",
                plugin_id=OWNER,
                description="Attribute native usage",
                claims=(SessionControlClaim(domain="example.usage", access="shared"),),
                generation_id="generation",
                execution_contract_hash="c" * 64,
                activate_handler=lambda call: PluginSessionControllerOutput(),
                deactivate_handler=lambda call: PluginSessionControllerOutput(),
            ),
        ),
    )
    catalog = build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=app.tools,
            observers=app.observers,
            commands=None,
            compaction=None,
            continuations=None,
            tool_policies=None,
            session_controllers=controllers,
        ),
        (),
    )
    # This test owns one fixed catalog; execution still verifies the real controller contract.
    resource = Mock(spec=PluginRuntimeResource)
    resource.snapshot.side_effect = catalog.acquire
    app.bind_extensions(catalog=resource)
    session = SessionRecord(controls=goal_controls("goal-a"))
    store.create_session(session)
    try:
        result = await app.run("work", session_id=session.session_id)
        assert result.run.status == "completed"
        assert usage.session_scope_tokens(session.session_id) == {OWNER: {"goal-a": 5}}
        recorded = ledger.list_entries(UsageFilter()).entries[0]
        original_run = store.load_run(session.session_id, result.run.run_id)
        assert original_run.control_bindings[0].usage_scope == "goal-a"

        snapshot = store.load_session(session.session_id)
        store.commit_session(
            SessionCommit(
                before=snapshot.session, before_message_id=snapshot.messages[0].message_id
            )
        )
        assert store.load_messages_tail(session.session_id) == []
        assert usage.session_scope_tokens(session.session_id) == {OWNER: {"goal-a": 5}}
        assert ledger.list_entries(UsageFilter()).entries == (recorded,)

        # A new goal has a different scope. A late result still belongs to its captured request.
        store.commit_session(
            SessionCommit(
                before=store.load_session_record(session.session_id),
                steps=(SessionCommitStep(harness=HarnessState(controls=goal_controls("goal-b"))),),
            )
        )
        latest = RunRecord(
            session_id=session.session_id,
            history_epoch=store.load_session_record(session.session_id).history_epoch,
        )
        with catalog.acquire() as lease:
            latest.metadata["session_control_snapshot"] = app._session_control_snapshot(
                session.session_id, run_id=latest.run_id, model_round=1, catalog=lease
            )
        store.save_run(latest)
        for _ in range(2):
            app.run_lifecycle.record_usage(
                original_run,
                Usage(input_tokens=6, output_tokens=4),
                model=MODEL,
                request_id="late-a",
                purpose="agent",
            )
        assert usage.session_scope_tokens(session.session_id) == {OWNER: {"goal-a": 15}}
        app.run_lifecycle.record_usage(
            latest,
            Usage(input_tokens=2, output_tokens=1),
            model=MODEL,
            request_id="request-b",
            purpose="agent",
        )
        assert usage.session_scope_tokens(session.session_id) == {
            OWNER: {"goal-a": 15, "goal-b": 3}
        }
        assert len(ledger.list_entries(UsageFilter()).entries) == 3

        reopened = UsageService(LocalUsageLedger(tmp_path / "home"))
        call = capture_harness(store, session.session_id, run=latest, usage_service=reopened)
        view = PluginStateView(call, OWNER, writable=False)
        assert view.usage_tokens() == {"goal-a": 15, "goal-b": 3}
        assert view.run_controls()[0].usage_scope == "goal-b"
        assert view.finish() is None
        assert store.load_session_record(session.session_id).history_epoch == 1
    finally:
        await app.close()
