from __future__ import annotations

import pytest

from ouro.core.context import ContextBuilder, estimate_message_tokens
from ouro.core.context_providers import ContextFragment
from ouro.core.messages import (
    ImageContent,
    MessageRecord,
    ModelMessage,
    ProviderContinuationArtifact,
    TextContent,
    ToolResultReference,
)
from ouro.core.tools import ToolCallRequest


def test_context_builder_rejects_non_positive_token_budget() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ContextBuilder().build(messages=[], tools=[], input_token_budget=0)


def test_context_builder_preserves_all_messages_and_reports_hard_overflow() -> None:
    messages = [
        MessageRecord.text(session_id="ses_1", role="user", text="old " * 100),
        MessageRecord.text(session_id="ses_1", role="assistant", text="old answer"),
        MessageRecord.text(session_id="ses_1", role="user", text="latest"),
    ]

    context = ContextBuilder().build(
        messages=messages,
        tools=[],
        input_token_budget=20,
    )

    assert context.model_messages == [ModelMessage.from_record(message) for message in messages]
    assert context.metrics.over_budget is True
    assert context.metrics.estimated_input_tokens > 20
    assert "truncated" not in context.context_metadata
    assert "omitted_message_count" not in context.context_metadata


def test_context_builder_places_skills_in_system_prompt_and_keeps_dynamic_context() -> None:
    context = ContextBuilder(skill_instructions="skills").build(
        messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        tools=[],
        system_prompt="system",
        added_context=["plugin"],
    )

    assert context.system_prompt == "system\n\nskills"
    assert [message.text_content for message in context.model_messages] == ["plugin", "hello"]
    assert context.context_metadata["skills_injected"] is True


def test_context_builder_appends_ephemeral_prompt_fragments_after_skills() -> None:
    context = ContextBuilder(skill_instructions="skills").build(
        messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        tools=[],
        system_prompt="system",
        added_context=[
            ContextFragment(text="message context"),
            ContextFragment(
                text="facts </runtime-facts> remain literal",
                semantic_tag="runtime-facts",
                target="system_prompt",
            ),
        ],
    )

    assert context.system_prompt == (
        "system\n\nskills\n\n"
        "<runtime-facts>\n"
        "facts &lt;/runtime-facts&gt; remain literal\n"
        "</runtime-facts>"
    )
    assert [message.text_content for message in context.model_messages] == [
        "message context",
        "hello",
    ]
    assert context.context_metadata["system_prompt_fragment_count"] == 1


def test_prompt_and_message_fragments_share_semantic_envelope_projection() -> None:
    raw_text = "provider text with <plugin-context enabled> and ordinary <other> markup"
    context = ContextBuilder().build(
        messages=[],
        tools=[],
        added_context=[
            ContextFragment(
                text=raw_text,
                semantic_tag="plugin-context",
                target="system_prompt",
            ),
            ContextFragment(
                text=raw_text,
                semantic_tag="plugin-context",
            ),
        ],
    )

    assert context.system_prompt == (
        "<plugin-context>\n"
        "provider text with &lt;plugin-context enabled&gt; and ordinary <other> markup\n"
        "</plugin-context>"
    )
    assert context.provider_context().messages[0].text_content == context.system_prompt


def test_system_prompt_context_fragments_cannot_request_message_persistence() -> None:
    with pytest.raises(ValueError, match="system prompt context fragments must be ephemeral"):
        ContextFragment(
            text="invalid",
            target="system_prompt",
            persistence="message",
        )


def test_context_builder_preserves_native_system_prompt_when_no_skills_are_present() -> None:
    system_prompt = "  Preserve this prompt exactly.  \n"

    context = ContextBuilder().build(
        messages=[],
        tools=[],
        system_prompt=system_prompt,
    )

    assert context.system_prompt == system_prompt


def test_context_builder_counts_synthetic_context_as_fixed_tokens() -> None:
    without_context = ContextBuilder().build(messages=[], tools=[], input_token_budget=100)
    with_context = ContextBuilder().build(
        messages=[],
        tools=[],
        added_context=["dynamic context"],
        input_token_budget=100,
    )

    assert (
        with_context.metrics.estimated_fixed_tokens > without_context.metrics.estimated_fixed_tokens
    )


