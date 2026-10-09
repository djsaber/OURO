from copy import deepcopy

import pytest

from ouro.core.content import ImageContent, TextContent
from ouro.core.messages import ModelMessage, ToolResultReference
from ouro.core.tools import ToolCallRequest
from ouro.providers.anthropic_messages_codec import messages_to_anthropic_messages
from ouro.providers.openai_chat_completions_codec import messages_to_chat_completions
from ouro.providers.openai_responses_codec import responses_input

URI = "data:image/png;base64,aW1hZ2U="


def encode(protocol, messages, *, vision):
    kwargs = dict(system_prompt="", provider_id="p", model_id="m", supports_vision=vision)
    if protocol == "responses":
        return responses_input(messages, scope=("scope",), **kwargs)["input"]
    if protocol == "messages":
        return messages_to_anthropic_messages(messages, include_continuation=False, **kwargs)[1]
    return messages_to_chat_completions(messages, include_continuation=False, **kwargs)


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
def test_route_projection_preserves_canonical_results_and_degrades_without_vision(protocol):
    messages = exchange()
    before = deepcopy(messages)
    wire = encode(protocol, messages, vision=False)
    assert URI not in str(wire)
    assert str(wire).count("target endpoint does not support") == 2
    assert "before" in str(wire) and "after" in str(wire)
    assert messages == before
    # Switching routes or capabilities reuses the execution fact, not a previous fallback.
    assert URI in str(encode("responses", messages, vision=True))
    assert messages == before


def test_responses_embeds_ordered_native_content_in_each_function_output():
    wire = encode("responses", exchange(), vision=True)
    results = [item for item in wire if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in results] == ["call-0", "call-1"]
    assert results[0]["output"] == [
        {"type": "input_text", "text": "before"},
        {"type": "input_image", "image_url": URI, "detail": "auto"},
        {"type": "input_text", "text": "after"},
    ]
    assert not any(item.get("role") == "user" for item in wire)


def test_messages_groups_native_tool_results_before_user_content():
    wire = encode("messages", exchange(), vision=True)
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
    wire = encode("chat", messages, vision=True)
    assert [item["role"] for item in wire] == ["assistant", "tool", "tool", "user", "user", "user"]
    for index in range(2):
        result, context = wire[index + 1], wire[index + 3]
        assert result["tool_call_id"] == f"call-{index}"
        assert isinstance(result["content"], str)
        assert "following tool context" in result["content"]
        assert URI not in result["content"]
        assert f"call-{index}" in str(context["content"])
        assert {"type": "image_url", "image_url": {"url": URI}} in context["content"]
    assert wire[-1]["content"] == "next question"
