from __future__ import annotations

import pytest
from pydantic import ValidationError

from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    AgentSpawnPolicy,
    AgentWaitRequest,
    AgentWaitResolution,
    DelegationRecord,
    EffectiveToolRef,
    ToolSelection,
)
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunRecord, SessionRecord, session_status_for_run


def _definition(agent_id: str = "general") -> AgentDefinition:
    return AgentDefinition(
        agent_id=agent_id,
        name="General",
        description="General-purpose subagent.",
        system_prompt="Work independently and report a concise result.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
    )


def test_agent_definition_contract_is_stable_and_content_addressed() -> None:
    first = _definition()
    second = AgentDefinition.model_validate(first.model_dump())

    assert first.contract_hash == second.contract_hash
    with pytest.raises(ValidationError, match="contract hash"):
        AgentDefinition.model_validate(
            {**first.model_dump(), "description": "changed", "contract_hash": first.contract_hash}
        )
    assert "source" not in AgentDefinition.model_fields
    assert "editable" not in AgentDefinition.model_fields
    assert "context_policy" not in AgentDefinition.model_fields


def test_agent_catalog_snapshot_freezes_definition_and_tool_contract() -> None:
    definition = _definition()
    tool = EffectiveToolRef(
        public_name="Read",
        canonical_tool_id="builtin/Read",
        execution_contract_hash="sha256:read",
    )
    snapshot = AgentCatalogSnapshot(
        revision=3,
        entries=(AgentCatalogEntry(definition=definition, effective_tools=(tool,)),),
    )

    assert snapshot.resolve("general").definition == definition
    assert snapshot.resolve("general").effective_tools == (tool,)
    assert snapshot.contract_hash
    with pytest.raises(KeyError, match="unknown or unavailable"):
        snapshot.resolve("missing")


def test_agent_catalog_rejects_entries_that_exceed_frozen_spawn_policy() -> None:
    definition = _definition()
    write_tool = EffectiveToolRef(
        public_name="Write",
        canonical_tool_id="builtin/Write",
        execution_contract_hash="sha256:write",
        effect="state_changing",
    )

    with pytest.raises(ValidationError, match="agent_id spawn policy"):
        AgentCatalogSnapshot(
            entries=(AgentCatalogEntry(definition=definition),),
            spawn_policy=AgentSpawnPolicy(allowed_agent_id_patterns=("reader-*",)),
        )
    with pytest.raises(ValidationError, match="tool spawn policy"):
        AgentCatalogSnapshot(
            entries=(AgentCatalogEntry(definition=definition, effective_tools=(write_tool,)),),
            spawn_policy=AgentSpawnPolicy(allow_write=False),
        )


def test_subagent_session_requires_explicit_delegation_identity() -> None:
    with pytest.raises(ValidationError, match="subagent session"):
        SessionRecord(session_kind="subagent")

    child = SessionRecord(
        session_kind="subagent",
        root_session_id="ses_root",
        parent_session_id="ses_parent",
        delegation_id="dlg_child",
    )
    assert child.root_session_id == "ses_root"
    assert child.parent_session_id == "ses_parent"

    with pytest.raises(ValidationError, match="primary session"):
        SessionRecord(delegation_id="dlg_invalid")


def test_waiting_child_is_an_active_run_and_projects_session_running() -> None:
    run = RunRecord(session_id="ses_parent", status="waiting_child", phase="subagent_join")

    assert run.status == "waiting_child"
    assert session_status_for_run(run.status) == "running"


def test_delegation_spawn_phases_validate_child_identity() -> None:
    definition = _definition()
    requested = DelegationRecord(
        delegation_id="dlg_request",
        idempotency_key="host/request-1",
        root_session_id="ses_root",
        parent_session_id="ses_root",
        parent_run_id="run_parent",
        agent_id=definition.agent_id,
        task_description="Inspect the runtime.",
        prompt_digest="sha256:prompt",
        definition=definition,
    )
    assert requested.child_session_id is None
    assert "context_message_ids" not in DelegationRecord.model_fields
    assert "context_compaction_id" not in DelegationRecord.model_fields

    with pytest.raises(ValidationError, match="child identity"):
        requested.model_copy(update={"spawn_state": "child_created"}).__class__.model_validate(
            requested.model_copy(update={"spawn_state": "child_created"}).model_dump()
        )


def test_agent_wait_request_targets_one_delegation() -> None:
    wait = AgentWaitRequest(delegation_id="dlg_one")

    assert wait.delegation_id == "dlg_one"
    with pytest.raises(ValidationError, match="requested delegation"):
        AgentWaitRequest(
            delegation_id="dlg_one",
            state="resolved",
            resolution=AgentWaitResolution(
                resolved_delegation_id="dlg_other",
                reason="condition_met",
            ),
        )


def test_delegated_budget_intersection_cannot_expand_parent_limit() -> None:
    requested = RunBudgetPolicy(max_model_rounds=8, max_output_tokens=500)
    parent_remaining = RunBudgetPolicy(max_model_rounds=3, max_input_tokens=1_000)

    effective = requested.constrained_by(parent_remaining)

    assert effective == RunBudgetPolicy(
        max_model_rounds=3,
        max_input_tokens=1_000,
        max_output_tokens=500,
    )
