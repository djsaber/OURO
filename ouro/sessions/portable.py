"""Backend-neutral, complete Session transfer records and consistency checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from ouro.core.agents import DelegationRecord
from ouro.core.base import OuroModel
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError
from ouro.core.resources import SessionResourceReference
from ouro.core.state import ACTIVE_RUN_STATUSES
from ouro.sessions.base import SessionRepository, SessionSnapshot
from ouro.sessions.layout import DATA_LAYOUT_VERSION


class PortableSession(OuroModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: SessionSnapshot
    resources: list[SessionResourceReference] = Field(default_factory=list)
    latest_context: ContextSnapshotRecord | None = None


class SessionBundle(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    layout_version: int = DATA_LAYOUT_VERSION
    sessions: list[PortableSession]
    delegations: list[DelegationRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        if self.layout_version != DATA_LAYOUT_VERSION:
            raise ValueError("unsupported Session data layout")
        records = {item.snapshot.session.session_id: item for item in self.sessions}
        if len(records) != len(self.sessions):
            raise ValueError("duplicate Session identity")
        for identity, item in records.items():
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity) is None:
                raise ValueError("invalid Session identity")
            snapshot = item.snapshot
            session = snapshot.session
            if session.active_run_id is not None or session.status not in {"idle", "failed"}:
                raise ValueError("only inactive Sessions can be transferred")
            for collection, attribute in (
                (snapshot.messages, "message_id"),
                (snapshot.runs, "run_id"),
                (snapshot.events, "event_id"),
                (snapshot.compactions, "compaction_id"),
                (snapshot.harness_versions, "version_id"),
            ):
                ids = [getattr(record, attribute) for record in collection]
                if len(ids) != len(set(ids)) or any(
                    record.session_id != identity for record in collection
                ):
                    raise ValueError("duplicate or foreign Session record")
            run_ids = {run.run_id for run in snapshot.runs}
            if any(run.status in ACTIVE_RUN_STATUSES for run in snapshot.runs):
                raise ValueError("Session has unfinished Runs")
            if session.last_run_id is not None and session.last_run_id not in run_ids:
                raise ValueError("Session references a missing Run")
            if any(
                message.run_id is not None and message.run_id not in run_ids
                for message in snapshot.messages
            ):
                raise ValueError("message references a missing Run")
            if session.agent_root_session_id not in records:
                raise ValueError("Session bundle is missing its agent root")
            if records[session.agent_root_session_id].snapshot.session.session_kind != "primary":
                raise ValueError("Session agent root must be a primary Session")
            if any(
                event.run_id is not None and event.run_id not in run_ids
                for event in snapshot.events
            ):
                raise ValueError("event references a missing Run")
            if item.latest_context is not None and item.latest_context.session_id != identity:
                raise ValueError("context belongs to a different Session")
            if session.message_count != len(snapshot.messages) or session.last_message_id != (
                snapshot.messages[-1].message_id if snapshot.messages else None
            ):
                raise ValueError("Session message projection does not match history")
        identities: set[str] = set()
        children: dict[str, DelegationRecord] = {}
        for delegation in self.delegations:
            if delegation.delegation_id in identities:
                raise ValueError("duplicate Delegation identity")
            identities.add(delegation.delegation_id)
            if any(
                identity not in records
                for identity in (
                    delegation.root_session_id,
                    delegation.parent_session_id,
                )
            ):
                raise ValueError("Delegation references a missing Session")
            root = records[delegation.root_session_id].snapshot.session
            parent = records[delegation.parent_session_id].snapshot
            if (
                root.session_kind != "primary"
                or parent.session.agent_root_session_id != root.session_id
            ):
                raise ValueError("Delegation belongs to a different Session tree")
            if delegation.parent_run_id not in {run.run_id for run in parent.runs}:
                raise ValueError("Delegation references a missing parent Run")
            if delegation.child_session_id is not None:
                if delegation.child_session_id not in records:
                    # Failed dispatch may retain a reserved identity that never
                    # became a child Session. This is an audit fact, not data loss.
                    if (
                        delegation.spawn_state not in {"requested", "failed"}
                        or delegation.current_child_run_id is not None
                        or delegation.result is not None
                    ):
                        raise ValueError("Delegation references a missing child Session")
                    continue
                if delegation.child_session_id in children:
                    raise ValueError("child Session has multiple Delegations")
                child = records[delegation.child_session_id].snapshot
                if child.session.session_kind != "subagent" or (
                    delegation.current_child_run_id is not None
                    and delegation.current_child_run_id not in {run.run_id for run in child.runs}
                ):
                    raise ValueError("Delegation references an invalid child Session or Run")
                children[delegation.child_session_id] = delegation
        for identity, item in records.items():
            session = item.snapshot.session
            if session.session_kind == "subagent":
                relation = children.get(identity)
                if relation is None or (
                    relation.delegation_id != session.delegation_id
                    or relation.parent_session_id != session.parent_session_id
                    or relation.root_session_id != session.agent_root_session_id
                ):
                    raise ValueError("child Session has no matching Delegation")
        return self


def populate_sessions(
    target: SessionRepository,
    bundle: SessionBundle,
    *,
    resource_contents: Mapping[str, Mapping[str, bytes]] | None = None,
) -> None:
    """Write into a private staging store or a backend transaction, then verify."""
    for item in bundle.sessions:
        snapshot = item.snapshot
        target.create_session_snapshot(
            snapshot.session,
            snapshot.messages,
            resource_references=item.resources,
            harness_versions=snapshot.harness_versions,
            commits=snapshot.commits,
            compactions=snapshot.compactions,
            resource_contents=(
                (record, resource_contents[snapshot.session.session_id][record.filename])
                for record in item.resources
            )
            if resource_contents is not None
            else (),
        )
        for run in snapshot.runs:
            target.save_run(run)
        target.append_events(snapshot.events)
        if item.latest_context is not None:
            target.save_latest_context(item.latest_context)
        actual = target.load_session(snapshot.session.session_id)
        if actual != snapshot:
            raise PersistenceError("Session transfer failed snapshot verification")
    for delegation in bundle.delegations:
        target.create_delegation(delegation)