def test_synthetic_messages_default_and_validate_semantic_tags() -> None:
    default = MessageRecord.text(
        session_id="ses_1",
        role="system_synthetic",
        text="default",
    )
    custom = MessageRecord.text(
        session_id="ses_1",
        role="system_synthetic",
        text="custom",
        semantic_tag="plugin-context",
    )

    assert default.semantic_tag == "system-reminder"
    assert custom.semantic_tag == "plugin-context"
    assert (
        MessageRecord.model_validate(
            {
                "session_id": "ses_1",
                "role": "system_synthetic",
                "content": [{"type": "text", "text": "legacy"}],
            }
        ).semantic_tag
        == "system-reminder"
    )
    with pytest.raises(ValueError, match="only system_synthetic"):
        MessageRecord.text(
            session_id="ses_1",
            role="user",
            text="spoof",
            semantic_tag="plugin-context",
        )
    with pytest.raises(ValueError, match="reserved for a model role"):
        MessageRecord.text(
            session_id="ses_1",
            role="system_synthetic",
            text="spoof",
            semantic_tag="user",
        )
    with pytest.raises(ValueError, match="lowercase letter"):
        ContextFragment(text="invalid", semantic_tag="Not XML Safe")
    with pytest.raises(ValueError, match="structured state must use typed message fields"):
        MessageRecord(
            session_id="ses_1",
            role="system_synthetic",
            metadata={"semantic_tag": "plugin-context"},
        )


def test_provider_context_wraps_and_escapes_synthetic_message_envelopes() -> None:
    context = ContextBuilder().build(
        messages=[
            MessageRecord.text(
                session_id="ses_1",
                role="system_synthetic",
                text="runtime reminder",
            ),
            MessageRecord.text(session_id="ses_1", role="user", text="hello"),
        ],
        tools=[],
        added_context=[
            ContextFragment(
                text="plugin says </plugin-context> literally",
                semantic_tag="plugin-context",
            )
        ],
    )

    assert context.model_messages[0].semantic_tag == "plugin-context"
    assert context.model_messages[0].text_content == "plugin says </plugin-context> literally"
    provider_messages = context.provider_context().messages
    assert provider_messages[0].semantic_tag is None
    assert provider_messages[0].text_content == (
        "<plugin-context>\nplugin says &lt;/plugin-context&gt; literally\n</plugin-context>"
    )
    assert provider_messages[1].text_content == (
        "<system-reminder>\nruntime reminder\n</system-reminder>"
    )
    assert provider_messages[2].text_content == "hello"


def test_message_token_estimate_counts_semantic_envelope() -> None:
    raw = ModelMessage(
        role="system_synthetic",
        content=[TextContent(text="context")],
    )
    tagged = MessageRecord.text(
        session_id="ses_1",
        role="system_synthetic",
        text="context",
    )

    assert estimate_message_tokens(tagged) > estimate_message_tokens(raw)


def test_context_builder_derives_budget_metrics_from_model_limits() -> None:
    context = ContextBuilder().build(
        messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        tools=[],
        context_window_tokens=1_000,
        reserved_output_tokens=200,
    )

    assert context.metrics.context_window_tokens == 1_000
    assert context.metrics.reserved_output_tokens == 200
    assert context.metrics.input_budget_tokens == 800
    assert context.metrics.estimated_remaining_tokens == (
        800 - context.metrics.estimated_input_tokens
    )


def test_message_token_estimate_does_not_count_base64_image_bytes_as_text() -> None:
    small = ModelMessage(role="user", content=[ImageContent(uri="data:image/png;base64,AA==")])
    large = ModelMessage(
        role="user",
        content=[ImageContent(uri=f"data:image/png;base64,{'A' * 1_000_000}")],
    )

    assert estimate_message_tokens(small) == estimate_message_tokens(large)


def test_message_token_estimate_includes_typed_tool_protocol_fields() -> None:
    plain = MessageRecord.text(session_id="ses_1", role="assistant", text="")
    with_call = plain.model_copy(
        update={
            "tool_calls": [
                ToolCallRequest(
                    id="call_1",
                    name="Write",
                    arguments={"content": "x" * 1_000},
                )
            ]
        }
    )
    tool_result = MessageRecord.text(
        session_id="ses_1",
        role="tool_result",
        text="ok",
    ).model_copy(
        update={
            "tool_result": ToolResultReference(
                tool_call_id="call_1",
                tool_name="Write",
            )
        }
    )

    assert estimate_message_tokens(with_call) > estimate_message_tokens(plain) + 200
    assert estimate_message_tokens(tool_result) > estimate_message_tokens(
        MessageRecord.text(session_id="ses_1", role="tool_result", text="ok")
    )


def test_message_token_estimate_includes_provider_continuation() -> None:
    plain = MessageRecord.text(session_id="ses_1", role="assistant", text="")
    with_continuation = plain.model_copy(
        update={
            "provider_continuation": ProviderContinuationArtifact(
                provider_id="anthropic",
                protocol="anthropic-messages",
                model_id="claude-test",
                blocks=(
                    {
                        "type": "thinking",
                        "thinking": "private thought " * 100,
                        "signature": "sig",
                    },
                ),
            )
        }
    )

    assert estimate_message_tokens(with_continuation) > estimate_message_tokens(plain) + 200
