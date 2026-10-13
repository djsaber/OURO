from __future__ import annotations

import base64

import pytest

from ouro.application.skill_activations import SkillInvocationRequest
from ouro.core import input_content
from ouro.core.errors import SessionBranchError
from ouro.core.messages import ImageContent
from tests.application.test_skill_activations import CapturingProvider
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("source_kind", ("upload", "reference"))
@pytest.mark.parametrize("characters", (3000, 6000))
async def test_utf16_text_exceeding_utf8_budget_keeps_head_and_tail(
    tmp_path, source_kind, characters
):
    app = application_at(tmp_path / "home")
    session = app.runtime.create_session(title="preview", workspace=tmp_path)
    path = tmp_path / "unicode.txt"
    path.write_bytes(("开头\n" + "汉" * characters + "\n结尾").encode("utf-16"))
    if source_kind == "upload":
        record = app.runtime.resources.import_resource(session.session_id, path).resource
        item = input_content.InputAttachment(name=path.name, resource_id=record.resource_id)
    else:
        item = input_content.InputFileReference(name=path.name, path=str(path))
    await app.runtime.run("Explain", session_id=session.session_id, attachments=(item,))
    receipt = app.runtime.store.load_runtime_session(session.session_id).messages[0]
    assert "head/tail preview" in receipt.text_content
    assert "开头" in receipt.text_content and "结尾" in receipt.text_content
    assert "omitted bytes" in receipt.text_content
    assert "�" not in receipt.text_content
    assert len(receipt.text_content.encode("utf-8")) < 9000
    await app.close()


