from __future__ import annotations

from copy import deepcopy

import pytest

from ouro.core.messages import (
    ImageContent,
    MessageRecord,
    ModelMessage,
    TextContent,
    ToolResultReference,
)
from ouro.core.tools import ToolCallRequest
from ouro.providers.openai_responses_codec import (
    continuation_from_output,
    responses_input,
    responses_tools,
    responses_usage,
)
from ouro.providers.openai_responses_errors import OpenAIResponsesError
from ouro.providers.openai_responses_stream import OpenAIResponsesStreamDecoder
from tests.providers.responses_fixtures import SCOPE, call, message, reasoning, terminal


def encode(messages, **kwargs):
    return responses_input(
        messages,
        system_prompt="system",
        provider_id="provider",
        model_id="model",
        scope=SCOPE,
        **kwargs,
    )


def artifact(items):
    return continuation_from_output(items, provider_id="provider", model_id="model", scope=SCOPE)


def assistant(items=None):
    return ModelMessage(
        role="assistant",
        content=[TextContent(text="canonical display")],
        tool_calls=[ToolCallRequest(id="call-1", name="echo", arguments={"round": 1})],
        provider_continuation=artifact(items or [reasoning(), message(), call()]),
    )


def test_input_preserves_system_order_canonical_calls_and_tool_result_references():
    canonical = assistant().model_copy(update={"provider_continuation": None})
    result = encode(
        [
            ModelMessage.text(role="system", text="second system"),
            ModelMessage.text(role="user", text="question"),
            canonical,
            ModelMessage(
                role="tool_result",
                content=[TextContent(text="result")],
                tool_result=ToolResultReference(tool_call_id="call-1", tool_name="echo"),
            ),
        ]
    )
    assert result == {
        "instructions": "system\n\nsecond system",
        "input": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "canonical display"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "echo",
                "arguments": '{"round": 1}',
            },
            {"type": "function_call_output", "call_id": "call-1", "output": "result"},
        ],
    }


def test_complete_items_survive_persistence_and_replace_canonical_reconstruction():
    output = [
        reasoning(content=[{"type": "reasoning_text", "text": "analysis"}]),
        message(phase="commentary"),
        call(),
    ]
    original = assistant(output)
    stored = MessageRecord(
        session_id="test",
        role="assistant",
        content=original.content,
        tool_calls=original.tool_calls,
        provider_continuation=original.provider_continuation,
    )
    restored = ModelMessage.from_record(MessageRecord.model_validate_json(stored.model_dump_json()))
    request = encode([restored])
    assert request["input"] == output
    assert "scope" not in request and "format_version" not in request
    assert "canonical display" not in str(request)
    request["input"][0]["encrypted_content"] = "changed"
    assert (
        restored.provider_continuation.blocks[0]["items"][0]["encrypted_content"]
        == "opaque-ciphertext"
    )


def test_two_tool_rounds_replay_reasoning_and_results_without_rebuilding_calls():
    history = [ModelMessage.text(role="user", text="use echo twice")]
    expected = [{"role": "user", "content": "use echo twice"}]
    for number in (1, 2):
        output = [reasoning(id=f"rs-{number}"), call(number)]
        stream = OpenAIResponsesStreamDecoder(provider_id="provider", model_id="model", scope=SCOPE)
        stream.feed(terminal(output))
        events = stream.finish()
        continuation = next(event.continuation for event in events if event.type == "continuation")
        history.extend(
            [
                ModelMessage(
                    role="assistant",
                    provider_continuation=continuation,
                    tool_calls=[
                        ToolCallRequest(
                            id=f"call-{number}", name="echo", arguments={"round": number}
                        )
                    ],
                ),
                ModelMessage(
                    role="tool_result",
                    content=[TextContent(text=f"result-{number}")],
                    tool_result=ToolResultReference(
                        tool_call_id=f"call-{number}", tool_name="echo"
                    ),
                ),
            ]
        )
        expected.extend(
            [
                *output,
                {
                    "type": "function_call_output",
                    "call_id": f"call-{number}",
                    "output": f"result-{number}",
                },
            ]
        )
        assert encode(history)["input"] == expected


