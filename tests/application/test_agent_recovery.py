from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agent_recovery import AgentRecoveryService
from ouro.application.agents import AgentService
from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    AgentWaitRequest,
    DelegationRecord,
    ToolSelection,
)
from ouro.core.errors import PersistenceError
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


class UnusedProvider:
    provider_id = "unused"

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
            message="ok",
        )


@pytest.fixture(params=("files", "sqlite"))
def recovery_store(request: pytest.FixtureRequest, tmp_path) -> Iterator[SessionStore]:
    store: SessionStore
    if request.param == "files":
        store = LocalSessionStore(tmp_path / "files")
    else:
        store = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    store.initialize()
    yield store


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="general",
        name="General",
        description="General subagent.",
        tool_selection=ToolSelection(mode="selected", names=()),
    )


def _relation(
    root: SessionRecord,
    *,
    delegation_id: str = "dlg_recovery",
) -> DelegationRecord:
    definition = _definition()
    return DelegationRecord(
        delegation_id=delegation_id,
        idempotency_key=f"host/{delegation_id}",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        parent_run_id="run_parent",
        agent_id=definition.agent_id,
        task_description="Recover child state.",
        prompt_digest="sha256:recovery",
        definition=definition,
    )


def _create_root(store: SessionStore) -> SessionRecord:
    root = SessionRecord(session_id="ses_root")
    store.create_session(root)
    store.save_run(
        RunRecord(
            run_id="run_parent",
            session_id=root.session_id,
            status="completed",
            agent_catalog=AgentCatalogSnapshot(
                entries=(AgentCatalogEntry(definition=_definition()),)
            ),
        )
    )
    return root


def _child(root: SessionRecord, relation: DelegationRecord) -> SessionRecord:
    return SessionRecord(
        session_id="ses_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id=relation.delegation_id,
    )


def _run(
    relation: DelegationRecord,
    child: SessionRecord,
    *,
    status: str = "queued",
) -> RunRecord:
    return RunRecord(
        run_id="run_child",
        session_id=child.session_id,
        status=status,
        parent_run_id=relation.parent_run_id,
        initiator_id=f"ouro.subagents/{relation.delegation_id}",
        metadata={
            "delegation_id": relation.delegation_id,
            "root_session_id": relation.root_session_id,
            "agent_id": relation.agent_id,
            "agent_definition_contract_hash": relation.definition.contract_hash,
        },
    )


def _persist_child_created(
    store: SessionStore,
    root: SessionRecord,
    relation: DelegationRecord,
) -> tuple[DelegationRecord, SessionRecord]:
    child = _child(root, relation)
    store.create_session(child)
    saved = store.save_delegation(
        relation.model_copy(
            update={
                "child_session_id": child.session_id,
                "spawn_state": "child_created",
            }
        ),
        expected_revision=relation.revision,
    )
    return saved, child


