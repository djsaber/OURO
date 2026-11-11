from __future__ import annotations

import json

import pytest

from ouro.core.messages import (
    ModelMessage,
    ProviderContinuationArtifact,
    TextContent,
    ToolResultReference,
)
from ouro.core.provider import ModelMetadata
from ouro.core.tools import ToolCallRequest
from ouro.providers import OpenAIChatCompletionsProvider
from ouro.providers.anthropic_messages.codec import messages_to_anthropic_messages
from ouro.providers.openai_chat_completions.codec import messages_to_chat_completions
from ouro.providers.openai_responses.codec import responses_input
from tests.providers.test_openai_chat_completions import FakeStreamingTransport, FakeTransport


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
def test_signed_thinking_is_not_overwritten_by_a_same_named_text_field(field):
    native = [{"type": "reasoning.text", "text": "Original.", "signature": "signed"}]
    assistant = ModelMessage(
        role="assistant",
        reasoning_content="Readable summary.",
        provider_continuation=ProviderContinuationArtifact(
            provider_id="p",
            protocol="openai-chat-completions",
            model_id="m",
            blocks=({field: native},),
        ),
    )
    wire = messages_to_chat_completions(
        [assistant],
        system_prompt="",
        supports_image_input=False,
        provider_id="p",
        model_id="m",
        reasoning_text_field=field,
    )
    assert wire == [{"role": "assistant", "content": "", field: native}]


@pytest.mark.parametrize("target", ["chat", "responses", "messages"])
@pytest.mark.parametrize("foreign_state", [False, True])
@pytest.mark.parametrize("answer", ["Checking the file.", ""])
def test_selected_reasoning_survives_protocol_projection_with_complete_tool_history(
    target, foreign_state, answer
):
    assistant = ModelMessage(
        role="assistant",
        content=[TextContent(text=answer)] if answer else [],
        reasoning_content="The cause might be configuration; inspect before editing.",
        tool_calls=[ToolCallRequest(id="call_1", name="Read", arguments={"path": "a"})],
        provider_continuation=ProviderContinuationArtifact(
            provider_id="source",
            protocol="another-protocol",
            model_id="old-model",
            blocks=({"ciphertext": "source-only-state"},),
        )
        if foreign_state
        else None,
    )
    result = ModelMessage.text(role="tool_result", text="Configuration is valid.").model_copy(
        update={"tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read")}
    )
    # The same persisted facts feed every target; projection must not mutate them.
    history = [ModelMessage.model_validate_json(m.model_dump_json()) for m in (assistant, result)]
    before = [m.model_dump_json() for m in history]
    options = dict(system_prompt="", provider_id="target", model_id="new-model")
    if target == "chat":
        wire = messages_to_chat_completions(history, supports_image_input=False, **options)
        assert wire[0]["tool_calls"][0]["id"] == wire[1]["tool_call_id"] == "call_1"
        assert wire[0]["content"] == assistant.reasoning_context_text + assistant.text_content
    elif target == "responses":
        wire = responses_input(history, scope=("account",), **options)["input"]
        assert wire[1]["call_id"] == wire[2]["call_id"] == "call_1"
        assert wire[0]["content"] == assistant.reasoning_context_text + assistant.text_content
    else:
        _, wire = messages_to_anthropic_messages(history, supports_image_input=False, **options)
        assert wire[0]["content"][-1]["id"] == wire[1]["content"][0]["tool_use_id"] == "call_1"
        assert wire[0]["content"][0] == {"type": "text", "text": assistant.reasoning_context_text}
    encoded = json.dumps(wire)
    assert encoded.count(assistant.reasoning_content) == 1
    assert answer in encoded and "Configuration is valid." in encoded
    assert "source-only-state" not in encoded
    assert [m.model_dump_json() for m in history] == before


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "output",
    [
        {"reasoning_content": "Check first."},
        {"reasoning": "Check first."},
        {"reasoning": [{"text": "Check first."}]},
        {"reasoning": [{"type": "text", "text": "Check first."}]},
        {"reasoning_details": [{"type": "reasoning.text", "text": "Check first.", "index": 0}]},
    ],
)
@pytest.mark.parametrize("field", [None, "reasoning", "reasoning_content"])
async def test_plain_reasoning_has_one_history_path_independent_of_response_spelling(
    streaming, output, field
):
    choice = {
        "delta" if streaming else "message": {"content": "Answer.", **output},
        "finish_reason": "stop",
    }
    transport = (
        FakeStreamingTransport([{"choices": [choice]}])
        if streaming
        else FakeTransport({"choices": [choice]})
    )
    provider = OpenAIChatCompletionsProvider(
        base_url="https://example.test", transport=transport, reasoning_text_field=field
    )
    model = ModelMetadata(
        provider_id=provider.provider_id, model_id="source", supports_streaming=streaming
    )
    events = [event async for event in provider.stream(model=model, messages=[])]
    assert not any(event.type == "continuation" for event in events)
    assistant = ModelMessage.text(role="assistant", text="Answer.").model_copy(
        update={"reasoning_content": "".join(event.reasoning_content or "" for event in events)}
    )
    assert assistant.reasoning_content == "Check first."
    for model_id in ("source", "different"):
        _ = [
            event
            async for event in provider.stream(
                model=model.model_copy(update={"model_id": model_id}), messages=[assistant]
            )
        ]
    first, second = [r["payload"]["messages"][0] for r in transport.requests[-2:]]
    assert (
        first
        == second
        == {
            "role": "assistant",
            "content": "Answer." if field else assistant.reasoning_context_text + "Answer.",
            **({field: "Check first."} if field else {}),
        }
    )
