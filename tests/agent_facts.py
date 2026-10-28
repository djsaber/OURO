"""Native frozen Delegation fixtures for isolated child Runtime tests."""

from ouro.core.agents import (
    AgentDefinition,
    DelegatedConstraints,
    DelegationRecord,
    EffectiveToolRef,
)
from ouro.core.base import new_id
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import SessionStore


def seed_delegation(
    store: SessionStore,
    child: SessionRecord,
    *,
    effective_tools: tuple[EffectiveToolRef, ...] = (),
    budget: RunBudgetPolicy | None = None,
) -> DelegationRecord:
    root = SessionRecord()
    store.create_session(root)
    parent = RunRecord(session_id=root.session_id, status="completed")
    store.save_run(parent)
    definition = AgentDefinition(
        agent_id="reader",
        name="Reader",
        description="Read within frozen authority.",
        system_prompt="Frozen child instructions.",
    )
    relation = DelegationRecord(
        idempotency_key=new_id("request"),
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        parent_run_id=parent.run_id,
        child_session_id=child.session_id,
        agent_id=definition.agent_id,
        definition=definition,
        effective_tools=effective_tools,
        constraints=DelegatedConstraints(workspace=child.workspace),
        approval_mode=child.approval_mode,
        interaction_mode=child.interaction_mode,
        budget=budget or RunBudgetPolicy(),
        task_description="Inspect the workspace.",
        prompt_digest="sha256:test-task",
        spawn_state="child_created",
    )
    store.create_delegation(relation)
    store.save_session(
        child.model_copy(
            update={
                "session_kind": "subagent",
                "root_session_id": root.session_id,
                "parent_session_id": root.session_id,
                "delegation_id": relation.delegation_id,
            }
        )
    )
    return relation
