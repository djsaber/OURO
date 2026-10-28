"""Backend-neutral, complete Session transfer records and consistency checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from ouro.core.agents import DelegationRecord
from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import (
    ModelRequestHeader,
    RequestManifestRecord,
    validate_request_snapshot,
)
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.inputs import InputRecord, InputState, validate_input_snapshot
from ouro.core.message_content import materialize_message, validate_tool_message_snapshot
from ouro.core.messages import MessageFact
from ouro.core.resources import SessionResourceReference
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallState, validate_call_states
from ouro.core.tool_facts import ToolCallFacts
from ouro.resources.media import validate_media_references
from ouro.sessions.base import SessionRepository, SessionSnapshot
from ouro.sessions.layout import DATA_LAYOUT_VERSION


class SessionArchiveSnapshot(OuroModel):
    """The complete transfer representation stores message facts, never read projections."""

    session: SessionRecord
    messages: list[MessageFact] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    compactions: list[CompactionRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    extension_states: list[SessionExtensionStateRecord] = Field(default_factory=list)
    harness_versions: list[HarnessVersion] = Field(default_factory=list)
    inputs: list[InputRecord] = Field(default_factory=list)
    input_states: list[InputState] = Field(default_factory=list)
    tool_facts: ToolCallFacts = Field(default_factory=ToolCallFacts)
    call_states: tuple[ToolCallState, ...] = ()
    request_headers: list[ModelRequestHeader] = Field(default_factory=list)
    latest_manifest: RequestManifestRecord | None = None

    @classmethod
    def from_snapshot(cls, snapshot: SessionSnapshot) -> SessionArchiveSnapshot:
        return cls(
            **snapshot.model_dump(exclude={"messages"}),
            messages=[message.as_fact() for message in snapshot.messages],
        )

    def materialize(self) -> SessionSnapshot:
        inputs = {record.item.queue_id: record for record in self.inputs}
        calls = {call.identity: call for call in self.tool_facts.calls}
        outcomes = {(item.identity, item.attempt): item for item in self.tool_facts.outcomes}
        return SessionSnapshot(
            **self.model_dump(exclude={"messages"}),
            messages=[
                materialize_message(
                    fact,
                    inputs.get(fact.input_id) if fact.input_id else None,
                    calls=[
                        calls[identity]
                        for identity in fact.referenced_call_ids
                        if identity in calls
                    ],
                    outcome=outcomes.get(
                        (fact.outcome_ref.identity, fact.outcome_ref.outcome_attempt)
                    )
                    if fact.outcome_ref is not None and fact.outcome_ref.outcome_attempt is not None
                    else None,
                )
                for fact in self.messages
            ],
        )


class PortableSession(OuroModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: SessionArchiveSnapshot
    resources: list[SessionResourceReference] = Field(default_factory=list)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: SessionSnapshot,
        *,
        resources: list[SessionResourceReference] | None = None,
    ) -> PortableSession:
        return cls(
            snapshot=SessionArchiveSnapshot.from_snapshot(snapshot),
            resources=resources or [],
        )


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
            validate_input_snapshot(
                identity,
                session.pending_input_ids,
                tuple(snapshot.inputs),
                tuple(snapshot.input_states),
                messages=snapshot.messages,
            )
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
            snapshot.tool_facts.validate_snapshot(
                identity, run_epochs={run.run_id: run.history_epoch for run in snapshot.runs}
            )
            validate_call_states(snapshot.call_states, snapshot.tool_facts, session_id=identity)
            validate_tool_message_snapshot(snapshot.messages, snapshot.tool_facts)
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
            validate_request_snapshot(identity, snapshot.request_headers, snapshot.latest_manifest)
            validate_media_references(
                snapshot.messages,
                snapshot.latest_manifest,
                (record.resource_id for record in item.resources),
                snapshot.tool_facts.outcomes,
            )
            if snapshot.latest_manifest is not None:
                body = snapshot.latest_manifest
                referenced_messages = set(body.manifest.referenced_message_ids)
                if not referenced_messages.issubset(
                    message.message_id for message in snapshot.messages
                ):
                    raise ValueError("request references a missing message")
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
        snapshot = item.snapshot.materialize()
        target.create_session_snapshot(
            snapshot.session,
            snapshot.messages,
            resource_references=item.resources,
            harness_versions=snapshot.harness_versions,
            compactions=snapshot.compactions,
            inputs=tuple(snapshot.inputs),
            input_states=tuple(snapshot.input_states),
            tool_facts=snapshot.tool_facts,
            call_states=snapshot.call_states,
            request_headers=tuple(snapshot.request_headers),
            latest_manifest=snapshot.latest_manifest,
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
        actual = target.load_session(snapshot.session.session_id)
        if SessionArchiveSnapshot.from_snapshot(actual) != item.snapshot:
            raise PersistenceError("Session transfer failed snapshot verification")
    for delegation in bundle.delegations:
        target.create_delegation(delegation)
