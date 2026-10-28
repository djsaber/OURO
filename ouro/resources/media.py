"""Explicitly materialize selected immutable media; never revisit original paths."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Iterable

from ouro.core.base import new_id
from ouro.core.content import MediaContent, media_data_uri, media_reference, media_resource_ids
from ouro.core.context_snapshots import ContextSnapshotRecord, RequestManifestRecord
from ouro.core.errors import PersistenceError
from ouro.core.messages import ContentBlock, MessageRecord, ModelMessage
from ouro.core.resources import ResourceSource, SessionResourceReference
from ouro.core.tool_facts import ToolFactConflict, ToolOutcomeRecord

ResourcePublisher = Callable[[SessionResourceReference, bytes], None]


def capture_media(
    block: MediaContent, *, source: ResourceSource, publish: ResourcePublisher
) -> MediaContent:
    if block.resource_id is not None or (decoded := media_data_uri(block.uri)) is None:
        return block
    prefix, content = decoded
    media_type = block.media_type or prefix[5:].partition(";")[0] or "application/octet-stream"
    identity = new_id("res")
    record = SessionResourceReference(
        resource_id=identity,
        filename=identity,
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type=media_type,
        size=len(content),
        source=source,
        internal=True,
    )
    publish(record, content)
    return block.model_copy(
        update={
            "resource_id": record.resource_id,
            "uri_prefix": prefix if prefix != f"data:{block.media_type};base64" else None,
        }
    )


def capture_outcome_media(
    outcome: ToolOutcomeRecord, previous: ToolOutcomeRecord | None, publish: ResourcePublisher
) -> ToolOutcomeRecord:
    value = outcome.value
    if previous is not None:
        if not previous.value.same_observation(value):
            raise ToolFactConflict("a certain Outcome cannot be replaced")
        value = previous.value
    if isinstance(value.content, str):
        return outcome.model_copy(update={"value": value})
    content = [
        media_reference(
            capture_media(
                block, source=ResourceSource(kind="tool", name="tool-media"), publish=publish
            )
        )
        if isinstance(block, MediaContent)
        else block
        for block in value.content
    ]
    return outcome.model_copy(update={"value": value.model_copy(update={"content": content})})


def materialize_outcome_media(
    outcome: ToolOutcomeRecord | None, read: Callable[[str], bytes]
) -> ToolOutcomeRecord | None:
    if outcome is None or isinstance(outcome.value.content, str):
        return outcome
    value = outcome.value.model_copy(
        update={
            "content": [
                materialize_media(block, read) if isinstance(block, MediaContent) else block
                for block in outcome.value.content
            ]
        }
    )
    return outcome.model_copy(update={"value": value})


def validate_media_references(
    messages: Iterable[MessageRecord],
    manifest: RequestManifestRecord | None,
    available: Iterable[str],
    outcomes: Iterable[ToolOutcomeRecord] = (),
) -> None:
    required = set(media_resource_ids(block for message in messages for block in message.content))
    if manifest is not None:
        required.update(manifest.manifest.referenced_resource_ids)
    for outcome in outcomes:
        required.update(media_resource_ids(outcome.value.content_blocks))
    if not required.issubset(available):
        raise PersistenceError("media references a missing Session resource")


def materialize_media(block: MediaContent, read: Callable[[str], bytes]) -> MediaContent:
    if block.resource_id is None or block.uri:
        return block
    content = read(block.resource_id)
    prefix = block.uri_prefix or f"data:{block.media_type};base64"
    return block.model_copy(update={"uri": f"{prefix},{base64.b64encode(content).decode('ascii')}"})


def materialize_message_media[Message: (MessageRecord, ModelMessage)](
    message: Message, read: Callable[[str], bytes]
) -> Message:
    if not any(
        isinstance(block, MediaContent) and block.resource_id is not None and not block.uri
        for block in message.content
    ):
        return message
    content: list[ContentBlock] = [
        materialize_media(block, read) if isinstance(block, MediaContent) else block
        for block in message.content
    ]
    return message.model_copy(update={"content": content})


def materialize_context_media(
    context: ContextSnapshotRecord, read: Callable[[str], bytes]
) -> ContextSnapshotRecord:
    provider = context.provider_context
    return context.model_copy(
        update={
            "provider_context": provider.model_copy(
                update={
                    "messages": [
                        materialize_message_media(message, read) for message in provider.messages
                    ]
                }
            )
        }
    )