async def test_same_named_uploads_have_unambiguous_model_file_labels(tmp_path):
    app = application_at(tmp_path / "home")
    session = app.runtime.create_session(title="names")
    path = tmp_path / "notes.txt"
    items = []
    for value in ("first contents", "second contents"):
        path.write_text(value, encoding="utf-8")
        record = app.runtime.resources.import_resource(session.session_id, path).resource
        items.append(input_content.InputAttachment(name=path.name, resource_id=record.resource_id))
    await app.runtime.run("Compare", session_id=session.session_id, attachments=tuple(items))
    receipt = app.runtime.store.load_runtime_session(session.session_id).messages[0]
    headers = [block.text for block in receipt.content if getattr(block, "type", None) == "text"]
    for filename, contents in (
        ("notes.txt", "first contents"),
        ("notes (2).txt", "second contents"),
    ):
        assert any(
            f'Session file: "{filename}"' in header and contents in header for header in headers
        )
        projected = app.runtime.sessions.context(session.session_id).provider_context.messages[0]
        assert f'"{filename}": ' in projected.text_content
    for item in items:
        path = app.runtime.resources.resource_path(session.session_id, item.resource_id)
        assert path.as_posix() in receipt.text_content
    assert projected.text_content.count("Session attachment read paths:") == 1
    await app.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_paired_attachment_scaffold_is_not_a_separate_undo_input(tmp_path, backend):
    app = application_at(tmp_path / "home", backend=backend)
    path = tmp_path / "notes.txt"
    path.write_text("data", encoding="utf-8")
    item = input_content.InputFileReference(name=path.name, path=str(path))
    result = await app.runtime.run("Keep this question", attachments=(item,))
    before = app.runtime.store.load_runtime_session(result.session.session_id)
    for invalid_fork_target in (
        None,
        before.messages[0].message_id,
        before.messages[-1].message_id,
    ):
        with pytest.raises(SessionBranchError, match="user message is not a fork point"):
            app.runtime.sessions.branch(
                result.session.session_id, invalid_fork_target, before_input=True
            )
    assert len(app.runtime.store.list_sessions()) == 1
    with pytest.raises(ValueError, match="directly submitted user input"):
        await app.runtime.undo(result.session.session_id, message_id=before.messages[0].message_id)
    assert (
        app.runtime.store.load_runtime_session(result.session.session_id).messages
        == before.messages
    )
    restored = await app.runtime.undo(result.session.session_id)
    assert restored.input.text_content == "Keep this question" and restored.attachments == (item,)
    await app.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_uploads_are_session_owned_named_files_and_keep_distinct_names(tmp_path, backend):
    app = application_at(tmp_path / "home", backend=backend)
    first = app.runtime.create_session(title="first")
    second = app.runtime.create_session(title="second")
    source = tmp_path / "notes.txt"
    source.write_text("same bytes", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_bytes(source.read_bytes())
    resources = app.runtime.resources
    one = resources.import_resource(first.session_id, source).resource
    two = resources.import_resource(first.session_id, other).resource
    three = resources.import_resource(second.session_id, source).resource
    assert one.source.name == "notes.txt" and two.source.name == "other.txt"
    paths = [
        resources.resource_path(session_id, record.resource_id)
        for session_id, record in (
            (first.session_id, one),
            (first.session_id, two),
            (second.session_id, three),
        )
    ]
    assert [path.name for path in paths] == ["notes.txt", "other.txt", "notes.txt"]
    assert all(path.is_relative_to(tmp_path / "home" / "sessions") for path in paths)
    assert paths[0].parent != paths[2].parent
    source.unlink()
    assert all(path.read_bytes() == b"same bytes" for path in paths)
    assert not (tmp_path / "home" / "resources").exists()
    await app.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_reference_uses_original_path_and_freezes_observed_text(tmp_path, backend):
    app = application_at(tmp_path / "home", backend=backend)
    source = tmp_path / "SECURITY.md"
    source.write_text("original security policy", encoding="utf-8")
    item = input_content.InputFileReference(name=source.name, path=str(source))
    result = await app.runtime.run(
        "What is this file?", attachments=(item,), workspace=str(tmp_path)
    )
    session_id = result.session.session_id
    context = app.runtime.sessions.context(session_id)
    text = context.provider_context.messages[0].text_content
    assert "original security policy" in text and source.as_posix() in text
    receipt = app.runtime.store.load_runtime_session(session_id).messages[0]
    assert "original security policy" in receipt.text_content
    assert "file_observations" not in receipt.model_dump()
    assert "res_" not in text
    assert not app.runtime.resources.list_resources(session_id).resources
    source.write_text("changed policy", encoding="utf-8")
    await app.runtime.run("Explain further", session_id=session_id)
    latest = app.runtime.sessions.context(session_id)
    assert "original security policy" in latest.provider_context.messages[0].text_content
    assert app.runtime.store.load_runtime_session(session_id).messages[1].input_attachments == (
        item,
    )
    names = set(app.runtime.tools.names())
    assert not {"ResourceRead", "ResourceMaterialize"} & names
    await app.close()


async def test_file_only_reference_is_visible_to_prompt_policies_before_persistence(tmp_path):
    from ouro.core.errors import PromptRejectedError
    from ouro.core.prompt_policies import PromptPolicyDecision, PromptPolicyRegistration

    app = application_at(tmp_path / "home")
    path = tmp_path / "SECURITY.md"
    path.write_text("policy", encoding="utf-8")
    item = input_content.InputFileReference(name=path.name, path=str(path))
    calls = []

    def deny(call):
        calls.append(call)
        return PromptPolicyDecision(action="deny", reason="Test rejection")

    app.runtime.prompt_policies.register(
        PromptPolicyRegistration(
            plugin_id="test.files",
            policy_id="files",
            handler=deny,
        )
    )
    with pytest.raises(PromptRejectedError):
        await app.runtime.run("", attachments=(item,), workspace=str(tmp_path))
    assert calls[0].prompt == ""
    assert calls[0].attachments[0].kind == "reference"
    assert calls[0].attachments[0].path == str(path)
    assert calls[0].attachments[0].name == path.name
    assert app.runtime.store.list_sessions() == []
    await app.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_reference_image_snapshot_survives_source_removal(tmp_path, backend):
    app = application_at(tmp_path / "home", backend=backend)
    app.runtime.adapter.model.supports_image_input = True
    image = tmp_path / "picture.png"
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    image.write_bytes(raw)
    item = input_content.InputFileReference(name=image.name, path=str(image))
    result = await app.runtime.run("Describe", attachments=(item,), workspace=str(tmp_path))
    saved = app.runtime.sessions.context(result.session.session_id)
    blocks = saved.provider_context.messages[0].content
    image_blocks = [block for block in blocks if isinstance(block, ImageContent)]
    assert image_blocks
    receipt = app.runtime.store.load_runtime_session(result.session.session_id).messages[0]
    persisted = [block for block in receipt.content if isinstance(block, ImageContent)]
    assert persisted == image_blocks
    assert base64.b64decode(persisted[0].uri.partition(",")[2]) == raw
    image.unlink()
    assert app.runtime.sessions.context(result.session.session_id) == saved
    await app.runtime.run("Explain further", session_id=result.session.session_id)
    latest = app.runtime.sessions.context(result.session.session_id)
    assert [
        block
        for block in latest.provider_context.messages[0].content
        if isinstance(block, ImageContent)
    ] == persisted
    await app.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_branch_owns_its_upload_after_source_is_trashed(tmp_path, backend):
    app = application_at(tmp_path / "home", backend=backend)
    source = tmp_path / "SECURITY.md"
    source.write_bytes(b"private session copy")
    session = app.runtime.create_session(title="source", workspace=tmp_path)
    resources = app.runtime.resources
    record = resources.import_resource(session.session_id, source).resource
    item = input_content.InputAttachment(name=source.name, resource_id=record.resource_id)
    await app.runtime.run("Explain", session_id=session.session_id, attachments=(item,))
    message = app.runtime.store.load_runtime_session(session.session_id).messages[0]
    branch = app.runtime.sessions.branch(session.session_id, message.message_id).session
    branch_path = resources.resource_path(branch.session_id, record.resource_id)
    source_path = resources.resource_path(session.session_id, record.resource_id)
    assert branch_path != source_path
    copied = app.runtime.store.load_runtime_session(branch.session_id).messages[0]
    assert branch_path.as_posix() in copied.text_content
    assert source_path.as_posix() not in copied.text_content
    assert source_path.as_posix() in message.text_content
    app.runtime.sessions.trash(session.session_id)
    source.unlink()
    await app.runtime.run("Continue", session_id=branch.session_id)
    assert branch_path.read_bytes() == b"private session copy"
    assert (
        branch_path.as_posix()
        in app.runtime.sessions.context(branch.session_id).provider_context.messages[0].text_content
    )
    assert source_path.as_posix() not in (
        app.runtime.sessions.context(branch.session_id).provider_context.messages[0].text_content
    )
    await app.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("fork_point", (0, 1))
async def test_branch_preserves_observed_files_and_skill_instructions(
    tmp_path, backend, fork_point
):
    skill = tmp_path / "skills" / "reviewer" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: reviewer\ndescription: Review.\n---\nFrozen review instructions.\n",
        encoding="utf-8",
    )
    provider = CapturingProvider()
    app = application_at(tmp_path, provider, backend=backend)
    runtime = app.runtime
    session = runtime.create_session(title="mixed history", workspace=tmp_path)
    source = tmp_path / "upload.txt"
    source.write_text("Frozen upload.", encoding="utf-8")
    reference = tmp_path / "reference.txt"
    reference.write_text("Frozen reference.", encoding="utf-8")
    resource = runtime.resources.import_resource(session.session_id, source).resource
    await runtime.run(
        "Review",
        session_id=session.session_id,
        attachments=(
            input_content.InputAttachment(name=source.name, resource_id=resource.resource_id),
            input_content.InputFileReference(name=reference.name, path=str(reference)),
        ),
        skill_invocation=SkillInvocationRequest(name="reviewer", arguments="Review"),
    )
    saved_context = runtime.sessions.context(session.session_id)
    before = runtime.store.load_runtime_session(session.session_id).messages
    branch = runtime.sessions.branch(session.session_id, before[fork_point].message_id).session
    copied = runtime.store.load_runtime_session(branch.session_id).messages
    assert len(copied) == 2
    assert copied[1].skill_activations == before[1].skill_activations
    source_path = runtime.resources.resource_path(session.session_id, resource.resource_id)
    branch_path = runtime.resources.resource_path(branch.session_id, resource.resource_id)
    assert branch_path.as_posix() in copied[0].text_content
    assert source_path.as_posix() not in copied[0].text_content
    assert reference.as_posix() in copied[0].text_content
    assert runtime.sessions.context(session.session_id) == saved_context
    runtime.sessions.trash(session.session_id)
    for path in (source, reference, skill):
        path.unlink()
    result = await runtime.run("Continue reviewing", session_id=branch.session_id)
    assert result.run.status == "completed"
    sent = "\n".join(message.text_content for message in provider.calls[-1])
    for contents in ("Frozen upload.", "Frozen reference.", "Frozen review instructions."):
        assert sent.count(contents) == 1
    assert branch_path.as_posix() in sent and source_path.as_posix() not in sent
    assert sent.count("Session attachment read paths:") == 1
    await app.close()


