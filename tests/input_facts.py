"""Synthetic accepted inputs used by persistence and query integration fixtures."""

from ouro.application.inputs.queue import consumption_changes
from ouro.core.inputs import InputRecord, InputState, QueuedInput
from ouro.core.session_commits import SessionCommit, SessionCommitStep


def input_records(session, messages):
    bodies = {}
    for message in messages:
        if message.input_id is not None:
            bodies.setdefault(message.input_id, message)
    return tuple(
        InputRecord(
            session_id=session.session_id,
            item=QueuedInput(
                queue_id=identity,
                history_epoch=session.history_epoch,
                prompt=message.text_content,
                skill_activations=message.skill_activations,
                attachments=message.input_attachments,
                resource_refs=tuple(message.resource_refs),
                queued_at=message.created_at,
                source="user",
                mode="follow_up",
                role=message.role,
                scaffold=message.scaffold,
                semantic_tag=message.semantic_tag,
            ),
        )
        for identity, message in bodies.items()
    )


def input_snapshot(session, messages):
    records = input_records(session, messages)
    states = tuple(
        InputState.pending(record).model_copy(
            update={
                "status": "consumed",
                "message_ids": tuple(
                    message.message_id
                    for message in messages
                    if message.input_id == record.item.queue_id
                ),
            }
        )
        for record in records
    )
    return {"inputs": records, "input_states": states}


def message_request(session, messages):
    records = input_records(session, messages)
    return SessionCommit(
        before=session,
        inputs=records,
        steps=tuple(SessionCommitStep(message=message) for message in messages),
        input_changes=consumption_changes(
            tuple(record.item.queue_id for record in records), messages
        ),
    )


def append_input_messages(store, messages):
    session = store.load_session_record(messages[0].session_id)
    result = store.commit_session(message_request(session, messages))
    for original, fact in zip(messages, result.messages, strict=True):
        original.sequence = fact.sequence
        original.before_harness_version = fact.before_harness_version
    return result
