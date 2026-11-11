from __future__ import annotations

import pytest

from ouro.config.reasoning import ReasoningProfileConfig
from ouro.core.messages import ModelMessage, ProviderContinuationArtifact
from ouro.core.provider import ModelMetadata
from ouro.providers import OpenAIChatCompletionsProvider
from ouro.providers.reasoning import ReasoningPayloadEncoder
from tests.providers.test_openai_chat_completions import FakeStreamingTransport, FakeTransport


@pytest.mark.parametrize("field", [None, "reasoning_content", "reasoning"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_target_text_field_encodes_canonical_text_without_replaying_foreign_state(
    field, foreign
):
    provider = OpenAIChatCompletionsProvider(
        provider_id="target",
        base_url="https://example.test",
        transport=FakeTransport(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ),
        reasoning_encoder=ReasoningPayloadEncoder(
            ReasoningProfileConfig(name="reasoning-text", text_field=field)
        ),
    )
    artifact = ProviderContinuationArtifact(
        provider_id="source" if foreign else "target",
        protocol="openai-chat-completions",
        model_id="model",
        blocks=({"reasoning": "original text"},),
    )
    messages = [
        ModelMessage(
            role="assistant", reasoning_content="original text", provider_continuation=artifact
        ),
        ModelMessage.text(role="assistant", text="answer without reasoning"),
    ]
    _ = [
        e
        async for e in provider.stream(
            model=ModelMetadata(provider_id="target", model_id="model"), messages=messages
        )
    ]
    sent = provider.transport.requests[0]["payload"]["messages"]
    if field is not None:
        assert sent[0] == {"role": "assistant", "content": "", field: "original text"}
        assert sent[1] == {"role": "assistant", "content": "answer without reasoning"}
    else:
        assert sent[0] == {
            "role": "assistant",
            "content": "",
            **({} if foreign else {"reasoning": "original text"}),
        }
        assert sent[1] == {"role": "assistant", "content": "answer without reasoning"}
    assert artifact.blocks == ({"reasoning": "original text"},)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("plain_text", [False, True])
@pytest.mark.parametrize("text_field", [None, "reasoning_content", "reasoning"])
async def test_structured_reasoning_roundtrip_preserves_blocks_and_displays_text_once(
    streaming, plain_text, text_field
):
    details = [
        {"type": "reasoning.text", "text": "check", "index": 0, "signature": None},
        {"type": "reasoning.text", "text": "ed", "index": 0, "signature": "signed"},
        {"type": "reasoning.summary", "summary": " facts", "index": 1},
        {"type": "reasoning.encrypted", "data": "opaque", "id": "r2", "index": 2},
    ]
    parts = [
        {"reasoning_details": [details[0]], **({"reasoning": "check"} if plain_text else {})},
        {"reasoning_details": details[1:3], **({"reasoning": "ed facts"} if plain_text else {})},
        {"reasoning_details": details[3:]},
    ]
    transport = (
        FakeStreamingTransport(
            [
                *({"choices": [{"delta": part}]} for part in parts),
                {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
            ]
        )
        if streaming
        else FakeTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": "answer",
                            "reasoning_details": details,
                            **({"reasoning": "checked facts"} if plain_text else {}),
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
    )
    provider = OpenAIChatCompletionsProvider(
        base_url="https://example.test",
        transport=transport,
        reasoning_encoder=ReasoningPayloadEncoder(
            ReasoningProfileConfig(name="reasoning-text", text_field=text_field)
        ),
    )
    model = ModelMetadata(
        provider_id=provider.provider_id, model_id="reasoner", supports_streaming=streaming
    )
    events = [event async for event in provider.stream(model=model, messages=[])]
    assert "".join(event.reasoning_content or "" for event in events) == "checked facts"
    assert events[-1].type == "done" and events[-1].completion.has_reasoning
    artifact = next(event.continuation for event in events if event.type == "continuation")
    assert artifact.blocks[0]["reasoning_details"] == details
    # Persistence must not flatten signatures, ciphertext or repeated native coordinates.
    artifact = ProviderContinuationArtifact.model_validate_json(artifact.model_dump_json())
    assistant = ModelMessage.text(role="assistant", text="answer").model_copy(
        update={"reasoning_content": "checked facts", "provider_continuation": artifact}
    )
    replay = FakeTransport({"choices": [{"message": {"content": "next"}, "finish_reason": "stop"}]})
    provider.transport = replay
    async for _ in provider.stream(model=model, messages=[assistant]):
        pass
    assert replay.requests[0]["payload"]["messages"][0]["reasoning_details"] == details
    if text_field is not None:
        assert replay.requests[0]["payload"]["messages"][0][text_field] == "checked facts"
    async for _ in provider.stream(
        model=model.model_copy(update={"model_id": "other"}), messages=[assistant]
    ):
        pass
    assert "reasoning_details" not in replay.requests[1]["payload"]["messages"][0]
    if text_field is not None:
        assert replay.requests[1]["payload"]["messages"][0][text_field] == "checked facts"
    assert artifact.blocks[0]["reasoning_details"] == details


@pytest.mark.parametrize("streaming", [False, True])
async def test_encrypted_reasoning_is_a_terminal_fact_without_display_text(streaming):
    part = {"reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]}
    transport = (
        FakeStreamingTransport([{"choices": [{"delta": part, "finish_reason": "stop"}]}])
        if streaming
        else FakeTransport({"choices": [{"message": part, "finish_reason": "stop"}]})
    )
    provider = OpenAIChatCompletionsProvider(base_url="https://example.test", transport=transport)
    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(
                provider_id=provider.provider_id, model_id="reasoner", supports_streaming=streaming
            ),
            messages=[],
        )
    ]
    assert [event.type for event in events] == ["continuation", "done"]
    assert events[-1].completion.has_reasoning


async def test_unfinished_structured_reasoning_stream_cannot_publish_continuation():
    provider = OpenAIChatCompletionsProvider(
        base_url="https://example.test",
        transport=FakeStreamingTransport(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_details": [{"type": "reasoning.text", "text": "partial"}]
                            }
                        }
                    ]
                }
            ]
        ),
    )
    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(provider_id=provider.provider_id, model_id="reasoner"), messages=[]
        )
    ]
    assert [event.type for event in events] == ["reasoning_delta", "error"]


@pytest.mark.parametrize("details", ["opaque", ["opaque"]])
async def test_malformed_structured_reasoning_cannot_publish_success(details):
    provider = OpenAIChatCompletionsProvider(
        base_url="https://example.test",
        transport=FakeTransport(
            {"choices": [{"message": {"reasoning_details": details}, "finish_reason": "stop"}]}
        ),
    )
    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(provider_id=provider.provider_id, model_id="reasoner"), messages=[]
        )
    ]
    assert [event.type for event in events] == ["error"]