async def test_workspace_change_rejects_bound_reference_before_creating_session(tmp_path):
    from ouro.core.errors import ResourceAttachmentError

    app = application_at(tmp_path / "home")
    source = tmp_path / "SECURITY.md"
    source.write_text("policy", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    item = input_content.InputFileReference(
        name=source.name, path=str(source), workspace=str(tmp_path)
    )
    with pytest.raises(ResourceAttachmentError, match="different workspace"):
        await app.runtime.run("Explain", attachments=(item,), workspace=str(other))
    assert app.runtime.store.list_sessions() == []
    await app.close()


async def test_unavailable_reference_only_is_valid_without_inventing_user_text(tmp_path):
    app = application_at(tmp_path / "home")
    item = input_content.InputFileReference(name="missing.md", path=str(tmp_path / "missing.md"))
    result = await app.runtime.run("", attachments=(item,), workspace=str(tmp_path))
    messages = app.runtime.store.load_runtime_session(result.session.session_id).messages
    assert messages[0].scaffold and messages[0].input_attachments == (item,)
    projection = app.runtime.sessions.context(result.session.session_id).provider_context
    assert "File unavailable" in projection.messages[0].text_content
    assert len([message for message in messages if message.role == "user"]) == 1
    assert not app.runtime.resources.list_resources(result.session.session_id).resources
    await app.close()