def test_relation_without_child_fails_once_and_keeps_stable_diagnostic(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    relation = recovery_store.create_delegation(_relation(root))
    recovery = AgentRecoveryService(recovery_store)

    first = recovery.reconcile()
    failed = recovery_store.load_delegation(root.session_id, relation.delegation_id)
    second = recovery.reconcile()
    unchanged = recovery_store.load_delegation(root.session_id, relation.delegation_id)

    assert [(item.code, item.resource_id) for item in first] == [
        ("delegation_child_missing", relation.delegation_id)
    ]
    assert [(item.code, item.resource_id) for item in second] == [
        ("delegation_child_missing", relation.delegation_id)
    ]
    assert failed.spawn_state == "failed"
    assert failed.error == "delegation_child_missing"
    assert unchanged.revision == failed.revision


def test_parent_catalog_contract_mismatch_fails_before_child_repair(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    changed_definition = _definition().model_copy(
        update={
            "description": "A different frozen contract.",
            "contract_hash": "",
        }
    )
    relation = _relation(root).model_copy(
        update={
            "definition": AgentDefinition.model_validate(changed_definition.model_dump()),
        }
    )
    recovery_store.create_delegation(relation)

    diagnostics = AgentRecoveryService(recovery_store).reconcile()

    assert [item.code for item in diagnostics] == ["agent_catalog_contract_unavailable"]


def test_recovery_commits_only_uniquely_attributed_child_run(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    relation = recovery_store.create_delegation(_relation(root))
    relation, child = _persist_child_created(recovery_store, root, relation)
    run = _run(relation, child)
    recovery_store.save_run(run)

    assert AgentRecoveryService(recovery_store).reconcile() == ()

    recovered = recovery_store.load_delegation(root.session_id, relation.delegation_id)
    assert recovered.spawn_state == "submitted"
    assert recovered.current_child_run_id == run.run_id


def test_missing_child_run_fails_closed_without_deleting_history(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    relation = recovery_store.create_delegation(_relation(root))
    relation, child = _persist_child_created(recovery_store, root, relation)

    diagnostics = AgentRecoveryService(recovery_store).reconcile()

    failed = recovery_store.load_delegation(root.session_id, relation.delegation_id)
    assert [item.code for item in diagnostics] == ["delegation_run_missing"]
    assert failed.spawn_state == "failed"
    assert recovery_store.load_session_record(child.session_id) == child


def test_orphan_child_and_backlink_mismatch_are_retained_for_inspection(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    relation = recovery_store.create_delegation(_relation(root))
    mismatched = SessionRecord(
        session_id="ses_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_other",
    )
    recovery_store.create_session(mismatched)
    relation = recovery_store.save_delegation(
        relation.model_copy(
            update={
                "child_session_id": mismatched.session_id,
                "spawn_state": "child_created",
            }
        ),
        expected_revision=relation.revision,
    )

    diagnostics = AgentRecoveryService(recovery_store).reconcile()

    assert {item.code for item in diagnostics} == {"child_delegation_mismatch"}
    assert recovery_store.load_session_record(mismatched.session_id) == mismatched
    failed = recovery_store.load_delegation(root.session_id, relation.delegation_id)
    assert failed.spawn_state == "failed"
    assert failed.child_session_id == mismatched.session_id


def test_child_policy_mismatch_is_distinct_from_identity_corruption(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    relation = recovery_store.create_delegation(_relation(root))
    child = _child(root, relation).model_copy(update={"workspace": "D:/other"})
    recovery_store.create_session(child)
    recovery_store.save_delegation(
        relation.model_copy(
            update={
                "child_session_id": child.session_id,
                "spawn_state": "child_created",
            }
        ),
        expected_revision=relation.revision,
    )

    diagnostics = AgentRecoveryService(recovery_store).reconcile()

    assert [item.code for item in diagnostics] == ["child_policy_invalid"]


def test_child_run_contract_mismatch_fails_closed(
    recovery_store: SessionStore,
) -> None:
    root = _create_root(recovery_store)
    relation = recovery_store.create_delegation(_relation(root))
    relation, child = _persist_child_created(recovery_store, root, relation)
    run = _run(relation, child).model_copy(
        update={
            "metadata": {
                **_run(relation, child).metadata,
                "agent_definition_contract_hash": "sha256:mismatch",
            }
        }
    )
    recovery_store.save_run(run)
    recovery_store.save_delegation(
        relation.model_copy(
            update={
                "current_child_run_id": run.run_id,
                "spawn_state": "submitted",
            }
        ),
        expected_revision=relation.revision,
    )

    diagnostics = AgentRecoveryService(recovery_store).reconcile()

    assert [item.code for item in diagnostics] == ["child_contract_unavailable"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("completed", "running"))
async def test_agent_target_reconcile_rebuilds_terminal_result_without_replaying_child(
    tmp_path,
    status: str,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            UnusedProvider(),
            ModelMetadata(model_id="unused", provider_id="unused"),
        ),
        tools=tools,
    )
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    supervisor = RuntimeSupervisor(runtime)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    root = _create_root(store)
    relation = store.create_delegation(_relation(root))
    relation, child = _persist_child_created(store, root, relation)
    run = _run(relation, child, status=status)
    store.save_run(run)
    relation = store.save_delegation(
        relation.model_copy(
            update={
                "current_child_run_id": run.run_id,
                "spawn_state": "submitted",
            }
        ),
        expected_revision=relation.revision,
    )

    diagnostics = await runtime.reconcile_session(root.session_id)

    recovered_run = store.load_run(child.session_id, run.run_id)
    recovered = store.load_delegation(root.session_id, relation.delegation_id)
    assert recovered.result is not None
    assert recovered.result.terminal_status == (
        "recoverable" if status == "running" else "completed"
    )
    assert "result_delivery_incomplete" in {item.code for item in diagnostics}
    if status == "running":
        assert recovered_run.status == "recoverable"
        assert "child_run_recovered" in {item.code for item in diagnostics}
    second_revision = recovered.revision
    second = await service.reconcile()
    assert "result_delivery_incomplete" not in {item.code for item in second}
    assert (
        store.load_delegation(root.session_id, relation.delegation_id).revision == second_revision
    )
    await service.close()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_local_application_reports_agent_recovery_when_root_is_opened(tmp_path) -> None:
    application = LocalApplication.build(tmp_path, session_backend="files")
    root = _create_root(application.runtime.store)
    relation = application.runtime.store.create_delegation(_relation(root))

    await application.start()

    assert application.startup_report.diagnostics == ()
    diagnostics = await application.runtime.reconcile_session(root.session_id)
    assert "delegation_child_missing" in {
        item.code for item in diagnostics
    }
    assert (
        application.runtime.store.load_delegation(
            root.session_id,
            relation.delegation_id,
        ).spawn_state
        == "failed"
    )
    await application.close()


@pytest.mark.asyncio
async def test_incompatible_delegation_is_isolated_until_root_is_opened(tmp_path) -> None:
    application = LocalApplication.build(tmp_path, session_backend="files")
    root = _create_root(application.runtime.store)
    relation = application.runtime.store.create_delegation(_relation(root))
    payload = relation.model_dump(mode="json")
    payload["execution_mode"] = "background"
    relation_path = (
        application.runtime.store.sessions_dir
        / root.session_id
        / "delegations"
        / f"{relation.delegation_id}.json"
    )
    relation_path.write_text(json.dumps(payload), encoding="utf-8")

    await application.start()

    assert application.startup_report.diagnostics == ()
    diagnostics = await application.runtime.reconcile_session(root.session_id)
    assert "session_runtime_incompatible" in {
        item.code for item in diagnostics
    }
    result = await application.runtime.run("new independent Session")
    assert result.run.status == "completed"
    assert result.session.session_id != root.session_id
    with pytest.raises(PersistenceError, match="incompatible persisted data"):
        await application.runtime.run("must remain isolated", session_id=root.session_id)
    await application.close()


@pytest.mark.asyncio
async def test_corrupt_child_session_blocks_only_the_child(tmp_path) -> None:
    application = LocalApplication.build(tmp_path, session_backend="files")
    store = application.runtime.store
    assert isinstance(store, LocalSessionStore)
    root = _create_root(store)
    relation = store.create_delegation(_relation(root))
    relation, child = _persist_child_created(store, root, relation)
    child_run = _run(relation, child, status="completed")
    store.save_run(child_run)
    store.save_delegation(
        relation.model_copy(
            update={
                "current_child_run_id": child_run.run_id,
                "spawn_state": "submitted",
            }
        ),
        expected_revision=relation.revision,
    )
    (store.sessions_dir / child.session_id / "session.json").write_text(
        "{not-json",
        encoding="utf-8",
    )

    async with application:
        root_result = await application.runtime.run(
            "continue the valid root Session",
            session_id=root.session_id,
        )
        with pytest.raises(PersistenceError, match="could not decode session record"):
            await application.runtime.run(
                "must remain isolated",
                session_id=child.session_id,
            )

    assert root_result.run.status == "completed"


@pytest.mark.asyncio
async def test_target_reconcile_reports_parent_wait_referencing_missing_delegation(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            UnusedProvider(),
            ModelMetadata(model_id="unused", provider_id="unused"),
        ),
        tools=ToolRegistry(),
    )
    definitions = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    supervisor = RuntimeSupervisor(runtime)
    service = AgentService(
        store=store,
        definitions=definitions,
        scheduler=supervisor,
        events=runtime.event_recorder,
    )
    runtime.bind_extensions(agent_definitions=definitions, agent_controls=service)
    wait = AgentWaitRequest(delegation_id="dlg_missing")
    batch = ToolCallBatchRecord(
        turn_id="turn_parent",
        assistant_message_id="msg_parent",
        phase="awaiting_child",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(
                    id="call_spawn",
                    name="SpawnAgent",
                    arguments={
                        "agent_id": "general",
                        "task": "Inspect the repository.",
                        "description": "Inspect repository",
                        "execution_mode": "sync",
                    },
                ),
                status="waiting_child",
                agent_wait=wait,
            )
        ],
    )
    run = RunRecord(
        run_id="run_parent",
        session_id="ses_root",
        status="waiting_child",
        phase="subagent_join",
        active_tool_batch=batch,
    )
    root = SessionRecord(
        session_id=run.session_id,
        status="running",
        active_run_id=run.run_id,
        last_run_id=run.run_id,
    )
    store.create_session(root)
    store.save_run(run)

    diagnostics = await runtime.reconcile_session(root.session_id)

    mismatch = next(item for item in diagnostics if item.code == "parent_agent_wait_mismatch")
    assert mismatch.session_id == root.session_id
    assert mismatch.run_id == run.run_id
    assert mismatch.resource_id == wait.wait_id
    assert store.load_run(root.session_id, run.run_id).status == "waiting_child"
    await service.close()
    await supervisor.shutdown()
