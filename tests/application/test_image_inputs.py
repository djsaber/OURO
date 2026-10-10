import base64

import pytest

from ouro.core.content import ImageContent
from ouro.core.input_content import InputAttachment, InputFileReference
from tests.application.test_attachments import PNG
from tests.application.test_tool_result_content import RecordingProvider
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
@pytest.mark.parametrize("source_kind", ["upload", "reference"])
async def test_image_input_keeps_observed_bytes_across_capability_changes(
    tmp_path, monkeypatch, backend, protocol, source_kind
):
    provider = RecordingProvider(protocol, request_tools=False)
    app = application_at(tmp_path / "home", provider, backend=backend)
    runtime = app.runtime
    session = runtime.create_session(title="images", workspace=tmp_path)
    path = tmp_path / "picture.png"
    path.write_bytes(PNG)
    if source_kind == "upload":
        resource = runtime.resources.import_resource(session.session_id, path).resource
        item = InputAttachment(resource_id=resource.resource_id, name=path.name)
        path.unlink()
    else:
        item = InputFileReference(name=path.name, path=str(path))
    encoded = base64.b64encode(PNG).decode("ascii")
    try:
        runtime.adapter.model.supports_image_input = True
        first = await runtime.run(
            "Describe this", session_id=session.session_id, attachments=(item,)
        )
        assert first.run.status == "completed"
        assert encoded in str(provider.requests[-1])
        observed = runtime.store.load_runtime_session(session.session_id).messages[0]
        assert any(isinstance(part, ImageContent) for part in observed.content)
        first_snapshot = runtime.sessions.context(session.session_id)
        if source_kind == "reference":
            path.unlink()

        def fail_read(*args, **kwargs):
            pytest.fail("observed attachments must not be read again")

        monkeypatch.setattr(runtime.resources._store, "read_ranges", fail_read)
        runtime.adapter.model.supports_image_input = False
        assert runtime.sessions.context(session.session_id) == first_snapshot
        second = await runtime.run("Continue", session_id=session.session_id)
        assert second.run.status == "completed", second.run.error
        wire = str(provider.requests[-1])
        assert encoded not in wire
        assert wire.count("This model cannot view images.") == 1
        assert "Trying again or changing its size will not help." in wire
        second_snapshot = runtime.sessions.context(session.session_id)
        assert not any(
            isinstance(part, ImageContent)
            for message in second_snapshot.provider_context.messages
            for part in message.content
        )

        runtime.adapter.model.supports_image_input = True
        assert runtime.sessions.context(session.session_id) == second_snapshot
        third = await runtime.run("Look again", session_id=session.session_id)
        assert third.run.status == "completed"
        assert encoded in str(provider.requests[-1])
        assert runtime.store.load_runtime_session(session.session_id).messages[0] == observed
    finally:
        await app.close()


@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
@pytest.mark.parametrize("source_kind", ["upload", "reference"])
async def test_unsupported_image_records_observed_capability_without_mislabeling_a_later_model(
    tmp_path, protocol, source_kind
):
    provider = RecordingProvider(protocol, request_tools=False)
    app = application_at(tmp_path / "home", provider)
    runtime = app.runtime
    session = runtime.create_session(title="images", workspace=tmp_path)
    path = tmp_path / "picture.png"
    path.write_bytes(PNG)
    if source_kind == "upload":
        resource = runtime.resources.import_resource(session.session_id, path).resource
        item = InputAttachment(resource_id=resource.resource_id, name=path.name)
        path.unlink()
    else:
        item = InputFileReference(name=path.name, path=str(path))
    encoded = base64.b64encode(PNG).decode("ascii")
    try:
        first = await runtime.run("Describe", session_id=session.session_id, attachments=(item,))
        assert first.run.status == "completed", first.run.error
        observed = runtime.store.load_runtime_session(session.session_id).messages[0]
        assert not any(isinstance(part, ImageContent) for part in observed.content)
        assert "model in use did not support it" in observed.text_content

        runtime.adapter.model.supports_image_input = True
        second = await runtime.run("Continue", session_id=session.session_id)
        assert second.run.status == "completed", second.run.error
        wire = str(provider.requests[-1])
        assert encoded not in wire
        assert "This model cannot view images." not in wire
        assert "model in use did not support it" in wire

        third = await runtime.run("Look again", session_id=session.session_id, attachments=(item,))
        assert third.run.status == "completed", third.run.error
        assert encoded in str(provider.requests[-1])
        assert runtime.store.load_runtime_session(session.session_id).messages[0] == observed
    finally:
        await app.close()
