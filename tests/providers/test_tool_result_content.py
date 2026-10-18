from copy import deepcopy

import pytest

from ouro.core.content import ImageContent, TextContent
from ouro.core.messages import MessageRecord, ModelMessage, ToolResultReference
from ouro.core.tools import ToolCallProducer, ToolCallRequest
from ouro.providers.anthropic_messages_codec import messages_to_anthropic_messages
from ouro.providers.openai_chat_completions_codec import messages_to_chat_completions
from ouro.providers.openai_responses_codec import responses_input

URI = "data:image/png;base64,aW1hZ2U="


def encode(protocol, messages, *, image_input, audio_input=False, video_input=False):
    kwargs = dict(system_prompt="", provider_id="p", model_id="m", supports_image_input=image_input)
    if protocol == "responses":
        return responses_input(messages, scope=("scope",), **kwargs)["input"]
    if protocol == "messages":
        return messages_to_anthropic_messages(messages, **kwargs)[1]
    return messages_to_chat_completions(
        messages,
        supports_audio_input=audio_input,
        supports_video_input=video_input,
        **kwargs,
    )


def exchange():
    calls = [ToolCallRequest(id=f"call-{i}", name="Render", source_index=i) for i in range(2)]
    return [
        ModelMessage(role="assistant", tool_calls=calls),
        *[
            ModelMessage(
                role="tool_result",
                content=[
                    TextContent(text="before"),
                    ImageContent(uri=URI),
                    TextContent(text="after"),
                ],
                tool_result=ToolResultReference(
                    tool_call_id=call.id, tool_name=call.name, is_error=index == 1
                ),
            )
            for index, call in enumerate(calls)
        ],
    ]


@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
def test_route_projection_preserves_canonical_results_and_degrades_without_image_input(protocol):
    messages = exchange()
    before = deepcopy(messages)
    wire = encode(protocol, messages, image_input=False)
    assert URI not in str(wire)
    assert str(wire).count("This model cannot view images.") == 2
    assert str(wire).count("Trying again or changing its size will not help.") == 2
    assert "endpoint" not in str(wire)
    expected = (
        "before\nThis model cannot view images. The image was not sent. "
        "Trying again or changing its size will not help.\nafter"
    )
    if protocol == "responses":
        results = [item["output"] for item in wire if item.get("type") == "function_call_output"]
    elif protocol == "messages":
        results = [item["content"] for item in wire[-1]["content"]]
    else:
        results = [item["content"] for item in wire if item["role"] == "tool"]
    assert results == [expected, expected]
    assert messages == before
    # Switching routes or capabilities reuses the execution fact, not a previous fallback.
    assert URI in str(encode("responses", messages, image_input=True))
    assert messages == before


def test_responses_embeds_ordered_native_content_in_each_function_output():
    wire = encode("responses", exchange(), image_input=True)
    results = [item for item in wire if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in results] == ["call-0", "call-1"]
    assert results[0]["output"] == [
        {"type": "input_text", "text": "before"},
        {"type": "input_image", "image_url": URI, "detail": "auto"},
        {"type": "input_text", "text": "after"},
    ]
    assert not any(item.get("role") == "user" for item in wire)


def test_messages_groups_native_tool_results_before_user_content():
    wire = encode("messages", exchange(), image_input=True)
    assert [item["role"] for item in wire] == ["assistant", "user"]
    results = wire[-1]["content"]
    assert [result["tool_use_id"] for result in results] == ["call-0", "call-1"]
    assert [result["is_error"] for result in results] == [False, True]
    assert results[0]["content"] == [
        {"type": "text", "text": "before"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="},
        },
        {"type": "text", "text": "after"},
    ]


def test_chat_completions_finishes_all_tool_results_before_supplemental_messages():
    messages = exchange()
    messages.append(ModelMessage.text(role="user", text="next question"))
    wire = encode("chat", messages, image_input=True)
    assert [item["role"] for item in wire] == ["assistant", "tool", "tool", "user", "user", "user"]
    for index in range(2):
        result, context = wire[index + 1], wire[index + 3]
        assert result["tool_call_id"] == f"call-{index}"
        assert isinstance(result["content"], str)
        assert "The image is included below." in result["content"]
        assert URI not in result["content"]
        assert f"call-{index}" in str(context["content"])
        assert {"type": "image_url", "image_url": {"url": URI}} in context["content"]
    assert wire[-1]["content"] == "next question"


@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
@pytest.mark.parametrize("image_input", [False, True])
def test_driver_result_preserves_media_as_input_without_fabricated_model_calls(
    protocol, image_input
):
    receipt = MessageRecord(
        session_id="session",
        role="tool_result",
        scaffold=True,
        content=[
            TextContent(text="Rendered evidence"),
            ImageContent(uri=URI, media_type="image/png"),
        ],
        tool_result=ToolResultReference(
            tool_call_id="native-call",
            tool_name="Render",
            producer=ToolCallProducer(kind="driver", driver_id="example/run_drivers/review"),
        ),
    )
    messages = [ModelMessage.from_record(receipt)]
    wire = encode(protocol, messages, image_input=image_input)
    assert ("aW1hZ2U=" in str(wire)) == image_input
    if not image_input:
        assert "This model cannot view images" in str(wire)
    assert "Rendered evidence" in str(wire)
    assert "<driver-tool-result>" in str(wire)
    assert "native-call" in str(wire)
    assert [item["role"] for item in wire] == ["user"]
    assert all(not item.tool_calls for item in messages)
    assert not any(item.get("type") == "function_call_output" for item in wire)
    assert receipt.tool_result.producer.kind == "driver"
    assert receipt.tool_result.producer.driver_id == "example/run_drivers/review"