def test_function_call_without_optional_item_id_survives_persistence_and_replay():
    item = call()
    del item["id"]
    msg = assistant([item])
    restored = ModelMessage.model_validate_json(msg.model_dump_json())
    assert encode([restored])["input"] == [item]


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("scope", ["test-service", "another-account"], "continuation_scope_mismatch"),
        ("scope", ["another-service", "test-account"], "continuation_scope_mismatch"),
        ("scope", [], "invalid_continuation"),
        ("scope", "test-account", "invalid_continuation"),
        ("format_version", 2, "invalid_continuation"),
        ("format_version", True, "invalid_continuation"),
        ("items", [], "invalid_continuation"),
        ("items", [{"type": "new-executable-kind"}], "unsupported_output_item"),
        ("extra", True, "invalid_continuation"),
    ],
)
def test_unreadable_or_foreign_continuation_is_not_silently_dropped(field, value, code):
    msg = assistant()
    msg.provider_continuation.blocks[0][field] = value
    with pytest.raises(OpenAIResponsesError) as failure:
        encode([msg])
    assert failure.value.code == code
    assert "opaque-ciphertext" not in str(failure.value)


@pytest.mark.parametrize("field", ["provider_id", "protocol", "model_id"])
def test_outer_route_mismatch_requires_restoring_route_or_starting_new_session(field):
    msg = assistant()
    msg.provider_continuation = msg.provider_continuation.model_copy(update={field: "different"})
    with pytest.raises(OpenAIResponsesError, match="Restore the original") as failure:
        encode([msg])
    assert failure.value.code == "continuation_route_mismatch"


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [ToolCallRequest(id="wrong", name="echo", arguments={"round": 1})],
        [ToolCallRequest(id="call-1", name="echo", arguments={"round": 2})],
    ],
)
def test_replay_must_agree_with_canonical_tool_frame(calls):
    with pytest.raises(OpenAIResponsesError) as failure:
        encode([assistant().model_copy(update={"tool_calls": calls})])
    assert failure.value.code == "continuation_tool_mismatch"


def test_text_only_messages_do_not_require_an_account_binding():
    result = responses_input(
        [ModelMessage.text(role="user", text="hi")],
        system_prompt="",
        provider_id="other",
        model_id="other",
        scope=(),
    )
    assert result == {"instructions": "", "input": [{"role": "user", "content": "hi"}]}


def test_unsupported_images_and_missing_tool_reference_fail_instead_of_disappearing():
    for msg in [
        ModelMessage(role="user", content=[ImageContent(uri="https://image.test/a")]),
        ModelMessage(role="tool_result", content=[TextContent(text="result")]),
    ]:
        with pytest.raises(OpenAIResponsesError) as failure:
            encode([msg])
        assert failure.value.code == "unsupported_input"


def test_tools_keep_optional_parameters_without_forcing_strict_schema():
    tools = [
        {
            "name": "echo",
            "description": "repeat",
            "parameters_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": [],
            },
        }
    ]
    before = deepcopy(tools)
    encoded = responses_tools(tools)
    assert encoded == [
        {
            "type": "function",
            "name": "echo",
            "description": "repeat",
            "parameters": tools[0]["parameters_schema"],
            "strict": False,
        }
    ]
    encoded[0]["parameters"]["required"].append("text")
    assert tools == before


def test_usage_counts_cached_and_reasoning_as_subsets_and_leaves_cost_unknown():
    usage = responses_usage(
        {
            "input_tokens": 20,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 4},
        }
    )
    assert usage.input_tokens == 20 and usage.output_tokens == 10 and usage.cache_read_tokens == 5
    assert usage.estimated_cost is None and responses_usage(None) is None


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": 20, "output_tokens": -1},
        {"input_tokens": 20, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 21}},
    ],
)
def test_invalid_usage_does_not_become_an_invented_zero(usage):
    with pytest.raises(OpenAIResponsesError, match="invalid_usage"):
        responses_usage(usage)
