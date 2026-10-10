import base64

import pytest

from ouro.application import file_inputs
from ouro.core.content import AudioContent, ImageContent, MediaContent, VideoContent
from ouro.core.input_content import InputAttachment, InputFileReference
from ouro.core.messages import MessageRecord, ToolResultReference
from tests.application.test_resources import _services
from tests.application.test_tool_result_content import RecordingProvider
from tests.tui.test_history_commands import application_at

# Container signatures exercise ingestion and transport; OURO does not decode tracks.
MEDIA = [
    ("audio", "recording.wav", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 32),
    ("audio", "recording.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\0" * 32),
    ("video", "clip.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"),
]


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("source_kind", ["upload", "reference"])
@pytest.mark.parametrize("kind,name,raw", MEDIA)
async def test_media_observation_survives_source_removal_model_switch_and_reopen(
    tmp_path, monkeypatch, backend, source_kind, kind, name, raw
):
    provider = RecordingProvider("chat", request_tools=False)
    app = application_at(tmp_path / "home", provider, backend=backend)
    runtime = app.runtime
    session = runtime.create_session(title="media", workspace=tmp_path)
    path = tmp_path / name
    path.write_bytes(raw)
    if source_kind == "upload":
        resource = runtime.resources.import_resource(session.session_id, path).resource
        item = InputAttachment(resource_id=resource.resource_id, name=name)
    else:
        item = InputFileReference(name=name, path=str(path))
    encoded = base64.b64encode(raw).decode("ascii")
    try:
        setattr(runtime.adapter.model, f"supports_{kind}_input", True)
        result = await runtime.run("Describe", session_id=session.session_id, attachments=(item,))
        assert result.run.status == "completed", result.run.error
        assert encoded in str(provider.requests[-1])
        observed = runtime.store.load_runtime_session(session.session_id).messages[0]
        media = next(part for part in observed.content if isinstance(part, MediaContent))
        assert media.type == kind and base64.b64decode(media.uri.partition(",")[2]) == raw
        saved = runtime.sessions.context(session.session_id)
        snapshot = runtime.store.load_latest_context(session.session_id)
        assert getattr(snapshot, f"supports_{kind}_input") is True
        path.unlink()

        def fail_read(*args, **kwargs):
            pytest.fail("observed media must not be read again")

        monkeypatch.setattr(runtime.resources._store, "read_ranges", fail_read)
        setattr(runtime.adapter.model, f"supports_{kind}_input", False)
        assert runtime.sessions.context(session.session_id) == saved
        result = await runtime.run("Continue", session_id=session.session_id)
        assert result.run.status == "completed", result.run.error
        assert encoded not in str(provider.requests[-1])
        assert f"The {kind} was not sent" in str(provider.requests[-1])
        assert runtime.store.load_runtime_session(session.session_id).messages[0] == observed
    finally:
        await app.close()

    replay = RecordingProvider("chat", request_tools=False)
    reopened = application_at(tmp_path / "home", replay, backend=backend)
    try:
        setattr(reopened.runtime.adapter.model, f"supports_{kind}_input", True)
        result = await reopened.runtime.run("Review again", session_id=session.session_id)
        assert result.run.status == "completed", result.run.error
        assert encoded in str(replay.requests[-1])
        branch = reopened.runtime.sessions.branch(session.session_id, observed.message_id)
        branched = reopened.runtime.store.load_runtime_session(branch.session.session_id)
        assert branched.messages[0].content == observed.content
    finally:
        await reopened.close()


@pytest.mark.parametrize("kind,name,raw", MEDIA)
@pytest.mark.parametrize("source_kind", ["upload", "reference"])
async def test_unsupported_media_is_a_durable_reference_not_a_future_implicit_read(
    tmp_path, kind, name, raw, source_kind
):
    provider = RecordingProvider("chat", request_tools=False)
    app = application_at(tmp_path / "home", provider)
    runtime = app.runtime
    session = runtime.create_session(workspace=tmp_path)
    path = tmp_path / name
    path.write_bytes(raw)
    item = (
        InputAttachment(
            resource_id=runtime.resources.import_resource(
                session.session_id, path
            ).resource.resource_id,
            name=name,
        )
        if source_kind == "upload"
        else InputFileReference(name=name, path=str(path))
    )
    try:
        first = await runtime.run("Describe", session_id=session.session_id, attachments=(item,))
        assert first.run.status == "completed", first.run.error
        observed = runtime.store.load_runtime_session(session.session_id).messages[0]
        assert not any(isinstance(part, MediaContent) for part in observed.content)
        assert "model in use did not support it" in observed.text_content
        setattr(runtime.adapter.model, f"supports_{kind}_input", True)
        second = await runtime.run("Continue", session_id=session.session_id)
        assert second.run.status == "completed", second.run.error
        assert base64.b64encode(raw).decode("ascii") not in str(provider.requests[-1])
    finally:
        await app.close()


@pytest.mark.parametrize("kind,name,raw", MEDIA)
def test_media_budget_is_checked_before_materializing_and_encoding_the_whole_resource(
    tmp_path, monkeypatch, kind, name, raw
):
    _, store, resources, sessions = _services(tmp_path / "home")
    session_id = sessions.create().session.session_id
    path = tmp_path / name
    path.write_bytes(raw + b"\0" * 32000)
    resource = resources.import_resource(session_id, path).resource
    message = MessageRecord.text(session_id=session_id, role="user", text="")
    message.resource_refs = [resource.resource_id]
    original = store.read_ranges
    reads = []

    def bounded_read(session_id, record, ranges):
        reads.extend(ranges)
        return original(session_id, record, ranges)

    def fail_encode(*args, **kwargs):
        pytest.fail("over-budget media must not be encoded")

    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", 1)
    monkeypatch.setattr(store, "read_ranges", bounded_read)
    monkeypatch.setattr(file_inputs.base64, "b64encode", fail_encode)
    projected = resources.project_model_messages([message], input_modalities=frozenset({kind}))[0]
    assert "too much media data" in projected.text_content
    assert max(length for _, length in reads) <= 4096
    assert not any(isinstance(part, MediaContent) for part in projected.content)


