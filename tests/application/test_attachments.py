from __future__ import annotations

import asyncio
import base64

import pytest

from ouro.core.compaction import conversation_turns
from ouro.core.errors import ResourceNotFoundError
from ouro.core.input_content import InputAttachment
from ouro.core.messages import ImageContent, MessageRecord
from ouro.core.session_commits import SessionCommitConflict
from tests.application.test_queued_input_modes import TurnGateProvider
from tests.tui.test_history_commands import application_at

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_input_is_atomic_and_snapshot_keeps_its_projection(tmp_path, backend):
    app = application_at(tmp_path, backend=backend)
    runtime = app.runtime
    session = runtime.create_session(title="attachments")
    path = tmp_path / "notes.txt"
    path.write_text("source data", encoding="utf-8")
    resource = runtime.resources.import_resource(session.session_id, path).resource
    item = InputAttachment(resource_id=resource.resource_id, name="notes.txt", text_offset=4)
    result = await runtime.run("read this", session_id=session.session_id, attachments=(item,))
    assert result.run.status == "completed"
    messages = runtime.store.load_runtime_session(session.session_id).messages
    receipt, user = messages[:2]
    assert receipt.scaffold and receipt.semantic_tag == "attachments"
    assert receipt.resource_refs == [resource.resource_id]
    assert user.text_content == "read this" and not user.resource_refs
    assert user.input_attachments == (item,)
    assert len(conversation_turns(messages)) == 1
    branch = runtime.sessions.branch(session.session_id, receipt.message_id)
    branch_messages = runtime.store.load_runtime_session(branch.session.session_id).messages
    assert len(branch_messages) == 2
    assert branch_messages[1].metadata["attachment_message_id"] == branch_messages[0].message_id
    branch_undo = await runtime.undo(branch.session.session_id)
    assert branch_undo.attachments == (item,)
    assert runtime.store.load_runtime_session(branch.session.session_id).messages == []
    saved = runtime.sessions.context(session.session_id)
    assert "source data" in saved.provider_context.messages[0].text_content
    runtime.resources._max_model_text_bytes = 1
    assert runtime.sessions.context(session.session_id) == saved
    undone = await runtime.undo(session.session_id)
    assert undone.attachments == (item,)
    assert undone.resource_refs == (resource.resource_id,)
    assert runtime.store.load_runtime_session(session.session_id).messages == []
    await app.close()


@pytest.mark.parametrize("mode", ["follow_up", "steer"])
async def test_active_run_can_import_and_consume_one_attachment_unit(tmp_path, mode):
    provider = TurnGateProvider()
    app = application_at(tmp_path, provider)
    runtime = app.runtime
    session = runtime.create_session(title="active")
    task = asyncio.create_task(runtime.run("initial", session_id=session.session_id))
    await asyncio.wait_for(provider.first_started.wait(), 5)
    try:
        path = tmp_path / "queued.txt"
        path.write_text("queued material", encoding="utf-8")
        ref = runtime.resources.import_resource(session.session_id, path).resource.resource_id
        item = InputAttachment(resource_id=ref, name=path.name, text_offset=4)
        accepted = []
        await runtime.run(
            "next",
            session_id=session.session_id,
            queue_mode=mode,
            attachments=(item,),
            on_input_accepted=lambda: accepted.append(True),
        )
        queued = runtime.pending_user_inputs(session.session_id)
        assert len(queued) == 1 and queued[0].attachments == (item,)
        assert accepted == [True]
    finally:
        provider.release_first.set()
    await asyncio.wait_for(task, 10)
    messages = runtime.store.load_runtime_session(session.session_id).messages
    index = next(
        index for index, message in enumerate(messages) if message.semantic_tag == "attachments"
    )
    assert messages[index + 1].text_content == "next"
    assert messages[index + 1].input_attachments == (item,)
    assert messages[index].sequence + 1 == messages[index + 1].sequence
    assert not runtime.pending_user_inputs(session.session_id)
    await app.close()


async def test_attachment_only_image_and_binary_downgrade_and_snapshot(tmp_path):
    app = application_at(tmp_path)
    runtime = app.runtime
    session = runtime.create_session(title="images")
    path = tmp_path / "image.png"
    path.write_bytes(PNG)
    resource = runtime.resources.import_resource(session.session_id, path).resource
    item = InputAttachment(resource_id=resource.resource_id, name=path.name)
    result = await runtime.run("", session_id=session.session_id, attachments=(item,))
    assert result.run.status == "completed"
    saved = runtime.sessions.context(session.session_id)
    assert "model in use did not support it" in saved.provider_context.messages[0].text_content
    runtime.adapter.model.supports_image_input = True
    result = await runtime.run("look now", session_id=session.session_id, attachments=(item,))
    assert result.run.status == "completed"
    snapshot = runtime.store.load_latest_context(session.session_id)
    assert "data:image" in snapshot.model_dump_json()
    restored = runtime.sessions.context(session.session_id)
    images = [
        block
        for message in restored.provider_context.messages
        for block in message.content
        if isinstance(block, ImageContent)
    ]
    assert len(images) == 1
    assert base64.b64decode(images[0].uri.split(",", 1)[1]) == PNG
    runtime.adapter.model.supports_image_input = False
    assert runtime.sessions.context(session.session_id) == restored
    binary = tmp_path / "opaque.bin"
    binary.write_bytes(b"\x00\x01\xffopaque")
    ref = runtime.resources.import_resource(session.session_id, binary).resource.resource_id
    result = await runtime.run("inspect", session_id=session.session_id, resource_refs=(ref,))
    assert result.run.status == "completed"
    assert (
        "Opaque binary"
        in runtime.sessions.context(session.session_id)
        .provider_context.messages[-1 - 1]
        .text_content
    )
    await app.close()


