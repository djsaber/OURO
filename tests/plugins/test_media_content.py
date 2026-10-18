import pytest

from ouro.core.content import AudioContent, VideoContent
from ouro.plugins.tool_content import decode_worker_content, from_sdk_content
from ouro.sdk.plugins.v4 import ToolAudioContent, ToolVideoContent


@pytest.mark.parametrize(
    "sdk_type,core_type,mime",
    [
        (ToolAudioContent, AudioContent, "audio/wav"),
        (ToolVideoContent, VideoContent, "video/mp4"),
    ],
)
def test_worker_and_sdk_boundaries_preserve_media_type_and_bytes(sdk_type, core_type, mime):
    uri = f"data:{mime};base64,YQ=="
    sdk_content = (sdk_type(uri=uri, media_type=mime),)
    content = from_sdk_content(sdk_content)
    assert content == [core_type(uri=uri, media_type=mime)]
    assert decode_worker_content([part.model_dump(mode="json") for part in content]) == sdk_content
