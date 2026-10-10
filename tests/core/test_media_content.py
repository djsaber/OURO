import pytest

from ouro.core.attachment_projection import file_media_type
from ouro.core.content import AudioContent, VideoContent
from ouro.core.context import estimate_message_tokens
from ouro.core.messages import ModelMessage


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"RIFF\x24\x00\x00\x00WAVE", "audio/wav"),
        (b"ID3\x04\0\0\0\0\0\0", "audio/mpeg"),
        (b"ID3 is a metadata format", None),
        (b"ID3\x04\0\0\xff\0\0\0", None),
        (b"\xff\xfb\x90\0", "audio/mpeg"),
        (b"\xff\xeb\x90\0", None),  # Reserved MPEG version.
        (b"\xff\xfb\xfc\0", None),  # Reserved bit/sample rates.
        (b"\0\0\0\x18ftypmp42\0\0\0\0", "video/mp4"),
        (b"\0\0\0\x18ftypM4A \0\0\0\0", "audio/mp4"),
        (b"\0\0\0\x18ftypavif\0\0\0\0", None),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"RIFF\0\0\0\0WEBP", "image/webp"),
        (b"file named mp4", None),
    ],
)
def test_container_identification_distinguishes_media_without_using_file_extensions(raw, expected):
    assert file_media_type(raw) == expected


@pytest.mark.parametrize(
    "content_type,mime", [(AudioContent, "audio/wav"), (VideoContent, "video/mp4")]
)
def test_media_round_trip_and_token_estimation_never_treat_binary_data_as_text(content_type, mime):
    part = content_type(uri=f"data:{mime};base64," + "AAAA" * 10000, media_type=mime)
    message = ModelMessage(role="user", content=[part])
    assert ModelMessage.model_validate_json(message.model_dump_json()) == message
    estimated = []

    def text_estimator(text):
        estimated.append(text)
        return 1

    assert estimate_message_tokens(message, text_estimator) > 1
    assert not any("AAAA" in text for text in estimated)