async def test_long_unicode_preview_named_path_and_scoped_export(tmp_path):
    app = application_at(tmp_path)
    runtime = app.runtime
    session = runtime.create_session(title="long text")
    path = tmp_path / "无后缀"
    text = "首行\n" + "中间内容🙂\n" * 10000 + "尾行\n"
    path.write_text(text, encoding="utf-8")
    ref = runtime.resources.import_resource(session.session_id, path).resource.resource_id
    message = MessageRecord.text(session_id=session.session_id, role="user", text="")
    message.resource_refs = [ref]
    preview = runtime.resources.project_model_messages([message], input_modalities=frozenset())[
        0
    ].text_content
    assert "head/tail preview" in preview and "omitted bytes" in preview
    assert "首行" in preview and "尾行" in preview and len(preview) < len(text)
    saved_path = runtime.resources.resource_path(session.session_id, ref)
    assert saved_path.read_bytes() == path.read_bytes()
    other = runtime.create_session(title="other")
    with pytest.raises(ResourceNotFoundError):
        runtime.resources.resource_path(other.session_id, ref)
    exported = runtime.resources.export_resource(session.session_id, ref, tmp_path / "copy.txt")
    assert exported.destination.read_bytes() == path.read_bytes()
    with pytest.raises(FileExistsError):
        runtime.resources.export_resource(session.session_id, ref, exported.destination)
    with pytest.raises(SessionCommitConflict):
        runtime.resources.import_resource(session.session_id, path, history_epoch=99)
    await app.close()


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
async def test_preview_preserves_supported_unicode_encodings(tmp_path, encoding):
    app = application_at(tmp_path)
    runtime = app.runtime
    session = runtime.create_session(title="encoded text")
    path = tmp_path / "encoded.txt"
    original = "首🙂\ufeff尾\n" * 12
    path.write_bytes(original.encode(encoding))
    ref = runtime.resources.import_resource(session.session_id, path).resource.resource_id
    message = MessageRecord.text(session_id=session.session_id, role="user", text="")
    message.resource_refs = [ref]
    projected = runtime.resources.project_model_messages([message], input_modalities=frozenset())[0]
    assert original in projected.text_content
    await app.close()


async def test_shared_attachment_budget_prioritizes_latest_material_and_retains_references(
    tmp_path,
):
    app = application_at(tmp_path)
    runtime = app.runtime
    session = runtime.create_session(title="budget")
    messages = []
    for index in range(3):
        path = tmp_path / f"{index}.txt"
        path.write_bytes(str(index).encode() * 3000)
        ref = runtime.resources.import_resource(session.session_id, path).resource.resource_id
        message = MessageRecord.text(session_id=session.session_id, role="user", text="")
        message.resource_refs = [ref]
        messages.append(message)
    projected = runtime.resources.project_model_messages(
        messages, input_modalities=frozenset(), input_budget_tokens=16000
    )
    assert "full text" in projected[-1].text_content
    assert "2" * 3000 in projected[-1].text_content
    for original, earlier in zip(messages[:-1], projected[:-1], strict=True):
        assert earlier.resource_refs == original.resource_refs
        assert "too much text to include at once" in earlier.text_content
        assert "full text" not in earlier.text_content
    assert not any(message.text_content for message in messages)
    await app.close()


async def test_durable_queue_acceptance_is_reported_before_notification_failure(
    tmp_path, monkeypatch
):
    provider = TurnGateProvider()
    app = application_at(tmp_path, provider)
    runtime = app.runtime
    session = runtime.create_session(title="queue receipt")
    running = asyncio.create_task(runtime.run("initial", session_id=session.session_id))
    await asyncio.wait_for(provider.first_started.wait(), 5)
    accepted = []
    emit = runtime.event_recorder.emit

    async def failed_notification(*args, **kwargs):
        if kwargs.get("event_type") == "follow_up_input_queued":
            raise ValueError("notification failed")
        return await emit(*args, **kwargs)

    monkeypatch.setattr(runtime.event_recorder, "emit", failed_notification)
    try:
        with pytest.raises(ValueError, match="notification failed"):
            await runtime.run(
                "queued",
                session_id=session.session_id,
                on_input_accepted=lambda: accepted.append(True),
            )
        assert len(runtime.pending_user_inputs(session.session_id)) == 1
        assert accepted == [True]
    finally:
        provider.release_first.set()
        await asyncio.wait_for(running, 10)
        await app.close()
