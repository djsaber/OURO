import pytest

from ouro.core.content import AudioContent, TextContent, VideoContent
from ouro.core.messages import ModelMessage, ToolResultReference
from ouro.core.provider import ModelMetadata
from ouro.core.tools import ToolCallRequest
from ouro.providers.openai_chat_completions.adapter import OpenAIChatCompletionsProvider
from tests.providers.test_openai_chat_completions import FakeStreamingTransport, FakeTransport
from tests.providers.test_tool_result_content import encode

MEDIA = [
    (
        AudioContent(uri="data:audio/wav;base64,YXVkaW8=", media_type="audio/wav"),
        {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
    ),
    (
        AudioContent(uri="data:audio/mpeg;base64,bXAz", media_type="audio/mpeg"),
        {"type": "input_audio", "input_audio": {"data": "bXAz", "format": "mp3"}},
    ),
    (
        VideoContent(uri="data:video/mp4;base64,dmlkZW8=", media_type="video/mp4"),
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,dmlkZW8="}},
    ),
]


@pytest.mark.parametrize("streaming", [False, True])
async def test_provider_request_uses_input_flags_without_requesting_audio_output(streaming):
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    transport = (
        FakeStreamingTransport(
            [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
        )
        if streaming
        else FakeTransport(response)
    )
    provider = OpenAIChatCompletionsProvider(base_url="https://media.test", transport=transport)
    model = ModelMetadata(
        model_id="av",
        provider_id="p",
        supports_audio_input=True,
        supports_video_input=True,
        supports_streaming=streaming,
    )
    try:
        events = [
            event
            async for event in provider.stream(
                model=model,
                messages=[ModelMessage(role="user", content=[item[0] for item in MEDIA])],
            )
        ]
        assert events[-1].type == "done" and not any(event.type == "error" for event in events)
        payload = transport.requests[0]["payload"]
        assert payload["messages"][0]["content"] == [item[1] for item in MEDIA]
        assert "modalities" not in payload and "audio" not in payload
    finally:
        await provider.close()


@pytest.mark.parametrize("part,expected", MEDIA)
def test_chat_encodes_media_in_order_without_enabling_output_modalities(part, expected):
    messages = [
        ModelMessage(
            role="user", content=[TextContent(text="before"), part, TextContent(text="after")]
        )
    ]
    wire = encode("chat", messages, image_input=False, audio_input=True, video_input=True)
    assert wire == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before"},
                expected,
                {"type": "text", "text": "after"},
            ],
        }
    ]
    with pytest.raises(ValueError, match=f"does not support {part.type}"):
        encode("chat", messages, image_input=True)


@pytest.mark.parametrize("part,expected", MEDIA)
def test_media_tool_results_follow_the_complete_tool_exchange(part, expected):
    messages = [
        ModelMessage(role="assistant", tool_calls=[ToolCallRequest(id="c", name="Media")]),
        ModelMessage(
            role="tool_result",
            content=[part],
            tool_result=ToolResultReference(tool_call_id="c", tool_name="Media"),
        ),
    ]
    before = [message.model_copy(deep=True) for message in messages]
    wire = encode("chat", messages, image_input=False, audio_input=True, video_input=True)
    assert [message["role"] for message in wire] == ["assistant", "tool", "user"]
    assert wire[1]["tool_call_id"] == "c"
    assert expected in wire[2]["content"]
    assert messages == before
    for protocol in ("chat", "responses", "messages"):
        fallback = encode(protocol, messages, image_input=True)
        assert part.uri not in str(fallback)
        assert f"The {part.type} was not sent" in str(fallback)


@pytest.mark.parametrize("protocol", ["responses", "messages"])
@pytest.mark.parametrize("role", ["user", "system", "assistant"])
@pytest.mark.parametrize("part,expected", MEDIA)
def test_protocols_reject_unprojected_media_instead_of_dropping_it(protocol, role, part, expected):
    with pytest.raises(ValueError):
        encode(protocol, [ModelMessage(role=role, content=[part])], image_input=True)


@pytest.mark.parametrize(
    "part",
    [
        AudioContent(uri="https://example.test/clip.wav", media_type="audio/wav"),
        AudioContent(uri="data:audio/mpeg;base64,YQ==", media_type="audio/wav"),
        AudioContent(uri="data:audio/ogg;base64,YQ==", media_type="audio/ogg"),
        VideoContent(uri="file:///private/clip.mp4", media_type="video/mp4"),
        VideoContent(uri="data:video/mp4;base64,", media_type="video/mp4"),
        VideoContent(uri="data:video/webm;base64,YQ==", media_type="video/webm"),
    ],
)
def test_chat_rejects_unsupported_media_sources_and_formats(part):
    with pytest.raises(ValueError):
        encode(
            "chat",
            [ModelMessage(role="user", content=[part])],
            image_input=True,
            audio_input=True,
            video_input=True,
        )


@pytest.mark.parametrize("part,expected", MEDIA)
def test_chat_rejects_native_media_outside_user_context(part, expected):
    with pytest.raises(ValueError, match="requires a user message"):
        encode(
            "chat",
            [ModelMessage(role="assistant", content=[part])],
            image_input=True,
            audio_input=True,
            video_input=True,
        )
