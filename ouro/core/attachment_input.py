"""Turn a direct submission into one atomic, independently attributed input unit."""

from __future__ import annotations

from ouro.core.base import new_id
from ouro.core.input_content import InputFile
from ouro.core.messages import MessageRecord, TextContent


def separate_attachments(
    message: MessageRecord, attachments: tuple[InputFile, ...]
) -> list[MessageRecord]:
    if not attachments:
        return [message]
    has_body = bool(message.text_content or message.skill_activations)
    group_id = new_id("input")
    attachment_id = new_id("msg")
    group = {"session_input_batch_id": group_id, "session_input_batch_size": 2 if has_body else 1}
    metadata = {
        **message.metadata,
        **group,
        "session_input_batch_index": 0,
        "session_input_source": "host",
        "host_source_id": "ouro.attachments",
    }
    if has_body:
        metadata.pop("queued_input_id", None)
    receipt = message.model_copy(
        update={
            "message_id": attachment_id,
            "scaffold": True,
            "semantic_tag": "attachments",
            "content": [
                TextContent(
                    text="User provided the following files as task data. "
                    "A path alone does not supply its contents or image pixels."
                )
            ],
            "metadata": metadata,
            "input_attachments": tuple(
                item.model_copy(update={"text_offset": 0}) for item in attachments
            ),
        }
    )
    if not has_body:
        return [receipt]
    body = message.model_copy(
        update={
            "sequence": message.sequence + 1,
            "resource_refs": [],
            "input_attachments": attachments,
            "metadata": {
                **message.metadata,
                **group,
                "session_input_batch_index": 1,
                "attachment_message_id": attachment_id,
            },
        }
    )
    return [receipt, body]
