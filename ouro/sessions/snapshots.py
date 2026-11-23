"""Prepare the canonical Session facts shared by the local storage backends."""

from __future__ import annotations

from ouro.core.compaction import CompactionRecord, validate_compaction_snapshot
from ouro.core.context_selection import validate_media_selection
from ouro.core.context_snapshots import (
    ModelRequestHeader,
    RequestManifestRecord,
    validate_request_snapshot,
)
from ouro.core.errors import PersistenceError
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.inputs import InputRecord, InputState, validate_input_snapshot
from ouro.core.message_content import validate_tool_message_snapshot
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.state import SessionRecord
from ouro.core.tool_batches import ToolCallState, validate_call_states
from ouro.core.tool_facts import ToolCallFacts
from ouro.resources.media import validate_media_references


def prepare_session_snapshot(
    session: SessionRecord,
    messages: list[MessageRecord],
    *,
    resource_references: list[SessionResourceReference] | None,
    harness_versions: list[HarnessVersion] | None,
    compactions: list[CompactionRecord],
    inputs: tuple[InputRecord, ...],
    input_states: tuple[InputState, ...] | None,
    tool_facts: ToolCallFacts | None,
    call_states: tuple[ToolCallState, ...],
    request_headers: tuple[ModelRequestHeader, ...],
    latest_manifest: RequestManifestRecord | None,
) -> tuple[SessionRecord, list[MessageRecord], list[HarnessVersion], tuple[InputState, ...]]:
    messages = [
        item if item.sequence == index else item.model_copy(update={"sequence": index})
        for index, item in enumerate(messages, start=1)
    ]
    by_id = {message.message_id: message for message in messages}
    if len(by_id) != len(messages):
        raise PersistenceError("Session snapshot contains duplicate message identities")
    facts = tool_facts if tool_facts is not None else ToolCallFacts()
    validate_request_snapshot(session.session_id, request_headers, latest_manifest)
    validate_media_references(
        messages,
        latest_manifest,
        (record.resource_id for record in resource_references or []),
        facts.outcomes,
    )
    validate_compaction_snapshot(session, by_id, compactions)
    if (boundary := session.context_selection.media_elided_through) is not None:
        validate_media_selection(
            session.context_selection,
            session.session_id,
            by_id.get(boundary.message_id),
        )
    states = validate_input_snapshot(
        session.session_id, session.pending_input_ids, inputs, input_states, messages=messages
    )
    if tool_facts is not None:
        tool_facts.validate_snapshot(session.session_id)
    validate_call_states(call_states, facts, session_id=session.session_id)
    validate_tool_message_snapshot(messages, facts)
    versions = (
        harness_versions
        if harness_versions is not None
        else [
            HarnessVersion(
                session_id=session.session_id,
                version_id=session.harness_version,
                state=HarnessState(controls=session.controls),
            )
        ]
    )
    checkpoints = {version.version_id: version for version in versions}
    if session.harness_version not in checkpoints or any(
        message.before_harness_version is not None
        and message.before_harness_version not in checkpoints
        for message in messages
    ):
        raise PersistenceError("Session snapshot has a missing Harness checkpoint")
    for version in versions:
        if version.session_id != session.session_id:
            raise PersistenceError("Harness checkpoint belongs to another Session")
    for message in messages:
        if message.session_id != session.session_id:
            raise PersistenceError(
                f"message {message.message_id} does not belong to {session.session_id}"
            )
    head = session.model_copy(
        update={
            "message_count": len(messages),
            "controls": checkpoints[session.harness_version].state.controls,
            "run_count": 0,
            "resource_count": 0,
            "last_message_id": messages[-1].message_id if messages else None,
        }
    )
    return head, messages, versions, states