@pytest.mark.parametrize("name", ["fake.wav", "fake.mp3", "fake.mp4"])
def test_media_extension_does_not_turn_plain_text_into_native_media(tmp_path, name):
    _, _, resources, sessions = _services(tmp_path / "home")
    session_id = sessions.create().session.session_id
    path = tmp_path / name
    path.write_text("not media", encoding="utf-8")
    resource = resources.import_resource(session_id, path).resource
    message = MessageRecord.text(session_id=session_id, role="user", text="")
    message.resource_refs = [resource.resource_id]
    projected = resources.project_model_messages(
        [message], input_modalities=frozenset({"audio", "video"})
    )[0]
    assert "could not be read as supported media" in projected.text_content
    assert not any(isinstance(part, MediaContent) for part in projected.content)


@pytest.mark.parametrize("source_kind", ["inline", "resource"])
@pytest.mark.parametrize(
    "content_type,mime,name,header",
    [
        (ImageContent, "image/png", "image.png", b"\x89PNG\r\n\x1a\n"),
        (AudioContent, "audio/wav", "recording.wav", MEDIA[0][2]),
        (VideoContent, "video/mp4", "clip.mp4", MEDIA[2][2]),
    ],
    ids=["image", "audio", "video"],
)
def test_unsupported_tool_media_neither_encodes_nor_displaces_supported_input(
    tmp_path, monkeypatch, source_kind, content_type, mime, name, header
):
    _, _, resources, sessions = _services(tmp_path / "home")
    session_id = sessions.create().session.session_id
    raw = header + b"\0" * 12_000
    uri = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    latest = MessageRecord(
        session_id=session_id,
        role="tool_result",
        tool_result=ToolResultReference(tool_call_id="media", tool_name="ReadMedia"),
    )
    if source_kind == "inline":
        latest.content = [content_type(uri=uri, media_type=mime)]
    else:
        path = tmp_path / name
        path.write_bytes(raw)
        latest.resource_refs = [resources.import_resource(session_id, path).resource.resource_id]
    supported = (
        AudioContent(uri="data:audio/wav;base64,YQ==", media_type="audio/wav")
        if content_type is ImageContent
        else ImageContent(uri="data:image/png;base64,YQ==", media_type="image/png")
    )
    earlier = MessageRecord(session_id=session_id, role="user", content=[supported])
    before = [message.model_copy(deep=True) for message in (earlier, latest)]
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", len(uri.partition(",")[2]))

    def fail_encode(*args, **kwargs):
        pytest.fail("unsupported tool media must not be encoded")

    monkeypatch.setattr(file_inputs.base64, "b64encode", fail_encode)
    projected = resources.project_model_messages(
        [earlier, latest], input_modalities=frozenset({supported.type})
    )
    assert projected[0].content == [supported]
    assert not any(isinstance(part, MediaContent) for part in projected[1].content)
    assert "was not sent" in projected[1].text_content
    assert [earlier, latest] == before


def test_unsupported_tool_media_format_does_not_displace_supported_input(tmp_path, monkeypatch):
    _, _, resources, sessions = _services(tmp_path / "home")
    session_id = sessions.create().session.session_id
    image = ImageContent(uri="data:image/png;base64,YQ==", media_type="image/png")
    earlier = MessageRecord(session_id=session_id, role="user", content=[image])
    latest = MessageRecord(
        session_id=session_id,
        role="tool_result",
        content=[AudioContent(uri="data:audio/ogg;base64,YQ==", media_type="audio/ogg")],
        tool_result=ToolResultReference(tool_call_id="audio", tool_name="ReadAudio"),
    )
    before = latest.model_copy(deep=True)
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", 4)

    projected = resources.project_model_messages(
        [earlier, latest], input_modalities=frozenset({"image", "audio"})
    )

    assert projected[0].content == [image]
    assert not any(isinstance(part, MediaContent) for part in projected[1].content)
    assert "audio format 'audio/ogg' cannot be sent" in projected[1].text_content
    assert latest == before


def test_inline_audio_and_video_resource_share_one_request_budget(tmp_path, monkeypatch):
    _, _, resources, sessions = _services(tmp_path / "home")
    session_id = sessions.create().session.session_id
    _, name, raw = MEDIA[-1]
    path = tmp_path / name
    path.write_bytes(raw)
    resource = resources.import_resource(session_id, path).resource
    earlier = MessageRecord.text(session_id=session_id, role="user", text="video")
    earlier.resource_refs = [resource.resource_id]
    audio = AudioContent(uri="data:audio/wav;base64,YXVkaW8=", media_type="audio/wav")
    latest = MessageRecord(session_id=session_id, role="user", content=[audio])
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", 8)

    def fail_encode(*args, **kwargs):
        pytest.fail("inline audio has already consumed the media budget")

    monkeypatch.setattr(file_inputs.base64, "b64encode", fail_encode)
    projected = resources.project_model_messages(
        [earlier, latest], input_modalities=frozenset({"audio", "video"})
    )
    assert projected[1].content == [audio]
    assert "too much media data" in projected[0].text_content
    assert (
        str(resources.resource_path(session_id, resource.resource_id).as_posix())
        in projected[0].text_content
    )
