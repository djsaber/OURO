import json

import pytest

from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError
from ouro.core.input_content import InputFileReference
from ouro.core.inputs import InputRecord, QueuedInput
from ouro.core.message_content import decode_message_content, materialize_message
from ouro.core.messages import (
    InputMessageReference,
    MessageFact,
    SkillActivationContent,
    TextContent,
)


def test_message_storage_encoding_omits_defaults_without_changing_payload_values():
    fact = MessageFact(
        session_id="ses_target",
        role="user",
        input_ref=InputMessageReference(input_id="input_one"),
        metadata={"null": None, "empty": [], "false": False, "zero": 0},
    )
    encoded = fact.storage_json()
    payload = json.loads(encoded)
    assert MessageFact.model_validate_json(encoded) == fact
    assert payload["schema_version"] == fact.schema_version
    assert payload["created_at"] and payload["message_id"] == fact.message_id
    assert payload["metadata"] == fact.metadata
    assert not {"tool_calls", "content", "input_attachments", "run_id"} & payload.keys()


@pytest.mark.parametrize("duplicate", ["body", "attachments"])
def test_persisted_input_reference_cannot_embed_owned_units(duplicate):
    values = {"session_id": "ses_target", "role": "user", "input_ref": {"input_id": "input_one"}}
    if duplicate == "body":
        values["content"] = [TextContent(text="copied body")]
    else:
        values["input_attachments"] = (
            InputFileReference(path="C:/workspace/one.txt", name="one.txt"),
        )
    with pytest.raises(ValueError, match="reference|copied"):
        MessageFact.model_validate(values)
    values["schema_version"] = MessageFact.model_fields["schema_version"].default
    if duplicate == "body":
        values["content"] = [block.model_dump() for block in values["content"]]
    else:
        values["input_attachments"] = [item.model_dump() for item in values["input_attachments"]]
    with pytest.raises(PersistenceError, match="reference|copied"):
        decode_message_content(json.dumps(values), label="message")


@pytest.mark.parametrize("record_kind", ["missing", "foreign_session", "foreign_input"])
def test_selected_message_cannot_resolve_a_foreign_input(record_kind):
    fact = MessageFact(
        session_id="ses_target", role="user", input_ref=InputMessageReference(input_id="input_one")
    )
    record = (
        None
        if record_kind == "missing"
        else InputRecord(
            session_id="ses_other" if record_kind == "foreign_session" else "ses_target",
            item=QueuedInput(
                queue_id="input_other" if record_kind == "foreign_input" else "input_one",
                history_epoch=0,
                prompt="fixed body",
                queued_at=utc_now(),
                source="user",
                mode="follow_up",
                role="user",
                scaffold=False,
                semantic_tag=None,
            ),
        )
    )
    with pytest.raises(PersistenceError, match="missing or foreign Input"):
        materialize_message(fact, record)


def test_materialized_message_does_not_mutate_its_input_or_message_fact():
    fact = MessageFact(
        session_id="ses_target", role="user", input_ref=InputMessageReference(input_id="input_one")
    )
    record = InputRecord(
        session_id=fact.session_id,
        item=QueuedInput(
            queue_id=fact.input_id,
            history_epoch=0,
            prompt="fixed body",
            queued_at=utc_now(),
            source="user",
            mode="follow_up",
            role="user",
            scaffold=False,
            semantic_tag=None,
            attachments=(InputFileReference(path="C:/workspace/one.txt", name="one.txt"),),
            skill_activations=(
                SkillActivationContent(
                    name="review",
                    source="workspace",
                    location="C:/workspace/SKILL.md",
                    base_directory="C:/workspace",
                    instructions="Fixed instruction.",
                    content_digest=f"sha256:{'a' * 64}",
                ),
            ),
        ),
    )
    original = record.model_dump_json()
    message = materialize_message(fact, record)
    message.skill_activations[0].instructions = "local projection"
    message.input_attachments[0].name = "local label"
    message.content.append(TextContent(text="local text"))
    assert record.model_dump_json() == original
    assert fact.content == [] and fact.input_attachments == ()
    restored = materialize_message(fact, record)
    assert restored.text_content == "fixed body"
    assert restored.skill_activations[0].instructions == "Fixed instruction."
    assert restored.input_attachments[0].name == "one.txt"


def test_input_body_materialization_checks_message_owned_attachment_offsets():
    fact = MessageFact(
        session_id="ses_target",
        role="user",
        input_ref=InputMessageReference(input_id="input_one", attachments_from_input=False),
        input_attachments=(
            InputFileReference(path="C:/workspace/one.txt", name="one.txt", text_offset=100),
        ),
    )
    record = InputRecord(
        session_id=fact.session_id,
        item=QueuedInput(
            queue_id="input_one",
            history_epoch=0,
            prompt="short",
            queued_at=utc_now(),
            source="user",
            mode="follow_up",
            role="user",
            scaffold=False,
            semantic_tag=None,
        ),
    )
    with pytest.raises(ValueError, match="attachment positions"):
        materialize_message(fact, record)
