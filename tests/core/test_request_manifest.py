from __future__ import annotations

import pytest

from ouro.core.context import ContextBuilder
from ouro.core.errors import PersistenceError
from ouro.core.messages import ImageContent, MessageRecord, ProviderContinuationArtifact
from ouro.core.request_manifest import RequestContentReference, RequestMessageReference
from ouro.core.tools import ToolCallRequest


def test_prepared_manifest_keeps_final_values_and_references_unchanged_content():
    user = MessageRecord.text(session_id="session", role="user", text="original input")
    user.content.append(ImageContent(uri="data:image/png;base64,Ynl0ZXM="))
    assistant = MessageRecord(
        session_id="session",
        role="assistant",
        tool_calls=[ToolCallRequest(id="call", name="Read", arguments={"path": "original"})],
        provider_continuation=ProviderContinuationArtifact(
            provider_id="test", protocol="test", model_id="test", blocks=({"opaque": "original"},)
        ),
    )
    tagged = MessageRecord.text(
        session_id="session",
        role="system",
        text="value </runtime-facts>",
        semantic_tag="runtime-facts",
    )
    sources = [user, assistant, tagged]
    view = ContextBuilder().build(messages=sources, source_messages=sources, tools=[])
    manifest = view.manifest
    assert all(isinstance(block, RequestContentReference) for block in manifest.messages[0].content)
    assert isinstance(manifest.messages[1].tool_calls, RequestMessageReference)
    assert isinstance(manifest.messages[1].provider_continuation, RequestMessageReference)
    assert "original input" not in manifest.model_dump_json()
    assert "original" not in manifest.model_dump_json()
    assert "&lt;/runtime-facts&gt;" in manifest.model_dump_json()
    assert (
        manifest.materialize("session", {item.message_id: item for item in sources})
        == view.provider_context.messages
    )


def test_projected_literal_does_not_reference_the_original_value():
    source = MessageRecord.text(session_id="session", role="user", text="original")
    replacement = source.model_copy(
        update={
            "content": source.text(role="user", session_id="session", text="replacement").content
        }
    )
    view = ContextBuilder().build(messages=[replacement], source_messages=[source], tools=[])
    assert view.manifest.referenced_message_ids == ()
    assert view.manifest.materialize("session", {}) == view.provider_context.messages


def test_manifest_rejects_missing_block_or_foreign_source():
    source = MessageRecord.text(session_id="session", role="user", text="original")
    manifest = (
        ContextBuilder().build(messages=[source], source_messages=[source], tools=[]).manifest
    )
    for invalid in (
        {},
        {source.message_id: source.model_copy(update={"content": []})},
        {source.message_id: source.model_copy(update={"session_id": "foreign"})},
    ):
        with pytest.raises(PersistenceError):
            manifest.materialize("session", invalid)
